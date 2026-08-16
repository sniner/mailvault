"""Command handlers for the `mailvault` CLI.

Each `run_*` function is the dispatch target for one command group in
`mailvault.cli.main`; the argument parsing lives there, the actual work in
`mailvault.jobs`.
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import json
import logging
import pathlib
import sys

from mailvault import conf, jobs, utils
from mailvault.backend import base
from mailvault.jobs import guard
from mailvault.store import cas, heads, marker, metalog

log = logging.getLogger(__name__)

# Failures that are already understood by the time they get here: a broken
# config, a refused operation, a mailbox that said no. There is nothing to
# debug in them, so they are reported as one line and the traceback is left to
# the errors nobody anticipated -- where the call stack is the only clue. The
# traceback is still there under `--verbose` for the rare case it is wanted.
EXPECTED_ERRORS = (conf.ConfigError, jobs.JobError, base.MailboxError, marker.FormatError)

# The commands that work on an archive directory, as opposed to `folders`, which
# only ever talks to the server.
ARCHIVE_COMMANDS = {"backup", "verify"}

# The configuration an archive carries. Named after the tool rather than after
# its purpose -- `config.toml` would be the better name inside an archive, where
# nothing else competes for it, but this is the same file in both roles, and the
# other role is the directory one happens to be standing in. That is a shared
# name space: a `config.toml` there belongs to whatever else lives in that
# directory, and reading one by accident is not a theoretical worry.
DEFAULT_CONFIG_NAME = "mailvault.toml"

# The query database, inside the archive. Named here as well as in `jobs.db`
# because the help texts talk about it and a help text that names a different
# file than the code writes is worse than one that names none.
DEFAULT_DB_NAME = jobs.DEFAULT_QUERY_DB_NAME


def archive_path(args: argparse.Namespace) -> pathlib.Path:
    """The archive a command works on: `--archive`, or the directory one is in.

    Two independent knobs, and nothing derived between them -- this is the only
    place an archive comes from. A configuration used to be able to name one,
    which cannot work across machines: the NAS hangs at a different path on each
    of them while the configuration sits in a home directory, so there is no
    path that is right on both. A configuration *inside* the archive has that
    distance by construction, and then there is nothing left for it to say.
    """
    if args.archive is not None:
        return args.archive
    return pathlib.Path.cwd()


def config_file(args: argparse.Namespace, archive: pathlib.Path) -> pathlib.Path:
    """The configuration to read: `--config`, or the one the archive carries."""
    if args.config is not None:
        return args.config
    return archive / DEFAULT_CONFIG_NAME


# The two commands that are allowed to meet a directory that is not an archive
# yet: one makes an archive out of it, the other lifts an older one into this
# layout. Everything else has an archive as its subject and says so.
WITHOUT_AN_ARCHIVE = {"init", "migrate"}


def require_archive(archive: pathlib.Path) -> None:
    """Stop a command that was pointed at something which is not an archive.

    The mark is the whole test, the way `.git` is for a repository. Before this,
    every command opened `<directory>/mail` and worked on whatever it found --
    which on an archive from before 0.10 is nothing at all, because the messages
    are still in the root. `archive check` then reported a healthy 131,000-message
    archive as a total loss, and `verify --repair` set about downloading the
    mailbox a second time.

    Both cases the mark cannot tell apart get named, because the answer differs:
    an older archive is lifted, a wrong directory is left alone.
    """
    if marker.is_archive(archive):
        return
    raise jobs.JobError(
        f"{archive}: not a mailvault archive. Make one here with"
        f" `mailvault archive init`. If it is an old mailvault archive,"
        f" migrate it with `mailvault archive migrate`"
    )


# --- folders / backup / verify -------------------------------------------------


def report_verify(job_name: str, results: list[jobs.VerifyResult], repaired: bool) -> None:
    """Say what each folder turned out to hold, and whether anything is missing.

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


def _run_job(
    job: conf.JobConfig,
    args: argparse.Namespace,
    config: conf.Config,
    destination: pathlib.Path | None = None,
    places: jobs.ArchivedPlaces | None = None,
) -> None:
    log.info("Job: %s", job.name)

    if args.command == "folders":
        jobs.folder_list(job)
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
        jobs.backup(
            job,
            destination,
            compress=compress,
            index_db=index_db,
            incremental=incremental,
            places=places,
        )
    elif args.command == "verify":
        compress = args.compress or config.compress
        results = jobs.verify(
            job, destination, repair=args.repair, compress=compress, places=places
        )
        report_verify(job.name, results, repaired=args.repair)


