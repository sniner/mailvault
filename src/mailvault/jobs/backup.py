"""Back up the selected folders, recording where each message was seen.

A backup writes only the message (into the content-addressed storage) and its
location (into the log). Everything else a query might want -- sender, subject,
date -- stays in the message itself. Deletion after export is gated on the log
being sealed, so a message leaves the server only once its location is durable.
"""

from __future__ import annotations

import collections.abc
import logging
import pathlib
from datetime import UTC, datetime

from mailvault import conf, mailutils
from mailvault.backend import base, session
from mailvault.jobs.common import _seal_log
from mailvault.jobs.migrate import migrate_archive
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
        try:
            _backup_folder(mb, store, job, folder, snapshot_state, log_root)
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
) -> None:
    """Back up one folder, recording where its messages were seen."""
    start_date = snapshot_state.get_date(job.name, folder) if job.incremental else None
    snapshot_date = datetime.now(UTC)
    log_writer = metalog.LogWriter(log_root)
    result = mb.folder_backup(
        folder, store, since=start_date, callback=_location_writer(log_writer)
    )
    sealed = _seal_log(log_writer, snapshot_date)
    if result.complete and sealed:
        _record_snapshot(snapshot_state, job.name, folder, snapshot_date)
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


def backup(job: conf.JobConfig, store_path: pathlib.Path, compress: bool = False) -> None:
    with session.open_mailbox(job) as mb:
        store = cas.ContentAddressedStorage(store_path, suffix=".eml", compress=compress)
        _backup_to_log(mb, store, job, store_path)
