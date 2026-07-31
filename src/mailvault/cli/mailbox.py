"""Back up emails from IMAP mailboxes to a local content-addressed archive."""

import argparse
import logging
import pathlib

from mailvault import conf, jobs
from mailvault.cli import get_version, setup_logger

log = logging.getLogger(__name__)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter, description=__doc__
    )

    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {get_version()}",
    )
    parser.add_argument(
        "--logfile",
        type=pathlib.Path,
        help="Log file path",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Set log level to DEBUG",
    )
    parser.add_argument(
        "--allow-exec",
        action="store_true",
        help="Allow execution of _cmd fields in configuration file",
    )
    parser.add_argument(
        "--config",
        type=pathlib.Path,
        help="Configuration file (YAML or TOML)",
    )
    parser.add_argument(
        "--job",
        action="append",
        help="Run only the named job(s), may be repeated",
    )

    subparsers = parser.add_subparsers(dest="subcommand")

    subparsers.add_parser(
        "folders",
        description="List available folders on IMAP server",
    )

    backup_parser = subparsers.add_parser(
        "backup",
        description="Backup mails to local storage",
    )
    backup_parser.add_argument(
        "--compress",
        action="store_true",
        help="Compress stored emails with zstd",
    )
    backup_parser.add_argument(
        "destination",
        type=pathlib.Path,
        help="Destination base directory",
    )

    verify_parser = subparsers.add_parser(
        "verify",
        description="Compare the mailbox against the local archive and report gaps",
    )
    verify_parser.add_argument(
        "--repair",
        action="store_true",
        help="Download and store the missing emails",
    )
    verify_parser.add_argument(
        "--compress",
        action="store_true",
        help="Compress stored emails with zstd",
    )
    verify_parser.add_argument(
        "destination",
        type=pathlib.Path,
        help="Archive directory to check",
    )

    args = parser.parse_args()
    if args.subcommand is None:
        parser.print_help()
        raise SystemExit(2)
    if args.config is None:
        parser.error("the following arguments are required: --config")
    return args


def report_verify(job_name: str, results: list[jobs.VerifyResult], repaired: bool) -> None:
    for r in results:
        line = (
            f"{job_name}::{r.folder}: {r.on_server:,} on server, {r.missing:,} not archived"
        )
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

    if args.subcommand == "folders":
        jobs.folder_list(job)
    elif args.subcommand == "backup":
        compress = args.compress or config.compress
        jobs.backup(job, args.destination, compress=compress)
    elif args.subcommand == "verify":
        compress = args.compress or config.compress
        results = jobs.verify(
            job, args.destination, repair=args.repair, compress=compress
        )
        report_verify(job.name, results, repaired=args.repair)


def main() -> int:
    args = parse_arguments()

    setup_logger(
        logfile=args.logfile,
        loglevel=logging.DEBUG if args.verbose else logging.INFO,
    )
    log.info("START")

    exit_code = 0
    try:
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
    except KeyboardInterrupt:
        log.warning("Interrupted!")
        exit_code = 130
    except Exception as exc:
        log.exception("Fatal error: %s", exc)
        exit_code = 1
    finally:
        log.info("FINISHED")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
