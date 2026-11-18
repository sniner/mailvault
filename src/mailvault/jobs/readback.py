"""Reading back what earlier runs stored, before a run relies on it.

Messages are written without a flush (`cas.Flush`). A power loss shortly after
can leave one empty or cut short under its name, recorded in the log and with
the resume point of its folder past it. Nothing would fetch it again on its own:
the store answers "is this message here?" by its name. So every command that
stores or rewrites messages first reads back the messages recorded since the
last time, holds each against its hash, sets aside what is damaged, and drops
the resume point of each folder it was recorded in. The next backup then
compares that folder with the server and fetches what is missing, and starts a
new resume point.

How far a place has been read back is noted in its head (`Head.checked`), as a
position in its log chain. Only log files older than `MIN_AGE` are read back.
Soon after the write the bytes still come from memory, and reading them would
vouch for what may not have reached the disk yet.

The log names the places a message was seen in, not the folder the backup was
reading when it stored it. Mostly they are the same. For Gmail they are not: a
message fetched from `[Gmail]/All Mail` is recorded under its labels, and a
label nobody configured has no resume point. Where none of a message's places
has one, every resume point of its job is dropped.
"""

from __future__ import annotations

import collections.abc
import dataclasses
import logging
import pathlib
import time

from mailvault import utils
from mailvault.jobs.check import quarantine_entry
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
    log names and the archive no longer holds; both are fetched again. `jobs`
    names the jobs that lost every resume point, because no place of a damaged
    message had one, and `folders` the other places whose resume points were
    dropped, as `job::folder`. `imports` names the imports whose messages have
    to be imported again. `unreadable` counts the messages that could not be read
    at all; their places are read back again next time.
    """

    read: int = 0
    damaged: list[str] = dataclasses.field(default_factory=list)
    folders: list[str] = dataclasses.field(default_factory=list)
    jobs: set[str] = dataclasses.field(default_factory=set)
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
        if path is not None and quarantine_entry(path) is None:
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
    """The heads whose resume points go, and in `result` the jobs that lose all of theirs."""
    dropped: set[pathlib.Path] = set()
    for store_id in damaged:
        holding = [entry for entry in pending if store_id in entry.store_ids]
        resumed = [entry.path for entry in holding if entry.head.resume is not None]
        dropped.update(resumed)
        for entry in holding:
            if entry.head.job is None:
                result.imports.add(entry.head.folder or "?")
            elif not resumed:
                result.jobs.add(entry.head.job)
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
        if head.resume is not None and (path in dropped or head.job in result.jobs):
            head.resume = None
            if head.job not in result.jobs:
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


def _report(result: ReadBackResult) -> None:
    if not result.damaged:
        log.info("read back: every message is intact")
        return
    log.warning(
        "read back: %s damaged or missing, most likely by a power loss",
        utils.counted(len(result.damaged), "message"),
    )
    for job in sorted(result.jobs):
        log.warning(
            "%s: the next backup compares all its folders with the server and fetches"
            " them again",
            job,
        )
    for place in result.folders:
        log.warning(
            "%s: the next backup compares the folder with the server and fetches them again",
            place,
        )
    for name in sorted(result.imports):
        log.warning(
            "%s: import its source again under this name"
            " (`archive import --name %s SOURCE`) to bring them back",
            name,
            name,
        )
