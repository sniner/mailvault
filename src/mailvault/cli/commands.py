"""Command handlers for the `mailvault` CLI.

Each `run_*` function is the dispatch target for one command group in
`mailvault.cli.main`; the argument parsing lives there, the actual work in
`mailvault.jobs` and `mailvault.importer`.
"""

from __future__ import annotations

import argparse
import logging
import pathlib

from mailvault import conf, importer, jobs
from mailvault.backend import base
from mailvault.jobs import guard
from mailvault.store import cas, metalog

log = logging.getLogger(__name__)

# Failures that are already understood by the time they get here: a broken
# config, a refused operation, a mailbox that said no. There is nothing to
# debug in them, so they are reported as one line and the traceback is left to
# the errors nobody anticipated -- where the call stack is the only clue. The
# traceback is still there under `--verbose` for the rare case it is wanted.
EXPECTED_ERRORS = (conf.ConfigError, jobs.JobError, base.MailboxError)

# The commands that work on an archive directory, as opposed to `folders`, which
# only ever talks to the server.
ARCHIVE_COMMANDS = {"backup", "verify"}


# --- folders / backup / verify -------------------------------------------------


def report_verify(job_name: str, results: list[jobs.VerifyResult], repaired: bool) -> None:
    for r in results:
        line = f"{job_name}::{r.folder}: {r.on_server:,} on server, {r.missing:,} not archived"
        if repaired:
            line += f", {r.restored:,} restored"
            if r.failed:
                line += f", {r.failed:,} failed"
        print(line)
    total_missing = sum(r.missing for r in results)
    total_restored = sum(r.restored for r in results)
    if not total_missing:
        print(f"{job_name}: archive is complete")
    elif not repaired:
        print(f"{job_name}: {total_missing:,} message(s) missing, run again with --repair")
    else:
        print(f"{job_name}: {total_restored:,} of {total_missing:,} message(s) restored")


def _same_place(first: pathlib.Path, second: pathlib.Path) -> bool:
    """True when two paths name the same directory, symlinks and `..` aside.

    Neither has to exist -- this is asked before anything is created.
    """
    try:
        return first.resolve() == second.resolve()
    except OSError:
        return first == second


def archive_path(args: argparse.Namespace, config: conf.Config) -> pathlib.Path:
    """Decide which archive to work on: the command line, or the configuration.

    The command line wins where both name one, so a one-off run into a different
    directory needs no edit to the file. That case is reported, though: it is
    indistinguishable from having reached for the wrong archive, and an override
    that passes unmentioned is what makes such a mistake hard to notice.
    """
    from_cli = getattr(args, "destination", None)
    if from_cli is None:
        if config.destination is None:
            raise conf.ConfigError(
                "no archive directory -- name one on the command line, or set "
                "'destination' under [global] in the configuration"
            )
        log.info("Archive from the configuration: %s", config.destination)
        return config.destination
    if config.destination is not None and not _same_place(from_cli, config.destination):
        log.warning(
            "Archive %s given on the command line, overriding %s from the configuration",
            from_cli,
            config.destination,
        )
    return from_cli


def _run_job(
    job: conf.JobConfig,
    args: argparse.Namespace,
    config: conf.Config,
    destination: pathlib.Path | None = None,
) -> None:
    log.info(f"Job item: {job.name}")

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
        )
    elif args.command == "verify":
        compress = args.compress or config.compress
        results = jobs.verify(job, destination, repair=args.repair, compress=compress)
        report_verify(job.name, results, repaired=args.repair)


def run_mailbox(args: argparse.Namespace) -> int:
    """Run a folders/backup/verify command over the selected config jobs."""
    exit_code = 0
    config = conf.load(args.config, allow_exec=args.allow_exec)
    selected = config.jobs
    if args.job:
        selected = [j for j in selected if j.name in args.job]
        unknown = set(args.job) - {j.name for j in selected}
        for name in unknown:
            log.error("Unknown job: %s", name)
            exit_code = 1

    # Both of these come before the first job, and deliberately so: they decide
    # whether this configuration and this archive belong together, and the answer
    # is worth nothing once a message has been written -- or, with
    # `delete_after_export`, removed from the server.
    destination = None
    if args.command in ARCHIVE_COMMANDS:
        destination = archive_path(args, config)
        guard.check_jobs(destination, selected, allow_new=args.allow_new_mailbox)

    for job in selected:
        # One broken job must not stop the remaining ones, but the run as a
        # whole reports failure so callers/cron can react.
        try:
            _run_job(job, args, config, destination)
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


