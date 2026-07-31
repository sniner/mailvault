"""MS Graph backend for accessing Microsoft 365 mailboxes."""

from __future__ import annotations

import collections.abc
import logging
import re
import time
import urllib.parse
from datetime import datetime
from typing import Any

import httpx
import msal

from mailvault import cas, conf, mailutils
from mailvault.backend import base

log = logging.getLogger(__name__)

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]

# Transient failures: throttling and gateway/backend hiccups. Graph produces
# these regularly during long-running bulk exports, so they must be retried
# rather than silently skipped.
RETRY_STATUS = frozenset({408, 429, 500, 502, 503, 504})
RETRY_BASE_DELAY = 2.0
RETRY_MAX_DELAY = 60.0

# Page size for the lightweight message index (no message bodies involved).
INDEX_PAGE_SIZE = 500


def _parse_graph_datetime(value: str | None) -> datetime | None:
    """Parse a Graph `receivedDateTime` like `2024-01-01T12:00:00Z`.

    `datetime.fromisoformat` accepts the trailing `Z` on Python 3.11+, which is
    the project's baseline.
    """
    if not value:
        return None
    return datetime.fromisoformat(value)


def _backoff_delay(attempt: int) -> float:
    """Exponentially growing delay (in seconds) for retry number `attempt`."""
    return min(RETRY_BASE_DELAY * 2**attempt, RETRY_MAX_DELAY)


def _retry_delay(resp: httpx.Response, attempt: int) -> float:
    """Delay before retrying `resp`, honouring a numeric Retry-After header."""
    retry_after = resp.headers.get("Retry-After")
    if retry_after:
        try:
            return min(max(float(retry_after), 0.0), RETRY_MAX_DELAY)
        except ValueError:
            # Retry-After may also be an HTTP date; fall back to plain backoff.
            pass
    return _backoff_delay(attempt)


