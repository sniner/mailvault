from __future__ import annotations

import collections.abc
import dataclasses
import functools
import imaplib
import logging
import re
import ssl
import sys
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Protocol

import imapclient

if sys.version_info >= (3, 14):
    # Monkeypatch for imapclient 3.1.0 on Python 3.14:
    # IMAP4WithTimeout.open explicitly sets self.file which is now a property without a setter.
    # Removing the open method makes it fallback to imaplib.IMAP4.open which works fine.
    try:
        import imapclient.imap4

        if hasattr(imapclient.imap4.IMAP4WithTimeout, "open"):
            del imapclient.imap4.IMAP4WithTimeout.open
    except Exception:
        pass

from imapbackup import cas, conf, mailutils, utils

log = logging.getLogger(__name__)


class MailboxError(Exception):
    pass


# Index page size for message_index(); only headers are fetched, no bodies.
INDEX_CHUNK_SIZE = 500


@dataclasses.dataclass
class BackupResult:
    """Outcome of a folder backup.

    `failed` counts messages that were seen on the server but could not be
    stored locally. A run with failures is incomplete, so the caller must not
    advance the incremental snapshot — otherwise those messages would fall
    outside the date filter of every future run and stay lost for good.
    """

    total: int = 0
    stored: int = 0
    failed: int = 0

    @property
    def complete(self) -> bool:
        """True if every message seen on the server was accounted for."""
        return self.failed == 0


@dataclasses.dataclass(frozen=True)
class MessageRef:
    """Reference to a message on the server, without its content.

    `msg_id` is backend-specific (IMAP UID, Graph message id) and only valid
    together with the folder it was listed from.
    """

    msg_id: Any
    message_id: str
    date: datetime | None = None


class MailboxClient(Protocol):
    """Protocol defining the interface for mailbox backends.

    Each backend (IMAP, MS Graph, ...) must implement these methods so that
    the job runner in ``jobs.py`` can treat them interchangeably.
    """

    job_name: str

    def folders(self) -> collections.abc.Generator[str, None, None]: ...

    def folder_backup(
        self,
        folder_name: str,
        store: cas.ContentAddressedStorage,
        since: datetime | None = ...,
        callback: collections.abc.Callable[[dict], None] | None = ...,
    ) -> BackupResult: ...

    def message_index(
        self, folder_name: str, since: datetime | None = ...,
    ) -> collections.abc.Generator[MessageRef, None, None]: ...

    def fetch_message(self, msg_id: Any, folder_name: str) -> bytes: ...

    def full_backup(
        self,
        store: cas.ContentAddressedStorage,
        since: datetime | None = ...,
        callback: collections.abc.Callable[[dict], None] | None = ...,
    ) -> None: ...

    def get_messages(
        self, folder_name: str, since: datetime | None = ...,
    ) -> collections.abc.Generator[tuple[Any, datetime | None, bytes], None, None]: ...

    def save_message(
        self, msg: bytes, folder_name: str, date: datetime | None = ...,
    ) -> None: ...

    def move_message(self, msg_id: Any, folder_name: str) -> None: ...

    def delete_message(self, msg_id: Any, expunge: bool = ...) -> None: ...


