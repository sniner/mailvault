"""Build and maintain `index.db`, the archive's queryable projection.

The archive itself holds no database. This makes one on demand out of the two
things that do live there -- the messages, for everything they carry in
themselves, and the log, for which mailbox and folder each was seen in. What
comes out is a snapshot, accurate when built and stale from the next backup on.

Named for the command it serves, like every other module here. It used to be
`storedb`, from the days when the projection *was* `store.db` and the archive
kept its truth in SQLite -- which stopped being true in 0.8.0, leaving a module
whose name pointed at a file it has nothing to do with, right next to
`mailvault.legacy.store_db`, which does.

It can also be kept up to date incrementally (`refresh_db`): a convenience
projection beside the archive, refreshed after a backup, never a source of truth.
Which log files it has already applied is recorded in the database itself, so a
routine refresh reads only the files added since -- and if the database is
missing or unreadable, it is simply rebuilt.
"""

from __future__ import annotations

import dataclasses
import logging
import pathlib
import sqlite3

from mailvault import mailutils, utils
from mailvault.jobs.common import JobError
from mailvault.store import cas, heads, index_db, metalog

log = logging.getLogger(__name__)

# Messages per transaction when building a database. Large enough that commits
# stop dominating, small enough that an interrupted run has not done much work
# it will have to repeat.
CREATE_DB_BATCH = 2000

# Default filename of the kept-fresh projection, beside the archive. Deliberately
# not `store.db`: that name is reserved for the legacy database the migration
# looks for, and a projection there would be exported into the log and renamed
# away on the next backup.
DEFAULT_QUERY_DB_NAME = "index.db"

# Bookkeeping table: which log files have already been folded into this database.
# Not part of the queryable schema -- it exists only so an incremental refresh
# knows what it has seen. A file is recorded by its content hash (its name).
_APPLIED_LOG_DDL = "CREATE TABLE IF NOT EXISTS applied_log (hash TEXT PRIMARY KEY)"

# Which chain head of each place the projection has folded in. Bookkeeping like
# `applied_log` and no part of the queryable schema, but it answers a question
# `applied_log` cannot: not "what have I read" but "was that everything". The
# archive's own `heads/` is one flat directory naming the current head of every
# place, so comparing the two says whether the database is behind -- before
# somebody bases an answer on it, rather than after.
#
# The names are held in plain text and not as ids. This is a statement about the
# archive, not about the mail: a place the projection has never seen a message
# from still has a head, and interning it would put a row in `mailbox` for a
# mailbox no query can find anything in.
_HEAD_DDL = """
    CREATE TABLE IF NOT EXISTS folded_head (
    mailbox TEXT,
    folder TEXT,
    log TEXT)
"""
# Same reason as in `message_location`: a place may name no mailbox, and SQLite
# holds every NULL distinct, so the uniqueness has to be spelt over IFNULL.
_HEAD_INDEX_DDL = """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_folded_head_1
    ON folded_head(IFNULL(mailbox, ''), IFNULL(folder, ''))
"""


# How a reader is told to build the projection again. Named once because it is a
# command line, and command lines move: this one is `archive create-db` today and
# becomes `db create` when the projection gets its own namespace. Advice that
# names a command which no longer exists is worse than no advice.
REBUILD_COMMAND = "mailvault archive create-db --force"


@dataclasses.dataclass
class Freshness:
    """What is wrong with the projection somebody is about to read, if anything.

    Two complaints, and they are different in kind. `outdated_shape` means the
    file was written by another version and cannot be read at all; `behind` names
    the places whose log has moved on since it was last brought up to date, so
    what it holds is true and incomplete.
    """

    outdated_shape: bool = False
    behind: list[str] = dataclasses.field(default_factory=list)

    def is_current(self) -> bool:
        return not self.outdated_shape and not self.behind

    def complaint(self, db_name: str) -> str | None:
        """One line naming the state and the move, or None when there is none."""
        if self.outdated_shape:
            return (
                f"{db_name}: built by an earlier version of mailvault and not"
                f" readable by this one -- build it again with `{REBUILD_COMMAND}`"
            )
        if not self.behind:
            return None
        places = ", ".join(self.behind[:3])
        if len(self.behind) > 3:
            places += f" and {len(self.behind) - 3:,} more"
        return (
            f"{db_name}: behind the archive in {len(self.behind):,} place(s)"
            f" ({places}) -- mail archived since is not in it, bring it up to date"
            f" with `{REBUILD_COMMAND}`"
        )


@dataclasses.dataclass
class ReplayResult:
    """Outcome of applying the metadata log to a database."""

    files: int = 0
    entries: int = 0
    applied: int = 0
    unknown: int = 0


