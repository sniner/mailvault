from __future__ import annotations

import collections.abc
import dataclasses
import imaplib
import logging
import pathlib
import time
from datetime import UTC, datetime

from mailvault import conf, mailutils
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


def _metadata_writer(
    db: metadb.MetaDatabaseConnection,
    mailbox_id: int,
    log_writer: metalog.LogWriter | None = None,
) -> collections.abc.Callable[[mailutils.MessageMetadata], None]:
    """Build the callback that records a message's metadata in the store database.

    With a log writer the mailbox and folder attribution is additionally recorded
    in the metadata log, which is the copy that survives a damaged database.
    """

    def _store_metadata(email: mailutils.MessageMetadata) -> None:
        msg = db.add_message(email.store_id, email.email_id, email.date, email.subject)
        db.assign_message_to_mailbox(msg, mailbox_id)
        db.add_message_labels(msg, *email.folders)
        db.add_message_sender(msg, *email.sender)
        db.add_message_recipients(msg, *email.recipients)
        if log_writer is not None:
            log_writer.add(email.mailbox, email.folders, email.store_id)

    return _store_metadata


def _seal_log(writer: metalog.LogWriter, date: datetime, complete: bool = True) -> None:
    """Write out a pass over a folder, tolerating a failure to do so.

    A log that cannot be written is reported but does not abort the run: the
    messages themselves are archived and the database still holds the location,
    so the loss is repairable while an aborted run is not.
    """
    recorded, places = len(writer), writer.places
    try:
        paths = writer.seal(date, complete=complete)
    except OSError as exc:
        log.error("%s: metadata log not written: %s", writer.root, exc)
        return
    if paths:
        log.info("%s: %s message(s) recorded in %s place(s)", writer.root, recorded, places)


@dataclasses.dataclass
class BootstrapResult:
    """Outcome of exporting an existing database's locations into the log."""

    messages: int = 0
    places: int = 0
    placeless: int = 0
    undecidable: int = 0
    written: bool = False
    skipped: bool = False


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
    db: metadb.MetaDatabaseConnection, log_root: pathlib.Path, date: datetime
) -> BootstrapResult:
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
    result = BootstrapResult()
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
    if len(writer):
        result.written = bool(writer.seal(date))
    return result


def bootstrap_metalog(store_path: pathlib.Path, force: bool = False) -> BootstrapResult:
    """Export an archive's existing locations into the metadata log.

    Skipped once a previous export has finished, unless `force` is given: the log
    is append-only, and exporting again would duplicate every location. Replaying
    duplicates is harmless -- the database methods are idempotent -- but it
    doubles the log for no gain.

    The decision rests on the completion marker rather than on log files being
    present. An export interrupted halfway leaves files behind, and treating
    those as "already done" would freeze the archive at partial coverage.
    """
    log_root = store_path / metalog.DEFAULT_LOG_DIR
    if metalog.bootstrap_done(log_root) and not force:
        return BootstrapResult(skipped=True)
    with metadb.MetaDatabase(path=store_path / metadb.DEFAULT_DB_NAME) as db:
        date = datetime.now(UTC)
        result = _export_metalog(db, log_root, date)
    metalog.mark_bootstrap_done(log_root, date)
    return result


def _bootstrap_missing_log(db: metadb.MetaDatabaseConnection, log_root: pathlib.Path) -> None:
    """Export the locations of an archive that has not been exported yet.

    Runs before the first folder of a backup so that an archive filled by an
    earlier version is protected from the very first run, without the user having
    to know that the log exists. The marker is written even when nothing was
    exported, so a fresh archive does not retry on every run.
    """
    if metalog.bootstrap_done(log_root):
        return
    date = datetime.now(UTC)
    try:
        result = _export_metalog(db, log_root, date)
        metalog.mark_bootstrap_done(log_root, date)
    except OSError as exc:
        log.error("%s: metadata log could not be created: %s", log_root, exc)
        return
    if result.written:
        log.info(
            "%s: no metadata log found, exported %s message(s) into %s place(s)",
            log_root,
            result.messages,
            result.places,
        )


