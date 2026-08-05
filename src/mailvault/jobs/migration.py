"""Move an archive written by an earlier version off its metadata database.

Older archives kept everything in `store.db`, including the only record of which
mailbox and folder each message was seen in. That pairing was never stored
directly -- the old schema held mailboxes and folders as two independent
relations -- so migrating it out into the log means reconstructing it, which is
what the helpers here do.
"""

from __future__ import annotations

import dataclasses
import logging
import pathlib
from datetime import UTC, datetime

from mailvault.store import metadb, metalog, state

log = logging.getLogger(__name__)

# What a migrated database is renamed to. Not deleted: renaming says "the log is
# the source now" without destroying anything, and the name alone answers which
# artefact counts at any moment.
MIGRATED_SUFFIX = ".migrated"


@dataclasses.dataclass
class MigrationResult:
    """Outcome of moving an archive off its metadata database."""

    needed: bool = False
    messages: int = 0
    places: int = 0
    placeless: int = 0
    undecidable: int = 0
    snapshots: int = 0
    verified: bool = False
    renamed_to: pathlib.Path | None = None


def _learn_by_elimination(
    owners: dict[str, set[str]],
    mailboxes: dict[int, list[str]],
    folders: dict[int, list[str]],
) -> int:
    """One pass of: one mailbox unexplained, one folder unplaced -- they pair up.

    Every run that saw a message recorded the folder it saw it in, so a mailbox
    listed for a message has to be explained by one of that message's folders.
    Where all but one folder is placed and all but one mailbox is explained, the
    two that remain belong together.

    Returns how many new pairings were learnt, because each one makes the next
    pass see further: a folder that no single-mailbox message ever witnessed can
    become decidable once its companions are placed.
    """
    learnt = 0
    for message_id, names in mailboxes.items():
        present = set(names)
        if len(present) < 2:
            continue
        explained: set[str] = set()
        orphans: list[str] = []
        for folder in folders.get(message_id, ()):
            candidates = owners.get(folder, set()) & present
            if len(candidates) == 1:
                explained |= candidates
            elif not candidates:
                orphans.append(folder)
        missing = present - explained
        if len(missing) == 1 and len(orphans) == 1:
            owner = missing.pop()
            if owner not in owners.setdefault(orphans[0], set()):
                owners[orphans[0]].add(owner)
                learnt += 1
    return learnt


def _folder_owners(db: metadb.MetaDatabaseConnection) -> dict[str, set[str]]:
    """Work out which mailbox each folder name can have come from.

    Three sources, in order of how much they assume. The snapshot table pairs
    mailbox and folder directly -- it is the one place in the old schema where
    the two were stored together. Every message that belongs to exactly one
    mailbox is a witness: whatever folders it carries can only have come from
    there, which is what catches Gmail's folder names, since those are never
    visited as folders and so never reach the snapshot table. And finally
    elimination, repeated until it stops finding anything, for folders that no
    single-mailbox message ever witnessed.
    """
    owners: dict[str, set[str]] = {}
    for mailbox, folder, _date in db.all_snapshots():
        owners.setdefault(folder, set()).add(mailbox)

    mailboxes = db.message_mailboxes()
    folders = db.message_labels()
    for message_id, names in mailboxes.items():
        if len(names) == 1:
            for folder in folders.get(message_id, ()):
                owners.setdefault(folder, set()).add(names[0])

    while True:
        learnt = _learn_by_elimination(owners, mailboxes, folders)
        if not learnt:
            return owners
        log.debug("Folder owners: %s pairing(s) learnt by elimination", learnt)


def _export_metalog(
    db: metadb.MetaDatabaseConnection,
    log_root: pathlib.Path,
    date: datetime,
    result: MigrationResult,
) -> list[pathlib.Path]:
    """Write the locations held in an existing database into the log.

    The old schema stored which mailboxes and which folders a message has as two
    independent relations, so the pairing between them was never recorded. It is
    reconstructed here: a folder that can only have come from one of the
    message's mailboxes belongs to that one.

    Where that does not decide it -- a folder name two of the message's mailboxes
    both have -- nothing is invented. The folder is counted as undecidable and
    left out, and a mailbox left without any folder is written with a null
    folder, which says "seen in this mailbox, where exactly is not knowable"
    instead of guessing a place.
    """
    owners = _folder_owners(db)
    mailboxes = db.message_mailboxes()
    folders = db.message_labels()
    writer = metalog.LogWriter(log_root)

    for message_id, store_id in db.iter_messages():
        result.messages += 1
        names = set(mailboxes.get(message_id, ()))
        if not names:
            result.placeless += 1
            continue
        placed: dict[str, list[str]] = {}
        for folder in folders.get(message_id, ()):
            candidates = owners.get(folder, set()) & names
            if len(candidates) == 1:
                placed.setdefault(candidates.pop(), []).append(folder)
            else:
                result.undecidable += 1
        for mailbox in sorted(names):
            here = placed.get(mailbox, [])
            if not here:
                result.placeless += 1
            writer.add(mailbox, here, store_id)

    result.places = writer.places
    return writer.seal(date)


