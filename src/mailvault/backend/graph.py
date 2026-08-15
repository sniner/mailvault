"""MS Graph backend for accessing Microsoft 365 mailboxes."""

from __future__ import annotations

import collections.abc
import logging
import re
import time
import urllib.parse
from datetime import UTC, datetime
from typing import Any

import httpx
import msal

from mailvault import conf, mailutils, utils
from mailvault.backend import base
from mailvault.store import cas

log = logging.getLogger(__name__)

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]

# The one host this client may carry its access token to. Taken from the base
# URL rather than written out again, so there is nothing to keep in step.
GRAPH_HOST = urllib.parse.urlsplit(GRAPH_BASE_URL).hostname or ""

# Transient failures: throttling and gateway/backend hiccups. Graph produces
# these regularly during long-running bulk exports, so they must be retried
# rather than silently skipped.
RETRY_STATUS = frozenset({408, 429, 500, 502, 503, 504})
RETRY_BASE_DELAY = 2.0
RETRY_MAX_DELAY = 60.0

# Page size for the lightweight message index (no message bodies involved).
INDEX_PAGE_SIZE = 500

# Page size for a delta round. Only ids come back, so the pages are small and
# the round trips are what costs -- a folder of 77,000 messages is 155 requests
# at this size rather than 777. The service caps it where it will not honour it.
DELTA_PAGE_SIZE = 500

# The `kind` this backend stamps on the resume points it produces. A point
# carrying any other kind belongs to a different backend and is read in full.
DELTA_RESUME_KIND = "graph-delta"

# A delta link can stop being honoured, and Graph says so in two different ways.
# `410 Gone` is the documented sync-reset; an expired token instead arrives as
# "a 40X-series error with error codes such as syncStateNotFound". Both mean the
# same thing to us, and both are normal recovery paths rather than exceptional
# ones -- for Outlook entities there is no fixed lifetime at all, only a
# service-side cache that drops the oldest tokens as it fills.
DELTA_EXPIRED_STATUS = 410
DELTA_EXPIRED_CODES = frozenset(
    {
        "syncstatenotfound",
        "resyncrequired",
        "resyncchangesapplydifferences",
        "resyncchangesuploaddifferences",
    }
)


def _is_delta_expired(resp: httpx.Response) -> bool:
    """Whether this response means the delta link is no longer usable.

    Restricted to 4xx with a resync error code, plus 410 outright: a 401 or 403
    is about credentials, not about the token, and must not be mistaken for one.
    """
    if resp.status_code == DELTA_EXPIRED_STATUS:
        return True
    if not 400 <= resp.status_code < 500:
        return False
    try:
        code = resp.json().get("error", {}).get("code", "")
    except (ValueError, AttributeError):
        return False
    return isinstance(code, str) and code.lower() in DELTA_EXPIRED_CODES


class _DeltaExpired(Exception):
    """Graph refused the delta link it gave us; the round cannot be continued."""


def _host_of(url: str) -> str:
    """The host a URL names, for a message that must not repeat the URL itself.

    A delta link carries a continuation token in its query string, so it does
    not belong in a log line. The host is the part worth reading anyway.
    """
    return urllib.parse.urlsplit(url).netloc or "nowhere in particular"


def _is_graph_url(url: str) -> bool:
    """Whether a URL may be requested with this client's access token.

    The token sits on the `httpx.Client`, not on the individual call, so every
    request carries it to whatever host the URL names. Nearly all of them are
    built from `GRAPH_BASE_URL` and are therefore safe by construction -- but
    two are not. `@odata.nextLink` comes out of a response body, and the delta
    link comes back out of a head file, which lives in the archive: on a network
    share, per the README, opened by more than one installation. A head file
    somebody edited would hand this mailbox's OAuth token to a server of their
    choosing, and the request would look entirely ordinary from here.
    """
    parts = urllib.parse.urlsplit(url)
    return parts.scheme == "https" and (parts.hostname or "").lower() == GRAPH_HOST


