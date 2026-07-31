"""Command handlers for the `mailvault` CLI.

Each `run_*` function is the dispatch target for one command group in
`mailvault.cli.main`; the argument parsing lives there, the actual work in
`mailvault.jobs` and `mailvault.importer`.
"""

from __future__ import annotations

import argparse
import logging
import sys

from mailvault import conf, importer, jobs
from mailvault.store import cas

log = logging.getLogger(__name__)


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
        jobs.backup(job, args.destination, compress=compress)
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
        try:
            _run_job(job, args, config)
        except Exception as exc:
            # One broken job must not stop the remaining ones, but the run
            # as a whole reports failure so callers/cron can react.
            log.exception("Job '%s' failed: %s", job.name, exc)
            exit_code = 1
    return exit_code


# --- copy ----------------------------------------------------------------------


def run_copy(args: argparse.Namespace) -> int:
    """Copy from the source-role mailbox to the destination-role mailbox."""
    if args.config.suffix.lower() != ".toml":
        print(
            f"Error: configuration file must be TOML format (.toml), got: {args.config}",
            file=sys.stderr,
        )
        return 1

    config = conf.load(args.config, allow_exec=args.allow_exec)
    source = conf.find(config.jobs, "role", "source")
    destination = conf.find(config.jobs, "role", "destination")

    if source is None or destination is None:
        log.error("Job missing source or destination role")
        return 1

    if args.list_folders:
        jobs.folder_list(source)
    else:
        log.info(f"Copy job: {source.name} -> {destination.name}")
        jobs.copy(source, destination, idle=args.idle)

    return 0


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


def run_archive(args: argparse.Namespace) -> int:
    """Run an `archive` subcommand (stats/import/addresses/compress/decompress/rebuild-db)."""
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
    elif cmd == "rebuild-db":
        jobs.rebuild_metadb(args.source, mailbox=args.mailbox)

    return 0