@dataclasses.dataclass
class RebuildResult:
    """Outcome of rebuilding the database from the archive and its log."""

    messages: int = 0
    replay: ReplayResult = dataclasses.field(default_factory=ReplayResult)


@dataclasses.dataclass
class RefreshResult:
    """Outcome of bringing a kept-fresh projection up to date with the archive."""

    rebuilt: bool = False
    outdated: bool = False
    files: int = 0
    messages: int = 0
    applied: int = 0
    unknown: int = 0


def _log_hash(path: pathlib.Path) -> str:
    return path.name.removesuffix(".jsonl")


def _ensure_bookkeeping(db: index_db.IndexDatabaseConnection) -> None:
    """The two tables that record what the projection has taken in."""
    with db.transaction():
        db.execute(_APPLIED_LOG_DDL)
        db.execute(_HEAD_DDL)
        db.execute(_HEAD_INDEX_DDL)


def _place_name(mailbox: str | None, folder: str | None) -> str:
    """How a place is written for somebody to read.

    A place with no mailbox is what an import writes, and `None::docuware-2019`
    is not a line for people.
    """
    if mailbox is None:
        return folder or "?"
    return f"{mailbox}::{folder}" if folder is not None else mailbox


def _archive_heads(heads_root: pathlib.Path) -> dict[tuple[str | None, str | None], str | None]:
    """The chain head of every place, as the archive currently has it."""
    return {(head.job, head.folder): head.log for head in heads.read_all(heads_root)}


def _record_heads(db: index_db.IndexDatabaseConnection, heads_root: pathlib.Path) -> None:
    """Write down which head of each place this projection has now folded in.

    Called after the log has been applied, never before: what is recorded here
    is a claim about what the database contains, and a claim made in advance of
    the work is the one thing worse than no claim at all.
    """
    with db.transaction():
        for (mailbox, folder), head_log in _archive_heads(heads_root).items():
            db.execute(
                "INSERT INTO folded_head(mailbox, folder, log) VALUES (?, ?, ?) "
                "ON CONFLICT DO UPDATE SET log=excluded.log",
                (mailbox, folder, head_log),
            )


def _folded_heads(
    db: index_db.IndexDatabaseConnection,
) -> dict[tuple[str | None, str | None], str | None]:
    """The heads the projection recorded, or nothing when it has none."""
    try:
        rows = db.execute("SELECT mailbox, folder, log FROM folded_head").fetchall()
    except sqlite3.OperationalError:
        # A projection built before this table existed. It is not wrong, only
        # unable to say how far it got -- and the caller is told the same thing
        # it would be told about a place that has moved on.
        return {}
    return {(row[0], row[1]): row[2] for row in rows}


def freshness(store_path: pathlib.Path, db_path: pathlib.Path) -> Freshness:
    """Ask the projection whether what it holds is still what the archive holds.

    For whoever is about to read it. The archive's `heads/` names the current
    chain head of every place; the projection records the head it folded in. A
    difference means mail has been archived since, and a place the projection
    has never heard of means the same. Neither is an error -- the projection is
    rebuildable by definition -- but a reader who is not told will take an
    answer from it and believe the answer is complete.
    """
    result = Freshness()
    if not db_path.exists():
        return result
    heads_root = store_path / heads.DEFAULT_HEADS_DIR
    try:
        with index_db.IndexDatabase(path=db_path) as db:
            if db.outdated:
                result.outdated_shape = True
                return result
            folded = _folded_heads(db)
    except sqlite3.DatabaseError:
        # Unreadable is its own answer, and `refresh_db` rebuilds it anyway.
        result.outdated_shape = True
        return result

    for place, head_log in _archive_heads(heads_root).items():
        if folded.get(place, _MISSING) != head_log:
            result.behind.append(_place_name(*place))
    result.behind.sort()
    return result


# A sentinel, because None is a legitimate recorded value: a place whose chain
# has no head yet is recorded with one, and it must not compare equal to a place
# the projection has never seen.
_MISSING = object()


def _mark_logs_applied(db: index_db.IndexDatabaseConnection, paths: list[pathlib.Path]) -> None:
    with db.transaction():
        for path in paths:
            db.execute(
                "INSERT OR IGNORE INTO applied_log (hash) VALUES (?)",
                (_log_hash(path),),
            )


def _insert_message_from_path(
    db: index_db.IndexDatabaseConnection,
    store: cas.ContentAddressedStorage,
    path: pathlib.Path,
) -> int:
    """Read one archived message's headers and insert its row, returning its id.

    Only the headers are read: everything the database keeps about a message --
    sender, recipients, subject, date -- is in them, and the attachments behind
    them are the bulk of the bytes.
    """
    header = mailutils.decode_email_header(store.read_header(path))
    from_addrs, to_addrs = mailutils.addresses(header)
    store_id = store.hashval_of(path)
    if store_id is None:
        raise ValueError(f"not an entry of the store: {path}")
    email_id = mailutils.message_id(header)
    date = mailutils.date(header)
    subject = mailutils.subject(header)

    msg_id = db.add_message(store_id, email_id, date, subject)
    db.add_message_sender(msg_id, *from_addrs)
    db.add_message_recipients(msg_id, *to_addrs)
    return msg_id


