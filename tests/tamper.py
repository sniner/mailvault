"""Changing a stored file behind the store's back, the way the world does it.

Entries are written without their write bits (`mailvault.utils.fs`), so a test
that plays bit rot, a botched restore or a viewer that "repairs" the file it is
showing has to take the protection off first -- which is the entire effort it
was ever meant to cost. Only the mode is lifted here; what a test then writes
into the file is its own business.
"""

from __future__ import annotations

import pathlib
import stat


def tamper(path: pathlib.Path, content: bytes | str) -> None:
    """Replace what a stored file holds, write protection or not.

    The mode goes back on afterwards. What the archive then has in front of it
    is what it would really find -- a protected entry whose bytes are wrong --
    and everything that has to keep working on one, quarantine above all, is
    tested against that file and not against a writable stand-in.
    """
    mode = stat.S_IMODE(path.stat().st_mode)
    path.chmod(mode | stat.S_IWUSR)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    path.chmod(mode)
