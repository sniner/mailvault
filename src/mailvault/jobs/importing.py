"""Import existing `.eml` collections into the content-addressed store.

For pulling mail into an archive from somewhere other than a live mailbox -- a
directory of `.eml` files, or a Docuware export where each message sits in its own
folder.

An import is given a name and records what it brought in under it, because that
is a fact no `.eml` carries: which import a message came from. It goes into the
metadata log the way a backup's observations do, with one difference that decides
everything else -- **the mailbox stays empty**. There is no mailbox behind an
import and nobody to ask about it again, and every reader that looks a place up
by a job's name therefore walks past it, because a job always has a name. The
import's own name lives in the folder field, where `db search --folder` finds it.

Two things follow that are worth knowing before reading further. Mail that is
already in the archive is recorded all the same -- that it lay in this import is
true whether or not the store had it -- so importing a source a second time under
a name is how mail imported by an earlier version gets its provenance after the
fact. And what has been recorded is written down in batches, because `--move`
deletes what it has read, and a file whose provenance is still only in memory
must not be the one that goes.
"""

from __future__ import annotations

import collections.abc
import dataclasses
import logging
import os
import pathlib
from datetime import UTC, datetime

from mailvault import utils
from mailvault.jobs.common import SEAL_BATCH, seal_log
from mailvault.store import cas, metalog, zstd

log = logging.getLogger(__name__)


@dataclasses.dataclass
class Provenance:
    """The name an import is recorded under, and the log it is recorded in.

    Both halves or neither: a name with nowhere to write it records nothing, and
    a log with no name has nothing to record. `--dry-run` passes neither, because
    it writes nothing at all.
    """

    name: str
    log: metalog.LogWriter


@dataclasses.dataclass
class ImportResult:
    """What an import did, or what it would have done.

    `failed` holds paths rather than a count, because a count alone does not
    tell anyone which file to look at. `undeleted` is the other half of that and
    deliberately not the same list: those messages are in the archive and their
    provenance is written, and all that is left of them is a source file that
    `--move` could not remove. Counting them as failures would say the import
    fell short where only the tidying up did.

    `recorded` is what the log has taken and made durable, and it is not the same
    as `stored + present`: a seal that fails leaves the messages in the archive
    and their provenance unwritten, which is a thing the report has to be able to
    say out loud.
    """

    stored: int = 0
    present: int = 0
    recorded: int = 0
    name: str | None = None
    dry_run: bool = False
    failed: list[pathlib.Path] = dataclasses.field(default_factory=list)
    undeleted: list[pathlib.Path] = dataclasses.field(default_factory=list)


# Suffixes an archived email can carry: plain and zstd-compressed.
_EML_SUFFIXES = (".eml", ".eml.zst")


def _read_eml(path: pathlib.Path) -> bytes:
    """Read an archived email, decompressing `.zst` files transparently."""
    if path.suffix == ".zst":
        with open(path, "rb") as f, zstd.open_reader(f) as reader:
            return reader.read()
    return path.read_bytes()


def _record(
    provenance: Provenance | None,
    pending: list[pathlib.Path],
    result: ImportResult,
) -> None:
    """Write down what has been observed, then let go of the sources it accounts for.

    In that order and never the other way round. An interrupt between the two
    leaves source files that are already stored and already recorded, and a
    second import finds them, says EXISTS and records them again -- which costs
    a duplicate observation that `archive compact` drops. The other order deletes
    a file whose provenance was never written, and nothing brings that back.

    A seal that fails keeps the sources too. What was observed stays in the
    writer and goes out with the next batch, so nothing has to be repeated by
    hand; what has not been written down must not be let go of in the meantime.

    A file that will not go is noted and stepped over. `remove_file` raises on
    purpose -- a caller meaning to be rid of a file has to hear that it is still
    there -- but here the message behind it is already stored and already
    recorded, and letting the error out ended the whole import: no report, no
    further batch, over one file somebody had open or a directory with the wrong
    write bit.
    """
    if provenance is not None:
        observed = len(provenance.log)
        if not seal_log(provenance.log, datetime.now(UTC)):
            return
        result.recorded += observed
    for path in pending:
        try:
            # The source may be another mailvault archive, whose entries carry a
            # write protection of their own.
            utils.remove_file(path)
        except OSError as exc:
            log.error("%s: file not deleted: %s", path, exc)
            result.undeleted.append(path)
            continue
        log.debug("%s: file deleted", path)
    pending.clear()


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
        provenance: Provenance | None = None,
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

        `provenance` is what records where these messages came from. Without it
        the import is what it used to be and leaves mail nothing says anything
        about.
        """
        result = ImportResult(
            dry_run=dry_run, name=None if provenance is None else provenance.name
        )
        # Sources that have been read but whose provenance is not written yet.
        # They are let go of a batch at a time, never one at a time -- see `_record`.
        pending: list[pathlib.Path] = []
        since_seal = 0
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
            if provenance is not None and not dry_run:
                # Recorded whether the store had it already or not: that this
                # message lay in this import is true either way, and it is what
                # lets a second import give older mail its provenance back.
                provenance.log.add(None, [provenance.name], uid)
            if move and not dry_run:
                pending.append(eml)
            since_seal += 1
            if since_seal >= SEAL_BATCH:
                _record(provenance, pending, result)
                since_seal = 0
        _record(provenance, pending, result)
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
            if not eml:
                continue
            # One directory is one message; where a Docuware export left more
            # than one file, the largest is the message and the rest are its
            # fragments. `max` takes the key it is given, so there is no tuple to
            # build and no `[1]` to remember at the end.
            yield max(eml, key=lambda f: f.stat().st_size)