def _archive(args: argparse.Namespace) -> importer.ExternalMailArchive:
    docuware = getattr(args, "docuware", False)
    cls = importer.DocuwareMailArchive if docuware else importer.ExternalMailArchive
    return cls(args.source)


def _human_size(size: int) -> str:
    units = ["bytes", "KiB", "MiB", "GiB", "TiB"]
    value = float(size)
    unit = units[0]
    for unit in units:
        if value < 1024 or unit == units[-1]:
            break
        value /= 1024
    return f"{value:.1f} {unit}"


def report_migration(source: pathlib.Path, result: jobs.MigrationResult) -> None:
    """Say what was moved out of the database and what became of it."""
    if not result.needed:
        print(f"{source}: no metadata database, nothing to migrate")
        return
    if not result.verified:
        print(f"{source}: the written log files did not verify, database left alone")
        return
    print(
        f"{source}: {result.messages:,} message(s) moved into "
        f"{result.places:,} mailbox/folder place(s)"
    )
    if result.snapshots:
        print(f"{source}: {result.snapshots:,} resume timestamp(s) moved into state.json")
    if result.placeless:
        print(
            f"{source}: {result.placeless:,} of them recorded without a folder -- the old "
            f"database did not store which folder of which mailbox"
        )
    if result.undecidable:
        print(
            f"{source}: {result.undecidable:,} folder name(s) fit more than one mailbox "
            f"and were left out rather than guessed"
        )
    if result.renamed_to is not None:
        print(f"{source}: the database is now {result.renamed_to.name} and is no longer used")
        print(f"{source}: delete it once you are satisfied with the archive")


def report_create_db(
    source: pathlib.Path,
    target: pathlib.Path,
    result: jobs.RebuildResult,
) -> None:
    """Say what went into the database, and name what could not."""
    replay = result.replay
    print(f"{source}: {result.messages:,} message(s) read from the archive")
    if replay.files:
        print(
            f"{source}: metadata log: {replay.files:,} file(s), "
            f"{replay.applied:,} of {replay.entries:,} location(s) applied"
        )
        if replay.unknown:
            print(
                f"{source}: {replay.unknown:,} log entry/entries name messages that are "
                f"not in the archive, ignored"
            )
    else:
        print(f"{source}: no metadata log found, mailbox and folder are NOT in the database")
    print(f"{target}: written -- a snapshot, stale from the next backup onwards")


def report_compact(source: pathlib.Path, result: metalog.CompactResult) -> None:
    """Say how much the log shrank and how many duplicate entries went."""
    if result.files_before == 0:
        print(f"{source}: no metadata log to compact")
        return
    if not result.verified:
        print(f"{source}: consolidated files did not verify, nothing was removed")
        return
    print(
        f"{source}: {result.files_before:,} log file(s) -> {result.files_after:,} "
        f"across {result.places:,} place(s)"
    )
    dropped = result.entries_before - result.entries_after
    if dropped:
        print(f"{source}: {dropped:,} duplicate observation(s) dropped")
    if result.transient_removed:
        # Said out loud rather than swept up quietly: each one is a write that
        # was interrupted, and that is worth knowing about.
        print(
            f"{source}: {result.transient_removed:,} leftover(s) of an interrupted"
            " write removed"
        )


# How many of a kind a report names before it stops listing them. A check on a
# damaged archive can find tens of thousands; the count is the finding, the
# names are there to give someone a place to start.
REPORT_LIMIT = 20


def _report_paths(source: pathlib.Path, finding: str, paths: list[pathlib.Path]) -> None:
    """Print a finding's count and the first few of whatever it found."""
    if not paths:
        return
    print(f"{source}: {len(paths):,} {finding}")
    for path in paths[:REPORT_LIMIT]:
        print(f"  {path}")
    if len(paths) > REPORT_LIMIT:
        print(f"  ... and {len(paths) - REPORT_LIMIT:,} more")


def report_import(
    source: pathlib.Path,
    destination: pathlib.Path,
    result: importer.ImportResult,
) -> int:
    """Say what the import did, or what it would have done.

    Both counts are named even when one of them is zero. "How many were already
    there" is what tells a dry run apart from a disaster: a source that has been
    through a converter on its way here looks exactly like a source full of new
    mail, and the difference only shows in the ratio.
    """
    total = result.stored + result.present
    verb = "would be imported" if result.dry_run else "imported"
    print(
        f"{source}: {total:,} message(s) read -- {result.stored:,} {verb},"
        f" {result.present:,} already in {destination}"
    )
    _report_paths(source, "message(s) could not be read", result.failed)
    return 1 if result.failed else 0


