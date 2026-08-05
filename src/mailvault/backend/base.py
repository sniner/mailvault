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

    `resume` is where the next pass over this folder may carry on, in whatever
    shape the backend that produced it uses. It is built from what this pass
    actually archived, never from the clock: "it is now 12:00" says nothing
    about what the source was willing to show, and a mailbox that is still
    starting up -- Proton Bridge before its first sync, an IMAP proxy with a
    cold cache -- reports an empty folder without reporting an error. It stays
    None when the pass earned no new point, and the caller then leaves the
    previous one standing.

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
    resume: dict | None = None
    deletable: list[Any] = dataclasses.field(default_factory=list)

    @property
    def complete(self) -> bool:
        """True if every message seen on the server was accounted for."""
        return self.failed == 0


# TRANSITIONAL -- the date mechanism of 0.9.2, carried as a resume token so the
# protocol can change ahead of the backends. Both backends still resume from a
# date; IMAP moves to a UID watermark and Graph to a delta link, and this goes
# with the second of those. It must not survive the branch: a date is not a
# resume point, which is the whole reason for the rebuild.
DATE_KIND = "date"


class DateResumeTracker:
    """Turns the date mechanism into a resume token, both ways.

    Decodes the incoming token into the `since` a backend still filters by,
    collects the timestamps of the messages that were archived, and hands back
    the token for next time -- or None when this pass earned none.

    The two guards that belong to a date live here rather than in the job
    runner, because they are properties of dates and vanish with them:

    - never past the moment the folder was read, so a message dated in the
      future cannot carry the point over whatever arrives in between
    - never backwards, because the search window reaches a day behind the point
      and a pass may legitimately end on an older message than the one it came
      from. A pass given no previous point has nothing to move back from, so a
      full read is authoritative for free -- which is what repairs a point that
      an earlier version set too far ahead.
    """

    def __init__(self, previous: dict | None, observed_at: datetime):
        self.previous = _date_from_token(previous)
        self.observed_at = observed_at
        self.newest: datetime | None = None

    @property
    def since(self) -> datetime | None:
        """The date filter this pass should use, or None to read in full."""
        return self.previous

    def saw(self, date: datetime | None) -> None:
        """Note the timestamp of a stored message, keeping the newest one.

        Call it only for messages that were actually archived: a message that
        failed must not contribute its date and let the next filter skip past it.

        A naive value is read as local time. That is what the IMAP backend hands
        over -- imapclient normalises INTERNALDATE to local time and drops the
        offset -- and it matches how the state file reads back a naive entry, so
        the two cannot disagree by a timezone.
        """
        if date is None:
            return
        if date.tzinfo is None:
            date = date.astimezone()
        if self.newest is None or date > self.newest:
            self.newest = date

    def token(self) -> dict | None:
        """The resume point this pass earned, or None if it earned none."""
        if self.newest is None:
            return None
        candidate = min(self.newest, self.observed_at)
        if self.previous is not None and candidate <= self.previous:
            return None
        return {"kind": DATE_KIND, "date": candidate.isoformat()}


def _date_from_token(resume: dict | None) -> datetime | None:
    """Read the transitional date token, or None for anything else."""
    if resume is None or resume.get("kind") != DATE_KIND:
        return None
    value = resume.get("date")
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        log.warning("resume point holds an unparsable date %r, reading in full", value)
        return None
    return parsed if parsed.tzinfo is not None else parsed.astimezone()


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
        resume: dict | None = ...,
        callback: collections.abc.Callable[[mailutils.MessageMetadata], None] | None = ...,
    ) -> BackupResult:
        """Store a folder's messages, recording each via `callback`.

        `resume` is a point this backend produced on an earlier pass. What it
        contains is the backend's own business -- the caller only stores it and
        hands it back. A backend that does not recognise it, or finds it no
        longer valid, reads the folder in full and says so; that one rule covers
        an upgrade from an older format, a job whose backend was swapped, and a
        source that invalidated its own token.

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
