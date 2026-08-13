"""Import existing `.eml` collections into the content-addressed store.

For pulling mail into an archive from somewhere other than a live mailbox -- a
directory of `.eml` files, or a Docuware export where each message sits in its own
folder. Unlike a backup, an import writes no metadata log, so which mailbox and
folder a message came from is not recorded; rebuild a database with
`db create` afterwards if you need to query it.
"""

from __future__ import annotations

import collections.abc
import dataclasses
import logging
import os
import pathlib

from mailvault import utils
from mailvault.store import cas, zstd

log = logging.getLogger(__name__)


@dataclasses.dataclass
class ImportResult:
    """What an import did, or what it would have done.

    `failed` holds paths rather than a count, because a count alone does not
    tell anyone which file to look at.
    """

    stored: int = 0
    present: int = 0
    dry_run: bool = False
    failed: list[pathlib.Path] = dataclasses.field(default_factory=list)


# Suffixes an archived email can carry: plain and zstd-compressed.
_EML_SUFFIXES = (".eml", ".eml.zst")


def _read_eml(path: pathlib.Path) -> bytes:
    """Read an archived email, decompressing `.zst` files transparently."""
    if path.suffix == ".zst":
        with open(path, "rb") as f, zstd.open_reader(f) as reader:
            return reader.read()
    return path.read_bytes()


class ExternalMailArchive:
    def __init__(self, root_dir: pathlib.Path):
        self.root_dir = root_dir

    def walk(self) -> collections.abc.Generator[pathlib.Path, None, None]:
        """Yield paths to all archived emails (plain and zstd-compressed)."""
        for path, _, files in os.walk(self.root_dir):
            for f in files:
                if f.endswith(_EML_SUFFIXES):
                    yield pathlib.Path(path, f)

    def archive_to_cas(
        self,
        store: cas.ContentAddressedStorage,
        move: bool = False,
        dry_run: bool = False,
    ) -> ImportResult:
        """Import all emails into the content-addressed store.

        With `dry_run` every message is still read and hashed -- the store is
        asked the same question in the same way -- but nothing is written and
        no source file is removed.

        The two counts are what the dry run is for. A source that was altered
        on its way here -- headers stripped, line endings rewritten, a message
        re-encoded -- yields bytes the store cannot recognise as ones it already
        holds, and the only sign of that is a "new" count where a small one was
        expected. It has to be seen *before* anything is written, because
        afterwards a mangled message is an entry like any other: nothing tells
        it apart from one that really is new.
        """
        result = ImportResult(dry_run=dry_run)
        for eml in self.walk():
            try:
                data = _read_eml(eml)
                if dry_run:
                    uid = store.hashval(data)
                    status = "EXISTS" if store.locate(uid, exists=True) else "NEW"
                else:
                    status, uid, _ = store.add(data)
            except Exception as exc:
                log.error("Error adding %s to store: %s", eml, exc)
                result.failed.append(eml)
                continue
            log.info("%s: %s: %s", eml, status, uid)
            if status == "NEW":
                result.stored += 1
            else:
                result.present += 1
            if move and not dry_run:
                # The source may be another mailvault archive, whose entries carry a
                # write protection of their own.
                utils.remove_file(eml)
                log.debug("%s: file deleted", eml)
        return result

    def stats(self) -> tuple[int, int]:
        """Return (count, total_size_in_bytes) for all emails in the archive."""
        size = 0
        count = 0
        for eml in self.walk():
            count += 1
            size += eml.stat().st_size
        return count, size


class DocuwareMailArchive(ExternalMailArchive):
    def walk(self) -> collections.abc.Generator[pathlib.Path, None, None]:
        """Yield paths to .eml files in a Docuware archive (one per directory, largest wins).

        Docuware exports are always plain `.eml` (never zstd-compressed), so --
        unlike the base class -- the `.eml.zst` variant is deliberately ignored.
        """
        for path, _, files in os.walk(self.root_dir):
            eml = [pathlib.Path(path, f) for f in files if f.endswith(".eml")]
            if len(eml) > 1:
                eml_file = max([(f.stat().st_size, f) for f in eml], key=lambda x: x[0])[1]
            elif len(eml) == 1:
                eml_file = eml[0]
            else:
                continue
            yield eml_file