def report_check(source: pathlib.Path, result: jobs.CheckResult) -> int:
    """Say what the archive turned out to be, and whether that is all right.

    The verdict at the end is not decoration, and neither is its wording. A
    check that did not read the contents cannot have found an entry whose bytes
    changed under it, so a clean run means two different things depending on how
    it was asked -- and a reader who is told only "sound" has no way to know
    which of the two they got. The exit code says the same thing, but nobody
    reads an exit code they did not go looking for.

    More places than messages is the normal case, not a discrepancy: a message
    filed in two folders is one entry the log names twice. Said in words for
    that reason -- two bare numbers that do not match invite the wrong worry.
    """
    print(
        f"{source}: {result.entries:,} message(s) stored,"
        f" filed in {result.observations:,} place(s) by {result.log_files:,} log file(s)"
    )
    if result.missing:
        print(f"{source}: {len(result.missing):,} message(s) referenced in the log are missing")
        for store_id, where in list(result.missing.items())[:REPORT_LIMIT]:
            print(f"  {store_id}  {where}")
        if len(result.missing) > REPORT_LIMIT:
            print(f"  ... and {len(result.missing) - REPORT_LIMIT:,} more")
    _report_paths(
        source,
        "log file(s) are damaged -- the content does not match its checksum",
        result.damaged_logs,
    )
    _report_paths(
        source,
        "message(s) are damaged -- the content does not match its checksum",
        result.corrupt,
    )
    _report_paths(source, "message(s) could not be read", result.unreadable)
    _report_paths(source, "file(s) in the archive are not messages", result.foreign)
    if result.orphans:
        print(
            f"{source}: {result.orphans:,} message(s) are not referenced in any log file"
            " -- nothing records which folder they came from"
        )
    if result.quarantined_before:
        print(f"{source}: {result.quarantined_before:,} message(s) set aside by an earlier run")
    if result.transient_removed:
        print(
            f"{source}: {result.transient_removed:,} leftover(s) of an interrupted"
            " write removed"
        )
    if result.quarantined:
        print(
            f"{source}: {len(result.quarantined):,} damaged message(s) set aside -- they"
            " count as missing now, fetch them with `verify --repair` or `backup --full`"
        )
    if not result.sound:
        print(f"{source}: NOT sound -- {result.findings:,} finding(s) above")
    elif result.contents_checked:
        print(f"{source}: sound -- every message was read and matches its checksum")
    else:
        print(
            f"{source}: sound as far as this went -- nothing was read, so no damaged"
            " message could have been found. Use --contents for the integrity check"
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
    print(f"{source}: {result.converted:,} files {done}, {result.skipped:,} {already}")
    for path in result.failed:
        print(f"{path}: could not be converted, left as it is")
    if not result.failed:
        return 0
    print(f"{source}: {len(result.failed):,} file(s) failed, see the log for the reason")
    return 1


def run_archive(args: argparse.Namespace) -> int:
    """Run an `archive` subcommand (stats/import/addresses/compress/create-db/...)."""
    cmd = args.archive_command

    if cmd == "stats":
        count, size = _archive(args).stats()
        print(f"{args.source}: {count:,} emails, {_human_size(size)} total")
    elif cmd == "addresses":
        for where, addr in _archive(args).addresses():
            print(where, addr)
    elif cmd == "import":
        source = _archive(args)
        destination = cas.ContentAddressedStorage(
            args.destination,
            suffix=".eml",
            compress=args.compress,
        )
        return report_import(
            args.source,
            args.destination,
            source.archive_to_cas(destination, move=args.move, dry_run=args.dry_run),
        )
    elif cmd == "compress":
        store = cas.ContentAddressedStorage(args.source, suffix=".eml")
        return report_conversion(
            args.source, store.compress_all(), "compressed", "already compressed"
        )
    elif cmd == "decompress":
        store = cas.ContentAddressedStorage(args.source, suffix=".eml")
        return report_conversion(
            args.source, store.decompress_all(), "decompressed", "already plain"
        )
    elif cmd == "create-db":
        result = jobs.create_db(
            args.source,
            args.database,
            mailbox=args.mailbox,
            force=args.force,
        )
        report_create_db(args.source, args.database, result)
    elif cmd == "migrate":
        report_migration(args.source, jobs.migrate_archive(args.source))
    elif cmd == "compact":
        report_compact(args.source, metalog.compact(args.source / metalog.DEFAULT_LOG_DIR))
    elif cmd == "check":
        return report_check(
            args.source,
            jobs.check(args.source, contents=args.contents, quarantine=args.quarantine),
        )

    return 0
