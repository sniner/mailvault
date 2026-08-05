"""Back up the selected folders, recording where each message was seen.

A backup writes the message (into the content-addressed storage) and its location
(into the log); everything else a query might want -- sender, subject, date --
stays in the message itself. Deletion after export is gated on the log being
sealed, so a message leaves the server only once its location is durable. With
`--index-db` it also refreshes a queryable `index.db` projection afterwards
(see `storedb`).
"""

from __future__ import annotations

import collections.abc
import logging
import pathlib
from datetime import UTC, datetime

from mailvault import conf, mailutils
from mailvault.backend import base, session
from mailvault.jobs.common import _seal_log
from mailvault.jobs.migration import migrate_archive
from mailvault.jobs.storedb import DEFAULT_QUERY_DB_NAME, refresh_db
from mailvault.store import cas, metalog, state

log = logging.getLogger(__name__)


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


def _resume_point(
    result: base.BackupResult,
    current: datetime | None,
    observed_at: datetime,
    full_pass: bool = False,
) -> datetime | None:
    """Where the next run may resume, or None to leave the snapshot untouched.

    The snapshot means "everything up to here is in the archive", and the only
    thing that can back that claim up is mail that was actually stored. Taking
    the wall clock instead trusts the source to have shown everything it had,
    which is exactly what a mailbox that is still starting up does not do:
    Proton Bridge answers before its first sync has run, and reports an empty
    folder rather than an error. A clock-based snapshot would then jump to
    today, and every message already sitting in that mailbox would fall before
    the next date filter -- fetched never, missed silently.

    Hence: no message stored, no advance. The folder is read in full again next
    run, which costs a search and, for what is already there, nothing else --
    the content-addressed storage absorbs the repeats.

    `full_pass` says the folder was read without a date filter, and that makes
    the result authoritative: everything the source was willing to show was
    seen, so the point is set to exactly that -- backwards too, if the mail says
    so. An unfiltered pass against a source holding less than it should is the
    one case where trusting it is safe, because the error runs the harmless way:
    the next window comes out too wide and costs a download the storage
    discards, rather than too narrow and costing the mail itself.
    """
    if result.newest is None:
        return None
    # A message dated in the future -- a sender's clock is off, or the server
    # stamped it oddly -- must not carry the resume point past the moment the
    # folder was actually read.
    candidate = min(result.newest, observed_at)
    # Only ever forwards, unless the pass was authoritative. The incremental
    # search window reaches back a day before the snapshot, so such a pass can
    # legitimately end on an older message than the one the snapshot was taken
    # from; moving back to it would widen every future run's window for no gain.
    if not full_pass and current is not None and candidate <= current:
        return None
    return candidate


def _location_writer(
    log_writer: metalog.LogWriter,
) -> collections.abc.Callable[[mailutils.MessageMetadata], None]:
    """Build the callback that records where a message was seen.

    A backup records only the location; subject, sender and date are in the
    message itself, so anything that wants them reads them back out of the archive
    rather than from a row kept in step with it.
    """

    def _record(email: mailutils.MessageMetadata) -> None:
        log_writer.add(email.mailbox, email.folders, email.store_id)

    return _record


def _backup_to_log(
    mb: base.MailboxClient,
    store: cas.ContentAddressedStorage,
    job: conf.JobConfig,
    store_path: pathlib.Path,
    incremental: bool = True,
) -> None:
    """Back up the selected folders, recording locations and resume state."""
    snapshot_state = state.SnapshotState.load(store_path / state.DEFAULT_STATE_NAME)
    log_root = store_path / metalog.DEFAULT_LOG_DIR
    folders = job.folders if job.folders else mb.folders()
    for folder in folders:
        try:
            _backup_folder(mb, store, job, folder, snapshot_state, log_root, incremental)
        except Exception as exc:
            # One folder that cannot be read must not cost the remaining ones;
            # its snapshot simply does not advance and the next run tries again.
            log.error("%s::%s: backup failed: %s", job.name, folder, exc)


