from __future__ import annotations

import collections.abc
import dataclasses
import imaplib
import logging
import pathlib
import time
from datetime import UTC, datetime

from mailvault import conf, mailutils, utils
from mailvault.backend import base, imap, session
from mailvault.store import cas, metadb, metalog, state

log = logging.getLogger(__name__)


class JobError(Exception):
    pass


@dataclasses.dataclass
class VerifyResult:
    """Outcome of comparing one server folder against the local archive."""

    folder: str
    on_server: int = 0
    missing: int = 0
    restored: int = 0
    failed: int = 0


def _seal_log(writer: metalog.LogWriter, date: datetime) -> None:
    """Write out a pass over a folder, tolerating a failure to do so.

    A log that cannot be written is reported but does not abort the run: the
    messages themselves are archived and the database still holds the location,
    so the loss is repairable while an aborted run is not.
    """
    recorded, places = len(writer), writer.places
    try:
        paths = writer.seal(date)
    except OSError as exc:
        log.error("%s: metadata log not written: %s", writer.root, exc)
        return
    if paths:
        log.info("%s: %s message(s) recorded in %s place(s)", writer.root, recorded, places)


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


def _record_snapshot(
    snapshot_state: state.SnapshotState, job_name: str, folder: str, date: datetime
) -> None:
    """Advance the snapshot of one folder.

    A state file that cannot be written is logged and otherwise tolerated. The
    folder is simply fetched again next time, which the content-addressed storage
    absorbs; aborting here would instead cost the remaining folders of the run for
    a failure that has no effect on the archived mail.
    """
    snapshot_state.set_date(job_name, folder, date)
    try:
        snapshot_state.save()
    except OSError as exc:
        log.error("%s: resume state not written: %s", snapshot_state.path, exc)


def _adopt_database_snapshots(
    snapshot_state: state.SnapshotState, db: metadb.MetaDatabaseConnection
) -> int:
    """Copy the snapshot table of a legacy archive into the state file.

    Only ever fills an empty state file: one that already holds something is the
    newer truth and must not be overwritten by the database.
    """
    if not snapshot_state.is_empty():
        return 0
    adopted = 0
    for mailbox, folder, timestamp in db.all_snapshots():
        try:
            snapshot_state.set_date(mailbox, folder, datetime.fromisoformat(timestamp))
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
        store.db.migrated   the log is the source, this file is spare
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

    log_root = store_path / metalog.DEFAULT_LOG_DIR
    snapshot_state = state.SnapshotState.load(store_path / state.DEFAULT_STATE_NAME)
    date = datetime.now(UTC)
    with metadb.MetaDatabase(path=legacy) as db:
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
        "%s: migrated -- %s message(s) into %s place(s), %s snapshot(s); %s is now spare",
        store_path,
        result.messages,
        result.places,
        result.snapshots,
        target.name,
    )
    return result


def _location_writer(
    log_writer: metalog.LogWriter,
) -> collections.abc.Callable[[mailutils.MessageMetadata], None]:
    """Build the callback that records where a message was seen.

    That is all a backup writes about a message now. Subject, sender and date are
    in the message itself, so anything that wants them reads them back out of the
    archive -- there is no database to keep up to date any more.
    """

    def _record(email: mailutils.MessageMetadata) -> None:
        log_writer.add(email.mailbox, email.folders, email.store_id)

    return _record


def _backup_to_log(
    mb: base.MailboxClient,
    store: cas.ContentAddressedStorage,
    job: conf.JobConfig,
    store_path: pathlib.Path,
) -> None:
    """Back up the selected folders, recording locations and resume state."""
    migrate_archive(store_path)
    snapshot_state = state.SnapshotState.load(store_path / state.DEFAULT_STATE_NAME)
    log_root = store_path / metalog.DEFAULT_LOG_DIR
    folders = job.folders if job.folders else mb.folders()
    for folder in folders:
        start_date = snapshot_state.get_date(job.name, folder) if job.incremental else None
        snapshot_date = datetime.now(UTC)
        log_writer = metalog.LogWriter(log_root)
        result = mb.folder_backup(
            folder, store, since=start_date, callback=_location_writer(log_writer)
        )
        _seal_log(log_writer, snapshot_date)
        if result.complete:
            _record_snapshot(snapshot_state, job.name, folder, snapshot_date)
        else:
            # Advancing the snapshot now would push the failed messages
            # out of every future date filter, losing them permanently.
            log.warning(
                "%s::%s: %s of %s message(s) failed, snapshot not advanced",
                job.name,
                folder,
                result.failed,
                result.total,
            )