def _replay_metalog(
    db: index_db.IndexDatabaseConnection,
    log_root: pathlib.Path,
) -> ReplayResult:
    """Apply every log file to the database, in order.

    Idempotent, so replaying is nothing more than calling it again: a message
    seen in two places ends up with two rows exactly as the runs that observed it
    would have written. Entries whose message is not in the archive are counted
    and skipped -- the blob was removed, and inventing a row for it would
    describe mail that is not there.

    One log file is one place, and it goes in as one row. A file that names no
    mailbox is applied all the same: it used to be skipped with a warning, which
    threw away the only statement it carried. Whether both halves of a place are
    known is not this reader's business -- the database can hold either as NULL,
    and a folder without a mailbox is what an import writes.
    """
    result = ReplayResult()
    known = db.store_id_map()
    for logfile in metalog.read_all(log_root):
        result.files += 1
        if logfile.mailbox is None and logfile.folder is None:
            log.warning("%s: names no place at all, skipped", metalog.where(logfile.path))
            continue
        # One transaction per file rather than per entry: the write methods
        # commit individually when called at the top level, which would mean a
        # commit per message and is ruinous over a network share.
        with db.transaction():
            for store_id in logfile.store_ids:
                result.entries += 1
                message_id = known.get(store_id)
                if message_id is None:
                    result.unknown += 1
                    continue
                db.add_message_location(message_id, logfile.mailbox, logfile.folder)
                result.applied += 1
    return result


def create_db(
    store_path: pathlib.Path,
    db_path: pathlib.Path,
    mailbox: str | None = None,
    force: bool = False,
) -> RebuildResult:
    """Build a queryable database from the archive and its log.

    Not a rebuild of something the archive owns: the archive holds no database.
    This makes one, wherever the caller asks for it, out of the two things that do
    live there. The messages supply everything they carry in themselves -- sender,
    recipients, subject, date -- and the log supplies the one thing they do not,
    which mailbox and folder each was seen in.

    What comes out is a snapshot. It is accurate for the moment it was built and
    goes stale from the next backup onwards; build it again when that matters, or
    keep it fresh with `refresh_db`.

    An existing file is refused unless `force` is given, and `force` replaces it
    rather than adding to it. Writing into a database that is already there would
    make the result an accumulation instead of a snapshot -- rows from an earlier
    run stay even when the archive no longer yields them, and a correction to how
    a header is read would never reach them.

    Built through a temporary file beside the target and renamed at the end, the
    same discipline the archive uses. An interrupted run leaves no half-built
    database where a whole one is expected, and the previous one survives it.
    """
    if db_path.exists() and not force:
        raise JobError(f"{db_path}: already exists, use --force to replace it")
    store = cas.mail_store(store_path)
    result = RebuildResult()
    tmp_path = db_path.with_name(db_path.name + "._tmp_")
    tmp_path.unlink(missing_ok=True)
    try:
        _build_db(store, store_path, tmp_path, mailbox, result)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    tmp_path.replace(db_path)
    return result


def _build_db(
    store: cas.ContentAddressedStorage,
    store_path: pathlib.Path,
    db_path: pathlib.Path,
    mailbox: str | None,
    result: RebuildResult,
) -> None:
    """Fill a fresh database from the archive and its log."""
    log_root = store_path / metalog.DEFAULT_LOG_DIR
    with index_db.IndexDatabase(path=db_path) as db:
        _ensure_bookkeeping(db)
        # One transaction per batch, not per message. Every write method commits
        # on its own when called at the top level, which over a whole archive is
        # half a million commits -- unnoticeable on a RAM disk and hours on a
        # real filesystem, where each one waits for the device.
        for batch in utils.batched(store.walk(), CREATE_DB_BATCH):
            with db.transaction():
                for path in batch:
                    msg_id = _insert_message_from_path(db, store, path)
                    if mailbox:
                        # `--mailbox` files messages the archive records no place
                        # for. It names a mailbox and no folder, which is exactly
                        # what it is: an assertion about whose mail this is, and
                        # none about where in it.
                        db.add_message_location(msg_id, mailbox, None)
                    result.messages += 1
            # Named for what it is doing, not merely counted. This reads every
            # message in the archive and takes half an hour on a large one, and
            # a bare "N message(s) read" in the middle of a backup leaves a
            # reader watching a number climb with no idea what it is for.
            log.info("building the query database: %s message(s) read", f"{result.messages:,}")

        result.replay = _replay_metalog(db, log_root)
        # Prime the bookkeeping so a later refresh reads only files added since,
        # and so a later *reader* can tell whether the archive has moved on.
        _mark_logs_applied(db, metalog.log_files(log_root))
        _record_heads(db, store_path / heads.DEFAULT_HEADS_DIR)


