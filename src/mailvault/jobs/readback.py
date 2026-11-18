"""Reading back what earlier runs stored, before a run relies on it.

Messages are written without a flush (`cas.Flush`). A power loss shortly after
can leave one empty or cut short under its name, recorded in the log and with
the resume point of its folder past it. Nothing would fetch it again on its own:
the store answers "is this message here?" by its name. So every command that
stores or rewrites messages opens the archive through `open_for_writing`, which
first reads back the messages recorded since the last time, holds each against
its hash, sets aside what is damaged, and drops the resume point of each folder
it was recorded in. The next backup then
compares that folder with the server and fetches what is missing, and starts a
new resume point.

How far a place has been read back is noted in its head (`Head.checked`), as a
position in its log chain. Only log files older than `MIN_AGE` are read back.
Soon after the write the bytes still come from memory, and reading them would
vouch for what may not have reached the disk yet.

A backup records every message under the folder it was reading, and a Gmail
message under its labels besides (`base.places_read_from`). So the folder whose
resume point lies past a damaged message is always among its places. A place
without a resume point is a label, or a folder whose next pass compares it with
the server anyway.
"""

from __future__ import annotations

import collections.abc
import dataclasses
import logging
import pathlib
import time

from mailvault import utils
from mailvault.store import cas, heads, metalog

log = logging.getLogger(__name__)

# How old a log file has to be before its messages are read back. Linux writes
# out what a program wrote within about 30 seconds, a filesystem commits its
# journal every few seconds, and a network share has the data once the file is
# closed. An hour leaves room for laptop modes that hold writes for minutes. What
# is younger waits for a later run.
MIN_AGE = 60 * 60

# How often reading back says where it is. After a first backup it reads every
# message that backup stored.
PROGRESS_EVERY = 10_000


@dataclasses.dataclass
class ReadBackResult:
    """What reading back found.

    `damaged` holds the store ids of the messages that were set aside or that the
    log names and the archive no longer holds; both are fetched again. `folders`
    names the places whose resume points were dropped for them, as
    `job::folder`, `mailboxes` the jobs that recorded a damaged message, and
    `imports` the imports whose messages have to be imported again.
    `unreadable` counts the messages that could not be read at all; their
    places are read back again next time.
    """

    read: int = 0
    damaged: list[str] = dataclasses.field(default_factory=list)
    folders: list[str] = dataclasses.field(default_factory=list)
    mailboxes: set[str] = dataclasses.field(default_factory=set)
    imports: set[str] = dataclasses.field(default_factory=set)
    unreadable: int = 0


@dataclasses.dataclass
class _Pending:
    """One place with messages to read back."""

    path: pathlib.Path
    head: heads.Head
    # Where `checked` moves to once the messages below are read back.
    upto: str
    store_ids: set[str]


def _unchecked(
    log_store: cas.ContentAddressedStorage,
    head: heads.Head,
    oldest_write: float,
) -> tuple[str, list[str]] | None:
    """The log files of `head` past `checked` and old enough, as (newest, store ids).

    None when there is nothing to read back yet. The walk stops where the chain
    is broken; `archive check` reports that.
    """
    files: list[metalog.LogFile] = []
    hashval = head.log
    while hashval is not None and hashval != head.checked:
        path = log_store.locate(hashval, exists=True)
        logfile = None if path is None else metalog.read_log(path)
        if logfile is None:
            break
        files.append(logfile)
        hashval = logfile.prev
    for index, logfile in enumerate(files):
        try:
            written = logfile.path.stat().st_mtime
        except OSError:
            continue
        if written <= oldest_write:
            store_ids = [sid for older in files[index:] for sid in older.store_ids]
            return logfile.hashval, store_ids
    return None


def _pending(archive: pathlib.Path, oldest_write: float) -> list[_Pending]:
    log_store = metalog.open_store(archive / metalog.DEFAULT_LOG_DIR)
    found = []
    for path in heads.head_files(archive / heads.DEFAULT_HEADS_DIR):
        head = heads.read_file(path)
        if head is None or head.log is None or head.log == head.checked:
            continue
        unchecked = _unchecked(log_store, head, oldest_write)
        if unchecked is not None:
            upto, store_ids = unchecked
            found.append(_Pending(path, head, upto, set(store_ids)))
    return found