def backup(job: conf.JobConfig, store_path: pathlib.Path, compress: bool = False) -> None:
    with session.open_mailbox(job) as mb:
        store = cas.ContentAddressedStorage(store_path, suffix=".eml", compress=compress)
        if job.with_db:
            _backup_to_log(mb, store, job, store_path)
        elif job.folders:
            for folder in job.folders:
                mb.folder_backup(folder, store)
        else:
            mb.full_backup(store)


def _places_from_log(log_root: pathlib.Path) -> dict[tuple[str, str | None], set[str]]:
    """Read the whole log once into `(mailbox, folder) -> store ids`."""
    places: dict[tuple[str, str | None], set[str]] = {}
    for logfile in metalog.read_all(log_root):
        if logfile.mailbox is None:
            continue
        places.setdefault((logfile.mailbox, logfile.folder), set()).update(logfile.store_ids)
    return places


def _archived_message_ids(
    store: cas.ContentAddressedStorage, store_ids: collections.abc.Iterable[str]
) -> set[str]:
    """Return the normalised Message-IDs of the given archived messages.

    The log says which messages are at a place; the Message-ID itself is only in
    the message, so each one is parsed. That is a few thousand header reads for a
    folder -- `verify` is a once-in-a-blue-moon command, and the database it used
    to ask is no longer part of the archive.

    Messages without a usable Message-ID are omitted: they cannot serve as a
    comparison key and must count as "not present" so a verify run re-fetches
    them, which is harmless because the storage deduplicates by content.
    """
    known: set[str] = set()
    for store_id in store_ids:
        path = store.locate(store_id, exists=True)
        if path is None:
            continue
        try:
            header = mailutils.decode_email_header(store.read_header(path))
        except (OSError, ValueError) as exc:
            log.warning("%s: unreadable, not counted as archived: %s", path, exc)
            continue
        known.add(mailutils.normalize_message_id(mailutils.message_id(header)))
    known.discard("")
    return known


def _verify_folder(
    mb: base.MailboxClient,
    store: cas.ContentAddressedStorage,
    log_root: pathlib.Path,
    archived: set[str],
    job_name: str,
    folder: str,
    repair: bool = False,
) -> VerifyResult:
    """Compare one server folder against the archive and optionally refetch gaps.

    Matching is done by Message-ID, which is the only key both sides share
    without transferring the message: listing a folder's headers costs a handful
    of requests, while re-downloading it costs one request per message. The
    content hash would be exact, but the server does not know it.

    A message counts as missing whenever its Message-ID is not archived for this
    folder, so messages with an absent or duplicated Message-ID may be fetched
    needlessly -- deliberate, since the storage discards the redundant copy. The
    reverse mistake, skipping a message that really is missing, is the one worth
    avoiding.
    """
    known = mailutils.MessageIdIndex(_archived_message_ids(store, archived))
    log.info("%s::%s: %s message(s) in archive", job_name, folder, len(known))

    result = VerifyResult(folder=folder)
    missing: list[base.MessageRef] = []
    for ref in mb.message_index(folder):
        result.on_server += 1
        if mailutils.normalize_message_id(ref.message_id) not in known:
            missing.append(ref)
        if result.on_server % 5000 == 0:
            log.info("%s::%s: %s message(s) indexed", job_name, folder, result.on_server)
    result.missing = len(missing)

    log.info(
        "%s::%s: %s of %s message(s) on the server are not archived",
        job_name,
        folder,
        result.missing,
        result.on_server,
    )
    if not repair or not missing:
        return result

    # A repaired message is new archive content, so its location has to reach the
    # log as well -- otherwise nothing records where it belongs.
    log_writer = metalog.LogWriter(log_root)
    for ref in missing:
        label = ref.message_id or ref.msg_id
        try:
            msg = mb.fetch_message(ref.msg_id, folder)
        except Exception as exc:
            log.error("%s::%s: download failed for %s: %s", job_name, folder, label, exc)
            result.failed += 1
            continue
        try:
            status, store_id, _path = store.add(msg)
            log_writer.add(job_name, [folder], store_id)
        except Exception as exc:
            log.exception("%s::%s: storing %s failed: %s", job_name, folder, label, exc)
            result.failed += 1
            continue
        log.info("%s::%s: restored %s: %s id=%s", job_name, folder, label, status, store_id)
        result.restored += 1

    _seal_log(log_writer, datetime.now(UTC))
    return result


