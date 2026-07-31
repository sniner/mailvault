"""Backend-agnostic mailbox interface.

The shared types every mailbox backend (IMAP, MS Graph, ...) speaks in. Kept in
a module of its own so that both the backends and the job runner can depend on
them without the backends having to import each other.
"""

from __future__ import annotations

import collections.abc
import dataclasses
import logging
from datetime import datetime
from typing import Any, Protocol

from mailvault import mailutils
from mailvault.store import cas

log = logging.getLogger(__name__)


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
        callback: collections.abc.Callable[[mailutils.MessageMetadata], None] | None = ...,
    ) -> BackupResult: ...

    def message_index(
        self,
        folder_name: str,
        since: datetime | None = ...,
    ) -> collections.abc.Generator[MessageRef, None, None]: ...

    def fetch_message(self, msg_id: Any, folder_name: str) -> bytes: ...

    def full_backup(
        self,
        store: cas.ContentAddressedStorage,
        since: datetime | None = ...,
        callback: collections.abc.Callable[[mailutils.MessageMetadata], None] | None = ...,
    ) -> None: ...

    def get_messages(
        self,
        folder_name: str,
        since: datetime | None = ...,
    ) -> collections.abc.Generator[tuple[Any, datetime | None, bytes], None, None]: ...

    def save_message(
        self,
        msg: bytes,
        folder_name: str,
        date: datetime | None = ...,
    ) -> None: ...

    def move_message(self, msg_id: Any, folder_name: str) -> None: ...

    def delete_message(self, msg_id: Any, expunge: bool = ...) -> None: ...

    def close(self) -> None: ...


def store_message(
    store: cas.ContentAddressedStorage,
    msg: bytes,
    *,
    result: BackupResult,
    log_ctx: str,
    callback: collections.abc.Callable[[mailutils.MessageMetadata], None] | None = None,
    metadata_fn: collections.abc.Callable[[str], mailutils.MessageMetadata] | None = None,
) -> str | None:
    """Add one message to the store and record its metadata.

    The shared tail of every backend's ``folder_backup``: store the bytes, log
    the outcome, and -- if a callback is given -- build and hand over the
    metadata. ``result.stored`` is incremented on success, ``result.failed`` on a
    callback error. ``log_ctx`` is the ``mailbox::folder[id]`` prefix the caller
    would otherwise repeat in every log line.

    Returns the store id on success, or ``None`` when the message could not be
    recorded. A ``None`` return means the caller must not treat the message as
    archived -- in particular it must not be deleted from the server.
    """
    status, store_id, _path = store.add(msg)
    log.info("%s: %s: id=%s", log_ctx, status, store_id)
    if callback is not None and metadata_fn is not None:
        try:
            callback(metadata_fn(store_id))
        except Exception as exc:
            log.exception("%s: error in callback: %s", log_ctx, exc)
            result.failed += 1
            return None
    result.stored += 1
    return store_id


def run_full_backup(
    client: MailboxClient,
    store: cas.ContentAddressedStorage,
    since: datetime | None = None,
    callback: collections.abc.Callable[[mailutils.MessageMetadata], None] | None = None,
) -> None:
    """Back up every folder of a mailbox, logging and skipping folders that fail.

    The default whole-mailbox backup shared by all backends: iterate
    ``client.folders()`` and delegate each to ``client.folder_backup``. A folder
    that raises is logged and skipped so the remaining folders still run.
    """
    for folder in client.folders():
        try:
            client.folder_backup(folder, store, since=since, callback=callback)
        except Exception as exc:
            log.error("%s::%s: backup failed: %s", client.job_name, folder, exc)
