"""`folders`, `backup` and `verify` -- the commands that talk to a mailbox.

The only three that need a configuration and a login. What they have in common is
the shape of a run: pick the jobs, check them against the archive, work one
mailbox after another, and report per job so that a failure in one says which one.
"""

from __future__ import annotations

import argparse
import logging
import pathlib

from mailvault import conf, jobs, utils
from mailvault.cli.common import (
    DEFAULT_CONFIG_NAME,
    EXPECTED_ERRORS,
    archive_path,
    config_file,
    report_items,
    require_archive,
)
from mailvault.jobs import guard
from mailvault.store import metalog

log = logging.getLogger(__name__)


# The commands that work on an archive directory, as opposed to `folders`, which
# only ever talks to the server.
ARCHIVE_COMMANDS = {"backup", "verify"}


def report_folders(job_name: str, folders: list[str]) -> None:
    """Name every folder of the job, one per line, as `job::folder`.

    The answer to a question and nothing else: no count, no heading, nothing
    that would have to be filtered back out. `folders | grep` is what this is
    for, and the form is the one every other command takes back.
    """
    for folder in folders:
        print(f"{job_name}::{folder}")


def report_backup(job_name: str, report: jobs.BackupReport) -> int:
    """Say what the run took in, and whether it got through everything.

    Three numbers where one would do, and the two extra ones are the point.
    "2 stored" on its own is read as "two mails arrived", and the night that
    prompted this said exactly that after a folder had been tidied on the
    server: six messages offered, four of them mail from months ago that had
    been filed into another folder and was being shown again at its new place.
    Every number was right and the reader was left to work that out by adding
    up nine lines and knowing what `EXISTS` means. `6 seen, 2 stored, 4 already
    archived` is the same run saying it itself.

    Not shown: how many folders had anything. It answers no question a reader
    of this line is asking, and the counts above already separate a quiet night
    from a busy one. A run that saw nothing at all is the exception, because
    then there is nothing else to say and how much was looked through is the
    whole statement.

    A folder that fell short is named, because it is the one thing here that a
    reader can act on: it is read again next run, and if the same name keeps
    coming back, that is the folder to look into. The messages that failed are
    counted and not named -- there is nothing to look up, and their folders are
    on the lines below.
    """
    if not report.folders:
        print(f"{job_name}: no folders to read")
        return 0
    if report.seen:
        line = (
            f"{job_name}: {utils.counted(report.seen, 'message')} seen,"
            f" {report.stored:,} stored, {report.present:,} already archived"
        )
    elif report.complete:
        line = f"{job_name}: nothing new in {utils.counted(report.folders, 'folder')}"
    else:
        # "nothing new" is a statement about the mailbox and would be a lie
        # here: a run whose folders fell over stored nothing and found out
        # nothing either. What is left to say is what it did, not what was
        # there -- and the lines below say why.
        line = f"{job_name}: nothing stored in {utils.counted(report.folders, 'folder')}"
    if report.deleted:
        line += f", {utils.counted(report.deleted, 'message')} removed from the server"
    print(line)
    if report.failed:
        print(f"{utils.counted(report.failed, 'message')} could not be stored")
    report_items(report.retried, "folder", "not finished -- read again next run")
    return 0 if report.complete else 1


