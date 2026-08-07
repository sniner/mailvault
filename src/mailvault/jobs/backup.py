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
from mailvault.jobs.reconcile import places_from_log, reconcile_folder
from mailvault.jobs.storedb import DEFAULT_QUERY_DB_NAME, refresh_db
from mailvault.store import cas, heads, metalog

log = logging.getLogger(__name__)


class _ArchivedPlaces:
    """What the archive already holds, read from the log at most once per run.

    Only a folder that has to be caught up needs this, and reading the whole
    metadata log is not a cost an ordinary incremental run should carry. In the
    steady state every folder has a resume point, nothing asks, and the log stays
    unread.
    """

    def __init__(self, log_root: pathlib.Path, job_name: str):
        self._log_root = log_root
        self._job_name = job_name
        self._places: dict[tuple[str, str | None], set[str]] | None = None

    def of(self, folder: str) -> set[str]:
        if self._places is None:
            log.info("%s: reading the metadata log", self._job_name)
            self._places = places_from_log(self._log_root)
        return self._places.get((self._job_name, folder), set())


def _resume_point(heads_root: pathlib.Path, job_name: str, folder: str) -> dict | None:
    """Where the next pass over this folder carries on, or None to read it all.

    The value is handed to the backend as it was stored. Nothing here knows what
    a `uid` or a `delta_link` means, and nothing here should.
    """
    head = heads.read(heads_root, job_name, folder)
    return None if head is None else head.resume


def _record_pass(
    heads_root: pathlib.Path,
    job_name: str,
    folder: str,
    last_run: datetime,
    resume: dict | None,
) -> None:
    """Note that a pass read this folder, and where the next one may carry on.

    `last_run` is written whatever the outcome -- it says the folder was read,
    not that the read went well. `resume` is None unless this pass earned a new
    one, and None leaves the previous point standing rather than clearing it:
    a pass that archived nothing has no new point to offer, and forgetting the
    old one would throw away coverage that still holds. That is why the head is
    read before it is replaced, rather than built from scratch -- the same read
    also keeps the chain head of the metadata log, which belongs to `compact`
    and not to this pass.

    A head that cannot be written is logged and otherwise tolerated. The folder
    is simply fetched again next time, which the content-addressed storage
    absorbs; aborting here would instead cost the remaining folders of the run
    for a failure that has no effect on the archived mail.
    """
    head = heads.read(heads_root, job_name, folder) or heads.Head(job=job_name, folder=folder)
    head.last_run = last_run.isoformat()
    if resume is not None:
        head.resume = resume
    try:
        heads.write(heads_root, head)
    except OSError as exc:
        log.error("%s::%s: resume point not written: %s", job_name, folder, exc)


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
    heads_root = store_path / heads.DEFAULT_HEADS_DIR
    log_root = store_path / metalog.DEFAULT_LOG_DIR
    places = _ArchivedPlaces(log_root, job.name)
    folders = job.folders if job.folders else mb.folders()
    for folder in folders:
        try:
            _backup_folder(
                mb,
                store,
                job,
                folder,
                heads_root,
                log_root,
                places,
                incremental,
            )
        except Exception as exc:
            # One folder that cannot be read must not cost the remaining ones;
            # its snapshot simply does not advance and the next run tries again.
            log.error("%s::%s: backup failed: %s", job.name, folder, exc)