def _replay_metalog(db: metadb.MetaDatabaseConnection, log_root: pathlib.Path) -> ReplayResult:
    """Apply every log file to the database, in order.

    Uses the same idempotent methods a backup uses, so replaying is nothing more
    than calling them again: a message seen in two mailboxes ends up with two
    rows exactly as it would have during the runs that observed it. Entries whose
    message is not in the archive are counted and skipped -- the blob was
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


def _snapshot_start(
    snapshot_state: state.SnapshotState,
    db: metadb.MetaDatabaseConnection,
    job_name: str,
    mailbox_id: int,
    label_id: int,
    folder: str,
) -> datetime | None:
    """Return the timestamp an incremental run of this folder has to start from.

    `store.json` wins whenever it knows the folder, because it is the copy that
    survives a damaged database. The database is consulted as a fallback, and
    that is what makes the changeover seamless: an archive written by an earlier
    version carries its timestamps only in the database, and the first run copies
    them over into the state file without re-fetching anything.
    """
    date = snapshot_state.get_date(job_name, folder)
    if date is not None:
        return date
    return db.get_snapshot_date(mailbox_id, label_id)


def _record_snapshot(
    snapshot_state: state.SnapshotState,
    db: metadb.MetaDatabaseConnection,
    job_name: str,
    mailbox_id: int,
    label_id: int,
    folder: str,
    date: datetime,
) -> None:
    """Advance the snapshot of one folder in both the database and the state file.

    A state file that cannot be written is logged and otherwise tolerated: the
    database still holds the timestamp, so the next run falls back to it and
    nothing is re-fetched. Aborting here would instead cost the remaining folders
    of the run for a failure that has no effect on the archived mail.
    """
    db.set_snapshot(mailbox_id, label_id, date=date)
    snapshot_state.set_date(job_name, folder, date)
    try:
        snapshot_state.save()
    except OSError as exc:
        log.error(
            "%s: snapshot state not written, database still holds it: %s",
            snapshot_state.path,
            exc,
        )


def _adopt_database_snapshots(
    snapshot_state: state.SnapshotState, db: metadb.MetaDatabaseConnection
) -> None:
    """Seed an empty state file from the snapshot table of an existing archive.

    An archive written before the state file existed carries its timestamps only
    in the database. They are copied across in one go rather than one folder per
    run as folders happen to be visited, so the state file is complete after the
    first run and the database stops being the load-bearing copy immediately.

    Only ever fills an empty state file: a state file that already holds
    something is the newer truth and must not be overwritten by the database.
    """
    if not snapshot_state.is_empty():
        return
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
    if not adopted:
        return
    try:
        snapshot_state.save()
    except OSError as exc:
        log.error("%s: snapshot state not written: %s", snapshot_state.path, exc)
        return
    log.info(
        "%s: adopted %s snapshot(s) from the metadata database",
        snapshot_state.path,
        adopted,
    )


def _backup_with_db(
    mb: base.MailboxClient,
    store: cas.ContentAddressedStorage,
    job: conf.JobConfig,
    store_path: pathlib.Path,
) -> None:
    """Back up the selected folders, recording metadata, log and snapshot state."""
    snapshot_state = state.SnapshotState.load(store_path / state.DEFAULT_STATE_NAME)
    log_root = store_path / metalog.DEFAULT_LOG_DIR
    with metadb.MetaDatabase(path=store_path / metadb.DEFAULT_DB_NAME) as db:
        _adopt_database_snapshots(snapshot_state, db)
        _bootstrap_missing_log(db, log_root)
        mb_id = db.add_mailbox(job.name)
        folders = job.folders if job.folders else mb.folders()
        for folder in folders:
            folder_id = db.add_label(folder)
            start_date = (
                _snapshot_start(snapshot_state, db, job.name, mb_id, folder_id, folder)
                if job.incremental
                else None
            )
            snapshot_date = datetime.now(UTC)
            log_writer = metalog.LogWriter(log_root)
            result = mb.folder_backup(
                folder,
                store,
                since=start_date,
                callback=_metadata_writer(db, mb_id, log_writer),
            )
            _seal_log(log_writer, snapshot_date, complete=result.complete)
            if result.complete:
                _record_snapshot(
                    snapshot_state, db, job.name, mb_id, folder_id, folder, snapshot_date
                )
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
            _backup_with_db(mb, store, job, store_path)
        elif job.folders:
            for folder in job.folders:
                mb.folder_backup(folder, store)
        else:
            mb.full_backup(store)


def _verify_folder(
    mb: base.MailboxClient,
    db: metadb.MetaDatabaseConnection,
    store: cas.ContentAddressedStorage,
    mailbox_id: int,
    job_name: str,
    folder: str,
    log_root: pathlib.Path,
    repair: bool = False,
) -> VerifyResult:
    """Compare one server folder against the archive and optionally refetch gaps.

    Matching is done by Message-ID, which is cheap: listing a folder's headers
    costs a handful of requests, while re-downloading it costs one request per
    message. A message counts as missing whenever its Message-ID is not archived
    for this folder, so messages with an absent or duplicated Message-ID may be
    fetched needlessly -- that is deliberate, since the content-addressed storage
    discards the redundant copy anyway. The reverse mistake, skipping a message
    that really is missing, is the one worth avoiding.
    """
    label_id = db.add_label(folder)
    known = mailutils.MessageIdIndex(db.get_known_message_ids(mailbox_id, label_id))
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

    # A repaired message is new archive content, so its attribution has to reach
    # the log as well -- otherwise it exists only in the database.
    log_writer = metalog.LogWriter(log_root)
    store_metadata = _metadata_writer(db, mailbox_id, log_writer)
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
            store_metadata(
                mailutils.metadata(msg, mailbox=job_name, folder=folder, store_id=store_id)
            )
        except Exception as exc:
            log.exception("%s::%s: storing %s failed: %s", job_name, folder, label, exc)
            result.failed += 1
            continue
        log.info("%s::%s: restored %s: %s id=%s", job_name, folder, label, status, store_id)
        result.restored += 1

    _seal_log(log_writer, datetime.now(UTC), complete=result.failed == 0)
    return result


def verify(
    job: conf.JobConfig,
    store_path: pathlib.Path,
    repair: bool = False,
    compress: bool = False,
) -> list[VerifyResult]:
    """Check the archive for messages the server still has but the archive lacks.

    Gaps arise when individual downloads fail during a backup run. The snapshot
    date of an incremental job hides them from every later run, so they need an
    explicit comparison to be found. With `repair` the missing messages are
    fetched and added to the archive. The snapshot dates are left untouched.
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
    with session.open_mailbox(job) as mb:
        store = cas.ContentAddressedStorage(store_path, suffix=".eml", compress=compress)
        with metadb.MetaDatabase(path=store_path / metadb.DEFAULT_DB_NAME) as db:
            mb_id = db.add_mailbox(job.name)
            folders = job.folders if job.folders else list(mb.folders())
            for folder in folders:
                try:
                    results.append(
                        _verify_folder(
                            mb, db, store, mb_id, job.name, folder, log_root, repair=repair
                        )
                    )
                except Exception as exc:
                    log.error("%s::%s: verify failed: %s", job.name, folder, exc)
    return results


