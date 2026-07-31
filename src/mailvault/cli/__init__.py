"""Single-command CLI front-end for mailvault.

    mailvault [global options] <command> [args]

The command groups map onto the former ib-mailbox / ib-copy / ib-archive tools;
the actual work still lives in mailvault.jobs. This module only builds the
argument parser and dispatches to the handler modules in this package.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import logging
import pathlib
import sys

from mailvault.cli import commands

log = logging.getLogger(__name__)

# Commands that read a job configuration file and therefore require --config.
_CONFIG_COMMANDS = {"folders", "backup", "verify", "copy"}


def get_version() -> str:
    """Return the installed package version, or 'unknown' when not packaged."""
    try:
        return importlib.metadata.version("mailvault")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def setup_logger(loglevel: int = logging.INFO, logfile: pathlib.Path | None = None) -> None:
    logger_format = "%(asctime)s %(levelname)s -- %(message)s"
    if logfile:
        logging.basicConfig(filename=logfile, level=loglevel, format=logger_format)
    else:
        logging.basicConfig(stream=sys.stderr, level=loglevel, format=logger_format)

    # Third-party libraries that are excessively verbose at INFO/DEBUG level.
    # Only suppress when not explicitly asked for verbose output.
    if loglevel > logging.DEBUG:
        for name in ("httpx", "msal", "imapclient"):
            logging.getLogger(name).setLevel(logging.WARNING)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mailvault",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Back up and archive email from IMAP and Microsoft 365 (Graph) mailboxes.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {get_version()}")
    parser.add_argument("-v", "--verbose", action="store_true", help="Set log level to DEBUG")
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Set log level to WARNING (results and errors only)",
    )
    parser.add_argument(
        "--log-file",
        dest="log_file",
        type=pathlib.Path,
        help="Write the log to this file instead of stderr",
    )
    parser.add_argument(
        "--config",
        type=pathlib.Path,
        help="Configuration file (YAML or TOML); required by folders/backup/verify/copy",
    )
    parser.add_argument(
        "--allow-exec",
        action="store_true",
        help="Allow execution of _cmd fields in the configuration file",
    )
    parser.add_argument(
        "--job",
        action="append",
        metavar="NAME",
        help="Run only the named job(s); may be repeated (folders/backup/verify)",
    )

    sub = parser.add_subparsers(dest="command", metavar="<command>")

    sub.add_parser(
        "folders",
        help="List the folders of each configured mailbox",
        description="List available folders on the configured IMAP/Graph mailboxes.",
    )

    p_backup = sub.add_parser(
        "backup",
        help="Back up mailboxes to a local archive",
        description="Back up mails to the local content-addressed archive.",
    )
    p_backup.add_argument(
        "--compress", action="store_true", help="Compress stored emails with zstd"
    )
    p_backup.add_argument("destination", type=pathlib.Path, help="Destination base directory")

    p_verify = sub.add_parser(
        "verify",
        help="Compare mailboxes against the archive and report gaps",
        description="Compare mailboxes against their archives and report missing messages.",
    )
    p_verify.add_argument(
        "--repair", action="store_true", help="Download and store the missing emails"
    )
    p_verify.add_argument(
        "--compress", action="store_true", help="Compress stored emails with zstd"
    )
    p_verify.add_argument("destination", type=pathlib.Path, help="Archive directory to check")

    p_copy = sub.add_parser(
        "copy",
        help="Copy mails between two mailboxes",
        description="Copy mails from the source-role to the destination-role mailbox.",
    )
    p_copy.add_argument(
        "--idle",
        action="store_true",
        help="Keep the connection open and copy new mail as it arrives",
    )
    p_copy.add_argument(
        "--list-folders",
        dest="list_folders",
        action="store_true",
        help="List the source mailbox folders instead of copying",
    )

    p_archive = sub.add_parser(
        "archive",
        help="Maintain the local archive (stats, import, compress, ...)",
        description="Manage and maintain the local email archive.",
    )
    asub = p_archive.add_subparsers(
        dest="archive_command", metavar="<subcommand>", required=True
    )

    a_stats = asub.add_parser(
        "stats",
        help="Show archive statistics",
        description="Show statistics of the email archive.",
    )
    a_stats.add_argument(
        "--docuware", action="store_true", help="Archive is a Docuware archive"
    )
    a_stats.add_argument("source", type=pathlib.Path, help="Email archive directory")

    a_import = asub.add_parser(
        "import",
        help="Import emails from another archive",
        description="Import emails from a source archive into the destination archive.",
    )
    a_import.add_argument(
        "--docuware", action="store_true", help="Source archive is a Docuware email archive"
    )
    a_import.add_argument(
        "--move", action="store_true", help="Remove emails from the source after import"
    )
    a_import.add_argument(
        "--compress", action="store_true", help="Compress stored emails with zstd"
    )
    a_import.add_argument("source", type=pathlib.Path, help="Directory to copy/move mails from")
    a_import.add_argument("destination", type=pathlib.Path, help="Archive directory")

    a_addr = asub.add_parser(
        "addresses",
        help="List all email addresses in the archive",
        description="Show mail addresses of all emails in the archive.",
    )
    a_addr.add_argument(
        "--docuware", action="store_true", help="Directory is a Docuware archive"
    )
    a_addr.add_argument("source", type=pathlib.Path, help="Archive directory")

    a_comp = asub.add_parser(
        "compress",
        help="Compress uncompressed archive files",
        description="Compress uncompressed files in the archive with zstd.",
    )
    a_comp.add_argument("source", type=pathlib.Path, help="Email archive directory")

    a_decomp = asub.add_parser(
        "decompress",
        help="Decompress compressed archive files",
        description="Decompress compressed files in the archive.",
    )
    a_decomp.add_argument("source", type=pathlib.Path, help="Email archive directory")

    a_rebuild = asub.add_parser(
        "rebuild-db",
        help="Rebuild the metadata database from the archive",
        description="Rebuild the metadata database by reading all archive items.",
    )
    a_rebuild.add_argument(
        "--mailbox", type=str, help="Mailbox identifier (matching a config entry)"
    )
    a_rebuild.add_argument("source", type=pathlib.Path, help="Email archive directory")

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        return 2
    if args.command in _CONFIG_COMMANDS and args.config is None:
        parser.error("the following arguments are required: --config")

    if args.verbose:
        loglevel = logging.DEBUG
    elif args.quiet:
        loglevel = logging.WARNING
    else:
        loglevel = logging.INFO
    setup_logger(loglevel=loglevel, logfile=args.log_file)

    log.info("START")
    exit_code = 0
    try:
        if args.command in {"folders", "backup", "verify"}:
            exit_code = commands.run_mailbox(args)
        elif args.command == "copy":
            exit_code = commands.run_copy(args)
        elif args.command == "archive":
            exit_code = commands.run_archive(args)
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
