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
from mailvault.store import cas, metalog

log = logging.getLogger(__name__)

# Failures that are already understood by the time they get here: a broken
# config, a refused operation, a mailbox that said no. There is nothing to
# debug in them, so they are reported as one line and the traceback is left to
# the errors nobody anticipated -- where the call stack is the only clue. The
# traceback is still there under `--verbose` for the rare case it is wanted.
EXPECTED_ERRORS = (conf.ConfigError, jobs.JobError, base.MailboxError)


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


def _run_job(job: conf.JobConfig, args: argparse.Namespace, config: conf.Config) -> None:
    log.info(f"Job item: {job.name}")

    if args.command == "folders":
        jobs.folder_list(job)
    elif args.command == "backup":
        compress = args.compress or config.compress
        index_db = args.index_db or config.index_db
        jobs.backup(
            job,
            args.destination,
            compress=compress,
            index_db=index_db,
            incremental=config.incremental,
        )
    elif args.command == "verify":
        compress = args.compress or config.compress
        results = jobs.verify(job, args.destination, repair=args.repair, compress=compress)
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
    for job in selected:
        # One broken job must not stop the remaining ones, but the run as a
        # whole reports failure so callers/cron can react.
        try:
            _run_job(job, args, config)
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
    source: pathlib.Path, target: pathlib.Path, result: jobs.RebuildResult
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
            args.destination, suffix=".eml", compress=args.compress
        )
        source.archive_to_cas(destination, move=args.move)
    elif cmd == "compress":
        store = cas.ContentAddressedStorage(args.source, suffix=".eml")
        compressed, skipped = store.compress_all()
        print(f"{args.source}: {compressed:,} files compressed, {skipped:,} already compressed")
    elif cmd == "decompress":
        store = cas.ContentAddressedStorage(args.source, suffix=".eml")
        decompressed, skipped = store.decompress_all()
        print(f"{args.source}: {decompressed:,} files decompressed, {skipped:,} already plain")
    elif cmd == "create-db":
        result = jobs.create_db(
            args.source, args.database, mailbox=args.mailbox, force=args.force
        )
        report_create_db(args.source, args.database, result)
    elif cmd == "migrate":
        report_migration(args.source, jobs.migrate_archive(args.source))
    elif cmd == "compact":
        report_compact(args.source, metalog.compact(args.source / metalog.DEFAULT_LOG_DIR))

    return 0
