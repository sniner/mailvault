"""Build a queryable SQLite database from the archive and its log.

The archive itself holds no database. This makes one on demand out of the two
things that do live there -- the messages, for everything they carry in
themselves, and the log, for which mailbox and folder each was seen in. What
comes out is a snapshot, accurate when built and stale from the next backup on.
"""

from __future__ import annotations

import dataclasses
import logging
import pathlib

from mailvault import mailutils, utils
from mailvault.jobs.common import JobError
from mailvault.store import cas, metadb, metalog

log = logging.getLogger(__name__)

# Messages per transaction when building a database. Large enough that commits
# stop dominating, small enough that an interrupted run has not done much work
# it will have to repeat.
CREATE_DB_BATCH = 2000


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
            log.warning("%s: no mailbox in the header, skipped", logfile.path)
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
    goes stale from the next backup onwards; build it again when that matters.

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
    store = cas.ContentAddressedStorage(store_path, suffix=".eml")
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
    with metadb.MetaDatabase(path=db_path) as db:
        mb_id = db.add_mailbox(mailbox) if mailbox else None
        # One transaction per batch, not per message. Every write method commits
        # on its own when called at the top level, which over a whole archive is
        # half a million commits -- unnoticeable on a RAM disk and hours on a
        # real filesystem, where each one waits for the device.
        for batch in utils.batched(store.walk(), CREATE_DB_BATCH):
            with db.transaction():
                for path in batch:
                    # Only the headers: everything this needs is in them, and the
                    # attachments behind them are 98% of the bytes.
                    header = mailutils.decode_email_header(store.read_header(path))
                    from_addrs, to_addrs = mailutils.addresses(header)
                    store_id = path.name.split(".")[0]
                    email_id = mailutils.message_id(header)
                    date = mailutils.date(header)
                    subject = mailutils.subject(header)

                    msg_id = db.add_message(store_id, email_id, date, subject)
                    if mb_id:
                        db.assign_message_to_mailbox(msg_id, mb_id)
                    db.add_message_sender(msg_id, *from_addrs)
                    db.add_message_recipients(msg_id, *to_addrs)
                    result.messages += 1
            log.info("%s: %s message(s) read", store_path, result.messages)

        result.replay = _replay_metalog(db, store_path / metalog.DEFAULT_LOG_DIR)