class ImapClient:
    def __init__(self, mailbox: Mailbox):
        self.mbox = mailbox
        self.conn: imapclient.IMAPClient = mailbox._conn  # type: ignore
        self.job_name = mailbox.job_name
        self.lock = threading.RLock()
        self.capabilities = self.conn.capabilities()
        self.delete_after_export = self.mbox.job.delete_after_export
        self.exchange_journal = self.mbox.job.exchange_journal
        self.gmail = functools.reduce(
            lambda acc, c: acc or c.startswith(b"X-GM-"), self.capabilities, False
        )
        self.move_cap = b"MOVE" in self.capabilities
        self.trash_folder = self.mbox.job.trash_folder
        self.error_folder = self.mbox.job.error_folder if self.move_cap else None

    @staticmethod
    def _isfoldertype(folder: tuple, *flags: str) -> str | None:
        folderflags = set(folder[0])
        bflags = [(b"\\" + f.encode(), f) for f in [f.capitalize() for f in flags]]
        for flag in bflags:
            if flag[0] in folderflags:
                return flag[1]
        return None

    @staticmethod
    def _isfoldername(folder: tuple, *patterns: str) -> str | None:
        foldername = folder[2]
        for pattern in patterns:
            if re.match(pattern, foldername):
                return pattern
        return None

    def folders(self) -> collections.abc.Generator[str, None, None]:
        with self.lock:
            for folder in self.conn.list_folders():
                if self._isfoldertype(folder, *self.mbox.job.ignore_folder_flags):
                    continue
                if self._isfoldername(folder, *self.mbox.job.ignore_folder_names):
                    continue
                yield folder[2]

    def select_folder(self, folder_name: str, readonly: bool = True) -> None:
        if not self.conn.folder_exists(folder_name):
            self.conn.create_folder(folder_name)
        self.conn.select_folder(folder_name, readonly=readonly)

    def watch_folder(
        self, folder_name: str, timeout: int = 20, break_out: int = 3600,
    ) -> collections.abc.Generator[tuple[str, list], None, None]:
        with self.lock:
            start_time = time.monotonic()
            while time.monotonic() - start_time < break_out:
                self.select_folder(folder_name)
                self.conn.idle()
                responses = None
                while not responses:
                    idle_time = time.monotonic()
                    responses = self.conn.idle_check(timeout=max(timeout, 10))
                    log.debug("%s::%s: IDLE response %s", self.job_name, folder_name, responses)
                    if responses:
                        self.conn.idle_done()
                        break
                    now = time.monotonic()
                    if now - idle_time < timeout / 2:
                        # Workaround: idle_check() does not raise an exception when
                        # connection breaks, instead it returns immediately.
                        log.warning(
                            "%s::%s: IDLE connection broken", self.job_name, folder_name
                        )
                        return
                    if now - start_time >= break_out:
                        self.conn.idle_done()
                        return
                yield folder_name, responses

    def _walk_folder(
        self,
        folder_name: str,
        message_ids: list[int],
        chunk_size: int = 10,
        delete: bool = False,
        result: BackupResult | None = None,
    ) -> collections.abc.Generator[tuple[int, bytes, datetime | None], None, None]:
        for msg_ids in utils.chunks(message_ids, chunk_size):
            msg_ids_str = ", ".join([str(i) for i in msg_ids])
            log.debug("%s::%s: fetching %s", self.job_name, folder_name, msg_ids_str)
            msg_id = None
            try:
                for msg_id, msg_data in self.conn.fetch(
                    msg_ids, ["INTERNALDATE", "RFC822"]
                ).items():
                    yield msg_id, msg_data[b"RFC822"], msg_data[b"INTERNALDATE"]  # type: ignore
            except (OSError, imaplib.IMAP4.error) as exc:
                log.exception("%s::%s[%s]: fetch failed: %s", self.job_name, folder_name, msg_id, exc)
                if result is not None:
                    # fetch() returns the whole chunk at once, so nothing of it
                    # was yielded before the failure.
                    result.failed += len(msg_ids)
            else:
                if delete:
                    log.debug("%s::%s: deleting %s", self.job_name, folder_name, msg_ids_str)
                    self.conn.delete_messages(msg_ids)

    def _collect_metadata(self, folder_name: str, msg_id: Any, store_id: str, msg: bytes) -> dict:
        if self.gmail:
            labels = self.conn.get_gmail_labels(msg_id)
            labels = labels.get(msg_id, [])
            if not folder_name.startswith("[Google Mail]"):
                labels = [folder_name] + labels
        else:
            labels = [folder_name]
        return mailutils.metadata(
            msg,
            mailbox=self.mbox.job_name,
            folder=folder_name,
            store_id=store_id,
            labels=labels,
        )

    def _clear_folder(self, folder_name: str) -> None:
        with self.lock:
            try:
                self.conn.select_folder(folder_name, readonly=False)
                try:
                    message_ids = self.conn.search()
                    for msg_ids in utils.chunks(message_ids, 10):
                        self.conn.delete_messages(msg_ids)
                except Exception as exc:
                    log.error("%s::%s: %s", self.job_name, folder_name, exc)
                finally:
                    self.conn.expunge()
                    self.conn.unselect_folder()
            except Exception as exc:
                log.error("%s::%s: %s", self.job_name, folder_name, exc)

    def _search_folder(self, since: datetime | None = None) -> list[int]:
        """Search the selected folder for messages, optionally limited by date."""
        if since:
            start_date = since - timedelta(days=1)
            query = ["NOT", "DELETED", "SINCE", start_date.date()]
        else:
            query = ["NOT", "DELETED"]
        return self.conn.search(query)  # type: ignore

    def _iter_folder(
        self,
        folder_name: str,
        since: datetime | None = None,
        result: BackupResult | None = None,
        auto_delete: bool = True,
    ) -> collections.abc.Generator[tuple[int, bytes, datetime | None], None, None]:
        """Select folder, search and yield messages, handle cleanup.

        With `auto_delete` (the default) each yielded message is deleted right
        after the caller processed it, if delete_after_export is configured.
        Callers that must only delete messages they actually archived (e.g. the
        exchange_journal path, which skips non-journal items) pass
        `auto_delete=False` and delete successfully handled messages themselves.
        On successful completion, deleted messages are expunged. The folder is
        always unselected on exit.
        """
        with self.lock:
            folder_info = self.conn.select_folder(
                folder_name, readonly=not self.delete_after_export
            )
            try:
                items_in_folder = folder_info[b"EXISTS"]
                message_ids = self._search_folder(since)
                items_found = len(message_ids)
                if result is not None:
                    result.total = items_found
                if items_found != items_in_folder:
                    log.info(
                        "%s::%s: found %s/%s messages",
                        self.job_name,
                        folder_name,
                        items_found,
                        items_in_folder,
                    )
                else:
                    log.info(
                        "%s::%s: found %s messages", self.job_name, folder_name, items_found
                    )
                processed = 0
                for msg_id, msg, msg_date in self._walk_folder(
                    folder_name, message_ids, delete=False, result=result
                ):
                    yield msg_id, msg, msg_date
                    processed += 1
                    if processed % 100 == 0:
                        log.info(
                            "%s::%s: %s/%s messages processed",
                            self.job_name, folder_name, processed, items_found,
                        )
                    if self.delete_after_export and auto_delete:
                        self.conn.delete_messages(msg_id)
            except Exception as exc:
                log.error("%s::%s: %s", self.job_name, folder_name, exc)
                raise
            else:
                if self.delete_after_export:
                    self.conn.expunge()
            finally:
                self.conn.unselect_folder()

    def folder_backup(
        self,
        folder_name: str,
        store: cas.ContentAddressedStorage,
        since: datetime | None = None,
        callback: collections.abc.Callable[[dict], None] | None = None,
    ) -> BackupResult:
        result = BackupResult()
        # In journal mode the non-journal items are skipped, so deletion must be
        # tied to a successful archive run instead of the generic per-message
        # auto-delete -- otherwise a skipped item would be removed from the
        # server unarchived.
        auto_delete = not self.exchange_journal
        for msg_id, msg, _ in self._iter_folder(
            folder_name, since, result=result, auto_delete=auto_delete
        ):
            if self.exchange_journal:
                msg = mailutils.unwrap_exchange_journal_item(msg)
                if msg is None:
                    if self.error_folder:
                        # move_message uses MOVE (copy + delete), so the item is
                        # safely relocated and gone from the source folder.
                        log.warning(
                            "%s::%s[%s]: not a journal item, moving to error folder",
                            self.job_name,
                            folder_name,
                            msg_id,
                        )
                        self.move_message(msg_id, self.error_folder)
                    else:
                        # No error folder: deliberately skip. The item is left on
                        # the server (auto_delete is off for the journal path), so
                        # it is never deleted unarchived. A skip is not a failure,
                        # so the snapshot may still advance.
                        log.warning(
                            "%s::%s[%s]: not a journal item, skipping (kept on server)",
                            self.job_name,
                            folder_name,
                            msg_id,
                        )
                    continue
            status, store_id, _path = store.add(msg)
            log.info(
                "%s::%s[%s]: %s: id=%s",
                self.job_name,
                folder_name,
                msg_id,
                status,
                store_id,
            )
            if callback:
                try:
                    md = self._collect_metadata(
                        folder_name=folder_name,
                        msg_id=msg_id,
                        store_id=store_id,
                        msg=msg,
                    )
                    callback(md)
                except Exception as exc:
                    log.exception(
                        "%s::%s[%s]: Error in callback: %s",
                        self.job_name,
                        folder_name,
                        msg_id,
                        exc,
                    )
                    result.failed += 1
                    continue
            if not auto_delete and self.delete_after_export:
                # Journal path: delete only now that the item is archived. The
                # generic auto-delete was disabled for this run.
                self.conn.delete_messages(msg_id)
            result.stored += 1
        if self.gmail and self.trash_folder:
            self._clear_folder(self.trash_folder)
        return result

    def full_backup(
        self,
        store: cas.ContentAddressedStorage,
        since: datetime | None = None,
        callback: collections.abc.Callable[[dict], None] | None = None,
    ) -> None:
        for folder in self.folders():
            try:
                self.folder_backup(
                    folder_name=folder, store=store, since=since, callback=callback
                )
            except Exception as exc:
                log.error("%s::%s: backup failed: %s", self.job_name, folder, exc)

    def get_messages(
        self, folder_name: str, since: datetime | None = None
    ) -> collections.abc.Generator[tuple[int, datetime | None, bytes], None, None]:
        for msg_id, msg, msg_date in self._iter_folder(folder_name, since):
            log.info("%s::%s[%s]: fetched", self.job_name, folder_name, msg_id)
            yield msg_id, msg_date, msg

    def message_index(
        self, folder_name: str, since: datetime | None = None
    ) -> collections.abc.Generator[MessageRef, None, None]:
        """List the folder's messages by Message-ID only, without fetching bodies."""
        with self.lock:
            self.conn.select_folder(folder_name, readonly=True)
            try:
                message_ids = self._search_folder(since)
                log.info(
                    "%s::%s: indexing %s messages",
                    self.job_name, folder_name, len(message_ids),
                )
                for chunk in utils.chunks(message_ids, INDEX_CHUNK_SIZE):
                    try:
                        fetched = self.conn.fetch(chunk, ["ENVELOPE"])
                    except (OSError, imaplib.IMAP4.error) as exc:
                        raise MailboxError(
                            f"{folder_name}: indexing failed: {exc}"
                        ) from exc
                    for msg_id, msg_data in fetched.items():
                        envelope = msg_data[b"ENVELOPE"]
                        raw_id = getattr(envelope, "message_id", None)
                        yield MessageRef(
                            msg_id=msg_id,
                            message_id=raw_id.decode("ascii", "replace") if raw_id else "",
                            date=getattr(envelope, "date", None),
                        )
            finally:
                self.conn.unselect_folder()

    def fetch_message(self, msg_id: int, folder_name: str) -> bytes:
        """Fetch a single message by UID from the given folder."""
        with self.lock:
            self.conn.select_folder(folder_name, readonly=True)
            try:
                msg_data = self.conn.fetch([msg_id], ["RFC822"]).get(msg_id)
                if not msg_data or b"RFC822" not in msg_data:
                    raise MailboxError(f"{folder_name}[{msg_id}]: message not found")
                return msg_data[b"RFC822"]  # type: ignore[return-value]
            finally:
                self.conn.unselect_folder()

    def save_message(self, msg: bytes, folder_name: str, date: datetime | None = None) -> None:
        with self.lock:
            if not self.conn.folder_exists(folder_name):
                self.conn.create_folder(folder_name)
            self.conn.append(folder_name, msg, msg_time=date)

    def move_message(self, msg_id: int, folder_name: str) -> None:
        with self.lock:
            if self.move_cap:
                if not self.conn.folder_exists(folder_name):
                    self.conn.create_folder(folder_name)
                self.conn.move(msg_id, folder_name)
            else:
                raise MailboxError(
                    "IMAP server has no MOVE capability, moving messages is not supported"
                )

    def delete_message(self, msg_id: int, expunge: bool = False) -> None:
        with self.lock:
            self.conn.delete_messages(msg_id)
            if expunge:
                self.conn.expunge(msg_id)


