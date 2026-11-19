"""Atomic file replacement -- the write discipline the archive metadata relies on.

The snapshot state (`state.json`) is written through here: a small file the
archive replaces on every run, where a torn write over SMB or NFS would take the
record of what has already been fetched with it. The metadata log does not go
through here -- it is a content-addressed store now, so each of its files is
written once under its own hash and never rewritten in place. This is the reason
the archive tolerates a network share that does not honour fsync the way a local
filesystem does.

Write the new content to a temporary file in the *same* directory, flush it all
the way to the device, then rename it onto the destination. A rename within one
directory is atomic on every filesystem in practical use, so a reader sees either
the whole previous file or the whole new one -- never a mixture, and never a file
that was destroyed halfway through being rewritten.

The temporary file has to share the destination's directory: a rename is only
atomic within a single filesystem, and anything else risks a copy-then-delete.

`sync_together` is the other way to make files durable: many at once, after the
fact, for a store that does not flush each file as it writes it.
"""

from __future__ import annotations

import collections.abc
import concurrent.futures
import logging
import os
import pathlib
import stat
import sys

log = logging.getLogger(__name__)

# Suffix of the transient file, matching the convention the CAS already uses.
TEMP_SUFFIX = "._tmp_"

# How many fsyncs `sync_together` keeps in flight. A local filesystem merges the
# fsyncs that wait at the same time into one journal commit: 2,000 files and their
# shard directories cost about 80 flushes at the SSD this way and 4,000 one after
# the other (btrfs, measured). `os.fsync` releases the GIL, so threads are enough.
SYNC_THREADS = 64


def sync_directory(path: pathlib.Path) -> None:
    """Flush a directory entry so a rename survives a power loss.

    Not every platform supports opening a directory for fsync -- Windows refuses,
    and some network shares do not implement it. The rename itself is still
    atomic there, so a failure costs durability of the last update only and is
    logged at debug level rather than raised.
    """
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError as exc:
        log.debug("%s: directory not open-able for sync: %s", path, exc)
        return
    try:
        os.fsync(fd)
    except OSError as exc:
        log.debug("%s: directory sync failed: %s", path, exc)
    finally:
        os.close(fd)


def _fsync_opened(path: pathlib.Path, flags: int) -> None:
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_posix(path: pathlib.Path) -> None:
    """Flush the content of the file at `path`; POSIX flushes through any descriptor."""
    _fsync_opened(path, os.O_RDONLY)


def _fsync_windows(path: pathlib.Path) -> None:
    """Flush the content of the file at `path` through a handle that may write.

    Windows flushes through no other kind, and an entry of the store is
    read-only, so the protection comes off for the moment the flush takes. A
    file that is not protected is flushed as it is: where the protection cannot
    be set, lifting it would fail every flush for nothing. A protected file
    whose protection cannot come off cannot be flushed, and raises. Putting the
    protection back is not part of the flush: when that fails, the file is
    flushed and merely writable.
    """
    mode = path.stat().st_mode
    if mode & stat.S_IWRITE:
        _fsync_opened(path, os.O_RDWR)
        return
    path.chmod(mode | stat.S_IWRITE)
    try:
        _fsync_opened(path, os.O_RDWR)
    finally:
        try:
            path.chmod(mode)
        except OSError as exc:
            log.debug("%s: write protection not restored: %s", path, exc)


# What `sync_together` flushes each file with.
_fsync_path = _fsync_windows if os.name == "nt" else _fsync_posix


if sys.platform == "darwin":
    import fcntl

    def flush_drive_cache(path: pathlib.Path) -> None:
        """Make the drive write out its cache, after a group of fsyncs.

        `fsync` on macOS hands the data to the drive and returns; `F_FULLFSYNC`
        on any file makes the drive flush everything it holds, so one call
        covers all the fsyncs before it. A share refuses it, and there the
        fsyncs have already gone to the server, which is all a share offers.
        Never raises.
        """
        try:
            fd = os.open(path, os.O_RDONLY)
            try:
                fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
            finally:
                os.close(fd)
        except OSError as exc:
            log.debug("%s: F_FULLFSYNC refused: %s", path, exc)

else:

    def flush_drive_cache(path: pathlib.Path) -> None:
        """Nothing to do: `fsync` reaches past the drive's cache here."""


def sync_together(paths: collections.abc.Iterable[pathlib.Path]) -> None:
    """Flush the content of `paths`, then the directories that name them.

    All at the same time from a thread pool, so that a local filesystem can merge
    them. The directories come second: once they are flushed, a file is on the
    device under its name. A directory that cannot be flushed is logged and
    passed over, as in `sync_directory`.

    Raises the OSError of the first file that could not be flushed. Nothing that
    depends on these files may go ahead then.
    """
    files = list(dict.fromkeys(paths))
    if not files:
        return
    directories = sorted({path.parent for path in files})
    threads = min(SYNC_THREADS, len(files))
    with concurrent.futures.ThreadPoolExecutor(threads) as pool:
        for _ in pool.map(_fsync_path, files):
            pass
        for _ in pool.map(sync_directory, directories):
            pass
    flush_drive_cache(files[0])


def write_text(path: pathlib.Path, text: str) -> None:
    """Replace `path` with `text`, atomically, creating parent directories.

    Raises OSError if the content could not be written; the destination is left
    untouched in that case and no temporary file remains.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + TEMP_SUFFIX)
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        tmp_path.replace(path)
    except OSError:
        tmp_path.unlink(missing_ok=True)
        raise
    sync_directory(path.parent)