def report_verify(job_name: str, results: list[jobs.VerifyResult], repaired: bool) -> int:
    """Say what each folder turned out to hold, and whether anything is missing.

    The exit code says what the last line says, and nothing besides. An archive
    with a gap in it ends non-zero whether or not `--repair` was asked to close
    it -- the same answer `archive check` gives to a message the log names and
    the archive does not have, and for the same reason: a run that found the
    thing it exists to find must not look like one that found nothing.

    What it does *not* count is the extra copies, nor a download of one that
    failed. They are duplicates of mail already archived, a folder can hold
    thousands, and a run ending non-zero over them every night would teach its
    owner to stop reading the exit code -- which is the one thing that must not
    happen to it. The rule holds in both directions: a count kept out of the
    verdict is kept out of the exit code too.

    Two counts where there used to be one, and the whole point of the second is
    that it is *not* added to the first. A folder can hold the same message
    twice, byte for byte; the archive is addressed by content and holds it once,
    so every copy after the first is a message the server has and the archive
    cannot separately have. Counted as missing, it made a complete archive report
    thousands of gaps after every run, for good -- and the summary line told its
    owner to run `--repair`, which fetched all of them and changed nothing. A
    number that is always there and never means anything is one a reader learns
    to skip, and the next one along with it.

    So the extra copies are named where there are any, and left out of both the
    verdict and the advice. `verify` may now say an archive is complete while
    still reporting a few thousand of them, and that is exactly the statement
    intended: nothing is missing, and the server keeps more copies than a
    content-addressed store has any way of keeping.
    """
    for r in results:
        line = f"{job_name}::{r.folder}: {r.on_server:,} on server, {r.missing:,} not archived"
        if r.extra_copies:
            copies = utils.counted(r.extra_copies, "further copy", "further copies")
            line += f", {copies} of mail already archived"
        # Only where the pass had something to fetch. A folder with nothing
        # missing reporting "0 restored" is a number answering a question nobody
        # asked, on every line of every repair run.
        if repaired and (r.missing or r.extra_copies):
            line += f", {r.restored:,} restored"
            if r.recovered_copies:
                line += f", {r.recovered_copies:,} of the further copies differed and were kept"
            if r.failed:
                line += f", {r.failed:,} failed"
        print(line)
    total_missing = sum(r.missing for r in results)
    total_extra = sum(r.extra_copies for r in results)
    total_restored = sum(r.restored for r in results)
    total_recovered = sum(r.recovered_copies for r in results)
    unsealed = [f"{job_name}::{r.folder}" for r in results if not r.sealed]
    if not total_missing:
        line = f"{job_name}: archive is complete"
        if total_extra:
            # Named in the verdict too, because a reader who sees "complete"
            # after a run that fetched thousands of messages is owed the reason
            # in the same breath, not three lines further up.
            copies = utils.counted(total_extra, "further copy", "further copies")
            line += (
                f" -- {copies} of mail already archived, which a deduplicating"
                f" archive holds once"
            )
        print(line)
    elif not repaired:
        missing = utils.counted(total_missing, "message")
        print(f"{job_name}: {missing} missing, run again with --repair")
    else:
        missing = utils.counted(total_missing, "message")
        line = f"{job_name}: {total_restored:,} of {missing} restored"
        if total_recovered:
            copies = utils.counted(total_recovered, "further copy", "further copies")
            line += f", plus {copies} that really did differ"
        print(line)
    # After the verdict rather than before it, because this qualifies whatever
    # the verdict said: the mail is in the archive and nothing records where it
    # belongs, so a folder counted as complete is not yet finished. Ending on
    # the line that names the way out is the point of putting it last.
    report_items(
        unsealed,
        "folder",
        "whose metadata log was not written -- what was fetched is in the archive"
        " with nothing recording where it came from, run again",
    )
    outstanding = total_missing - total_restored if repaired else total_missing
    return 0 if outstanding <= 0 and not unsealed else 1