def run_mailbox(args: argparse.Namespace) -> int:
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
            _run_job(job, args, config, destination, places)
        except EXPECTED_ERRORS as exc:
            # A misconfigured or refused job is a user error, not a crash --
            # reported as one line here for the same reason `main` does it.
            log.error("Job '%s' failed: %s", job.name, exc)
            log.debug("Job '%s' failed", job.name, exc_info=exc)
            exit_code = 1
        except Exception as exc:
            log.exception("Job '%s' failed: %s", job.name, exc)
            exit_code = 1
    return exit_code


# --- archive -------------------------------------------------------------------


def _external(path: pathlib.Path, docuware: bool = False) -> jobs.ExternalMailArchive:
    """A directory of mails read from the outside, whichever layout it has."""
    cls = jobs.DocuwareMailArchive if docuware else jobs.ExternalMailArchive
    return cls(path)


def _human_size(size: int) -> str:
    units = ["bytes", "KiB", "MiB", "GiB", "TiB"]
    value = float(size)
    for unit in units[:-1]:
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    # The largest unit is where it stops, however big the number gets.
    return f"{value:.1f} {units[-1]}"


def report_init(archive: pathlib.Path, result: jobs.InitResult) -> int:
    """Say that there is an archive here now, and what is left to do.

    Which directories were made is the archive's own business -- `git init` says
    it made a repository, not which files went into `.git`. What the reader does
    need is the one thing still missing: an archive nobody has configured cannot
    back anything up.
    """
    print(f"{archive}: {'archive created' if result.created else 'already an archive'}")
    if result.config is not None:
        if result.config_existed:
            print(f"{result.config.name} is already there and was left alone")
        else:
            print(f"{result.config.name} written -- fill in your mailboxes, then back up")
    return 0


def report_migration(source: pathlib.Path, result: jobs.MigrationResult) -> int:
    """Say what was lifted, and what the archive is now.

    Every step is named even where it moved nothing. A migration that says only
    what it happened to find leaves a reader unable to tell "there was nothing
    to do" from "that part did not run".
    """
    if result.generation == marker.CURRENT_FORMAT:
        print(f"already {marker.describe(marker.CURRENT_FORMAT)}, nothing to do")
        return 0

    points = utils.counted(result.resume_points, "resume point")
    print(f"{points} moved into {heads.DEFAULT_HEADS_DIR}/")
    if not result.needed:
        print("no metadata database, nothing to move out of one")
        return _report_rest(source, result)
    if not result.verified:
        print("the written log files did not verify, database left alone")
        return 1
    print(
        f"{utils.counted(result.messages, 'message')} moved into"
        f" {utils.counted(result.places, 'mailbox/folder place')}"
    )
    if result.snapshots:
        print(
            f"{utils.counted(result.snapshots, 'resume timestamp')} taken from the"
            f" database -- as a record of when, never as a point to carry on from"
        )
    if result.placeless:
        print(
            f"{result.placeless:,} of them recorded without a folder -- the old "
            f"database did not store which folder of which mailbox"
        )
    if result.undecidable:
        print(
            f"{utils.counted(result.undecidable, 'folder name')} could fit more than "
            f"one mailbox, left out rather than guessed"
        )
    if result.renamed_to is not None:
        print(f"the database is now {result.renamed_to.name} and is no longer used")
        print("delete it once you are satisfied with the archive")
    return _report_rest(source, result)


def _report_rest(source: pathlib.Path, result: jobs.MigrationResult) -> int:
    """The steps after the two older formats gave up what they held."""
    print(f"{utils.counted(result.shards_moved, 'shard')} moved into {cas.MAIL_DIR}/")
    if result.consolidated is not None:
        report_compact(result.consolidated)
    return _report_generation(source)


def _report_generation(source: pathlib.Path) -> int:
    """Say what the archive says about itself now, or why it says nothing yet.

    The mark is the verdict, and that is why it is also the exit code: it is
    written last, so an archive that carries it got through every step. A cron
    job whose migration stopped half way must not be told the run went well.
    """
    now = marker.read(source)
    if now == marker.CURRENT_FORMAT:
        print(f"{marker.describe(now)}")
        return 0
    print(
        "NOT marked -- something above did not finish, so the next"
        " run picks the migration up again"
    )
    return 1