def _backup_folder(
    mb: base.MailboxClient,
    store: cas.ContentAddressedStorage,
    job: conf.JobConfig,
    folder: str,
    snapshot_state: state.SnapshotState,
    log_root: pathlib.Path,
    incremental: bool = True,
) -> None:
    """Back up one folder, recording where its messages were seen."""
    current = snapshot_state.get_date(job.name, folder)
    start_date = current if incremental else None
    observed_at = datetime.now(UTC)
    log_writer = metalog.LogWriter(log_root)
    result = mb.folder_backup(
        folder, store, since=start_date, callback=_location_writer(log_writer)
    )
    # The seal date stamps the log with when the folder was read, which is a
    # fact about the run and stays the wall clock. Where the *next* run resumes
    # is a claim about coverage, and that one is derived from the mail itself.
    sealed = _seal_log(log_writer, observed_at)
    if result.complete and sealed:
        resume_at = _resume_point(result, current, observed_at, full_pass=not incremental)
        if resume_at is not None:
            _record_snapshot(snapshot_state, job.name, folder, resume_at)
        elif current is None:
            # No snapshot yet and nothing came back. Either the folder really is
            # empty, or the source is not serving its mail yet -- and the two are
            # indistinguishable from here, so treat it as the second and read the
            # folder in full again next run.
            log.info("%s::%s: no messages offered, snapshot not started", job.name, folder)
        else:
            log.debug(
                "%s::%s: nothing newer than %s, snapshot held",
                job.name,
                folder,
                current,
            )
    elif not result.complete:
        # Advancing the snapshot now would push the failed messages
        # out of every future date filter, losing them permanently.
        log.warning(
            "%s::%s: %s of %s message(s) failed, snapshot not advanced",
            job.name,
            folder,
            result.failed,
            result.total,
        )
    else:
        # Downloads were clean but the location log did not reach disk. Holding
        # the snapshot back re-fetches the folder next run and writes the log
        # again, rather than advancing past locations that were never recorded.
        log.warning("%s::%s: metadata log not sealed, snapshot not advanced", job.name, folder)
    _purge_after_seal(mb, job, folder, result, sealed)


def _purge_after_seal(
    mb: base.MailboxClient,
    job: conf.JobConfig,
    folder: str,
    result: base.BackupResult,
    sealed: bool,
) -> None:
    """Delete the archived messages from the server, but only once the log is on disk.

    This is the ordering the archive depends on when it deletes after export: a
    message's location reaches the log and is fsync'd *before* the message is
    removed from its source. A seal that failed holds the deletion back entirely
    -- the messages stay on the server and are re-fetched next run, which the
    content-addressed storage deduplicates -- rather than leaving `.eml` files in
    the archive whose one unrecoverable fact, where they were seen, went with the
    deleted server copy.

    Deletion does not wait for a *complete* folder, only a sealed one: the
    messages in `deletable` were stored and their locations written, so removing
    them is safe even when other messages of the same folder failed. Those failed
    ones are not in `deletable`, so they stay and are retried while the snapshot
    holds.
    """
    if not job.delete_after_export or not result.deletable:
        return
    if not sealed:
        log.error(
            "%s::%s: metadata log not sealed, %s message(s) left on the server",
            job.name,
            folder,
            len(result.deletable),
        )
        return
    try:
        mb.purge(folder, result.deletable)
    except Exception as exc:
        # The log is already durable, so a failed purge costs nothing but server
        # space: the messages stay and are deleted on the next clean run.
        log.error("%s::%s: purge failed: %s", job.name, folder, exc)


def backup(
    job: conf.JobConfig,
    store_path: pathlib.Path,
    compress: bool = False,
    index_db: bool = False,
    incremental: bool = True,
) -> None:
    """Back up one job's folders into the archive at `store_path`.

    `compress` stores the messages zstd-compressed; `index_db` refreshes the
    queryable `index.db` projection beside the archive once the backup is done;
    `incremental` resumes each folder from its snapshot instead of re-fetching it.

    Migrating a legacy archive happens first, before the mailbox is opened: it is
    a purely local operation that can take a while on a large `store.db`, and
    there is no reason to hold a server connection open across it. Doing it here
    also puts the "migrating, this may take a moment" line right after the job
    starts, rather than after a silent connect.
    """
    migrate_archive(store_path)
    with session.open_mailbox(job) as mb:
        store = cas.ContentAddressedStorage(store_path, suffix=".eml", compress=compress)
        _backup_to_log(mb, store, job, store_path, incremental=incremental)
    if index_db:
        _refresh_query_db(store_path)


def _refresh_query_db(store_path: pathlib.Path) -> None:
    """Keep the queryable projection beside the archive up to date, tolerantly.

    A convenience only: a failure to update it is logged and never allowed to
    fail the backup, whose real output -- the messages and the log -- is already
    written. `refresh_db` itself rebuilds the projection when it is missing or
    unreadable.
    """
    db_path = store_path / DEFAULT_QUERY_DB_NAME
    try:
        result = refresh_db(store_path, db_path)
    except Exception as exc:
        log.error("%s: query database not updated: %s", db_path, exc)
        return
    if result.rebuilt:
        log.info("%s: query database rebuilt, %s message(s)", db_path, result.messages)
    else:
        log.info(
            "%s: query database updated, %s new message(s) from %s log file(s)",
            db_path,
            result.messages,
            result.files,
        )