def _run_job(
    job: conf.JobConfig,
    args: argparse.Namespace,
    config: conf.Config,
    destination: pathlib.Path | None = None,
    places: jobs.ArchivedPlaces | None = None,
) -> int:
    """Run one job, and say whether it got through what it was asked to do.

    The answer comes back as an exit code rather than being printed here,
    because it is the run as a whole that is sound or not: a configuration with
    four jobs where one folder fell short has to end non-zero, whatever the
    other three did. A run that reported only in words left cron with nothing
    to react to and a script no way of asking.
    """
    log.info("Job: %s", job.name)

    if args.command == "folders":
        report_folders(job.name, jobs.folder_list(job))
    elif destination is None:
        # `run_mailbox` resolves the archive for every command that works on one,
        # so this does not happen from the CLI. It is here because the argument
        # may be absent and running the job anyway would mean guessing where.
        raise jobs.JobError(f"{args.command}: no archive directory")
    elif args.command == "backup":
        compress = args.compress or config.compress
        index_db = args.index_db or config.index_db
        # The one switch that turns something off rather than on, so it cannot
        # follow the `args.x or config.x` pattern of the two above: `--full` is
        # a veto on the configured default, not an addition to it.
        incremental = config.incremental and not args.full
        return report_backup(
            job.name,
            jobs.backup(
                job,
                destination,
                compress=compress,
                index_db=index_db,
                incremental=incremental,
                places=places,
            ),
        )
    elif args.command == "verify":
        compress = args.compress or config.compress
        results = jobs.verify(
            job, destination, repair=args.repair, compress=compress, places=places
        )
        return report_verify(job.name, results, repaired=args.repair)
    return 0


def run(args: argparse.Namespace) -> int:
    """Run a folders/backup/verify command over the selected config jobs."""
    exit_code = 0
    needs_archive = args.command in ARCHIVE_COMMANDS
    if needs_archive and args.config is not None and args.archive is None:
        # Reaching for a configuration somewhere else is what somebody does who
        # is *not* standing in the archive, so the directory they happen to be in
        # is the last thing that should decide where the mail goes. Nothing else
        # is left to derive it from, so this asks instead of guessing.
        raise conf.ConfigError(
            f"{args.config}: a configuration was named, but no archive -- name that "
            f"too, with --archive"
        )

    archive = archive_path(args)
    if needs_archive:
        require_archive(archive)
    path = config_file(args, archive)
    try:
        config = conf.load(path, allow_exec=args.allow_exec)
    except conf.ConfigError:
        # Naming the file that was looked for is not enough when nobody asked
        # for it: a reader is left wondering why that path of all paths. What
        # they need to be told is the rule that produced it. Only for a file
        # that is not there -- a broken one in the archive keeps its own
        # message, which says what is wrong with it.
        if args.config is not None or path.exists():
            raise
        raise conf.ConfigError(
            f"no {DEFAULT_CONFIG_NAME} here -- an archive carries its own"
            f" configuration. Stand in the archive, name it with --archive, or name a"
            f" configuration with --config"
        ) from None
    selected = config.jobs
    if args.job:
        selected = [j for j in selected if j.name in args.job]
        unknown = set(args.job) - {j.name for j in selected}
        for name in unknown:
            log.error("Unknown job: %s", name)
            exit_code = 1

    # This comes before the first job, and deliberately so: it decides whether
    # this configuration and this archive belong together, and the answer is
    # worth nothing once a message has been written -- or, with
    # `delete_after_export`, removed from the server.
    destination = None
    places = None
    if needs_archive:
        destination = archive
        guard.check_jobs(destination, selected, allow_new=args.allow_new_mailbox)
        # One archive, one metadata log, one reading of it -- however many jobs
        # the configuration names. Every job used to read all of it to keep the
        # part that is theirs.
        places = jobs.ArchivedPlaces(destination / metalog.DEFAULT_LOG_DIR)

    for job in selected:
        # One broken job must not stop the remaining ones, but the run as a
        # whole reports failure so callers/cron can react.
        try:
            exit_code |= _run_job(job, args, config, destination, places)
        except EXPECTED_ERRORS as exc:
            # A misconfigured or refused job is a user error, not a crash --
            # reported as one line here for the same reason `main` does it.
            log.error("Job '%s' failed: %s", job.name, exc)
            log.debug("Job '%s' failed", job.name, exc_info=exc)
            exit_code = 1
        except BrokenPipeError:
            # `mailvault verify | head -1`: nobody is reading the report any
            # more. Every job after this one would write into the same closed
            # pipe and fail the same way, so the run ends here and `main` decides
            # what to make of it.
            raise
        except Exception as exc:
            log.exception("Job '%s' failed: %s", job.name, exc)
            exit_code = 1
    return exit_code


# --- archive -------------------------------------------------------------------