def report_create_db(target: pathlib.Path, result: jobs.RebuildResult) -> int:
    """Say what went into the database, and name what could not.

    The first line says where its number comes from, and that is not decoration.
    It used to be the count of a walk over the whole store and is now the count
    of what the log accounts for -- the same command, the same wording, and on an
    archive with mail nothing records a place for, a smaller number. Somebody
    comparing two runs across that change has to be able to see why.
    """
    replay = result.replay
    if replay.files:
        print(
            f"{utils.counted(result.messages, 'message')} named by"
            f" {utils.counted(replay.files, 'log file')},"
            f" {replay.applied:,} of {utils.counted(replay.entries, 'location')} applied"
        )
        if replay.unknown:
            print(
                f"{utils.counted(replay.unknown, 'log entry', 'log entries')} about "
                f"mail that is not in the archive, ignored"
            )
    else:
        # An empty database, and the reason has a move: an archive built entirely
        # from imports made before an import recorded what it brought in has
        # nothing here, and everything in it is waiting to be taken in.
        print("no metadata log -- nothing names a message, so the database is empty")
        print("  `archive check` says what lies here, `archive adopt` gives it a place")
    print(f"{target.name}: written")
    return 0


def report_update_db(target: pathlib.Path, result: jobs.RefreshResult) -> int:
    """Say what the update took in, and stay quiet when there was nothing.

    Non-zero for a database this version cannot read: it was not updated, and a
    caller that only reads the exit code must not file that as a success.
    """
    if result.outdated:
        # `refresh_db` has already said what it is and what to do about it.
        return 1
    if result.rebuilt:
        messages = utils.counted(result.messages, "message")
        print(f"{target.name}: built from the whole archive, {messages}")
        return 0
    if not result.files:
        print(f"{target.name}: already up to date")
        return 0
    # Both numbers, because they come apart and the difference is the whole
    # answer. A log file about mail the database already had -- what `archive
    # adopt` writes, or a folder read in full a second time -- records locations
    # and adds no message, and "0 messages added" on its own reads like a run
    # that did nothing.
    print(
        f"{target.name}: {utils.counted(result.files, 'log file')} taken in,"
        f" {utils.counted(result.applied, 'location')} recorded,"
        f" {utils.counted(result.messages, 'message')} new"
    )
    if result.unknown:
        print(
            f"{utils.counted(result.unknown, 'log entry', 'log entries')} about "
            f"mail that is not in the archive, ignored"
        )
    return 0


def report_compact(result: metalog.CompactResult) -> int:
    """Say how much the log shrank and how many duplicate entries went.

    Non-zero when the consolidated files did not verify: the log is unchanged
    and nothing was lost, but the pass did not do what it was asked, and a
    scheduler reading only the exit code would file it as a success.
    """
    if result.files_before == 0:
        print("no metadata log to compact")
        return 0
    if not result.verified:
        print("consolidated files did not verify, nothing was removed")
        return 1
    print(
        f"{utils.counted(result.files_before, 'log file')} -> {result.files_after:,} "
        f"across {utils.counted(result.places, 'place')}"
    )
    dropped = result.entries_before - result.entries_after
    if dropped:
        print(f"{utils.counted(dropped, 'duplicate observation')} dropped")
    if result.transient_removed:
        # Said out loud rather than swept up quietly: each one is a write that
        # was interrupted, and that is worth knowing about.
        leftovers = utils.counted(result.transient_removed, "leftover")
        print(f"{leftovers} of an interrupted write removed")
    return 0


# How many of a kind a report names before it stops listing them. A check on a
# damaged archive can find tens of thousands; the count is the finding, the
# names are there to give someone a place to start.
REPORT_LIMIT = 20


def _report_items(
    items: list[str],
    singular: str,
    finding: str = "",
    plural: str | None = None,
) -> None:
    """Print a finding's count and the first few of whatever it found.

    What each line names depends on what the finding is about. A message is
    named by its id, because that is what every other command takes and the
    only handle its owner has any use for; where the file happens to lie is the
    store's business. A finding *about a file* -- one that is not a message at
    all, or a log file -- names the path, because there the file is the thing.

    The count and the noun come from `utils.counted`, so what follows has to
    read the same whether there is one of them or a thousand -- which is why
    these findings say "damaged" rather than "is damaged". Where that cannot be
    had, the finding is written out in both forms instead.
    """
    if not items:
        return
    print(f"{utils.counted(len(items), singular, plural)} {finding}".rstrip())
    for item in items[:REPORT_LIMIT]:
        print(f"  {item}")
    if len(items) > REPORT_LIMIT:
        print(f"  ... and {len(items) - REPORT_LIMIT:,} more")


