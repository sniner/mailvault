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
"""

from __future__ import annotations

import logging
import os
import pathlib

log = logging.getLogger(__name__)

# Suffix of the transient file, matching the convention the CAS already uses.
TEMP_SUFFIX = "._tmp_"


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
