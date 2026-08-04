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


class MailboxError(Exception):
    """The mailbox could not do what was asked, and why is already known.

    Raised where a backend has diagnosed the failure -- a refused login, a
    folder that will not open, a message the server does not have. The CLI
    reports these as a single line: the traceback is reserved for the errors
    nobody expected, where the call stack is the only clue there is.
    """


@dataclasses.dataclass
class BackupResult:
    """Outcome of a folder backup.

    `failed` counts messages that were seen on the server but could not be
    stored locally. A run with failures is incomplete, so the caller must not
    advance the incremental snapshot — otherwise those messages would fall
    outside the date filter of every future run and stay lost for good.

    `deletable` lists the backend message ids that were stored successfully and
    may be removed from the server -- but only once the metadata log for this
    folder is sealed, so a message is never deleted from its source before the
    record of where it was seen is durable. It is populated only when the job
    deletes after export; the backup runner drives the deletion through `purge`,
    after the seal. A skipped or failed message is never listed, so it is never
    deleted unarchived.
    """

    total: int = 0
    stored: int = 0
    failed: int = 0
    deletable: list[Any] = dataclasses.field(default_factory=list)

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
    ) -> BackupResult:
        """Store a folder's messages, recording each via `callback`.

        Deletes nothing; the ids of stored messages go into
        `BackupResult.deletable` for the caller to `purge` after the log is sealed.
        """
        ...

    def purge(self, folder_name: str, msg_ids: collections.abc.Sequence[Any]) -> None:
        """Delete the given messages from the server.

        Called by the backup runner only after the folder's metadata log has been
        sealed, so a message leaves its source only once the record of where it
        was seen is durable. A no-op for an empty `msg_ids`.
        """
        ...

    def message_index(
        self,
        folder_name: str,
        since: datetime | None = ...,
    ) -> collections.abc.Generator[MessageRef, None, None]: ...

    def fetch_message(self, msg_id: Any, folder_name: str) -> bytes: ...

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
    metadata. ``result.stored`` is incremented on success, ``result.failed`` when
    the location could not be built or recorded. ``log_ctx`` is the
    ``mailbox::folder[id]`` prefix the caller would otherwise repeat in every log
    line.

    Returns the store id on success, or ``None`` when the message's location
    could not be recorded. A ``None`` return means the caller must not treat the
    message as archived -- in particular it must not be deleted from the server.
    """
    status, store_id, _path = store.add(msg)
    log.info("%s: %s: id=%s", log_ctx, status, store_id)
    if callback is not None and metadata_fn is not None:
        try:
            metadata = metadata_fn(store_id)
        except Exception as exc:
            # Building the location can fail on its own -- for Gmail it fetches
            # the message's labels over the network. The message is stored, but
            # its location is not, so it must not be treated as archived: a
            # non-None return here would let the caller delete it from the server
            # with no record of where it was seen -- the one fact the archive
            # cannot reconstruct. Fail closed: count it failed so the snapshot
            # holds, and let the next run re-fetch (the storage deduplicates the
            # message) and record the location then.
            log.warning("%s: metadata could not be extracted: %s", log_ctx, exc)
            result.failed += 1
            return None
        try:
            callback(metadata)
        except Exception as exc:
            # Same reasoning: the message is archived but its location was not
            # written down, so a rerun -- never a deletion -- is what fixes it.
            log.exception("%s: recording the metadata failed: %s", log_ctx, exc)
            result.failed += 1
            return None
    result.stored += 1
    return store_id
