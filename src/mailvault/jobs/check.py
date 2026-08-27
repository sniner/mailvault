"""Check that an archive is what it says it is.

The store is built so that an entry cannot be written half-way: content on the
device before the rename, the directory entry after it, the name a hash of the
bytes. What none of that covers is the time afterwards -- bit rot, a restore
that dropped a file, a copy that ran out of disk, an entry written by a version
that did not yet flush. The archive is usually the only copy, and it has no way
of noticing any of that on its own: `add` asks whether a *name* is there, never
whether the bytes behind it are still the ones it was named for.

So this reads the archive and holds it against what it claims:

- every file lying in a shard is an entry, not something that wandered in
- every message the metadata log references is there
- every log file still matches its own name
- and every message still hashes to the name it is filed under

The last one is the integrity check, and it is on by default because it turned
out to be far cheaper than it sounds. It reads twenty times the bytes of the
walk above it, but bytes are not what a network share charges for: the walk pays
a round trip per shard directory and the read one per message, and at two
messages per shard those come out level. Measured over SMB on a 131,000-message
archive: 16 minutes for the walk, 17 for reading every message.

`contents=False` leaves it out for whoever wants the tree checked without the second
half of the wait -- and a run that did that says so, because otherwise "nothing
found" would mean two different things on two different days.

Nothing here repairs. What it removes is what cannot be data: the transient file
of a write that was interrupted, under the same age rule `compact` uses. A
message that is missing comes back through a command that has a server to ask.
The one exception is `quarantine`, which does not repair either -- it takes the
name away from an entry that has been proved not to deserve it, so that the
store stops answering "yes, that one is here" and something can fetch it again.
"""

from __future__ import annotations

import collections.abc
import dataclasses
import logging
import os
import pathlib
import re

from mailvault import utils
from mailvault.jobs.common import JobError
from mailvault.store import cas, heads, metalog

log = logging.getLogger(__name__)

# How often the two long passes say where they are. The walk is quick per item
# and huge in number; reading contents is the opposite, so it reports sooner.
WALK_PROGRESS_EVERY = 20_000
CONTENTS_PROGRESS_EVERY = 2_000

# What an entry that failed its own hash is renamed to. The suffix is what takes
# it out of the store's name space: `hashval_of` no longer recognises the name,
# so `walk`, `create-db` and the existence check behind `add` all stop seeing it.
QUARANTINE_SUFFIX = ".corrupt"

# Enough to get past the entries quarantined by earlier runs of a message that
# keeps coming back broken. Past that, something else is wrong.
_QUARANTINE_ATTEMPTS = 100

# What a quarantined entry looks like afterwards: the name it had, the suffix,
# and a number when an earlier quarantine already took the plain form.
_QUARANTINED = re.compile(rf"(?P<entry>.+){re.escape(QUARANTINE_SUFFIX)}(\.\d+)?\Z")


@dataclasses.dataclass
class CheckResult:
    """What an archive turned out to be.

    The lists hold what a reader has to be able to name -- a count alone does
    not tell anyone which file to look at, and "110 messages have no
    provenance" is not something anyone can act on without seeing which. Long
    lists are not the worry: a report prints the first few and says how many
    more there are, which is what makes even an archive of nothing but imported
    mail -- where every message is an orphan -- readable.
    """

    entries: int = 0
    referenced: int = 0
    observations: int = 0
    places: int = 0
    log_files: int = 0
    transient_removed: int = 0
    quarantined_before: int = 0
    contents_checked: bool = False
    missing: dict[str, str] = dataclasses.field(default_factory=dict)
    broken_chains: list[str] = dataclasses.field(default_factory=list)
    unreadable_chains: list[str] = dataclasses.field(default_factory=list)
    newer_chains: list[str] = dataclasses.field(default_factory=list)
    unchained: list[pathlib.Path] = dataclasses.field(default_factory=list)
    orphans: list[pathlib.Path] = dataclasses.field(default_factory=list)
    foreign: list[pathlib.Path] = dataclasses.field(default_factory=list)
    damaged_logs: list[pathlib.Path] = dataclasses.field(default_factory=list)
    corrupt: list[pathlib.Path] = dataclasses.field(default_factory=list)
    unreadable: list[pathlib.Path] = dataclasses.field(default_factory=list)
    quarantined: list[pathlib.Path] = dataclasses.field(default_factory=list)

    @property
    def findings(self) -> int:
        """How many things were found that say the archive is not what it claims.

        A foreign file and an orphan are deliberately not among them. Neither
        says anything is wrong with the archive: somebody put a file in a
        directory, and a message can be in the archive with nothing recording
        where it came from -- everything imported before an import took a name,
        and everything a lost log entry used to account for.

        A log file no chain reaches is not among them either, for the same
        reason: it is still read, because the glob and not the chain is what
        enumerates the log. It says the chain is behind, not that anything is
        gone. A chain naming a file that is *not there* does say that, and
        counts.
        """
        return (
            len(self.missing)
            + len(self.damaged_logs)
            + len(self.corrupt)
            + len(self.unreadable)
            + len(self.broken_chains)
        )

    @property
    def sound(self) -> bool:
        """True when nothing found says the archive is not what it claims."""
        return not self.findings


