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

    `stored` counts messages this pass took responsibility for -- the bytes are
    in the store and the location is recorded -- whether or not the store had
    them already. `present` is how many of those it already had, so the mail
    that is *new to the archive* is `stored - present`. The two are worth
    keeping apart at the point where a run sums itself up: a `--full` pass over
    an archived folder stores nothing and would otherwise report every message
    in it as archived that night. They are not worth conflating in `stored`,
    which is what the delta round asks about coverage (see `_delta_token`), and
    that question is "did this pass account for anything", not "was any of it
    new".

    `failed` counts messages that were seen on the server but could not be
    stored locally. A run with failures is incomplete, so the caller must not
    advance the incremental snapshot — otherwise those messages would sit below
    the resume point of every future run and stay lost for good.

    `resume` is where the next pass over this folder may carry on, in whatever
    shape the backend that produced it uses. It is built from what this pass
    actually archived, never from the clock: "it is now 12:00" says nothing
    about what the source was willing to show, and a mailbox that is still
    starting up -- Proton Bridge before its first sync, an IMAP proxy with a
    cold cache -- reports an empty folder without reporting an error. It stays
    None when the pass earned no new point, and the caller then leaves the
    previous one standing.

    `resume_lost` says the point the caller handed in is no longer usable -- a
    UID space the server rebuilt, a delta token it will not honour, a point from
    a backend that is not this one. The pass then does *nothing*: reading the
    folder in full is the caller's decision, because only the caller knows
    whether the archive can be brought back in step by listing instead.

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
    present: int = 0
    failed: int = 0
    resume: dict[str, Any] | None = None
    resume_lost: bool = False
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
        resume: dict[str, Any] | None = ...,
        callback: collections.abc.Callable[[mailutils.MessageMetadata], None] | None = ...,
    ) -> BackupResult:
        """Store a folder's messages, recording each via `callback`.

        `resume` is a point this backend produced on an earlier pass. What it
        contains is the backend's own business -- the caller only stores it and
        hands it back. A point that is no longer usable is *reported* rather than
        worked around: the pass stops and sets `BackupResult.resume_lost`, and
        what to do instead is the caller's decision. A point of None means read
        the folder in full, which is how the caller asks for exactly that.

        Returns the point for next time in `BackupResult.resume`, built from what
        was actually archived, or None when the pass earned none.

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

    def empty_trash(self) -> None:
        """Finish off what the purges of this job left behind.

        Some providers do not remove a deleted message at all, they move it into
        a trash folder -- so `purge` alone frees no quota, and the mailbox the
        job was meant to make room in is exactly as full as before. Emptying that
        folder is what completes the deletion.

        Called once, after the *last* folder of the job has been purged, and that
        ordering is the whole point: a message reaches the trash during `purge`,
        so anything emptied before that belongs to some earlier pass, not to this
        one. A backend whose deletions leave nothing behind does nothing here.
        """
        ...

    def message_index(
        self,
        folder_name: str,
    ) -> collections.abc.Generator[MessageRef, None, None]: ...

    def resume_point(self, folder_name: str) -> dict[str, Any] | None:
        """A resume point over the folder as it stands right now, fetching nothing.

        For when a folder was brought back in step by other means and only its
        position still has to be recorded. Returns None when no point can be
        established, which leaves the folder to be read in full again.
        """
        ...

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
    already_here = status is cas.AddStatus.EXISTS
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
    if already_here:
        result.present += 1
    return store_id