def verify(
    job: conf.JobConfig,
    store_path: pathlib.Path,
    repair: bool = False,
    compress: bool = False,
) -> list[VerifyResult]:
    """Check the archive for messages the server still has but the archive lacks.

    Gaps are rare by design: a folder whose downloads partly failed does not
    advance its snapshot, so the next run fetches it again. What is left are
    archives from older versions, jobs that keep no state, and mail moved into a
    folder with an internal date older than the snapshot. This is a last resort,
    not part of the routine -- which is why it can afford to read the archive
    itself rather than keep an index alongside it.
    """
    if not job.with_db:
        raise JobError(f"{job.name}: verify requires a job with 'with_db' enabled")
    if job.exchange_journal:
        raise JobError(
            f"{job.name}: verify does not support 'exchange_journal' jobs, because the"
            " archive holds the unwrapped message whose Message-ID differs from the"
            " journal envelope reported by the server"
        )

    results: list[VerifyResult] = []
    log_root = store_path / metalog.DEFAULT_LOG_DIR
    places = _places_from_log(log_root)
    with session.open_mailbox(job) as mb:
        store = cas.ContentAddressedStorage(store_path, suffix=".eml", compress=compress)
        folders = job.folders if job.folders else list(mb.folders())
        for folder in folders:
            try:
                results.append(
                    _verify_folder(
                        mb,
                        store,
                        log_root,
                        places.get((job.name, folder), set()),
                        job.name,
                        folder,
                        repair=repair,
                    )
                )
            except Exception as exc:
                log.error("%s::%s: verify failed: %s", job.name, folder, exc)
    return results


def folder_list(job: conf.JobConfig) -> None:
    with session.open_mailbox(job) as mb:
        for folder in mb.folders():
            print(f"{job.name}::{folder}")


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


def _format_archive_folder(template: str) -> str:
    now = datetime.now()
    return now.strftime(template)


def _copy_folder(
    mb_from: base.MailboxClient,
    mb_to: base.MailboxClient,
    folder: str,
    archive_folder: str | None = None,
) -> None:
    for msg_id, msg_date, msg in mb_from.get_messages(folder):
        mb_to.save_message(msg, folder, date=msg_date)
        if archive_folder:
            dest_folder = _format_archive_folder(archive_folder)
            log.info(
                "%s::%s: Moving message '%s' to folder '%s'",
                mb_from.job_name,
                folder,
                msg_id,
                dest_folder,
            )
            try:
                mb_from.move_message(msg_id, dest_folder)
            except imap.MailboxError:
                mb_from.save_message(msg, dest_folder, date=msg_date)
                mb_from.delete_message(msg_id, expunge=True)


def _copy(
    source: conf.JobConfig, destination: conf.JobConfig, archive_folder: str | None = None
) -> None:
    with session.open_mailbox(source) as mb_from:
        with session.open_mailbox(destination) as mb_to:
            folders = source.folders if source.folders else ["INBOX"]
            for folder in folders:
                _copy_folder(mb_from, mb_to, folder, archive_folder=archive_folder)


def _idle_copy(
    source: conf.JobConfig,
    folder_name: str,
    destination: conf.JobConfig,
    archive_folder: str | None = None,
) -> None:
    def _copy_to_dest(mb_from: base.MailboxClient):
        with session.open_mailbox(destination) as mb_to:
            _copy_folder(mb_from, mb_to, folder_name, archive_folder=archive_folder)

    backoff = 1
    while True:
        try:
            with session.open_mailbox(source) as mb_from:
                # IDLE is IMAP-specific; the caller guarantees an IMAP source, but
                # assert it so a misuse fails loudly instead of as an AttributeError.
                if not isinstance(mb_from, imap.ImapClient):
                    raise JobError(f"{source.name}: --idle requires an IMAP source")
                backoff = 1
                _copy_to_dest(mb_from)
                while True:
                    for _, _ in mb_from.watch_folder("INBOX"):
                        _copy_to_dest(mb_from)
        except (OSError, imaplib.IMAP4.abort):
            log.warning(
                "%s::%s: Connection lost, reconnecting in %ds",
                source.name,
                folder_name,
                backoff,
            )
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)


def copy(source: conf.JobConfig, destination: conf.JobConfig, idle: bool = False) -> None:
    if source.move_to_archive:
        if source.archive_folder:
            archive_folder = source.archive_folder
        else:
            raise JobError("Option 'move_to_archive' given, but 'archive_folder' missing")
    else:
        archive_folder = None

    if idle:
        if source.backend != "imap":
            raise JobError(
                f"{source.name}: --idle is only supported for IMAP sources, "
                f"not backend {source.backend!r}"
            )
        # FIXME: currently only INBOX
        _idle_copy(source, "INBOX", destination, archive_folder=archive_folder)
    else:
        _copy(source, destination, archive_folder=archive_folder)