def report_import(
    source: pathlib.Path,
    destination: pathlib.Path,
    result: jobs.ImportResult,
) -> int:
    """Say what the import did, or what it would have done.

    Both counts are named even when one of them is zero. "How many were already
    there" is what tells a dry run apart from a disaster: a source that has been
    through a converter on its way here looks exactly like a source full of new
    mail, and the difference only shows in the ratio.

    The name is said back for the same reason it is asked for: it is the only
    handle these messages have afterwards, and a line that names it also names
    where to type it. Where fewer were recorded than read, that is the finding
    and it goes first -- the mail is in the archive either way, and it is what
    the archive knows about it that fell short.
    """
    total = result.stored + result.present
    verb = "would be imported" if result.dry_run else "imported"
    print(
        f"{utils.counted(total, 'message')} read -- {result.stored:,} {verb},"
        f" {result.present:,} already in {destination}"
    )
    # An import with no name records nothing and falls short of nothing.
    unrecorded = total - result.recorded if result.name is not None else 0
    if total and result.name is not None:
        if result.dry_run:
            print(f"they would be recorded as {result.name}")
        elif unrecorded:
            print(
                f"{unrecorded:,} of them are in the archive with nothing recording"
                f" where they came from -- the metadata log could not be written."
                f" Import the same source again under {result.name} to record them"
            )
        else:
            print(
                f"recorded as {result.name} -- `mailvault db update` takes it in,"
                f" then `mailvault db search --folder {result.name}` finds them"
            )
    _report_items(
        [utils.under(source, p) for p in result.failed], "message", "could not be read"
    )
    # A different outcome from the failures above: this mail is in the archive
    # and recorded, and what is left over is the source file --move was asked to
    # take away. Importing the same source again is harmless and removes them.
    _report_items(
        [utils.under(source, p) for p in result.undeleted],
        "source file",
        "could not be deleted, the mail is archived --"
        " import the same source again to be rid of them",
    )
    shortfall = result.failed or result.undeleted
    return 1 if shortfall or (unrecorded and not result.dry_run) else 0


# How wide the two name columns may get before they are cut. A mailbox is a host
# name and stays short; a folder can be `[Google Mail]/Alle Nachrichten` or a
# nested path, and cutting it is better than a table that wraps.
MAILBOX_WIDTH = 30
FOLDER_WIDTH = 44


def report_places(summary: metalog.LogSummary) -> int:
    """List what the archive has mail from, one line per place.

    Two name columns rather than the `mailbox::folder` the findings print,
    because these two are what the reader types into `db search`, and because an
    empty mailbox column says by itself what kind of place that is -- an import
    or what `archive adopt` took in, neither of which has a mailbox behind it.

    The total is not the column added up, and where they differ the difference is
    named: a message under three Gmail labels lies at three places and is one
    message. Two numbers that do not add up and no word about it is how a report
    sends a reader looking for a fault that is not there.
    """
    if not summary.places:
        print("no place recorded yet -- a backup records one per folder,")
        print("`archive import` and `archive adopt` one per name they are given")
        return 0

    mailboxes = [_cut(place.mailbox, MAILBOX_WIDTH) for place in summary.places]
    folders = [_cut(place.folder, FOLDER_WIDTH) for place in summary.places]
    counts = [f"{place.messages:,}" for place in summary.places]
    first = max(len("mailbox"), *(len(name) for name in mailboxes))
    second = max(len("folder"), *(len(name) for name in folders))
    third = max(len("messages"), *(len(count) for count in counts))

    print(f"{'mailbox':<{first}}  {'folder':<{second}}  {'messages':>{third}}  last seen")
    for place, mailbox, folder, count in zip(summary.places, mailboxes, folders, counts):
        day = (place.last_seen or "")[:10] or "?"
        print(f"{mailbox:<{first}}  {folder:<{second}}  {count:>{third}}  {day}")

    print(
        f"{utils.counted(len(summary.places), 'place')},"
        f" {utils.counted(summary.messages, 'message')}"
    )
    if sum(place.messages for place in summary.places) != summary.messages:
        print("  the column adds up to more: a message can be in several places")
    return 0