def refresh_db(store_path: pathlib.Path, db_path: pathlib.Path) -> RefreshResult:
    """Bring a kept-fresh projection up to date with the archive, incrementally.

    A convenience projection, not a source of truth. If it is missing or not a
    usable database it is built from scratch; otherwise only the log files it has
    not applied yet are read in, so a routine refresh after a backup costs a
    handful of small reads plus a header read for each newly archived message.

    Only backups feed this: a message reaches the projection because a log file
    records it. Mail added by `archive import`, which writes no log, is not
    picked up here -- rebuild with `archive create-db` when that matters.
    """
    result = RefreshResult()
    if not db_path.exists():
        # Said before the work starts, not after: building one means reading
        # every message in the archive, which is minutes to half an hour of a
        # backup that had nothing else left to do. Why it is happening at all is
        # the part a reader cannot guess.
        log.info(
            "%s: no query database yet, building one from the whole archive",
            utils.under(store_path, db_path),
        )
        result.rebuilt = True
        result.messages = create_db(store_path, db_path, force=True).messages
        return result

    store = cas.mail_store(store_path)
    log_root = store_path / metalog.DEFAULT_LOG_DIR
    heads_root = store_path / heads.DEFAULT_HEADS_DIR
    try:
        with index_db.IndexDatabase(path=db_path) as db:
            if db.outdated:
                # Left exactly as it is, and said out loud. Rebuilding it here
                # would be a backup deciding on its own to spend half an hour
                # reading every message in the archive, for a file that is a
                # convenience -- and doing it without being asked, at the end of
                # a run that had nothing else to do. The projection is not a
                # source of truth; whoever wants it back says so.
                result.outdated = True
                log.warning(
                    "%s: built by an earlier version of mailvault (shape %d, this"
                    " one writes %d) -- left untouched and NOT updated, build it"
                    " again with `%s`",
                    utils.under(store_path, db_path),
                    db.shape_on_open,
                    index_db.SCHEMA_VERSION,
                    REBUILD_COMMAND,
                )
                return result
            _apply_new_logs(db, store, log_root, result)
            _record_heads(db, heads_root)
    except sqlite3.DatabaseError as exc:
        log.warning(
            "%s: not a usable database (%s), building one from the whole archive",
            utils.under(store_path, db_path),
            exc,
        )
        db_path.unlink(missing_ok=True)
        result.rebuilt = True
        result.messages = create_db(store_path, db_path, force=True).messages
    return result


def _apply_new_logs(
    db: index_db.IndexDatabaseConnection,
    store: cas.ContentAddressedStorage,
    log_root: pathlib.Path,
    result: RefreshResult,
) -> None:
    """Fold every log file not yet applied into the database.

    Idempotent and self-healing: the set of applied files is read from the
    database, so a refresh interrupted halfway simply resumes, and a projection
    that fell behind (the option was off for a while) catches up on all of them.
    A message row is created the first time a store id is seen; after that only
    its location is added.
    """
    _ensure_bookkeeping(db)
    applied = {row[0] for row in db.execute("SELECT hash FROM applied_log")}
    known = db.store_id_map()
    for path in metalog.log_files(log_root):
        if _log_hash(path) in applied:
            continue
        logfile = metalog.read_log(path)
        if logfile is None or (logfile.mailbox is None and logfile.folder is None):
            # Unreadable or naming no place at all: leave it unmarked so a later,
            # repaired file is retried rather than skipped for good.
            continue
        result.files += 1
        with db.transaction():
            for store_id in logfile.store_ids:
                msg_id = known.get(store_id)
                if msg_id is None:
                    msg_id = _insert_message(db, store, store_id)
                    if msg_id is None:
                        result.unknown += 1
                        continue
                    known[store_id] = msg_id
                    result.messages += 1
                db.add_message_location(msg_id, logfile.mailbox, logfile.folder)
                result.applied += 1
            db.execute(
                "INSERT OR IGNORE INTO applied_log (hash) VALUES (?)",
                (_log_hash(path),),
            )


def _insert_message(
    db: index_db.IndexDatabaseConnection,
    store: cas.ContentAddressedStorage,
    store_id: str,
) -> int | None:
    """Insert the row for one archived message, or None when its blob is gone."""
    path = store.locate(store_id, exists=True)
    if path is None:
        return None
    return _insert_message_from_path(db, store, path)
