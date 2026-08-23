"""Back up the selected folders, recording where each message was seen.

A backup writes the message (into the content-addressed storage) and its location
(into the log); everything else a query might want -- sender, subject, date --
stays in the message itself. Deletion after export is gated on the log being
sealed, so a message leaves the server only once its location is durable. With
`--index-db` it also refreshes a queryable `index.db` projection afterwards
(see `mailvault.jobs.db`).
"""

from __future__ import annotations

import collections.abc
import dataclasses
import logging
import pathlib
from datetime import UTC, datetime
from typing import Any

from mailvault import conf, mailutils, utils
from mailvault.backend import base, session
from mailvault.jobs.common import seal_log
from mailvault.jobs.db import DEFAULT_QUERY_DB_NAME, refresh_db
from mailvault.jobs.migration import migrate_archive
from mailvault.jobs.reconcile import ArchivedPlaces, reconcile_folder
from mailvault.store import cas, heads, metalog

log = logging.getLogger(__name__)


def _resume_point(
    heads_root: pathlib.Path,
    job_name: str,
    folder: str,
) -> dict[str, Any] | None:
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
    resume: dict[str, Any] | None,
    void_previous: bool = False,
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

    `void_previous` is the one case where the old point must go: the source
    itself declared it dead. Keeping it then is not caution but a loop -- the
    next run offers the same dead point, the source rejects it again, and the
    whole folder is reconciled from scratch every night, for good. Nothing is
    lost by forgetting it, because a point the source refuses buys no coverage.

    A head that cannot be written is logged and otherwise tolerated. The folder
    is simply fetched again next time, which the storage absorbs; aborting here
    would instead cost the remaining folders of the run for a failure that has
    no effect on the archived mail.
    """
    head = heads.read(heads_root, job_name, folder) or heads.Head(job=job_name, folder=folder)
    head.last_run = last_run.isoformat()
    if resume is not None:
        head.resume = resume
    elif void_previous and head.resume is not None:
        log.info(
            "%s::%s: the resume point the source rejected is forgotten,"
            " so the next run does not offer it again",
            job_name,
            folder,
        )
        head.resume = None
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


@dataclasses.dataclass
class BackupReport:
    """What a run of this job came to, counted as it goes.

    Filled in through the pass rather than assembled at the end, because it has
    to survive a job that fails part way through: what was written before the
    failure is written, the projection that comes after must not skip it, and a
    run that ended badly still owes its caller an account of what it did manage.

    `folders` is how many were read, `with_mail` how many of them had anything
    new -- the difference is the ordinary shape of an incremental run and not a
    shortfall. `deleted` counts messages removed from their source afterwards,
    which only a job with `delete_after_export` does at all.

    `seen`, `stored` and `present` are the three that answer "why only two, I
    had more mail than that". A message the pass looked at is either new to the
    archive or already in it, and mail that moves between folders is the second
    while looking every bit like the first: it is offered again at its new
    place, and a run that counted it as stored would report a busy night for
    mail it has held for months. They do not have to add up to `seen` -- a
    backend may pass over a message without either, an exchange journal item
    that is not one being the case in hand -- and forcing them to would mean
    inventing a number rather than reporting one.

    `failed` and `retried` are the two ways a pass falls short, and they are
    separate because they count different things: messages the server offered
    and the archive could not take, and folders whose resume point therefore
    did not move -- including one that could not be read at all. Both mean the
    same for the next run, which reads those folders again; only the second is
    a list, because a folder is worth naming and a message is not.
    """

    folders: int = 0
    with_mail: int = 0
    seen: int = 0
    stored: int = 0
    present: int = 0
    deleted: int = 0
    failed: int = 0
    retried: list[str] = dataclasses.field(default_factory=list)

    @property
    def complete(self) -> bool:
        """True when every folder was read to the end and everything seen was stored."""
        return not self.failed and not self.retried


def _backup_to_log(
    mb: base.MailboxClient,
    store: cas.ContentAddressedStorage,
    job: conf.JobConfig,
    store_path: pathlib.Path,
    report: BackupReport,
    places: ArchivedPlaces,
    incremental: bool = True,
) -> None:
    """Back up the selected folders, recording locations and resume state."""
    heads_root = store_path / heads.DEFAULT_HEADS_DIR
    log_root = store_path / metalog.DEFAULT_LOG_DIR
    folders = job.folders if job.folders else mb.folders()
    for folder in folders:
        report.folders += 1
        try:
            if _backup_folder(
                mb,
                store,
                job,
                folder,
                heads_root,
                log_root,
                places,
                report,
                incremental,
            ):
                report.with_mail += 1
        except Exception as exc:
            # Nothing of this folder is durable, so its resume point stands
            # where it stood and the next run comes back to it. Said here
            # rather than in the report, which counts what a pass came to and
            # not why -- the reason is the line below.
            report.retried.append(folder)
            # One folder that cannot be read must not cost the remaining ones;
            # its snapshot simply does not advance and the next run tries again.
            # A diagnosed failure -- the server said what was wrong -- is one
            # line. Anything else brings its stack: this catch is wide enough to
            # swallow a bug in this program, and then the line alone says
            # "backup failed: list index out of range" and nothing about where.
            log.error(
                "%s::%s: backup failed: %s",
                job.name,
                folder,
                exc,
                exc_info=not isinstance(exc, base.MailboxError),
            )
    _empty_trash(mb, job)


def _empty_trash(mb: base.MailboxClient, job: conf.JobConfig) -> None:
    """Finish off the deletions of this job, once every folder has been purged.

    What a provider like Gmail keeps in its trash folder arrives there during
    `purge`, which runs per folder after the seal -- so this belongs after the
    loop, not in it. Emptied any earlier it clears what an earlier run left and
    keeps what this one just deleted, and the mailbox stays full.

    A failure costs nothing but server space: the messages are archived and their
    locations durable, and the next run empties the folder again.
    """
    if not job.delete_after_export:
        return
    try:
        mb.empty_trash()
    except Exception as exc:
        log.error("%s: trash not emptied: %s", job.name, exc)


def _backup_folder(
    mb: base.MailboxClient,
    store: cas.ContentAddressedStorage,
    job: conf.JobConfig,
    folder: str,
    heads_root: pathlib.Path,
    log_root: pathlib.Path,
    places: ArchivedPlaces,
    report: BackupReport,
    incremental: bool = True,
) -> bool:
    """Back up one folder, recording where its messages were seen.

    Returns whether anything was written down. An incremental pass over a folder
    that has had no mail since the last run records nothing, and that answer is
    what spares the query database a refresh with nothing to take in. What the
    pass came to goes into `report` on the way, which is a different question:
    a folder can store nothing and still have gone perfectly well.

    The resume point is the backend's to make and the backend's to read; nothing
    here looks inside it. `--full` (`incremental=False`) simply withholds it,
    which is what makes a full pass authoritative: a backend given no point has
    nothing to hold itself back from and records exactly what it found.
    """
    previous = _resume_point(heads_root, job.name, folder) if incremental else None
    if previous is None and incremental:
        caught_up = _catch_up_if_possible(
            mb, store, job, folder, heads_root, log_root, places, report
        )
        if caught_up is not None:
            return caught_up

    observed_at = datetime.now(UTC)
    void = False
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
        # From here on the stored point is dead whatever happens next: every
        # path below either earns a new one or must forget this one.
        void = True
        caught_up = _catch_up_if_possible(
            mb, store, job, folder, heads_root, log_root, places, report, void_previous=True
        )
        if caught_up is not None:
            return caught_up
        log.info("%s::%s: reading the folder in full", job.name, folder)
        result = mb.folder_backup(
            folder,
            store,
            resume=None,
            callback=_location_writer(log_writer),
        )
    # Counted from the pass that stands, not from the one the source refused:
    # a void resume point makes the first call do nothing, and the read that
    # replaces it is the one that fetched the mail.
    report.seen += result.total
    report.stored += result.stored - result.present
    report.present += result.present
    report.failed += result.failed
    # Asked before the seal empties the writer: how much this pass observed, and
    # therefore whether the log grew at all.
    recorded = len(log_writer) > 0
    # The seal date stamps the log with when the folder was read, which is a
    # fact about the run and stays the wall clock. Where the *next* run resumes
    # is a claim about coverage, and that one comes back from the backend.
    sealed = seal_log(log_writer, observed_at)
    resume: dict[str, Any] | None = None
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
            "%s::%s: %s of %s failed, resume point not advanced",
            job.name,
            folder,
            result.failed,
            utils.counted(result.total, "message"),
        )
        report.retried.append(folder)
    else:
        # Downloads were clean but the location log did not reach disk. Holding
        # the point back re-fetches the folder next run and writes the log
        # again, rather than advancing past locations that were never recorded.
        log.warning(
            "%s::%s: metadata log not sealed, resume point not advanced",
            job.name,
            folder,
        )
        report.retried.append(folder)
    # Written whatever the outcome: the folder *was* read, and that is all
    # `last_run` claims. Only `resume` is held back when the pass fell short.
    _record_pass(heads_root, job.name, folder, observed_at, resume, void_previous=void)
    _purge_after_seal(mb, job, folder, result, sealed, report)
    return recorded


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
    places: ArchivedPlaces,
    report: BackupReport,
    void_previous: bool = False,
) -> bool | None:
    """Bring the folder back in step by listing it, and say whether it wrote.

    None when this was no option at all -- either the job is one that must not be
    caught up that way, or the archive holds nothing at this place to compare
    against, in which case listing first would only add a round trip to a
    download that has to happen anyway. The caller then goes on to the ordinary
    pass.

    True or False when it ran, saying whether anything reached the log. Three
    answers and not two, because "did not run" and "ran and found nothing new"
    lead in opposite directions: the first means carry on, the second means this
    folder is done and had nothing to add.

    The head is asked before the log is, and that is the whole point of asking
    it. Finding out whether a place holds anything used to mean reading the
    metadata log entire -- every file of it, once per job, as soon as a single
    folder turned up without a resume point. A folder that is empty on the
    server never earns one, so this happened on every run, for good: four such
    folders in one measured run cost 2.6 of its 12.5 seconds, and the log grows
    between compactions while they stay empty.

    A head with no chain pointer is the cheap answer. This program wrote that
    head, so it has passed over the place before and recorded nothing in the log
    for it -- there is nothing here to compare a listing against. Where there is
    no head at all the question stays open, and the log is read as before: an
    archive whose `heads/` was lost is exactly what the catch-up exists for.
    """
    if not _can_catch_up(job):
        return None
    head = heads.read(heads_root, job.name, folder)
    if head is not None and head.log is None:
        return None
    archived = places.of(job.name, folder)
    if not archived:
        return None
    return _catch_up_folder(
        mb,
        store,
        job,
        folder,
        heads_root,
        log_root,
        archived,
        report,
        void_previous=void_previous,
    )


def _catch_up_folder(
    mb: base.MailboxClient,
    store: cas.ContentAddressedStorage,
    job: conf.JobConfig,
    folder: str,
    heads_root: pathlib.Path,
    log_root: pathlib.Path,
    archived: set[str],
    report: BackupReport,
    void_previous: bool = False,
) -> bool:
    """Read a folder in full, downloading only what the archive does not have.

    Returns whether anything reached the log, which for this pass means whether
    anything was fetched at all: every message it stores gets its place written
    down in the same breath.

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
        "%s::%s: no resume point but %s -- reconciling against"
        " the archive by Message-ID instead of downloading the folder",
        job.name,
        folder,
        utils.counted(len(archived), "archived message"),
    )
    resume = mb.resume_point(folder)
    result = reconcile_folder(
        mb, store, log_root, heads_root, archived, job.name, folder, repair=True
    )
    # A catch-up stores what the folder was missing, and the copies that turned
    # out to differ after all. Both are mail that was not in the archive before
    # this pass, which is what the count outside means -- and everything the
    # listing matched to something already archived is what `present` means,
    # whether it was matched once or three times over.
    report.seen += result.on_server
    report.stored += result.restored + result.recovered_copies
    report.present += result.on_server - result.missing
    report.failed += result.failed
    if not result.complete:
        log.warning(
            "%s::%s: %s of %s failed, resume point not started",
            job.name,
            folder,
            result.failed,
            utils.counted(result.missing, "message"),
        )
        resume = None
        report.retried.append(folder)
    elif not result.sealed:
        # Downloads were clean but the locations did not reach disk. The same
        # rule as the ordinary pass in `_backup_folder`: holding the point back
        # reads the folder again next run and writes the log again, where
        # advancing would push messages whose place was never recorded out of
        # everything a later run asks for.
        log.warning(
            "%s::%s: metadata log not sealed, resume point not started",
            job.name,
            folder,
        )
        resume = None
        report.retried.append(folder)
    _record_pass(heads_root, job.name, folder, observed_at, resume, void_previous=void_previous)
    return result.restored + result.recovered_copies > 0