def report_adopt(result: jobs.AdoptResult) -> int:
    """Say what was taken into the archive, or what would have been.

    The count is the whole finding, and it is what the reader has to weigh: it
    is how many messages this run speaks for. So it comes first, the name is in
    the same sentence, and the dry run says in as many words that the log is not
    corrected afterwards -- that sentence has to arrive while the run can still
    be called off, which is the only moment it is worth anything.

    A name that is already a place is said differently in the two modes, and on
    purpose. Before the run it is a **choice**: these would go in with what is
    already there, and a different name is one keystroke away. Afterwards it can
    only be a **fact**, and it is written as one -- what the place holds now. A
    warning after the writing would be the kind of line that names a state and no
    move, and there is no move left to name. What it is still good for is the
    mistyped name: three messages adopted into a place that now holds five
    thousand says at a glance that this was not the name that was meant.

    Nothing was found is a good outcome and reads like one. It is also the
    outcome that says the archive is whole in itself, which is worth more than
    "0 adopted" would be.
    """
    if not result.found:
        print("every message in the archive has a place, nothing to take in")
        return 0

    if result.dry_run:
        print(
            f"{utils.counted(result.found, 'message belongs', 'messages belong')} to no"
            f" place and would be recorded as {result.name}"
        )
        if result.held:
            print(
                f"  {result.name} is already a place and holds"
                f" {utils.counted(result.held, 'message')}; these would go in with them"
            )
        print("  nothing corrects the log afterwards, so the name has to be right")
        print("  nothing was written; leave out --dry-run to record them")
        return 0

    unrecorded = result.found - result.recorded
    if unrecorded:
        print(
            f"{unrecorded:,} of {utils.counted(result.found, 'message')} stayed"
            f" unrecorded -- the metadata log could not be written. Nothing else"
            f" changed, so the same command records them once the log can be written"
        )
        return 1
    held_now = utils.counted(result.held + result.recorded, "message")
    place = f"{result.name}, which now holds {held_now}" if result.held else result.name
    found = utils.counted(result.found, "message belongs", "messages belong")
    print(f"{found} to no place, recorded as {place}")
    print(
        f"  `mailvault db update` takes it in, then `mailvault db search"
        f" --folder {result.name}` finds them"
    )
    return 0


def _report_orphans(store_ids: list[str]) -> None:
    """Say what a message with no place recorded is, and what follows from it.

    These are archived, intact and readable; the one thing missing is the note
    where they came from, and it is missing because it never was in the archive,
    not because something lost it. Saying so is the finding -- a reader who is
    told only "110 not referenced" goes looking for a repair, and what they need
    to know first is that there is nothing to repair.

    Two moves, in the order they are worth having, and the better one second
    because it comes with a condition. `archive adopt` always works and records
    what the person running it says; importing the same directory again works
    only where that directory still exists, and is better exactly there, because
    what it records cannot be wrong. Naming it the other way round would offer a
    move that leads nowhere for most readers, which costs more than it is worth.

    This one prints no list. A store id is the right handle for `archive export`
    and useless to a person deciding whether their archive is all right, and
    twenty of a hundred and ten is neither a list to work from nor short enough
    to skim. They go to the debug log, whole.
    """
    if not store_ids:
        return
    print(
        f"{utils.counted(len(store_ids), 'message belongs', 'messages belong')} to no"
        " known place -- stored and intact, but nothing records where that mail"
        " came from"
    )
    print(
        "  they are found like any other message: `db create` builds a"
        " query database with sender, subject and date"
    )
    print("  `archive adopt --name NAME` takes them into a place you name")
    print(
        "  where an import read them from a directory that is still there,"
        " importing it again under a --name is better: it records only what"
        " really lay in it"
    )
    log.debug("no place recorded: %s", ", ".join(store_ids))