def _backup_folder(
    mb: base.MailboxClient,
    store: cas.ContentAddressedStorage,
    job: conf.JobConfig,
    folder: str,
    heads_root: pathlib.Path,
    log_root: pathlib.Path,
    places: _ArchivedPlaces,
    incremental: bool = True,
) -> None:
    """Back up one folder, recording where its messages were seen.

    The resume point is the backend's to make and the backend's to read; nothing
    here looks inside it. `--full` (`incremental=False`) simply withholds it,
    which is what makes a full pass authoritative: a backend given no point has
    nothing to hold itself back from and records exactly what it found.
    """
    previous = _resume_point(heads_root, job.name, folder) if incremental else None
    if previous is None and incremental:
        if _catch_up_if_possible(mb, store, job, folder, heads_root, log_root, places):
            return

    observed_at = datetime.now(UTC)
    log_writer = metalog.LogWriter(log_root, heads_root)
    result = mb.folder_backup(
        folder,
        store,
        resume=previous,
        callback=_location_writer(log_writer),
    )

    if result.resume_lost:
        # The source will not honour the point any more and did nothing rather
        # than deciding for us. Listing beats downloading the folder again, and
        # where that is not on offer the full read is at least an explicit one.
        log.info("%s::%s: the resume point is void", job.name, folder)
        if _catch_up_if_possible(mb, store, job, folder, heads_root, log_root, places):
            return
        log.info("%s::%s: reading the folder in full", job.name, folder)
        result = mb.folder_backup(
            folder,
            store,
            resume=None,
            callback=_location_writer(log_writer),
        )
    # The seal date stamps the log with when the folder was read, which is a
    # fact about the run and stays the wall clock. Where the *next* run resumes
    # is a claim about coverage, and that one comes back from the backend.
    sealed = _seal_log(log_writer, observed_at)
    resume: dict | None = None
    if result.complete and sealed:
        resume = result.resume
        if resume is None and previous is None:
            # Nothing came back and there was nothing to begin with. Either the
            # folder really is empty, or the source is not serving its mail yet --
            # and the two are indistinguishable from here, so treat it as the
            # second and read the folder in full again next run.
            log.info("%s::%s: no messages offered, resume point not started", job.name, folder)
    elif not result.complete:
        # Taking the new point now would push the failed messages out of
        # everything the next pass asks for, losing them permanently.
        log.warning(
            "%s::%s: %s of %s message(s) failed, resume point not advanced",
            job.name,
            folder,
            result.failed,
            result.total,
        )
    else:
        # Downloads were clean but the location log did not reach disk. Holding
        # the point back re-fetches the folder next run and writes the log
        # again, rather than advancing past locations that were never recorded.
        log.warning(
            "%s::%s: metadata log not sealed, resume point not advanced",
            job.name,
            folder,
        )
    # Written whatever the outcome: the folder *was* read, and that is all
    # `last_run` claims. Only `resume` is held back when the pass fell short.
    _record_pass(heads_root, job.name, folder, observed_at, resume)
    _purge_after_seal(mb, job, folder, result, sealed)


def _can_catch_up(job: conf.JobConfig) -> bool:
    """Whether this job may be caught up by listing instead of downloading.

    Two jobs may not, for reasons that have nothing to do with speed:

    `delete_after_export` removes a message from the server once its location is
    durable, and that only happens for messages a pass actually *fetched*. A
    catch-up skips whatever is already archived, so those would stay on the
    server -- and stay below the new resume point, meaning no later run would
    ever see them again to delete them.

    `exchange_journal` stores the unwrapped message, whose Message-ID is not the
    one the server reports for the journal envelope around it. The comparison a
    catch-up rests on would match nothing and it would re-fetch the entire
    folder, which is the same reason `verify` refuses these jobs outright.
    """
    return not job.delete_after_export and not job.exchange_journal


def _catch_up_if_possible(
    mb: base.MailboxClient,
    store: cas.ContentAddressedStorage,
    job: conf.JobConfig,
    folder: str,
    heads_root: pathlib.Path,
    log_root: pathlib.Path,
    places: _ArchivedPlaces,
) -> bool:
    """Bring the folder back in step by listing it. False when that is no option.

    No option means either the job is one that must not be caught up that way, or
    the archive holds nothing at this place to compare against -- in which case
    listing first would only add a round trip to a download that has to happen
    anyway.
    """
    if not _can_catch_up(job):
        return False
    archived = places.of(folder)
    if not archived:
        return False
    _catch_up_folder(mb, store, job, folder, heads_root, log_root, archived)
    return True


def _catch_up_folder(
    mb: base.MailboxClient,
    store: cas.ContentAddressedStorage,
    job: conf.JobConfig,
    folder: str,
    heads_root: pathlib.Path,
    log_root: pathlib.Path,
    archived: set[str],
) -> None:
    """Read a folder in full, downloading only what the archive does not have.

    Reached when a folder holds archived mail but no resume point -- an archive
    upgraded from a format that had none, or one whose state file was lost. The
    alternative is downloading a mailbox that is already on disk.

    The position is taken *before* the comparison, not after: a message arriving
    while it runs is then either found by the listing and archived, or left above
    the point and picked up next run. Taking the point afterwards would leave a
    window in which a message is neither.
    """
    observed_at = datetime.now(UTC)
    log.info(
        "%s::%s: no resume point but %s archived message(s) -- reconciling against"
        " the archive by Message-ID instead of downloading the folder",
        job.name,
        folder,
        f"{len(archived):,}",
    )
    resume = mb.resume_point(folder)
    result = reconcile_folder(
        mb, store, log_root, heads_root, archived, job.name, folder, repair=True
    )
    if not result.complete:
        log.warning(
            "%s::%s: %s of %s message(s) failed, resume point not started",
            job.name,
            folder,
            result.failed,
            result.missing,
        )
        resume = None
    _record_pass(heads_root, job.name, folder, observed_at, resume)


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
        store = cas.mail_store(store_path, compress=compress)
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
