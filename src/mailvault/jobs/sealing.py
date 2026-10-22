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
from mailvault.jobs.check import CONTENTS_PROGRESS_EVERY
from mailvault.jobs.common import JobError
from mailvault.jobs.guard import named_hash
from mailvault.store import cas, heads, metalog, seals

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
            result.gone_logs.setdefault(_log_path(store, name), []).append(seal)


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


@dataclasses.dataclass
class SealCheck:
    """What checking a seal against the archive found.

    `log_files` and `messages` are what the seal records, `since_*` what the
    archive holds beyond it. The findings are kept apart by kind, because each
    calls for a different move, and a message is named with the place the seal
    recorded it in.
    """

    seal_id: str
    seal: seals.Seal | None = None
    unusable: seals.Unusable | None = None
    log_files: int = 0
    messages: int = 0
    since_logs: int = 0
    since_messages: int = 0
    gone_logs: list[pathlib.Path] = dataclasses.field(default_factory=list)
    damaged_logs: list[pathlib.Path] = dataclasses.field(default_factory=list)
    newer_logs: list[pathlib.Path] = dataclasses.field(default_factory=list)
    gone_messages: dict[str, str] = dataclasses.field(default_factory=dict)
    damaged_messages: dict[str, str] = dataclasses.field(default_factory=dict)
    unreadable_messages: dict[str, str] = dataclasses.field(default_factory=dict)

    @property
    def findings(self) -> int:
        """How many things are not as the seal recorded them."""
        return (
            len(self.gone_logs)
            + len(self.damaged_logs)
            + len(self.newer_logs)
            + len(self.gone_messages)
            + len(self.damaged_messages)
            + len(self.unreadable_messages)
        )

    @property
    def holds(self) -> bool:
        """True when the archive still holds everything the seal recorded, unchanged."""
        return self.seal is not None and not self.findings


def seal_hash(store_path: pathlib.Path, seal_id: str) -> str:
    """The hash a seal id is named in, told by its length.

    Not taken from the configuration: a copy of an archive restored without its
    configuration has to be checkable, and the configuration may lie outside the
    archive anyway. Raises JobError for anything but a whole id, and for an id in
    another hash than the archive's messages, which cannot be a seal of it.
    """
    archive_hash = named_hash(store_path)
    name = cas.hash_by_length(seal_id) if cas.is_hashval(seal_id) else None
    if name is None:
        raise JobError(
            f"{seal_id}: a seal id is checked only in full: {_id_length(archive_hash)}."
            f" The beginning of one proves nothing, a forged seal can be made to start"
            f" the same way"
        )
    if archive_hash is not None and archive_hash != name:
        raise JobError(
            f"{seal_id}: not a seal of this archive. Its messages are named in"
            f" {archive_hash}, and this id is a {name} hash"
        )
    return name


def check_seal(store_path: pathlib.Path, seal_id: str) -> SealCheck:
    """Check that the archive still holds everything the seal `seal_id` recorded.

    Every log file the seal names must be there and match its name, and every
    message those log files name is opened at the path its id gives and read in
    full. `mail/` is not listed: that finds files nobody expects, which is
    `archive check`'s business, and a seal claims nothing about them.

    The run's hash has to be the seal's; see `seal_hash`.
    """
    meta = store_path / metalog.DEFAULT_LOG_DIR
    result = SealCheck(seal_id=seal_id)
    result.seal, result.unusable = seals.find(meta, seal_id)
    if result.seal is None:
        return result
    sealed = _sealed_messages(meta, result.seal, result)
    log.info(
        "seal of %s: %s, %s",
        result.seal.when,
        utils.counted(result.log_files, "log file"),
        utils.counted(result.messages, "message"),
    )
    _check_messages(cas.mail_store(store_path), sealed, result)
    _count_since(meta, result.seal, sealed, result)
    return result


def _id_length(hash_name: str | None) -> str:
    """How long a whole id is, in this archive or, where that is not known, in any."""
    names = [hash_name] if hash_name is not None else list(cas.HASHES)
    lengths = " or ".join(str(len(cas.HASHES[name]().hexdigest())) for name in names)
    return f"{lengths} hex characters" + (" in this archive" if hash_name else "")


def _log_path(store: cas.ContentAddressedStorage, name: str) -> pathlib.Path:
    """Where the log file `name` lies, or would lie."""
    # Without `exists`, locate always names a path; the fallback is for the type.
    return store.locate(name) or store.root_dir / name


def _sealed_messages(
    meta: pathlib.Path,
    seal: seals.Seal,
    result: SealCheck,
) -> dict[str, str]:
    """Every message the sealed log files name, with the place first recorded for it.

    A log file that is gone, damaged or unreadable here is a finding, and what it
    names is not read: its content is not the content that was sealed.
    """
    store = metalog.open_store(meta)
    sealed: dict[str, str] = {}
    for name in sorted(seal.logs):
        path = store.locate(name, exists=True)
        if path is None:
            result.gone_logs.append(_log_path(store, name))
            continue
        logfile, why = metalog.examine_log(path)
        if logfile is None and why is metalog.Unreadable.NEWER:
            result.newer_logs.append(path)
        elif logfile is None or not logfile.intact:
            result.damaged_logs.append(path)
        else:
            where = heads.place_name(logfile.mailbox, logfile.folder)
            for store_id in logfile.store_ids:
                sealed.setdefault(store_id, where)
    result.log_files = len(seal.logs)
    result.messages = len(sealed)
    return sealed


def _check_messages(
    store: cas.ContentAddressedStorage,
    sealed: dict[str, str],
    result: SealCheck,
) -> None:
    """Read every sealed message in full and hold it against its id.

    In order of id, which is the order the store files them in, so the reads
    move through the shards instead of jumping between them.
    """
    total = len(sealed)
    log.info("reading %s, each one in full", utils.counted(total, "sealed message"))
    for read, (store_id, where) in enumerate(sorted(sealed.items()), start=1):
        if read % CONTENTS_PROGRESS_EVERY == 0:
            log.info("%s of %s checked", f"{read:,}", f"{total:,}")
        try:
            intact = store.verify_entry(store_id)
        except FileNotFoundError:
            result.gone_messages[store_id] = where
            continue
        except OSError as exc:
            log.error("%s: unreadable: %s", store_id, exc)
            result.unreadable_messages[store_id] = where
            continue
        if not intact:
            result.damaged_messages[store_id] = where


def _count_since(
    meta: pathlib.Path,
    seal: seals.Seal,
    sealed: dict[str, str],
    result: SealCheck,
) -> None:
    """Count the log files the seal does not name, and the messages only they name."""
    added: set[str] = set()
    for path in metalog.log_files(meta):
        if path.name.removesuffix(".jsonl") in seal.logs:
            continue
        result.since_logs += 1
        logfile = metalog.read_log(path)
        if logfile is not None:
            added.update(store_id for store_id in logfile.store_ids if store_id not in sealed)
    result.since_messages = len(added)
