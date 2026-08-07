"""Move an archive written by an earlier version off its metadata database.

Older archives kept everything in `store.db`, including the only record of which
mailbox and folder each message was seen in. That pairing was never stored
directly -- the old schema held mailboxes and folders as two independent
relations -- so migrating it out into the log means reconstructing it, which is
what the helpers here do.

The second, younger step lives here too: `import_state_file` moves a `state.json`
into `heads/` and removes it. Both are one-shot code that reads a format nothing
writes any more, and both go out with 1.0.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import pathlib
import re
from datetime import UTC, datetime

from mailvault.store import cas, heads, marker, metadb, metalog, state

log = logging.getLogger(__name__)

# What a migrated database is renamed to. Not deleted: renaming says "the log is
# the source now" without destroying anything, and the name alone answers which
# artefact counts at any moment.
MIGRATED_SUFFIX = ".migrated"


@dataclasses.dataclass
class MigrationResult:
    """Outcome of moving an archive off its metadata database."""

    needed: bool = False
    generation: int = 0
    shards_moved: int = 0
    resume_points: int = 0
    consolidated: metalog.CompactResult | None = None
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
    heads_root: pathlib.Path,
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
    writer = metalog.LogWriter(log_root, heads_root)

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
    heads_root: pathlib.Path,
    db: metadb.MetaDatabaseConnection,
) -> int:
    """Copy the snapshot table of a legacy archive into `heads/`.

    Only ever fills an archive that has no heads: one that already has them
    holds the newer truth, and the database must not overwrite it.

    What is carried over is `last_run` and nothing else. Those timestamps were
    written as resume points by a version that took them from the wall clock, and
    adopting them as such would inherit exactly the gap they could hide. Kept as
    a record of when the folder was last read, they cost nothing; as a resume
    point they would cost mail. So every adopted folder is read in full once.
    """
    if heads.head_files(heads_root):
        return 0
    adopted = 0
    for mailbox, folder, timestamp in db.all_snapshots():
        try:
            last_run = datetime.fromisoformat(timestamp)
        except ValueError:
            log.warning(
                "%s::%s: unparsable snapshot %r in the database, skipped",
                mailbox,
                folder,
                timestamp,
            )
            continue
        heads.write(
            heads_root,
            heads.Head(job=mailbox, folder=folder, last_run=last_run.isoformat()),
        )
        adopted += 1
    return adopted


def import_state_file(store_path: pathlib.Path) -> int:
    """Move an archive's `state.json` into `heads/` and delete it.

    Runs once per archive and then has nothing left to do, because the file it
    reads is gone afterwards. Both formats are carried over, and differently: a
    version 2 file yields `last_run` and the opaque `resume`, a version 1 file
    yields `last_run` alone. A version 1 timestamp is not a resume point -- it
    came from the wall clock, and adopting it would inherit the gap it can hide
    -- so those folders are read in full once.

    Both are carried over rather than only the newer one, because moving an
    opaque token costs nothing and a full pass over every folder of a large
    archive costs hours.

    An archive that already has heads keeps them: they are the newer truth. The
    file is still removed, so the question does not come up a second time.
    """
    path = store_path / state.DEFAULT_STATE_NAME
    if not path.exists():
        return 0
    heads_root = store_path / heads.DEFAULT_HEADS_DIR

    imported = 0
    if heads.head_files(heads_root):
        log.info("%s: heads are already there, the state file is only removed", path)
    else:
        for mailbox, folder, entry in state.SnapshotState.load(path).entries():
            heads.write(
                heads_root,
                heads.Head(
                    job=mailbox,
                    folder=folder,
                    last_run=entry.last_run,
                    resume=entry.resume,
                ),
            )
            imported += 1

    path.unlink()
    log.info("%s: %s place(s) moved into %s", path, f"{imported:,}", heads.DEFAULT_HEADS_DIR)
    return imported


# A shard of the message store: two lowercase hex characters. Lowercase because
# that is what a hexdigest is; an uppercase pattern would match nothing on a
# case-sensitive filesystem and report success having moved nothing.
_SHARD = re.compile(r"[0-9a-f]{2}\Z")


def _merge_into(shard: pathlib.Path, destination: pathlib.Path) -> None:
    """Fold one shard into an existing one, entry by entry, and remove the husk.

    Recursive, because a shard is not a flat directory -- the message store is
    two levels deep, so a shard holds sub-shards and only those hold entries.

    An entry already present on the other side is dropped rather than moved. The
    name is the hash of the content, so "present under this name" and "the same
    bytes" are the same statement; there is nothing to choose between them.
    """
    for entry in sorted(shard.rglob("*")):
        if entry.is_dir():
            continue
        here = destination / entry.relative_to(shard)
        if here.exists():
            entry.unlink()
            continue
        here.parent.mkdir(parents=True, exist_ok=True)
        entry.rename(here)
    for path, _dirs, _files in os.walk(shard, topdown=False):
        pathlib.Path(path).rmdir()


def move_shards_into_mail(store_path: pathlib.Path) -> int:
    """Move the message store out of the archive root and into `mail/`.

    Cheap, and not obviously so: a shard is one directory rename, and a rename
    within a filesystem moves no data. The cost is O(shards) -- at most 256 --
    and not O(messages).

    That holds as long as the target shard does not exist yet. It does when a
    version that already writes to `mail/` has run against an unmigrated archive:
    then the store is split, and the two halves have to be merged file by file.
    Nothing is lost either way -- the names are content hashes, so a file that is
    somehow in both places is the same file.
    """
    target = store_path / cas.MAIL_DIR
    moved = 0
    for shard in sorted(store_path.iterdir()):
        if not shard.is_dir() or not _SHARD.match(shard.name):
            continue
        target.mkdir(parents=True, exist_ok=True)
        destination = target / shard.name
        if not destination.exists():
            shard.rename(destination)
            moved += 1
            continue
        log.debug("%s: already in %s, merging", shard.name, cas.MAIL_DIR)
        _merge_into(shard, destination)
        moved += 1
    if moved:
        log.info("%s: %s shard(s) moved into %s", store_path, f"{moved:,}", cas.MAIL_DIR)
    return moved


def _migrate_database(store_path: pathlib.Path) -> MigrationResult:
    """Move an archive written by an earlier version onto the log.

    Older archives keep everything in `store.db`: the resume timestamps and, more
    importantly, the only record of which mailbox and folder each message was
    seen in. Both move out -- the timestamps into `heads/`, the locations into
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
    heads_root = store_path / heads.DEFAULT_HEADS_DIR
    date = datetime.now(UTC)
    # Read-only: the legacy database is only queried here and then renamed aside,
    # so setup() must not write DDL into it (nor demand write access to read it).
    with metadb.MetaDatabase(path=legacy, setup=False) as db:
        result.snapshots = _adopt_database_snapshots(heads_root, db)
        written = _export_metalog(db, log_root, heads_root, date, result)

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