def _store_files(root: pathlib.Path) -> collections.abc.Iterator[pathlib.Path]:
    """Yield every file below the message store.

    `root` is the store's own directory, not the archive: since the messages
    moved into `mail/`, nothing else lives under here, and the walk needs no
    exceptions. It used to run over the archive root and had to step around
    `meta/` -- and everything else beside the store, an archive's `FORMAT`, its
    `mailvault.toml`, an `index.db`, the `store.db.migrated` of a migration, was
    simply out of reach of any judgement.
    """
    for path, _dirs, files in os.walk(root):
        here = pathlib.Path(path)
        for fname in files:
            yield here / fname


def _quarantined_origin(store: cas.ContentAddressedStorage, path: pathlib.Path) -> str | None:
    """The store id a quarantined file used to be filed under, or None.

    So that what an earlier run set aside is recognised for what it is instead
    of being reported as a stray file on every run from now on.
    """
    match = _QUARANTINED.match(path.name)
    if match is None:
        return None
    return store.hashval_of(path.with_name(match.group("entry")))


def _classify(
    store: cas.ContentAddressedStorage,
    result: CheckResult,
    step: str,
) -> dict[str, pathlib.Path]:
    """Walk the shards once, sorting what is there and clearing away leftovers.

    Returns the entries by store id, which is what the comparison against the
    log needs -- and, because it comes from the walk rather than from asking
    about each store id in turn, the comparison costs nothing on top.
    """
    entries: dict[str, pathlib.Path] = {}
    seen = 0
    for path in _store_files(store.root_dir):
        seen += 1
        if seen % WALK_PROGRESS_EVERY == 0:
            log.info("%s: %s seen", step, utils.counted(seen, "file"))
        hashval = store.hashval_of(path)
        if hashval is not None:
            entries[hashval] = path
        elif store.is_stale_transient(path):
            result.transient_removed += int(store.drop_transient(path))
        elif _quarantined_origin(store, path) is not None:
            result.quarantined_before += 1
        elif store.transient_origin(path) is None:
            # Whatever is left is neither an entry, nor something an earlier run
            # set aside, nor a transient file a *running* writer may still hold
            # -- that last one is left alone rather than reported, because a
            # backup working on the archive right now is not a finding.
            result.foreign.append(path)
    result.entries = len(entries)
    return entries


def _read_log(
    root: pathlib.Path,
    result: CheckResult,
    chain: dict[str, metalog.LogFile],
    unread: dict[str, metalog.Unreadable],
) -> dict[str, str]:
    """Read the whole log into `store id -> where it was first seen`.

    Each file is held against its own name as well. `read_log` warns about a
    mismatch in passing, which is right in the middle of a backup and useless
    here -- a check has to come back with the list.

    Three counts come out of it, and only two of them are anybody's business
    outside this module. `referenced` and `places` are what a reader of the check
    can do something with: how many messages the log accounts for, and how many
    mailbox/folder places it knows. `observations` -- one per message per place,
    so a Gmail message under three labels counts three times -- is kept because
    counting it is free and it says something about the log's shape, but it is
    not reported: it is neither files nor messages nor folders, nothing follows
    from it being large, and six figures beside two-figure neighbours gets read
    as an answer to whichever question the reader brought.
    """
    seen_at: dict[str, str] = {}
    places: set[str] = set()
    for path in metalog.log_files(root):
        result.log_files += 1
        if not metalog.verify_file(path):
            result.damaged_logs.append(path)
        logfile, why = metalog.examine_log(path)
        if logfile is None:
            # Kept with its reason, because the chain walk has three things to
            # tell apart and they call for three different moves. A file that is
            # not there is gone. One written by a newer mailvault is present and
            # intact, and calling it gone would be a statement about the archive
            # for something that is true of the reader. One this version cannot
            # read for any other reason is damaged, and telling its owner to
            # upgrade would send them away from the only thing that helps.
            unread[path.name.removesuffix(".jsonl")] = why or metalog.Unreadable.DAMAGED
            continue
        chain[logfile.hashval] = logfile
        where = heads.place_name(logfile.mailbox, logfile.folder)
        places.add(where)
        for store_id in logfile.store_ids:
            result.observations += 1
            seen_at.setdefault(store_id, where)
    result.places = len(places)
    result.referenced = len(seen_at)
    return seen_at


