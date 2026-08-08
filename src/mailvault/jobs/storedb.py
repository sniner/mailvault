"""Build and maintain a queryable SQLite database from the archive and its log.

The archive itself holds no database. This makes one on demand out of the two
things that do live there -- the messages, for everything they carry in
themselves, and the log, for which mailbox and folder each was seen in. What
comes out is a snapshot, accurate when built and stale from the next backup on.

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
from mailvault.store import cas, metadb, metalog

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
    files: int = 0
    messages: int = 0
    applied: int = 0
    unknown: int = 0


def _log_hash(path: pathlib.Path) -> str:
    return path.name.removesuffix(".jsonl")


def _ensure_applied_log(db: metadb.MetaDatabaseConnection) -> None:
    with db.transaction():
        db.execute(_APPLIED_LOG_DDL)


def _mark_logs_applied(db: metadb.MetaDatabaseConnection, paths: list[pathlib.Path]) -> None:
    with db.transaction():
        for path in paths:
            db.execute(
                "INSERT OR IGNORE INTO applied_log (hash) VALUES (?)",
                (_log_hash(path),),
            )


def _insert_message_from_path(
    db: metadb.MetaDatabaseConnection,
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


def _replay_metalog(db: metadb.MetaDatabaseConnection, log_root: pathlib.Path) -> ReplayResult:
    """Apply every log file to the database, in order.

    Uses the same idempotent methods a backup used to, so replaying is nothing
    more than calling them again: a message seen in two mailboxes ends up with
    two rows exactly as the runs that observed it would have written. Entries
    whose message is not in the archive are counted and skipped -- the blob was
    removed, and inventing a row for it would describe mail that is not there.
    """
    result = ReplayResult()
    known = db.store_id_map()
    for logfile in metalog.read_all(log_root):
        result.files += 1
        if logfile.mailbox is None:
            log.warning("%s: no mailbox in the header, skipped", metalog.where(logfile.path))
            continue
        mailbox_id = db.add_mailbox(logfile.mailbox)
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
                db.assign_message_to_mailbox(message_id, mailbox_id)
                if logfile.folder is not None:
                    db.add_message_labels(message_id, logfile.folder)
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
    with metadb.MetaDatabase(path=db_path) as db:
        _ensure_applied_log(db)
        mb_id = db.add_mailbox(mailbox) if mailbox else None
        # One transaction per batch, not per message. Every write method commits
        # on its own when called at the top level, which over a whole archive is
        # half a million commits -- unnoticeable on a RAM disk and hours on a
        # real filesystem, where each one waits for the device.
        for batch in utils.batched(store.walk(), CREATE_DB_BATCH):
            with db.transaction():
                for path in batch:
                    msg_id = _insert_message_from_path(db, store, path)
                    if mb_id:
                        db.assign_message_to_mailbox(msg_id, mb_id)
                    result.messages += 1
            # Named for what it is doing, not merely counted. This reads every
            # message in the archive and takes half an hour on a large one, and
            # a bare "N message(s) read" in the middle of a backup leaves a
            # reader watching a number climb with no idea what it is for.
            log.info("building the query database: %s message(s) read", f"{result.messages:,}")

        result.replay = _replay_metalog(db, log_root)
        # Prime the bookkeeping so a later refresh reads only files added since.
        _mark_logs_applied(db, metalog.log_files(log_root))


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
    try:
        with metadb.MetaDatabase(path=db_path) as db:
            _apply_new_logs(db, store, log_root, result)
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
    db: metadb.MetaDatabaseConnection,
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
    _ensure_applied_log(db)
    applied = {row[0] for row in db.execute("SELECT hash FROM applied_log")}
    known = db.store_id_map()
    for path in metalog.log_files(log_root):
        if _log_hash(path) in applied:
            continue
        logfile = metalog.read_log(path)
        if logfile is None or logfile.mailbox is None:
            # Unreadable or headerless: leave it unmarked so a later, repaired
            # file is retried rather than skipped for good.
            continue
        result.files += 1
        mailbox_id = db.add_mailbox(logfile.mailbox)
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
                db.assign_message_to_mailbox(msg_id, mailbox_id)
                if logfile.folder is not None:
                    db.add_message_labels(msg_id, logfile.folder)
                result.applied += 1
            db.execute(
                "INSERT OR IGNORE INTO applied_log (hash) VALUES (?)",
                (_log_hash(path),),
            )


def _insert_message(
    db: metadb.MetaDatabaseConnection,
    store: cas.ContentAddressedStorage,
    store_id: str,
) -> int | None:
    """Insert the row for one archived message, or None when its blob is gone."""
    path = store.locate(store_id, exists=True)
    if path is None:
        return None
    return _insert_message_from_path(db, store, path)