def _delta_point(resume: dict | None) -> tuple[str, datetime | None] | None:
    """Read this backend's own resume point, or None for anything else.

    Returns the delta link and when it was issued -- the latter only so that a
    rejected token can say how old it was, which is the one way to find out how
    long these actually live.
    """
    if resume is None:
        return None
    if resume.get("kind") != DELTA_RESUME_KIND:
        log.info("resume point of kind %r is not ours, reading in full", resume.get("kind"))
        return None
    link = resume.get("delta_link")
    if not isinstance(link, str) or not link:
        log.warning("resume point %r has no usable delta link, reading in full", resume)
        return None
    if not _is_graph_url(link):
        # Refused here rather than at the request, so it takes the path a
        # worthless resume point already has: read the folder in full. Nothing
        # is lost but a round of downloading.
        log.warning(
            "resume point names %s and not %s, reading in full",
            _host_of(link),
            GRAPH_HOST,
        )
        return None
    return link, _parse_graph_datetime(resume.get("issued"))


def _delta_token(new_link: str | None, previous: object, stored: int) -> dict | None:
    """The point for next time, or None to leave the previous one standing.

    A completed delta round ends on a link meaning "you are caught up here", and
    it is the *server* saying so rather than something inferred from silence --
    so a round records it even when nothing changed. That is the whole point:
    without it, an unchanged folder would be walked from the beginning every
    night.

    The exception is the very first round over a folder. There is no earlier
    point to fall back on, and a round that starts from scratch *and* archives
    nothing is the one case where the link would claim coverage of mail nobody
    ever showed us -- the shape of the Proton Bridge failure, in Graph terms.
    Withholding it costs one more round next time, on a folder that by
    definition had nothing in it.
    """
    if new_link is None:
        return None
    if previous is None and stored == 0:
        return None
    return _make_delta_token(new_link)


def _make_delta_token(delta_link: str) -> dict:
    """Wrap a delta link as a resume point, stamped with when it was issued."""
    return {
        "kind": DELTA_RESUME_KIND,
        "delta_link": delta_link,
        "issued": datetime.now(UTC).isoformat(),
    }