def _purge_after_seal(
    mb: base.MailboxClient,
    job: conf.JobConfig,
    folder: str,
    result: base.BackupResult,
    sealed: bool,
    report: BackupReport,
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
            "%s::%s: metadata log not sealed, %s left on the server",
            job.name,
            folder,
            utils.counted(len(result.deletable), "message"),
        )
        return
    try:
        mb.purge(folder, result.deletable)
    except Exception as exc:
        # The log is already durable, so a failed purge costs nothing but server
        # space: the messages stay and are deleted on the next clean run.
        log.error("%s::%s: purge failed: %s", job.name, folder, exc)
        return
    report.deleted += len(result.deletable)


def backup(
    job: conf.JobConfig,
    store_path: pathlib.Path,
    compress: bool = False,
    index_db: bool = False,
    incremental: bool = True,
    places: ArchivedPlaces | None = None,
) -> BackupReport:
    """Back up one job's folders into the archive at `store_path`.

    `compress` stores the messages zstd-compressed; `index_db` refreshes the
    queryable `index.db` projection in the archive once the backup is done;
    `incremental` resumes each folder from its snapshot instead of re-fetching it.

    What the run came to comes back, for the caller to say out loud: the mail is
    in the archive either way, but a run whose whole account of itself was the
    log left the person who started it to read a night's worth of lines to find
    out whether anything is missing -- and left a script no way of asking at all.

    **The projection is only refreshed when this job wrote something down.** A
    job that had no new mail has nothing to add to it, and finding that out costs
    a listing of the whole metadata log directory -- 3.9 seconds per job over a
    network share, measured, for zero new messages, once for every job in the
    configuration. The information was there all along; it was simply not asked
    for.

    Asked in a `finally`, and counted as the folders go rather than returned at
    the end, so that a job which fails part way through still refreshes what it
    managed to write. "Failed" does not mean "wrote nothing", and the projection
    skipping those messages until some later run happened to pick them up was an
    accident rather than a decision.

    Migrating a legacy archive happens first, before the mailbox is opened: it is
    a purely local operation that can take a while on a large `store.db`, and
    there is no reason to hold a server connection open across it. Doing it here
    also puts the "migrating, this may take a moment" line right after the job
    starts, rather than after a silent connect.
    """
    migrate_archive(store_path)
    report = BackupReport()
    if places is None:
        places = ArchivedPlaces(store_path / metalog.DEFAULT_LOG_DIR)
    try:
        with session.open_mailbox(job) as mb:
            store = cas.mail_store(store_path, compress=compress)
            _backup_to_log(mb, store, job, store_path, report, places, incremental=incremental)
    finally:
        if index_db and report.with_mail:
            _refresh_query_db(store_path)
        elif index_db:
            log.debug(
                "%s: nothing was recorded, the query database has nothing to take in",
                job.name,
            )
    return report