def report_check(source: pathlib.Path, result: jobs.CheckResult) -> int:
    """Say what the archive turned out to be, and whether that is all right.

    The verdict at the end is not decoration, and neither is its wording. A
    check that did not read the contents cannot have found an entry whose bytes
    changed under it, so a clean run means two different things depending on how
    it was asked -- and a reader who is told only "sound" has no way to know
    which of the two they got. The exit code says the same thing, but nobody
    reads an exit code they did not go looking for.

    The two message counts are here to be subtracted from each other: what lies
    in the archive, and what the log accounts for. The difference is the orphans
    listed further down, and seeing it as arithmetic is what makes that list
    something other than a number to be alarmed by.

    A third count used to stand between them -- log entries, one per message per
    place, so a Gmail message under three labels counted three times. It is
    gone. It was neither files nor messages nor folders, it was six figures wide
    next to two-figure neighbours, and nothing a reader could decide followed
    from it either way. A number that answers no question is not neutral: it
    gets read as an answer to whichever question the reader had.
    """
    store = cas.mail_store(source)

    def ids(paths: list[pathlib.Path]) -> list[str]:
        """The message ids of entries -- what `archive export` and the log take."""
        return [store.hashval_of(path) or str(path) for path in paths]

    print(
        f"{utils.counted(result.entries, 'message')} stored, {result.referenced:,} of"
        f" them accounted for by {utils.counted(result.log_files, 'log file')}"
        f" in {utils.counted(result.places, 'place')}"
    )
    _report_items(
        [f"{store_id}  {where}" for store_id, where in result.missing.items()],
        "message",
        "referenced in the log and missing from the archive",
    )
    _report_items(
        [utils.under(source, path) for path in result.damaged_logs],
        "log file",
        "damaged -- the content does not match its checksum",
    )
    _report_items(
        result.broken_chains,
        "log file",
        "named by the chain and gone -- nothing records what was written there",
    )
    _report_items(
        [utils.under(source, path) for path in result.unchained],
        "log file",
        "no chain reaches -- still read, the chain is behind",
    )
    _report_items(
        ids(result.corrupt),
        "message",
        "damaged -- the content does not match its checksum",
    )
    _report_items(ids(result.unreadable), "message", "could not be read")
    _report_items(
        [utils.under(source, path) for path in result.foreign],
        "file in the archive that is not a message",
        plural="files in the archive that are not messages",
    )
    _report_orphans(ids(result.orphans))
    if result.quarantined_before:
        aside = utils.counted(result.quarantined_before, "message")
        print(f"{aside} set aside by an earlier run")
    if result.transient_removed:
        leftovers = utils.counted(result.transient_removed, "leftover")
        print(f"{leftovers} of an interrupted write removed")
    if result.quarantined:
        print(
            f"{utils.counted(len(result.quarantined), 'damaged message')} set aside --"
            " counted as missing now, fetch again with `verify --repair` or"
            " `backup --full`"
        )
    if not result.sound:
        print(f"NOT sound -- {utils.counted(result.findings, 'finding')} above")
    elif result.contents_checked:
        print("sound -- every message was read and matches its checksum")
    else:
        print(
            "sound as far as this went -- the integrity check was skipped,"
            " so no damaged message could have been found"
        )
    return 0 if result.sound else 1


def report_conversion(
    source: pathlib.Path,
    result: cas.ConversionResult,
    done: str,
    already: str,
) -> int:
    """Say what a conversion pass did, and exit non-zero when part of it failed.

    A pass keeps going when one entry fails, so without this the command would
    report how many files it converted and stay silent about the ones it did
    not: the archive would look converted when it is not, and a script driving
    the command would never find out.
    """
    print(f"{utils.counted(result.converted, 'file')} {done}, {result.skipped:,} {already}")
    for path in result.failed:
        print(f"{utils.under(source, path)}: could not be converted, left as it is")
    if not result.failed:
        return 0
    print(f"{utils.counted(len(result.failed), 'file')} failed, see the log for the reason")
    return 1


def _entry_path(store: cas.ContentAddressedStorage, wanted: str) -> pathlib.Path:
    """Find the entry a message id names.

    A path is accepted too, and only its file name is looked at -- someone who
    has been in the directory with `ls` should not be sent away. But an id is
    what the reports print and what every other command takes; where an entry
    lies is the store's business and no part of the interface.
    """
    # A store id has no suffix, so `hashval_of` declines it and the fallback
    # takes over; only what the directories say is ignored, which is why a path
    # copied from a report written on another machine still finds its entry.
    hashval = store.hashval_of(pathlib.Path(wanted))
    if hashval is None:
        if not cas.is_hashval(wanted):
            raise jobs.JobError(f"{wanted}: neither a store id nor the path of an entry")
        hashval = cas.normalize_hashval(wanted)
    found = store.locate(hashval, exists=True)
    if found is None:
        raise jobs.JobError(f"{hashval}: not in this archive")
    return found


