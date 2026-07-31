"""Handlers for the mailbox-facing commands: folders, backup, verify."""

from __future__ import annotations

import argparse
import logging

from mailvault import conf, jobs

log = logging.getLogger(__name__)


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


def run_job(job: conf.JobConfig, args: argparse.Namespace, config: conf.Config) -> None:
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


def run(args: argparse.Namespace) -> int:
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
            run_job(job, args, config)
        except Exception as exc:
            # One broken job must not stop the remaining ones, but the run
            # as a whole reports failure so callers/cron can react.
            log.exception("Job '%s' failed: %s", job.name, exc)
            exit_code = 1
    return exit_code