def _refresh_query_db(store_path: pathlib.Path) -> None:
    """Keep the queryable projection beside the archive up to date, tolerantly.

    A convenience only: a failure to update it is logged and never allowed to
    fail the backup, whose real output -- the messages and the log -- is already
    written. `refresh_db` builds the projection when it is missing or unreadable,
    and leaves one written by another version alone -- having already said so, in
    which case there is nothing to report here but the absence of a report. A
    line claiming the database was updated would be worse than silence.
    """
    db_path = store_path / DEFAULT_QUERY_DB_NAME
    name = utils.under(store_path, db_path)
    try:
        result = refresh_db(store_path, db_path)
    except Exception as exc:
        log.error("%s: query database not updated: %s", name, exc)
        return
    if result.unreadable:
        return
    if result.rebuilt:
        log.info(
            "%s: query database built, %s",
            name,
            utils.counted(result.messages, "message"),
        )
    else:
        # Two numbers, because the first one alone gets read as "two mails
        # arrived" -- the obvious sense of a line at the end of a backup, and
        # not what it says. What it counts is rows the projection gained, and a
        # message filed into a second folder gains none while gaining a place.
        # The places are the number that moves in step with the run.
        log.info(
            "%s: query database updated, %s new to it, %s recorded from %s",
            name,
            utils.counted(result.messages, "message"),
            utils.counted(result.applied, "place"),
            utils.counted(result.files, "log file"),
        )