def _walk_chains(
    heads_root: pathlib.Path,
    chain: dict[str, metalog.LogFile],
    result: CheckResult,
    unread: dict[str, metalog.Unreadable],
) -> None:
    """Follow every place's log chain and report where it does not hold.

    Four different things come out of this, and the first and the last of them
    say something is wrong with the archive:

    - a link names a file that is **not there**. That is a log file which has
      gone missing, and it is the one thing a heap of files cannot notice about
      itself
    - a link names a file that is there and was **written by a newer mailvault**.
      Nothing is missing; the archive is ahead of the program looking at it. The
      chain stops there all the same, because the link to the file before it is
      inside the file
    - a link names a file that is there and that **this version cannot read** for
      any other reason -- damaged, truncated, not UTF-8. Whether anything is
      missing cannot be said from outside it, so the move is to restore the file
      and not to upgrade the program. It is counted among the findings once, as a
      damaged log file
    - a file that **no chain reaches**. Written before the chain existed, or
      left behind when a head could not be updated. It is still read and nothing
      is lost by it, so it is reported and not counted

    Reading costs nothing extra: every log file was read a moment ago for its
    message lines, and `chain` is what that pass collected on the way.
    """
    if not heads.head_files(heads_root):
        # No heads at all: the archive predates them and the chain has not been
        # established yet. Reporting every log file as unreachable would be true
        # and useless -- thousands of lines saying the same thing about a state
        # the migration exists to leave. The question only applies once there is
        # something to ask it of.
        return

    reached: set[str] = set()
    for head in heads.read_all(heads_root):
        where = heads.place_name(head.job, head.folder)
        hashval = head.log
        while hashval is not None and hashval not in reached:
            logfile = chain.get(hashval)
            if logfile is None:
                why = unread.get(hashval)
                if why is metalog.Unreadable.NEWER:
                    # There, intact, and written by something this version does
                    # not know. The chain cannot go on -- the link to the file
                    # before it is inside the file -- but nothing is missing.
                    result.newer_chains.append(f"{hashval}  {where}")
                elif why is not None:
                    # There, and not readable here. Whether anything is missing
                    # cannot be said from outside the file, which is why the move
                    # is to restore it rather than to upgrade. It is already
                    # counted among the findings as a damaged log file.
                    result.unreadable_chains.append(f"{hashval}  {where}")
                else:
                    result.broken_chains.append(f"{hashval}  {where}")
                break
            reached.add(hashval)
            hashval = logfile.prev
    result.unchained = [
        logfile.path for hashval, logfile in sorted(chain.items()) if hashval not in reached
    ]


def _check_contents(
    store: cas.ContentAddressedStorage,
    entries: dict[str, pathlib.Path],
    result: CheckResult,
    step: str,
) -> None:
    """Hold every entry against the name it is filed under.

    The expensive half, and the only one that can find an entry whose bytes are
    not the ones it was named for. Everything else in the archive answers that
    question by looking at the name.
    """
    for read, path in enumerate(entries.values(), start=1):
        if read % CONTENTS_PROGRESS_EVERY == 0:
            log.info("%s: %s of %s checked", step, f"{read:,}", f"{len(entries):,}")
        try:
            if not store.verify(path):
                log.error(
                    "%s: damaged -- the content does not match its checksum",
                    store.where(path),
                )
                result.corrupt.append(path)
        except OSError as exc:
            log.error("%s: unreadable: %s", store.where(path), exc)
            result.unreadable.append(path)
    result.contents_checked = True