def _adopt_database_snapshots(
    snapshot_state: state.SnapshotState,
    db: metadb.MetaDatabaseConnection,
) -> int:
    """Copy the snapshot table of a legacy archive into the state file.

    Only ever fills an empty state file: one that already holds something is the
    newer truth and must not be overwritten by the database.

    What is carried over is `last_run` and nothing else. Those timestamps were
    written as resume points by a version that took them from the wall clock, and
    adopting them as such would inherit exactly the gap they could hide. Kept as
    a record of when the folder was last read, they cost nothing; as a resume
    point they would cost mail. So every adopted folder is read in full once.
    """
    if not snapshot_state.is_empty():
        return 0
    adopted = 0
    for mailbox, folder, timestamp in db.all_snapshots():
        try:
            snapshot_state.record(
                mailbox,
                folder,
                last_run=datetime.fromisoformat(timestamp),
                resume=None,
            )
        except ValueError:
            log.warning(
                "%s::%s: unparsable snapshot %r in the database, skipped",
                mailbox,
                folder,
                timestamp,
            )
            continue
        adopted += 1
    if adopted:
        snapshot_state.save()
    return adopted


def migrate_archive(store_path: pathlib.Path) -> MigrationResult:
    """Move an archive written by an earlier version onto the log.

    Older archives keep everything in `store.db`: the resume timestamps and, more
    importantly, the only record of which mailbox and folder each message was
    seen in. Both move out -- the timestamps into `state.json`, the locations into
    the log -- and the database is then no longer part of the archive.

    It is not deleted. It is renamed to `store.db.migrated`, which says the same
    thing without destroying anything: the name alone answers "which of these is
    the source" at any moment, so there is never a period where two artefacts hold
    the same information and nothing says which one counts.

        store.db            not migrated -- the old locations live only here
        store.db.migrated   the log is the source, nothing reads this file
        neither             the log is the source

    Idempotent by construction. An interrupted export leaves `store.db` in place,
    so the next attempt exports again; the duplicate entries make no difference
    because replaying them is idempotent. Called on an archive with no `store.db`
    it does nothing at all.
    """
    legacy = store_path / metadb.DEFAULT_DB_NAME
    result = MigrationResult()
    if not legacy.exists():
        return result
    result.needed = True

    # Said before the work, not after: reading the whole legacy database and
    # writing the log can take a minute on a large archive, and without this the
    # run looks stuck between the job starting and the "migrated" line below.
    log.info(
        "%s: migrating an archive from an earlier version onto the log -- "
        "this happens once and may take a moment",
        store_path,
    )

    log_root = store_path / metalog.DEFAULT_LOG_DIR
    snapshot_state = state.SnapshotState.load(store_path / state.DEFAULT_STATE_NAME)
    date = datetime.now(UTC)
    # Read-only: the legacy database is only queried here and then renamed aside,
    # so setup() must not write DDL into it (nor demand write access to read it).
    with metadb.MetaDatabase(path=legacy, setup=False) as db:
        result.snapshots = _adopt_database_snapshots(snapshot_state, db)
        written = _export_metalog(db, log_root, date, result)

    # Read back what was just written before anything is renamed. The files are
    # named after their own content, so this catches a write that did not land.
    result.verified = all(metalog.verify_file(path) for path in written)
    if not result.verified:
        log.error("%s: written log files did not verify, database left alone", log_root)
        return result

    target = legacy.with_name(legacy.name + MIGRATED_SUFFIX)
    legacy.replace(target)
    result.renamed_to = target
    log.info(
        "%s: migrated -- %s message(s) into %s place(s), %s snapshot(s); %s is no longer used",
        store_path,
        result.messages,
        result.places,
        result.snapshots,
        target.name,
    )
    return result