def folder_list(job: conf.JobConfig) -> None:
    with session.open_mailbox(job) as mb:
        for folder in mb.folders():
            print(f"{job.name}::{folder}")


def rebuild_metadb(store_path: pathlib.Path, mailbox: str | None = None) -> RebuildResult:
    """Rebuild the metadata database from the archive, then apply the metadata log.

    The archived messages supply everything a message carries in itself -- sender,
    recipients, subject, date. Which mailbox and which folder it was seen in is
    not in the message, so it comes from the log. Without a log that attribution
    stays missing, which also leaves `verify` with nothing to compare against.
    """
    store = cas.ContentAddressedStorage(store_path, suffix=".eml")
    result = RebuildResult()
    with metadb.MetaDatabase(path=store_path / metadb.DEFAULT_DB_NAME) as db:
        mb_id = db.add_mailbox(mailbox) if mailbox else None
        for path in store.walk():
            msg = store.read(path)
            header = mailutils.decode_email_header(msg)
            from_addrs, to_addrs = mailutils.addresses(header)
            store_id = path.name.split(".")[0]
            email_id = mailutils.message_id(header)
            date = mailutils.date(header)
            subject = mailutils.subject(header)
            log.debug("%s: message_id=%s, date=%s", store_id, email_id, date)

            msg_id = db.add_message(store_id, email_id, date, subject)
            if mb_id:
                db.assign_message_to_mailbox(msg_id, mb_id)
            db.add_message_sender(msg_id, *from_addrs)
            db.add_message_recipients(msg_id, *to_addrs)
            result.messages += 1
            if result.messages % 5000 == 0:
                log.info("%s: %s message(s) read", store_path, result.messages)

        result.replay = _replay_metalog(db, store_path / metalog.DEFAULT_LOG_DIR)
    return result


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