def export_entries(
    source: pathlib.Path,
    wanted: list[str],
    output: pathlib.Path | None,
) -> int:
    """Write out what an entry holds, decompressed, exactly as it was stored.

    The way to look at a message the reports can only name. What lies in the
    archive under that id may be a zstd frame in a sharded directory, and none
    of that is anyone's business outside the store -- this hands over the
    message.

    Raw and unmodified: whatever comes out here hashes back to the name it came
    from, so it is also the way to hand a message to another tool without the
    archive having an opinion about it.
    """
    store = cas.mail_store(source)
    paths = [_entry_path(store, one) for one in wanted]

    if output is None:
        if len(paths) > 1:
            raise jobs.JobError(
                "export: several messages need --output, or they would arrive as one"
                " stream with nothing between them"
            )
        sys.stdout.buffer.write(store.read(paths[0]))
        return 0

    if len(paths) > 1 or output.is_dir():
        if not output.is_dir():
            raise jobs.JobError(f"{output}: not a directory")
        for path in paths:
            target = output / path.name.removesuffix(".zst")
            target.write_bytes(store.read(path))
            print(f"{target}")
        return 0

    output.write_bytes(store.read(paths[0]))
    print(f"{output}")
    return 0


def _refuse_importing_the_archive(archive: pathlib.Path, source: pathlib.Path) -> None:
    """Refuse an import whose source and destination are the same mail.

    `import` reads from somewhere else -- that is what makes it an import. Since
    the archive stopped being a positional argument and became the directory one
    is standing in, `mailvault archive import --move .` is a plausible slip
    rather than an absurdity, and it is the one command in the program that
    deletes mail: every message is found, answered with EXISTS because it is
    already there, and then removed from the source. Which is the archive.

    Refused with or without `--move`. Without it the run is merely a long way of
    doing nothing, and one rule is easier to rely on than one that depends on a
    flag.
    """
    here = archive.resolve()
    there = source.resolve()
    if here == there or there.is_relative_to(here) or here.is_relative_to(there):
        raise jobs.JobError(
            f"{source}: an import reads from somewhere else, and this is the archive"
            f" itself. With --move it would find every message already stored and"
            f" then delete it. Name a source outside {archive}"
        )


def _provenance(archive: pathlib.Path, name: str) -> jobs.Provenance:
    """What the import records itself as, and where it writes it down.

    A dry run is handed one too and writes nothing with it, so that the run which
    reports what would happen is the same run in every other respect.
    """
    jobs.check_place_name(name)
    return jobs.Provenance(
        name=name,
        log=metalog.LogWriter(
            archive / metalog.DEFAULT_LOG_DIR, archive / heads.DEFAULT_HEADS_DIR
        ),
    )


# How much of a message id a table shows. The full value is 96 characters and
# would be three lines of terminal for a column nobody reads across -- it is
# there to be recognised, not typed. Every machine-readable format prints it
# whole, and `--ids` exists precisely so nothing has to be copied off a table.
ID_PREVIEW = 12

# How much of a subject a table shows before it costs the line its shape.
SUBJECT_WIDTH = 60


def _cut(value: str | None, width: int) -> str:
    if not value:
        return ""
    collapsed = " ".join(value.split())
    if len(collapsed) <= width:
        return collapsed
    return collapsed[: width - 1] + "…"


def report_search(hits: list[jobs.SearchHit], query: jobs.SearchQuery) -> int:
    """Print what was found, in the shape a person reads.

    The count at the end is not decoration: a search that filtered nothing out
    prints the whole archive, and a reader who has scrolled past two hundred
    lines has lost track of whether that was all of them.
    """
    for hit in hits:
        day = (hit.date or "")[:10] or "??????????"
        print(
            f"{day}  {hit.store_id[:ID_PREVIEW]}…  "
            f"{_cut(hit.sender, 32):32}  {_cut(hit.subject, SUBJECT_WIDTH)}"
        )
    if not hits:
        print("no message matches" if not query.is_empty() else "the database is empty")
        return 0
    print(utils.counted(len(hits), "message"))
    if query.limit is not None and len(hits) == query.limit:
        # Said out loud, because a limit that happens to be reached looks exactly
        # like a search that found that many.
        print(f"stopped at --limit {query.limit:,}; there may be more")
    return 0


def report_search_csv(hits: list[jobs.SearchHit]) -> int:
    writer = csv.writer(sys.stdout)
    writer.writerow(["store_id", "date", "sender", "subject", "places"])
    for hit in hits:
        writer.writerow(
            [
                hit.store_id,
                hit.date or "",
                hit.sender or "",
                hit.subject or "",
                " ".join(hit.places),
            ]
        )
    return 0


def report_search_json(hits: list[jobs.SearchHit]) -> int:
    json.dump(
        [dataclasses.asdict(hit) for hit in hits],
        sys.stdout,
        ensure_ascii=False,
        indent=2,
    )
    print()
    return 0


