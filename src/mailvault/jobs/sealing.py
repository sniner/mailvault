"""Seal an archive: record which log files it holds, under an id that proves it later.

What a seal is and where it lies is `mailvault.store.seals`. This is the command
around it, and three decisions shape it.

**A seal is not made over a disturbed archive.** A log file that does not match
its name, a log file that an earlier seal records and that is gone, and a seal
that cannot be read all stop it. A seal over any of them could never be checked,
or would cover up a loss: whoever runs the command would get a fresh id and
believe the archive is whole. Each refusal names its file, because each way out
starts with that file.

**Nothing new since the newest seal, no new seal.** The date makes every seal a
file of its own, so sealing twice would give two ids for one state. The newest
seal is reused instead when it records exactly the log files that are there, and
the command can run after every backup without piling up seals.

**The messages are not read.** That would cost as long as the integrity pass of
`archive check`, which is the command for it. A seal records log files, and the
log files name the messages; checking a seal later reads them all.
"""

from __future__ import annotations

import collections.abc
import dataclasses
import logging
import pathlib
from datetime import UTC, datetime

from mailvault import utils
from mailvault.store import metalog, seals

log = logging.getLogger(__name__)


@dataclasses.dataclass
class SealResult:
    """What sealing did, would have done, or why it did not.

    The four lists are the reasons to refuse, and each names files: a log file
    that is gone is named where it should lie, together with the seals that
    record it.
    """

    seal: seals.Seal | None = None
    reused: bool = False
    first: bool = False
    dry_run: bool = False
    log_files: int = 0
    messages: int = 0
    places: int = 0
    damaged_logs: list[pathlib.Path] = dataclasses.field(default_factory=list)
    gone_logs: dict[pathlib.Path, list[seals.Seal]] = dataclasses.field(default_factory=dict)
    damaged_seals: list[pathlib.Path] = dataclasses.field(default_factory=list)
    newer_seals: list[pathlib.Path] = dataclasses.field(default_factory=list)

    @property
    def refused(self) -> bool:
        """True when the archive was found in a state no seal may be made over."""
        return bool(
            self.damaged_logs or self.gone_logs or self.damaged_seals or self.newer_seals
        )


def seal_archive(
    store_path: pathlib.Path,
    dry_run: bool = False,
    now: datetime | None = None,
) -> SealResult:
    """Seal the archive at `store_path` as it is now, or reuse the seal that already does.

    An archive without any log file is not sealed: there is nothing to record,
    and the result says so by its count of none.
    """
    meta = store_path / metalog.DEFAULT_LOG_DIR
    result = SealResult(dry_run=dry_run)
    present = _present_logs(meta, result)
    existing = _existing_seals(meta, result)
    _find_gone_logs(meta, existing, present, result)
    if result.refused or not present:
        return result
    result.first = not existing
    newest = existing[-1] if existing else None
    if newest is not None and newest.logs == frozenset(present):
        result.seal, result.reused = newest, True
        return result
    _count(present.values(), result)
    if not dry_run:
        seals.open_store(meta).prune_transient_files()
        result.seal = seals.write(meta, present, now or datetime.now(UTC))
    return result


def _present_logs(meta: pathlib.Path, result: SealResult) -> dict[str, pathlib.Path]:
    """Every log file by its name, each one checked against that name."""
    paths = metalog.log_files(meta)
    log.info("checking the checksums of %s", utils.counted(len(paths), "log file"))
    present: dict[str, pathlib.Path] = {}
    for path in paths:
        if not metalog.verify_file(path):
            result.damaged_logs.append(path)
        present[path.name.removesuffix(".jsonl")] = path
    result.log_files = len(present)
    return present


def _existing_seals(meta: pathlib.Path, result: SealResult) -> list[seals.Seal]:
    """The seals already there, oldest first; the ones that cannot be read go to `result`."""
    usable: list[seals.Seal] = []
    for found in seals.all_seals(meta):
        if found.seal is not None:
            usable.append(found.seal)
        elif found.unusable is seals.Unusable.NEWER:
            result.newer_seals.append(found.path)
        else:
            result.damaged_seals.append(found.path)
    return usable


def _find_gone_logs(
    meta: pathlib.Path,
    existing: list[seals.Seal],
    present: dict[str, pathlib.Path],
    result: SealResult,
) -> None:
    """Note every log file a seal records and the archive no longer holds."""
    store = metalog.open_store(meta)
    for seal in existing:
        for name in sorted(seal.logs.difference(present)):
            where = store.locate(name)
            if where is not None:
                result.gone_logs.setdefault(where, []).append(seal)


def _count(paths: collections.abc.Iterable[pathlib.Path], result: SealResult) -> None:
    """How many messages and places the log files to be sealed record."""
    messages: set[str] = set()
    places: set[metalog.Place] = set()
    for path in paths:
        logfile = metalog.read_log(path)
        if logfile is None:
            continue
        messages.update(logfile.store_ids)
        places.add(logfile.place)
    result.messages = len(messages)
    result.places = len(places)