class MSGraphClient:
    """MailboxClient implementation using Microsoft Graph API.

    Authenticates via MSAL client credentials flow (service principal),
    suitable for unattended backup of Microsoft 365 mailboxes.
    """

    def __init__(self, job: conf.JobConfig):
        self.job = job
        self.job_name = job.name
        self.delete_after_export = job.delete_after_export
        self.exchange_journal = job.exchange_journal
        self.max_retries = max(job.max_retries, 0)

        authority = f"https://login.microsoftonline.com/{job.tenant_id}"
        self._msal_app: msal.ConfidentialClientApplication = msal.ConfidentialClientApplication(
            client_id=job.client_id,
            authority=authority,
            client_credential=job.client_secret,
        )
        token = self._acquire_token()

        self._http: httpx.Client = httpx.Client(
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
            timeout=60.0,
        )
        self._user = job.username

        self._folder_map: dict[str, str] = {}
        self._build_folder_map()

    def _acquire_token(self) -> str:
        result = self._msal_app.acquire_token_for_client(scopes=GRAPH_SCOPE)
        if not result or "access_token" not in result:
            error = result.get("error_description", "unknown error") if result else "no result"
            raise RuntimeError(f"MSAL authentication failed: {error}")
        return result["access_token"]

    def _refresh_auth(self) -> None:
        """Refresh the access token (MSAL handles caching internally)."""
        token = self._acquire_token()
        self._http.headers["Authorization"] = f"Bearer {token}"

    def close(self) -> None:
        """Close the HTTP client."""
        self._http.close()

    def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        """Send an HTTP request, refreshing the token on 401 and retrying transient errors.

        Connection/timeout errors and the status codes in RETRY_STATUS are retried
        with exponential backoff, up to `max_retries` times. A 401 triggers a single
        token refresh which does not count against the retry budget.
        """
        attempt = 0
        refreshed = False
        while True:
            try:
                resp = self._http.request(method, url, **kwargs)
            except httpx.TransportError as exc:
                if attempt >= self.max_retries:
                    log.error("%s: giving up after %s attempt(s): %s", url, attempt + 1, exc)
                    raise
                delay = _backoff_delay(attempt)
                attempt += 1
                log.warning(
                    "%s: %s, retrying in %.0fs (attempt %s/%s)",
                    url,
                    exc,
                    delay,
                    attempt,
                    self.max_retries,
                )
                time.sleep(delay)
                continue

            if resp.status_code == 401 and not refreshed:
                log.debug("Token expired, refreshing")
                refreshed = True
                self._refresh_auth()
                continue

            if resp.status_code in RETRY_STATUS and attempt < self.max_retries:
                delay = _retry_delay(resp, attempt)
                attempt += 1
                log.warning(
                    "%s: HTTP %s, retrying in %.0fs (attempt %s/%s)",
                    url,
                    resp.status_code,
                    delay,
                    attempt,
                    self.max_retries,
                )
                time.sleep(delay)
                continue

            return resp

    def _paginate(
        self,
        url: str,
        params: dict[str, str] | None = None,
    ) -> collections.abc.Generator[dict[str, Any], None, None]:
        """Yield items from a paginated Graph API response."""
        current_params = params.copy() if params else {}
        base_url = url

        while True:
            resp = self._request("GET", base_url, params=current_params)
            resp.raise_for_status()
            data = resp.json()

            yield from data.get("value", [])

            next_link = data.get("@odata.nextLink")
            if not next_link:
                break

            parsed = urllib.parse.urlparse(next_link)
            query = urllib.parse.parse_qs(parsed.query)
            skip_token = query.get("$skiptoken") or query.get("$skip")
            if skip_token:
                if "$skiptoken" in query:
                    current_params["$skiptoken"] = skip_token[0]
                elif "$skip" in query:
                    current_params["$skip"] = skip_token[0]
            else:
                base_url = next_link
                current_params = {}

    def _build_folder_map(self) -> None:
        """Recursively build a mapping of folder display paths to Graph IDs."""
        self._folder_map.clear()
        self._enumerate_folders(
            f"{GRAPH_BASE_URL}/users/{self._user}/mailFolders",
            prefix="",
        )

    def _enumerate_folders(self, url: str, prefix: str) -> None:
        params = {"$select": "id,displayName,childFolderCount", "$top": "100"}
        for folder in self._paginate(url, params):
            name = folder.get("displayName", "")
            path = f"{prefix}/{name}" if prefix else name
            folder_id = folder["id"]
            self._folder_map[path] = folder_id

            if folder.get("childFolderCount", 0) > 0:
                child_url = (
                    f"{GRAPH_BASE_URL}/users/{self._user}/mailFolders/{folder_id}/childFolders"
                )
                self._enumerate_folders(child_url, prefix=path)

    def _resolve_folder(self, folder_name: str) -> str:
        """Resolve a folder display name or path to its Graph ID."""
        if folder_name in self._folder_map:
            return self._folder_map[folder_name]
        lower = folder_name.casefold()
        for path, fid in self._folder_map.items():
            if path.casefold() == lower:
                return fid
        raise RuntimeError(f"Folder not found: {folder_name}")

    def _download_mime(self, msg_id: str) -> bytes:
        """Download a message as RFC 822 MIME content."""
        url = f"{GRAPH_BASE_URL}/users/{self._user}/messages/{msg_id}/$value"
        resp = self._request("GET", url)
        resp.raise_for_status()
        return resp.content

    def fetch_message(self, msg_id: str, folder_name: str = "") -> bytes:
        """Fetch a single message by its Graph id (folder is irrelevant here)."""
        return self._download_mime(msg_id)

    def _graph_delete(self, msg_id: str) -> None:
        url = f"{GRAPH_BASE_URL}/users/{self._user}/messages/{msg_id}"
        resp = self._request("DELETE", url)
        resp.raise_for_status()

    def _iter_messages(
        self,
        folder_name: str,
        folder_id: str,
        since: datetime | None = None,
        select: str = "id,receivedDateTime",
        page_size: int = 50,
    ) -> collections.abc.Generator[dict[str, Any], None, None]:
        """List messages in a folder, optionally filtered by date."""
        url = f"{GRAPH_BASE_URL}/users/{self._user}/mailFolders/{folder_id}/messages"
        params: dict[str, str] = {
            "$top": str(page_size),
            "$select": select,
            "$orderby": "receivedDateTime asc",
        }
        if since:
            since_str = since.strftime("%Y-%m-%dT%H:%M:%SZ")
            params["$filter"] = f"receivedDateTime ge {since_str}"

        yield from self._paginate(url, params)

    def message_index(
        self,
        folder_name: str,
        since: datetime | None = None,
    ) -> collections.abc.Generator[base.MessageRef, None, None]:
        """List the folder's messages by Message-ID only, without downloading bodies."""
        folder_id = self._resolve_folder(folder_name)
        for item in self._iter_messages(
            folder_name,
            folder_id,
            since=since,
            select="id,internetMessageId,receivedDateTime",
            page_size=INDEX_PAGE_SIZE,
        ):
            yield base.MessageRef(
                msg_id=item["id"],
                message_id=item.get("internetMessageId") or "",
                date=_parse_graph_datetime(item.get("receivedDateTime")),
            )

    def folders(self) -> collections.abc.Generator[str, None, None]:
        ignore_names = self.job.ignore_folder_names
        for path in self._folder_map:
            if any(re.match(pattern, path) for pattern in ignore_names):
                continue
            yield path

    def folder_backup(
        self,
        folder_name: str,
        store: cas.ContentAddressedStorage,
        since: datetime | None = None,
        callback: collections.abc.Callable[[dict], None] | None = None,
    ) -> base.BackupResult:
        folder_id = self._resolve_folder(folder_name)
        messages = list(self._iter_messages(folder_name, folder_id, since))
        log.info("%s::%s: found %s messages", self.job_name, folder_name, len(messages))

        result = base.BackupResult(total=len(messages))
        for idx, msg_info in enumerate(messages, 1):
            msg_id = msg_info["id"]
            try:
                msg = self._download_mime(msg_id)
            except Exception as exc:
                log.error(
                    "%s::%s[%s]: download failed: %s",
                    self.job_name,
                    folder_name,
                    idx,
                    exc,
                )
                result.failed += 1
                continue

            if self.exchange_journal:
                unwrapped = mailutils.unwrap_exchange_journal_item(msg)
                if unwrapped is None:
                    log.warning(
                        "%s::%s[%s]: not a journal item, skipping",
                        self.job_name,
                        folder_name,
                        idx,
                    )
                    continue
                msg = unwrapped

            status, store_id, _path = store.add(msg)
            log.info(
                "%s::%s[%s]: %s: id=%s",
                self.job_name,
                folder_name,
                idx,
                status,
                store_id,
            )

            if callback:
                try:
                    callback(
                        mailutils.metadata(
                            msg,
                            mailbox=self.job_name,
                            folder=folder_name,
                            store_id=store_id,
                        )
                    )
                except Exception as exc:
                    log.exception(
                        "%s::%s[%s]: Error in callback: %s",
                        self.job_name,
                        folder_name,
                        idx,
                        exc,
                    )
                    result.failed += 1
                    continue

            result.stored += 1
            if result.stored % 100 == 0:
                log.info(
                    "%s::%s: %s/%s messages processed",
                    self.job_name,
                    folder_name,
                    result.stored,
                    len(messages),
                )

            if self.delete_after_export:
                try:
                    self._graph_delete(msg_id)
                except Exception as exc:
                    log.error(
                        "%s::%s[%s]: delete failed: %s",
                        self.job_name,
                        folder_name,
                        idx,
                        exc,
                    )

        return result

    def full_backup(
        self,
        store: cas.ContentAddressedStorage,
        since: datetime | None = None,
        callback: collections.abc.Callable[[dict], None] | None = None,
    ) -> None:
        for folder in self.folders():
            try:
                self.folder_backup(folder, store, since=since, callback=callback)
            except Exception as exc:
                log.error("%s::%s: backup failed: %s", self.job_name, folder, exc)

    def get_messages(
        self,
        folder_name: str,
        since: datetime | None = None,
    ) -> collections.abc.Generator[tuple[Any, datetime | None, bytes], None, None]:
        folder_id = self._resolve_folder(folder_name)
        for msg_info in self._iter_messages(folder_name, folder_id, since):
            msg_id = msg_info["id"]
            msg_date = _parse_graph_datetime(msg_info.get("receivedDateTime"))
            try:
                msg = self._download_mime(msg_id)
            except Exception as exc:
                log.error(
                    "%s::%s: download failed for %s: %s",
                    self.job_name,
                    folder_name,
                    msg_id[:20],
                    exc,
                )
                continue
            log.info("%s::%s: fetched %s", self.job_name, folder_name, msg_id[:20])
            yield msg_id, msg_date, msg

    def save_message(
        self,
        msg: bytes,
        folder_name: str,
        date: datetime | None = None,
    ) -> None:
        folder_id = self._resolve_folder(folder_name)
        url = f"{GRAPH_BASE_URL}/users/{self._user}/mailFolders/{folder_id}/messages"
        resp = self._request("POST", url, content=msg, headers={"Content-Type": "text/plain"})
        resp.raise_for_status()

    def move_message(self, msg_id: Any, folder_name: str) -> None:
        folder_id = self._resolve_folder(folder_name)
        url = f"{GRAPH_BASE_URL}/users/{self._user}/messages/{msg_id}/move"
        resp = self._request("POST", url, json={"destinationId": folder_id})
        resp.raise_for_status()

    def delete_message(self, msg_id: Any, expunge: bool = False) -> None:
        self._graph_delete(msg_id)
