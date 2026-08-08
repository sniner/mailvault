"""Single-command CLI front-end for mailvault.

    mailvault [global options] <command> [args]

The command groups map onto the former ib-mailbox / ib-archive tools;
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


def add_mailbox_options(parser: argparse.ArgumentParser, writes: bool = False) -> None:
    """The options a command takes that reads the configuration and logs in.

    They belong to the command, not to `mailvault` itself. Which jobs to run and
    what the configuration may do are statements about the work being done, and
    `archive check` has no use for either -- an option whose help text has to
    name the commands it applies to is standing one level too high. It also puts
    them where the hand expects them: `backup --job proton.me` is how everybody
    writes it, and only the parser used to insist on `--job proton.me backup`.

    `writes` adds the one that only means anything to a command that puts
    messages *into* an archive.
    """
    parser.add_argument(
        "--job",
        action="append",
        metavar="NAME",
        help="Run only the named job(s); may be repeated",
    )
    parser.add_argument(
        "--allow-exec",
        action="store_true",
        help="Let the configuration's _cmd fields run, e.g. to fetch a password",
    )
    if writes:
        parser.add_argument(
            "--allow-new-mailbox",
            action="store_true",
            help=(
                "Let a job write into an archive it has never written into before;"
                " without it such a run is refused, on the assumption that the"
                " configuration and the archive do not belong together"
            ),
        )


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
        "--archive",
        type=pathlib.Path,
        metavar="DIR",
        help="The archive to work on (default: the directory you are standing in)",
    )
    parser.add_argument(
        "--config",
        type=pathlib.Path,
        metavar="FILE",
        help=(
            "Configuration file (TOML); by default the archive's own"
            f" {commands.DEFAULT_CONFIG_NAME}"
        ),
    )

    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p_folders = sub.add_parser(
        "folders",
        help="List the folders of each configured mailbox",
        description="List available folders on the configured IMAP/Graph mailboxes.",
    )
    add_mailbox_options(p_folders)

    p_backup = sub.add_parser(
        "backup",
        help="Back up mailboxes to a local archive",
        description=(
            "Add to the archive whatever the configured mailboxes hold and it does"
            " not. Each folder carries on where the last run left it, so a repeated"
            " run costs only the mail that has arrived since."
        ),
    )
    add_mailbox_options(p_backup, writes=True)
    p_backup.add_argument(
        "--full",
        action="store_true",
        help="Re-read every folder in full, ignoring where the last run left off",
    )
    p_backup.add_argument(
        "--compress",
        action="store_true",
        help="Store the messages compressed with zstd",
    )
    p_backup.add_argument(
        "--index-db",
        action="store_true",
        help="Keep index.db in step with the archive, so the mail can be queried with SQL",
    )

    p_verify = sub.add_parser(
        "verify",
        help="Compare mailboxes against the archive and report gaps",
        description="Compare mailboxes against their archives and report missing messages.",
    )
    add_mailbox_options(p_verify, writes=True)
    p_verify.add_argument(
        "--repair",
        action="store_true",
        help="Download and store the missing emails",
    )
    p_verify.add_argument(
        "--compress",
        action="store_true",
        help="Store the messages compressed with zstd",
    )

    p_archive = sub.add_parser(
        "archive",
        help="Maintain the local archive (stats, import, compress, ...)",
        description="Manage and maintain the local email archive.",
    )
    asub = p_archive.add_subparsers(
        dest="archive_command",
        metavar="<subcommand>",
        required=True,
    )

    asub.add_parser(
        "init",
        help="Make this directory an archive",
        description=(
            "Make a directory into an archive: the three directories it is made"
            " of, the mark that says which layout they are written in, and a"
            f" {commands.DEFAULT_CONFIG_NAME} to fill in. What `git init` is."
            " Every other command works on an archive and refuses a directory"
            " that is not one. An existing configuration is left alone."
        ),
    )

    a_stats = asub.add_parser(
        "stats",
        help="Show archive statistics",
        description="Show statistics of the email archive.",
    )
    a_stats.add_argument(
        "--docuware",
        action="store_true",
        help="Archive is a Docuware archive",
    )

    a_import = asub.add_parser(
        "import",
        help="Import emails from another archive",
        description="Import emails from a source archive into the destination archive.",
    )
    a_import.add_argument(
        "--docuware",
        action="store_true",
        help="Source archive is a Docuware email archive",
    )
    a_import.add_argument(
        "--move",
        action="store_true",
        help="Remove emails from the source after import",
    )
    a_import.add_argument(
        "--compress",
        action="store_true",
        help="Compress stored emails with zstd",
    )
    a_import.add_argument(
        "--dry-run",
        action="store_true",
        help="Only count what would be imported: nothing is written, nothing is removed",
    )
    a_import.add_argument("source", type=pathlib.Path, help="Directory to copy/move mails from")

    a_export = asub.add_parser(
        "export",
        help="Write out a stored message, decompressed and unchanged",
        description=(
            "Write out a message, exactly as it was stored. Takes the message id"
            " the reports print; without --output the message goes to standard"
            " output, which is the way to look at one the reports could only name."
        ),
    )
    a_export.add_argument(
        "--output",
        "-o",
        type=pathlib.Path,
        help="Write to this file, or into this directory when several are named",
    )
    a_export.add_argument(
        "entry",
        nargs="+",
        metavar="ID",
        help="Message id, as the reports print it",
    )

    a_addr = asub.add_parser(
        "addresses",
        help="List all email addresses in the archive",
        description="Show mail addresses of all emails in the archive.",
    )
    a_addr.add_argument(
        "--docuware",
        action="store_true",
        help="Directory is a Docuware archive",
    )

    asub.add_parser(
        "compress",
        help="Compress uncompressed archive files",
        description="Compress uncompressed files in the archive with zstd.",
    )

    asub.add_parser(
        "decompress",
        help="Decompress compressed archive files",
        description="Decompress compressed files in the archive.",
    )

    a_create = asub.add_parser(
        "create-db",
        help="Build a queryable database from the archive",
        description=(
            "Build an SQLite database of everything the archive knows about its"
            " mail -- sender, recipients, subject, date, and which mailbox and"
            " folder each message was seen in -- for querying with SQL. The archive"
            " itself holds no database: what this writes is a snapshot, accurate for"
            " the moment it was built and stale from the next backup onwards."
        ),
    )
    a_create.add_argument(
        "--mailbox",
        type=str,
        help="Mailbox to file messages under whose mailbox the archive does not record",
    )
    a_create.add_argument(
        "--force",
        action="store_true",
        help="Replace the database file if it already exists",
    )
    a_create.add_argument("database", type=pathlib.Path, help="Database file to write")

    asub.add_parser(
        "migrate",
        help="Bring an archive written by an earlier version up to date",
        description=(
            "Bring an archive of any earlier shape up to the layout this version"
            " writes, and say what was lifted. A backup does this by itself; the"
            " command is for doing it deliberately, once, and seeing the result."
            " Nothing is deleted, and a run that is interrupted is simply picked"
            " up by the next one."
        ),
    )

    asub.add_parser(
        "compact",
        help="Consolidate the metadata log, dropping duplicate entries",
        description=(
            "Merge the many small metadata-log files an archive accumulates -- one"
            " per folder per backup, with entries repeated across the incremental"
            " overlap -- into one file per mailbox/folder holding each observation"
            " once. Lossless and safe to run at any time; the originals are removed"
            " only after the consolidated files are written and verified."
        ),
    )

    a_check = asub.add_parser(
        "check",
        help="Check that the archive is what it says it is",
        description=(
            "Hold an archive against what it claims: every message it records is"
            " there, nothing lies in it that is not a message it knows, what it"
            " wrote down about them is undamaged, and every message still matches"
            " its checksum -- the last one being the only way to find one whose"
            " bytes have changed under it. --no-integrity-check leaves that last"
            " check out. Reports what it finds and repairs nothing; the only thing"
            " it removes is the leftover of a write that was interrupted."
        ),
    )
    a_check.add_argument(
        "--no-integrity-check",
        action="store_true",
        help="Skip the integrity check: no message is read, so none can be found damaged",
    )
    a_check.add_argument(
        "--quarantine",
        action="store_true",
        help="Set damaged messages aside, so they count as missing and can be fetched again",
    )

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        return 2

    if args.verbose:
        loglevel = logging.DEBUG
    elif args.quiet:
        loglevel = logging.WARNING
    else:
        loglevel = logging.INFO
    setup_logger(loglevel=loglevel, logfile=args.log_file)

    # The archive is named once, here, and nowhere else. Every line after this
    # is about it, and repeating the path on each of them buries the statement
    # behind it -- over a network share the prefix is routinely longer than what
    # it prefixes. `folders` is the one command that works on no archive at all.
    if args.command == "folders":
        log.info("START")
    else:
        log.info("START -- archive: %s", commands.archive_path(args))
    exit_code = 0
    try:
        if args.command in {"folders", "backup", "verify"}:
            exit_code = commands.run_mailbox(args)
        elif args.command == "archive":
            exit_code = commands.run_archive(args)
    except KeyboardInterrupt:
        log.warning("Interrupted!")
        exit_code = 130
    except commands.EXPECTED_ERRORS as exc:
        # A broken config or a refused operation is a user error, not a crash:
        # report it as one line instead of a traceback.
        log.error("%s", exc)
        log.debug("failed", exc_info=exc)
        exit_code = 1
    except Exception as exc:
        log.exception("Fatal error: %s", exc)
        exit_code = 1
    finally:
        log.info("FINISHED")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