def _parse_graph_datetime(value: object) -> datetime | None:
    """Parse a Graph `receivedDateTime` like `2024-01-01T12:00:00Z`, or give up.

    `datetime.fromisoformat` accepts the trailing `Z` on Python 3.11+, which is
    the project's baseline.

    Never raises, because one of its callers is handed a value out of a resume
    point -- a file on disk that anything may have happened to. `heads` promises
    that an unusable resume point degrades to "read the folder in full", and a
    `ValueError` from here instead escaped `folder_backup`, was caught by the
    per-folder handler in `_backup_to_log`, and dropped that folder from the
    backup. Every night, since nothing rewrites the head that caused it.

    A timestamp without a zone is refused for the same reason it cannot be used:
    subtracting it from an aware `now()` raises, and the only thing it is used
    for is an age.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        log.warning("%r is not a usable timestamp, treating it as unknown", value)
        return None
    if parsed.tzinfo is None:
        log.warning("%r has no timezone, treating it as unknown", value)
        return None
    return parsed


def _token_age(issued: datetime | None) -> str:
    """How old a resume point was, for the log line that reports its rejection."""
    if issued is None:
        return "an unknown time"
    delta = datetime.now(UTC) - issued
    hours = delta.total_seconds() / 3600
    return f"{hours:.1f}h" if hours < 48 else f"{delta.days}d"


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
        self.permanent_delete = job.permanent_delete
        self.exchange_journal = job.exchange_journal
        self.error_folder = job.error_folder
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
        """Get an access token, or say why the tenant refused to issue one.

        Wrong secret, wrong tenant, consent never granted: Azure names the
        reason in `error_description`, so this is a diagnosed failure like a
        refused IMAP login and is reported as one -- see `base.MailboxError`.
        """
        result = self._msal_app.acquire_token_for_client(scopes=GRAPH_SCOPE)
        if not result or "access_token" not in result:
            error = result.get("error_description", "unknown error") if result else "no result"
            raise base.MailboxError(f"authentication failed: {error}")
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

        Every URL passes `_is_graph_url` first. The check belongs here because
        this is the one place all of them come together -- the ones built from
        `GRAPH_BASE_URL`, the `@odata.nextLink` out of a response, and the delta
        link out of a head file -- and the access token is on the client, so it
        would go wherever any of them pointed.
        """
        if not _is_graph_url(url):
            raise base.MailboxError(
                f"refusing to send the access token to {_host_of(url)}:"
                f" mail is only ever asked of {GRAPH_HOST}"
            )
        attempt = 0
        refreshed = False
        while True:
            try:
                resp = self._http.request(method, url, **kwargs)
            except httpx.TransportError as exc:
                if attempt >= self.max_retries:
                    log.error(
                        "%s: giving up after %s: %s",
                        url,
                        utils.counted(attempt + 1, "attempt"),
                        exc,
                    )
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

    def _lookup_folder(self, folder_name: str) -> str | None:
        """Return the Graph ID for a folder display name or path, or None."""
        if folder_name in self._folder_map:
            return self._folder_map[folder_name]
        lower = folder_name.casefold()
        for path, fid in self._folder_map.items():
            if path.casefold() == lower:
                return fid
        return None

    def _resolve_folder(self, folder_name: str) -> str:
        """Resolve a folder display name or path to its Graph ID."""
        folder_id = self._lookup_folder(folder_name)
        if folder_id is None:
            raise base.MailboxError(f"folder not found: {folder_name}")
        return folder_id

    def _ensure_folder(self, folder_name: str) -> str:
        """Return the Graph ID for `folder_name`, creating the folder if needed.

        A background job must not stop because someone removed a folder it was
        told to file things into, so a missing one is created rather than
        reported. Missing *permission* is a different matter and is raised: with
        only `Mail.Read` granted, Graph answers 403 and no amount of retrying
        will help.

        Parent folders are resolved, not created -- `a/b/c` needs `a/b` to
        exist. Only the leaf is made.
        """
        folder_id = self._lookup_folder(folder_name)
        if folder_id is not None:
            return folder_id

        parent_path, _, leaf = folder_name.rpartition("/")
        if parent_path:
            parent_id = self._lookup_folder(parent_path)
            if parent_id is None:
                raise base.MailboxError(
                    f"cannot create folder {folder_name!r}: "
                    f"parent {parent_path!r} does not exist"
                )
            url = f"{GRAPH_BASE_URL}/users/{self._user}/mailFolders/{parent_id}/childFolders"
        else:
            url = f"{GRAPH_BASE_URL}/users/{self._user}/mailFolders"

        log.info("%s: creating folder '%s'", self.job_name, folder_name)
        resp = self._request("POST", url, json={"displayName": leaf})
        if resp.status_code == 403:
            raise base.MailboxError(
                f"not allowed to create folder {folder_name!r} -- "
                f"the application needs the Mail.ReadWrite permission"
            )
        resp.raise_for_status()

        folder_id = resp.json()["id"]
        self._folder_map[folder_name] = folder_id
        return folder_id

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
        """Delete one message, softly or for good depending on the job.

        A plain DELETE is a *soft* delete: Graph moves the message to Deleted
        Items, where it keeps occupying the mailbox quota. `permanent_delete`
        uses the `permanentDelete` action instead, which removes exactly this
        message -- the one just archived -- without touching anything else that
        happens to be in the bin. Retention policies and holds still apply; this
        is not a way around them.
        """
        base_url = f"{GRAPH_BASE_URL}/users/{self._user}/messages/{msg_id}"
        if self.permanent_delete:
            resp = self._request("POST", f"{base_url}/permanentDelete")
        else:
            resp = self._request("DELETE", base_url)
        resp.raise_for_status()

    def _iter_messages(
        self,
        folder_name: str,
        folder_id: str,
        select: str = "id,receivedDateTime",
        page_size: int = 50,
    ) -> collections.abc.Generator[dict[str, Any], None, None]:
        """List every message in a folder, oldest first."""
        url = f"{GRAPH_BASE_URL}/users/{self._user}/mailFolders/{folder_id}/messages"
        params: dict[str, str] = {
            "$top": str(page_size),
            "$select": select,
            "$orderby": "receivedDateTime asc",
        }
        yield from self._paginate(url, params)

    def _delta_round(
        self,
        folder_name: str,
        folder_id: str,
        point: tuple[str, datetime | None] | None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Walk one delta round; return its messages and the link for next time.

        A round is a chain of requests: the first carries the query options, and
        every one after it is a URL Graph handed back, followed *verbatim*.
        Graph encodes the options into those tokens, which is why they are given
        exactly once and why changing `$select` later means starting a new cycle.
        The chain ends when a response carries `@odata.deltaLink` instead of
        `@odata.nextLink`.

        Entries marked `@removed` are dropped. They arrive for a message that
        was deleted *or moved out of this folder*, and Graph emits them whether
        or not they were asked for: delta tracks the collection, not the
        individual message. Read/unread flips arrive the same way, and those are
        kept -- re-storing a message costs a download the storage then discards,
        while dropping one that only looked unchanged would cost the message.

        A rejected token raises `_DeltaExpired` rather than quietly starting
        over: reading the folder in full is the caller's decision, because only
        the caller knows whether the archive can be brought back in step by
        listing instead of downloading.
        """
        ctx = f"{self.job_name}::{folder_name}"
        if point is not None:
            url: str = point[0]
            params: dict[str, str] | None = None
        else:
            url = f"{GRAPH_BASE_URL}/users/{self._user}/mailFolders/{folder_id}/messages/delta"
            params = {"$select": "id"}
        headers = {"Prefer": f"odata.maxpagesize={DELTA_PAGE_SIZE}"}

        items: list[dict[str, Any]] = []
        removed = 0
        while True:
            resp = self._request("GET", url, params=params, headers=headers)
            if point is not None and _is_delta_expired(resp):
                log.info(
                    "%s: delta token rejected (HTTP %s) after %s",
                    ctx,
                    resp.status_code,
                    _token_age(point[1]),
                )
                raise _DeltaExpired
            resp.raise_for_status()
            data = resp.json()

            for item in data.get("value", []):
                if "@removed" in item:
                    removed += 1
                    continue
                items.append(item)

            next_link = data.get("@odata.nextLink")
            if next_link:
                url, params = next_link, None
                continue

            if removed:
                log.info(
                    "%s: %s gone from the folder",
                    ctx,
                    utils.counted(removed, "message"),
                )
            delta_link = data.get("@odata.deltaLink")
            if not isinstance(delta_link, str) or not delta_link:
                # Without it there is no position to carry forward, so the next
                # run starts over. Costly, never wrong.
                log.warning("%s: delta round ended without a link", ctx)
                return items, None
            return items, delta_link

    def resume_point(self, folder_name: str) -> dict | None:
        """A fresh delta link over the folder as it stands, fetching no bodies."""
        folder_id = self._resolve_folder(folder_name)
        _items, delta_link = self._delta_round(folder_name, folder_id, None)
        return _make_delta_token(delta_link) if delta_link else None

    def message_index(
        self,
        folder_name: str,
    ) -> collections.abc.Generator[base.MessageRef, None, None]:
        """List the folder's messages by Message-ID only, without fetching bodies."""
        folder_id = self._resolve_folder(folder_name)
        for item in self._iter_messages(
            folder_name,
            folder_id,
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
        resume: dict | None = None,
        callback: collections.abc.Callable[[mailutils.MessageMetadata], None] | None = None,
    ) -> base.BackupResult:
        """Store a folder's messages, recording each via `callback`.

        Nothing is deleted here: the ids of successfully stored messages are
        collected in `BackupResult.deletable`, and the caller purges them only
        after the metadata log is sealed.
        """
        point = _delta_point(resume)
        if resume is not None and point is None:
            # A point was handed in and it is not one of ours.
            return base.BackupResult(resume_lost=True)
        folder_id = self._resolve_folder(folder_name)
        try:
            messages, delta_link = self._delta_round(folder_name, folder_id, point)
        except _DeltaExpired:
            return base.BackupResult(resume_lost=True)
        log.info(
            "%s::%s: found %s",
            self.job_name,
            folder_name,
            utils.counted(len(messages), "message"),
        )

        result = base.BackupResult(total=len(messages))
        # Collected while walking the folder and relocated afterwards, so one
        # unmovable item cannot interrupt the pass. A skip is not a failure.
        non_journal: list[str] = []
        for idx, msg_info in enumerate(messages, 1):
            msg_id = msg_info["id"]
            log_ctx = f"{self.job_name}::{folder_name}[{idx}]"
            try:
                msg = self._download_mime(msg_id)
            except Exception as exc:
                log.error("%s: download failed: %s", log_ctx, exc)
                result.failed += 1
                continue

            if self.exchange_journal:
                unwrapped = mailutils.unwrap_exchange_journal_item(msg)
                if unwrapped is None:
                    log.warning(
                        "%s: not a journal item, %s",
                        log_ctx,
                        "moving to the error folder"
                        if self.error_folder
                        else "kept in mailbox",
                    )
                    non_journal.append(msg_id)
                    continue
                msg = unwrapped

            store_id = base.store_message(
                store,
                msg,
                result=result,
                log_ctx=log_ctx,
                callback=callback,
                metadata_fn=lambda sid: mailutils.metadata(
                    mailbox=self.job_name,
                    folder=folder_name,
                    store_id=sid,
                ),
            )
            if store_id is None:
                continue

            if result.stored % 100 == 0:
                log.info(
                    "%s::%s: %s/%s messages processed",
                    self.job_name,
                    folder_name,
                    result.stored,
                    len(messages),
                )

            # Not deleted here: a message is removed from the mailbox only after
            # the folder's log is sealed (see `purge`), so its location is durable
            # before it leaves its source.
            if self.delete_after_export:
                result.deletable.append(msg_id)

        if self.error_folder:
            self._relocate(folder_name, non_journal, self.error_folder)

        result.resume = _delta_token(delta_link, point, result.stored)
        return result

    def _relocate(self, folder_name: str, msg_ids: list[str], dest_folder: str) -> None:
        """Move the given messages into `dest_folder`, creating it if needed.

        A failure on one message is logged and costs only that relocation; the
        message stays where it is, unarchived and undeleted. A missing
        permission is not survivable that way, so it is raised.
        """
        if not msg_ids:
            return
        dest_id = self._ensure_folder(dest_folder)
        moved = 0
        for msg_id in msg_ids:
            url = f"{GRAPH_BASE_URL}/users/{self._user}/messages/{msg_id}/move"
            try:
                resp = self._request("POST", url, json={"destinationId": dest_id})
                if resp.status_code == 403:
                    raise base.MailboxError(
                        "not allowed to move messages -- "
                        "the application needs the Mail.ReadWrite permission"
                    )
                resp.raise_for_status()
                moved += 1
            except base.MailboxError:
                raise
            except Exception as exc:
                log.error(
                    "%s::%s[%s]: move to '%s' failed: %s",
                    self.job_name,
                    folder_name,
                    msg_id[:20],
                    dest_folder,
                    exc,
                )
        log.info(
            "%s::%s: %s of %s moved to '%s'",
            self.job_name,
            folder_name,
            moved,
            utils.counted(len(msg_ids), "message"),
            dest_folder,
        )

    def empty_trash(self) -> None:
        """Nothing to finish off: Graph deletes where the deletion is decided.

        `permanent_delete` is passed to the delete call itself, so a message is
        either gone by the time `purge` returns or it is in Deleted Items on
        purpose. There is no folder here that this job filled and left behind.
        """

    def purge(self, folder_name: str, msg_ids: collections.abc.Sequence[str]) -> None:
        """Delete the given messages from the mailbox, softly or for good.

        Called by the backup runner only after the folder's metadata log has been
        sealed, so a message leaves its source once the record of where it was
        seen is durable. A failed deletion is logged and does not abort the rest;
        the message stays and is re-fetched (and deduplicated) next run -- which
        is also what happens if `permanent_delete` is not available, so the
        failure direction is "still there", never "gone unarchived".
        """
        for msg_id in msg_ids:
            try:
                self._graph_delete(msg_id)
            except Exception as exc:
                log.error(
                    "%s::%s[%s]: delete failed: %s",
                    self.job_name,
                    folder_name,
                    msg_id,
                    exc,
                )
