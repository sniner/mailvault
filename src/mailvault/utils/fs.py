"""Filesystem work that `pathlib` leaves to the caller"""

from __future__ import annotations

import contextlib
import logging
import pathlib
import stat

log = logging.getLogger(__name__)

# What makes a file writable, for whoever it is that opens it.
WRITE_BITS = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH

# Whether this filesystem carries the mode at all, as answered by the first file
# a run protects and then not asked again -- see `set_read_only`.
_chmod_honoured: bool | None = None


def _is_read_only(path: pathlib.Path) -> bool:
    """Whether the write bits are really gone, which only a `stat` can say."""
    try:
        return not (stat.S_IMODE(path.stat().st_mode) & WRITE_BITS)
    except OSError as exc:
        log.debug("%s: unreadable right after chmod: %s", path, exc)
        return False


def _drop_write_bits(path: pathlib.Path) -> bool:
    """Take the write bits off, saying whether the call itself went through."""
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        path.chmod(mode & ~WRITE_BITS)
    except OSError as exc:
        log.debug("%s: not write-protected: %s", path, exc)
        return False
    return True


def set_read_only(path: pathlib.Path) -> bool:
    """Take the write bits off `path`, leaving the read bits alone.

    Comfort, not security: it stops a slip, not a decision, and not deletion.
    Whether the filesystem carries the mode at all is settled once per run by
    reading the first chmod back -- a desktop-mounted SMB share reports success
    and changes nothing -- and given up on for good the first time a later chmod
    is refused. Never raises: a file that could not be protected is still a file
    that was stored.
    """
    global _chmod_honoured

    if _chmod_honoured is False:
        return False
    elif _chmod_honoured is True:
        if not _drop_write_bits(path):
            log.debug("write protection no longer holds here")
            _chmod_honoured = False
    else:
        _chmod_honoured = _drop_write_bits(path) and _is_read_only(path)
        log.debug("write protection %s here", "holds" if _chmod_honoured else "does not hold")
    return _chmod_honoured


def remove_file(path: pathlib.Path, missing_ok: bool = False) -> None:
    """Delete a file, including one that is write-protected.

    Under POSIX the protection does not stand in the way -- `unlink` hangs on
    the directory's write bit, not the file's. Windows refuses while the
    read-only attribute is set, which would leave `archive compact` unable to
    remove the very files this archive protected. So it comes off, and the file
    is asked to go a second time.

    Raises whatever `unlink` raises on that second attempt: a caller deleting a
    file it means to be rid of has to hear that it is still there.
    """
    try:
        path.unlink(missing_ok=missing_ok)
        return
    except PermissionError:
        log.debug("%s: refused, lifting the write protection", path)
    with contextlib.suppress(OSError):
        path.chmod(stat.S_IMODE(path.stat().st_mode) | stat.S_IWUSR | stat.S_IWGRP)
    path.unlink(missing_ok=missing_ok)