def _verdicts(
    store: cas.ContentAddressedStorage,
    store_ids: collections.abc.Iterable[str],
    result: ReadBackResult,
) -> tuple[set[str], set[str]]:
    """Read each message once; return the ids that are damaged and the unreadable ones."""
    damaged: set[str] = set()
    unreadable: set[str] = set()
    unique = list(dict.fromkeys(store_ids))
    for store_id in unique:
        result.read += 1
        if result.read % PROGRESS_EVERY == 0:
            log.info(
                "read back: %s of %s", f"{result.read:,}", utils.counted(len(unique), "message")
            )
        try:
            if store.verify_entry(store_id):
                continue
        except FileNotFoundError:
            log.warning("%s: recorded, but not in the archive", store_id)
            damaged.add(store_id)
            continue
        except OSError as exc:
            log.error("%s: unreadable, read back again next time: %s", store_id, exc)
            unreadable.add(store_id)
            continue
        path = store.locate(store_id, exists=True)
        if path is not None and store.quarantine(path) is None:
            # Still under its name: the store goes on saying it is here, so it
            # is read back again next time rather than counted as handled.
            unreadable.add(store_id)
            continue
        damaged.add(store_id)
    return damaged, unreadable


def _to_drop(
    pending: list[_Pending],
    damaged: set[str],
    result: ReadBackResult,
) -> set[pathlib.Path]:
    """The heads whose resume points go, and in `result` whose messages were damaged."""
    dropped: set[pathlib.Path] = set()
    for entry in pending:
        if not damaged.intersection(entry.store_ids):
            continue
        if entry.head.job is None:
            result.imports.add(entry.head.folder or "?")
            continue
        result.mailboxes.add(entry.head.job)
        if entry.head.resume is not None:
            dropped.add(entry.path)
    return dropped


def _rewrite(
    archive: pathlib.Path,
    pending: list[_Pending],
    damaged: set[str],
    unreadable: set[str],
    result: ReadBackResult,
) -> None:
    """Move `checked` on, and drop the resume points the damaged messages lie behind.

    Each head is read again right before it is written, and one that a running
    backup changed meanwhile is left alone: its own write wins, and what is
    left is read back next time.
    """
    dropped = _to_drop(pending, damaged, result)
    root = archive / heads.DEFAULT_HEADS_DIR
    moving = {entry.path: entry for entry in pending}
    for path in heads.head_files(root):
        entry = moving.get(path)
        head = heads.read_file(path)
        if head is None:
            continue
        changed = False
        if entry is not None and head.log == entry.head.log:
            if not unreadable.intersection(entry.store_ids):
                head.checked = entry.upto
                changed = True
        if head.resume is not None and path in dropped:
            head.resume = None
            result.folders.append(heads.place_name(head.job, head.folder))
            changed = True
        if changed:
            try:
                heads.write(root, head)
            except OSError as exc:
                log.error("%s: head not written: %s", heads.where(path), exc)


def read_back(archive: pathlib.Path, now: float | None = None) -> ReadBackResult:
    """Read back the messages earlier runs recorded, and set aside what is damaged.

    `now` is for tests; the age of a log file is measured against it.
    """
    result = ReadBackResult()
    oldest_write = (time.time() if now is None else now) - MIN_AGE
    pending = _pending(archive, oldest_write)
    if not pending:
        return result
    store_ids = [sid for entry in pending for sid in entry.store_ids]
    log.info(
        "reading back %s that earlier runs stored, in case a power loss damaged them",
        utils.counted(len(set(store_ids)), "message"),
    )
    damaged, unreadable = _verdicts(cas.mail_store(archive), store_ids, result)
    result.damaged = sorted(damaged)
    result.unreadable = len(unreadable)
    _rewrite(archive, pending, damaged, unreadable, result)
    _report(result)
    return result


@dataclasses.dataclass
class WritableArchive:
    """An archive that a command is about to store or rewrite messages in.

    Made by `open_for_writing`, which reads back first; `found` is what that
    came to. A command gets its message store from here, so that none writes
    into an archive that was not read back. One is enough for a run: a command
    that runs several jobs opens it once and hands it to each.
    """

    path: pathlib.Path
    found: ReadBackResult

    def store(
        self,
        compress: bool = False,
        flush: cas.Flush = cas.Flush.NEVER,
    ) -> cas.ContentAddressedStorage:
        """The message store, as `cas.mail_store` opens it."""
        return cas.mail_store(self.path, compress=compress, flush=flush)


def open_for_writing(archive: pathlib.Path) -> WritableArchive:
    """Read back what earlier runs stored in `archive`, and open it for a writing command."""
    return WritableArchive(archive, read_back(archive))


def _report(result: ReadBackResult) -> None:
    if not result.damaged:
        log.info("read back: every message is intact")
        return
    log.warning(
        "read back: %s damaged or missing, most likely by a power loss",
        utils.counted(len(result.damaged), "message"),
    )
    for place in result.folders:
        log.warning(
            "%s: `backup` compares the folder with the server and fetches them again",
            place,
        )
    for name in sorted(result.imports):
        log.warning(
            "%s: import its source again under this name"
            " (`archive import --name %s SOURCE`) to bring them back",
            name,
            name,
        )