def _where(path: pathlib.Path) -> str:
    """A message as it reads inside the archive, for a caller with no store.

    `quarantine_entry` is handed the one path it acts on and nothing else, which
    is the right amount to know for renaming a file -- but a line about it still
    belongs to the archive that was named at the start of the run.
    """
    return utils.under_dir(cas.MAIL_DIR, path)


def quarantine_entry(
    path: pathlib.Path, attempts: int = _QUARANTINE_ATTEMPTS
) -> pathlib.Path | None:
    """Take an entry's name away from it, keeping every byte.

    Renamed, never deleted: a message with a flipped bit is still almost all of
    the message, and throwing away what was just found to be damaged is the
    worse of the two ways to be wrong. What has to stop is the *claim* -- while
    the file is called after a hash it does not have, the store keeps answering
    that the message is present and nothing ever fetches it again.

    It stays in its shard rather than moving to a directory of its own: the
    archive is three things and nobody wanted a fourth, and where a file sits is
    itself information.

    Numbered when a name is taken, which happens to a message that was fetched
    again after an earlier quarantine and broke a second time. Nothing is ever
    overwritten.
    """
    for serial in range(attempts):
        suffix = QUARANTINE_SUFFIX if serial == 0 else f"{QUARANTINE_SUFFIX}.{serial}"
        target = path.with_name(path.name + suffix)
        if target.exists():
            continue
        try:
            path.rename(target)
        except OSError as exc:
            log.error("%s: could not be quarantined: %s", _where(path), exc)
            return None
        log.warning("%s: quarantined as %s", _where(path), target.name)
        return target
    log.error("%s: could not be quarantined, every name is taken", _where(path))
    return None


def check(
    store_path: pathlib.Path,
    contents: bool = True,
    quarantine: bool = False,
) -> CheckResult:
    """Check an archive against what it claims, and report what it found.

    `contents` reads every message and holds it against its name, which is the
    only way to find one whose bytes have changed under it. On by default; see
    the module docstring for why it costs less than it looks.

    `quarantine` takes the name away from the messages that fail, so that the
    store stops reporting them as present. It cannot be combined with
    `contents=False`, and says so rather than quietly doing nothing: an option
    that looks effective while it cannot be is the kind that gets trusted.
    """
    if quarantine and not contents:
        raise JobError(
            "check: --quarantine cannot be combined with --no-integrity-check,"
            " because a damaged message is only found by reading it; there would"
            " be nothing to quarantine"
        )

    result = CheckResult()
    store = cas.mail_store(store_path)
    # Numbered because the second one is instant and the third takes half an
    # hour: someone watching a command they have not run before should be able
    # to tell how much of it is still ahead.
    steps = 3 if contents else 2

    log.info("step 1 of %s: looking through the archive", steps)
    entries = _classify(store, result, step=f"step 1 of {steps}")
    log.info("step 1 of %s: %s found", steps, utils.counted(result.entries, "message"))

    log.info("step 2 of %s: reading the metadata log", steps)
    chain: dict[str, metalog.LogFile] = {}
    unread: dict[str, metalog.Unreadable] = {}
    seen_at = _read_log(store_path / metalog.DEFAULT_LOG_DIR, result, chain, unread)
    _walk_chains(store_path / heads.DEFAULT_HEADS_DIR, chain, result, unread)
    log.info(
        "step 2 of %s: %s account for %s in %s",
        steps,
        utils.counted(result.log_files, "log file"),
        utils.counted(result.referenced, "message"),
        utils.counted(result.places, "place"),
    )

    for store_id, where in seen_at.items():
        if store_id not in entries:
            result.missing[store_id] = where
    result.orphans = [path for store_id, path in entries.items() if store_id not in seen_at]

    if contents:
        # The count belongs in the announcement, not after it: this is the step
        # that takes half an hour, and how long is the first thing anyone asks.
        log.info(
            "step 3 of %s: integrity check on %s -- each one is read in full",
            steps,
            utils.counted(result.entries, "message"),
        )
        _check_contents(store, entries, result, step=f"step 3 of {steps}")
    if quarantine:
        result.quarantined = [
            target for path in result.corrupt if (target := quarantine_entry(path))
        ]
    return result