class Mailbox:
    def __init__(self, job: conf.JobConfig):
        self.job = job
        self.job_name = self.job.name
        self._client: MailboxClient | None = None
        self._conn: imapclient.IMAPClient | None = None

    def __enter__(self) -> MailboxClient:
        if self.job.backend == "msgraph":
            from imapbackup.graph import MSGraphClient

            self._client = MSGraphClient(self.job)
            return self._client

        if self.job.tls:
            tls_context = ssl.create_default_context()
            if not self.job.tls_check_hostname:
                log.warning("%s: TLS hostname check disabled", self.job_name)
                tls_context.check_hostname = False
            if not self.job.tls_verify_cert:
                log.warning("%s: TLS certificate verification disabled", self.job_name)
                tls_context.verify_mode = ssl.CERT_NONE
        else:
            log.warning("%s: TLS disabled, connection is unencrypted", self.job_name)
            tls_context = None

        self._conn = imapclient.IMAPClient(
            host=self.job.server,
            port=self.job.port,
            ssl=self.job.tls,
            ssl_context=tls_context,
        )
        self._conn.login(self.job.username, self.job.password)
        self._client = ImapClient(self)
        return self._client

    def __exit__(self, exc_type: type | None, exc_val: BaseException | None, exc_tb: Any) -> None:
        if self._conn:
            self._conn.logout()
            self._conn = None
        if self._client:
            close = getattr(self._client, "close", None)
            if close:
                close()
        self._client = None

    @property
    def client(self) -> MailboxClient | None:
        return self._client
