"""Take messages the log accounts for nowhere into a place of their own.

An archive is the mail store and the metadata log together: a message belongs to
it once a log file names it. A message that lies in `mail/` and is named nowhere
is therefore not a damaged part of the archive but a file that is not part of it
yet -- closer to a file git does not track than to anything in `lost+found`.
`archive check` finds them, and this takes them in.

**The name is the user's statement, not the archive's.** `--name docuware-2019`
says "these came from that import"; `--name orphaned` says "I do not know where
these came from, and I am saying so". Both are true statements when the person
typing them means them, and neither is one the program could have made on its
own -- which is why there is a command and not an automatic repair. The archive
records it exactly the way it records an import, because it is the same
statement: mailbox empty, the name in the folder.

What it cannot do is undo. The log is append-only and nothing corrects it, so a
name given to the wrong messages stays. That is what `--dry-run` is for here, and
why the report says how many messages a run is about to speak for.

Where the source directory still exists, importing it again is the better move
and this command is the wrong one: an import records only what really lay in that
directory, and it therefore cannot be wrong. This is for what is left -- an
import whose source `--move` deleted, and the mail whose log entry was lost
before it was ever written.
"""

from __future__ import annotations

import dataclasses
import logging
import pathlib
from datetime import UTC, datetime

from mailvault.jobs.common import SEAL_BATCH, check_place_name, seal_log
from mailvault.store import cas, heads, metalog

log = logging.getLogger(__name__)

# How often the walk over the store says where it is. The same number `check`
# uses for the same pass: quick per item, huge in number.
WALK_PROGRESS_EVERY = 20_000


@dataclasses.dataclass
class AdoptResult:
    """What was taken in, or what would have been.

    `found` and `recorded` are two numbers on purpose. A seal that fails leaves
    the mail exactly where it was and records nothing, and a report that showed
    one number could not say so.
    """

    name: str = ""
    found: int = 0
    recorded: int = 0
    held: int = 0
    dry_run: bool = False


def _read_log(log_root: pathlib.Path, name: str) -> tuple[set[str], int]:
    """Every store id the log names, and how many the target place already holds.

    The place does not matter for the first: a message recorded in a mailbox and
    a message recorded under an import name are both accounted for, and neither
    is this command's business.

    The second is, and it costs nothing on the same pass. A name that is already
    a place is not an error -- adopting the leftovers of an import under that
    import's name is the case this command exists for -- but it is the one thing
    that tells a mistyped name from a fresh one, and the report cannot say so
    without counting it here.
    """
    referenced: set[str] = set()
    held: set[str] = set()
    for logfile in metalog.read_all(log_root):
        referenced.update(logfile.store_ids)
        if logfile.place == (None, name):
            held.update(logfile.store_ids)
    return referenced, len(held)


def adopt(store_path: pathlib.Path, name: str, dry_run: bool = False) -> AdoptResult:
    """Record every message the log names nowhere as having been in `name`.

    Two passes, and neither holds the archive in memory: the log is read into the
    set of store ids it accounts for, then the store is walked and everything not
    in that set is written down as it is found. The observations go out in
    batches, so an interrupted run has recorded what it got to and a rerun
    finishes the job -- it simply finds fewer.

    Messages that already have a place are not touched. A second place beside a
    real one would be a statement of its own, and a false one: this run knows
    nothing about where those messages were.
    """
    check_place_name(name)
    log_root = store_path / metalog.DEFAULT_LOG_DIR
    store = cas.mail_store(store_path)
    result = AdoptResult(name=name, dry_run=dry_run)

    log.info("step 1 of 2: reading the metadata log")
    referenced, result.held = _read_log(log_root, name)
    log.info("step 1 of 2: %s message(s) already have a place", f"{len(referenced):,}")

    log.info("step 2 of 2: looking through the archive")
    writer = metalog.LogWriter(log_root, store_path / heads.DEFAULT_HEADS_DIR)
    seen = 0
    since_seal = 0
    for path in store.walk():
        seen += 1
        if seen % WALK_PROGRESS_EVERY == 0:
            log.info("step 2 of 2: %s message(s) seen", f"{seen:,}")
        store_id = store.hashval_of(path)
        if store_id is None or store_id in referenced:
            continue
        result.found += 1
        if dry_run:
            continue
        writer.add(None, [name], store_id)
        since_seal += 1
        if since_seal >= SEAL_BATCH:
            # Counted rather than asking the writer how much it holds: a seal
            # that fails leaves everything in it, and "is it full" would then be
            # true for every message after it -- one failed write per message
            # instead of one per batch.
            result.recorded += _write_down(writer)
            since_seal = 0
    if not dry_run:
        result.recorded += _write_down(writer)
    return result


def _write_down(writer: metalog.LogWriter) -> int:
    """Seal what has been collected, returning how much became durable.

    Zero when the log could not be written. What was collected stays in the
    writer and goes out with the next batch, so a failure costs the run and never
    the record; `seal_log` has already said what went wrong.
    """
    observed = len(writer)
    return observed if seal_log(writer, datetime.now(UTC)) else 0