def migrate_archive(store_path: pathlib.Path) -> MigrationResult:
    """Bring an archive up to the current layout generation.

    The one place every older shape is lifted from, in the order the pieces
    depend on each other:

    1. `state.json` gives up its resume points, into `heads/`
    2. `store.db` gives up the locations it alone holds, into the log
    3. the message store moves out of the root and into `mail/`
    4. the log is consolidated, which gives every place's chain a root -- an
       archive from before the chain has no `prev` anywhere, and consolidating
       is what produces the first file that has one
    5. and only then the mark is written

    **The mark is last on purpose.** An interrupt anywhere above leaves the
    older number standing, so the next run picks the work up again; a mark
    written first would claim a layout that only half exists. Everything above
    is idempotent, so picking it up again costs nothing.

    Runs at the start of every backup, and returns immediately once the mark
    says the archive is current -- so the cost after the one migration is
    reading one small file.
    """
    generation = marker.check_readable(store_path)
    result = MigrationResult(generation=generation)
    if generation == marker.CURRENT_FORMAT:
        return result

    # Before the database, not after: both of them decline to overwrite heads
    # that are already there, so whichever runs first wins -- and `state.json`
    # is the *newer* artefact. The other way round, an archive carrying both
    # would keep the database's bare timestamps and throw away resume points
    # that would have saved it a full pass over every folder.
    resume_points = import_state_file(store_path)

    result = _migrate_database(store_path)
    result.generation = generation
    result.resume_points = resume_points
    if result.needed and not result.verified:
        # The database would not give up its locations. Going on would move the
        # store out from under it and mark the archive as done, with the one
        # thing that was not migrated still sitting there.
        return result

    result.shards_moved = move_shards_into_mail(store_path)
    result.consolidated = metalog.compact(
        store_path / metalog.DEFAULT_LOG_DIR, store_path / heads.DEFAULT_HEADS_DIR
    )
    if not result.consolidated.verified:
        log.error("%s: the log did not consolidate, the archive is not marked", store_path)
        return result

    marker.write(store_path)
    log.info("%s: %s", store_path, marker.describe(marker.CURRENT_FORMAT))
    return result