def _search_query(args: argparse.Namespace) -> jobs.SearchQuery:
    return jobs.SearchQuery(
        sender=args.sender,
        recipient=args.recipient,
        subject=args.subject,
        mailbox=args.mailbox,
        folder=args.folder,
        since=args.since,
        until=args.until,
        limit=args.limit,
    )


def run_db(args: argparse.Namespace) -> int:
    """Run a `db` subcommand against the archive's query database.

    Every one of them works on the `index.db` of the archive `--archive` names,
    or of the one being stood in. There is no second database and no way to name
    one: it is a feature of an archive, kept where the mail it describes is.
    """
    archive = archive_path(args)
    require_archive(archive)
    db_path = archive / DEFAULT_DB_NAME
    cmd = args.db_command

    if cmd == "create":
        if db_path.exists() and not args.force:
            raise jobs.JobError(
                f"{db_path.name}: already here. `db update` brings it up to date"
                f" for a fraction of what building it again costs; --force builds"
                f" it again anyway"
            )
        return report_create_db(
            db_path,
            jobs.create_db(archive, db_path, force=True, temp_dir=args.temp_dir),
        )
    elif cmd == "update":
        return report_update_db(db_path, jobs.refresh_db(archive, db_path))
    elif cmd == "drop":
        if jobs.drop_db(db_path):
            print(f"{db_path.name}: deleted -- `db create` builds it again")
        else:
            print(f"{db_path.name}: not here, nothing to delete")
        return 0
    elif cmd == "search":
        if not db_path.exists():
            raise jobs.JobError(
                f"{db_path.name}: no query database in this archive -- build one"
                f" with `mailvault db create`"
            )
        # Asked before the results are printed, so the warning is not scrolled
        # away by them: what follows is true and may be incomplete.
        state = jobs.freshness(archive, db_path)
        complaint = state.complaint(db_path.name)
        if complaint:
            log.warning("%s", complaint)
        query = _search_query(args)
        hits = jobs.search(db_path, query)
        if args.ids:
            for hit in hits:
                print(hit.store_id)
            return 0
        if args.csv:
            return report_search_csv(hits)
        if args.json:
            return report_search_json(hits)
        return report_search(hits, query)

    return 0


def run_archive(args: argparse.Namespace) -> int:
    """Run an `archive` subcommand (stats/import/compress/check/...).

    Every one of these works on the archive `--archive` names, or on the
    directory one is standing in. `import` is the only one with a directory of
    its own left to name: what it reads from is somebody else's archive, and
    that is a different thing from the one being written to.
    """
    cmd = args.archive_command
    archive = archive_path(args)
    if cmd not in WITHOUT_AN_ARCHIVE:
        require_archive(archive)

    if cmd == "init":
        # `git init [<directory>]`, and the same default: where you are standing.
        target = args.directory if args.directory is not None else archive
        return report_init(target, jobs.init_archive(target, DEFAULT_CONFIG_NAME))
    elif cmd == "export":
        return export_entries(archive, args.entry, args.output)
    elif cmd == "stats":
        count, size = _external(archive, args.docuware).stats()
        print(f"{count:,} emails, {_human_size(size)} total")
    elif cmd == "import":
        _refuse_importing_the_archive(archive, args.source)
        source = _external(args.source, args.docuware)
        destination = cas.mail_store(archive, compress=args.compress)
        return report_import(
            args.source,
            archive,
            source.archive_to_cas(
                destination,
                provenance=_provenance(archive, args.name),
                move=args.move,
                dry_run=args.dry_run,
            ),
        )
    elif cmd == "places":
        return report_places(metalog.summarize(archive / metalog.DEFAULT_LOG_DIR))
    elif cmd == "adopt":
        return report_adopt(jobs.adopt(archive, args.name, dry_run=args.dry_run))
    elif cmd == "compress":
        store = cas.mail_store(archive)
        return report_conversion(
            archive, store.compress_all(), "compressed", "already compressed"
        )
    elif cmd == "decompress":
        store = cas.mail_store(archive)
        return report_conversion(
            archive, store.decompress_all(), "decompressed", "already plain"
        )
    elif cmd == "migrate":
        return report_migration(archive, jobs.migrate_archive(archive))
    elif cmd == "compact":
        return report_compact(
            metalog.compact(
                archive / metalog.DEFAULT_LOG_DIR,
                archive / heads.DEFAULT_HEADS_DIR,
            )
        )
    elif cmd == "check":
        return report_check(
            archive,
            jobs.check(
                archive, contents=not args.no_integrity_check, quarantine=args.quarantine
            ),
        )

    return 0
