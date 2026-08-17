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
import os
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
        help="Run only the named job; may be repeated",
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

    a_init = asub.add_parser(
        "init",
        help="Make a directory an archive",
        description=(
            "Make a directory into an archive: the three directories it is made"
            " of, the mark that says which layout they are written in, and a"
            f" {commands.DEFAULT_CONFIG_NAME} to fill in. What `git init` is,"
            " down to taking the directory as an argument and making it if it is"
            " not there. Every other command works on an archive and refuses a"
            " directory that is not one. An existing configuration is left alone."
        ),
    )
    a_init.add_argument(
        "directory",
        nargs="?",
        type=pathlib.Path,
        help="Where to make the archive (default: --archive, or the one you are standing in)",
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
        help="Take a directory of mail into the archive under a name",
        description=(
            "Take mail into the archive from somewhere that is not a mailbox: a"
            " directory of .eml files, or a Docuware export. The name is what the"
            " archive records the mail under, and it is the answer to a question"
            " nothing else can answer afterwards -- which import a message came"
            " from. `mailvault db search --folder NAME` finds them again."
            " Importing the same source twice costs nothing but the reading: the"
            " archive holds each message once."
        ),
    )
    a_import.add_argument(
        "--name",
        required=True,
        metavar="NAME",
        help="Record the imported mail under this name, e.g. docuware-2019",
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

    asub.add_parser(
        "places",
        help="List the places the archive knows and what each holds",
        description=(
            "List every mailbox and folder the archive has mail from, every"
            " import, and everything `archive adopt` took in -- with how many"
            " messages each holds and when it was last written to. This is where"
            " the names come from that `db search --mailbox` and `--folder` take,"
            " and it is worth a look before `archive import --name` or"
            " `archive adopt --name`: a name already listed here is one you would"
            " be adding to."
        ),
    )

    a_adopt = asub.add_parser(
        "adopt",
        help="Take messages that belong to no place into one, under a name",
        description=(
            "Record where the messages came from that nothing records a place"
            " for. `archive check` reports them; this takes them in under a name"
            " you give, exactly as an import records what it brings in."
            " The name is your statement and not the archive's: use the import"
            " they came from if you know it, or `orphaned` to say that nobody"
            " knows any more. Messages that already have a place are left alone."
            " Nothing corrects the log afterwards, so try it with --dry-run"
            " first. Where the directory an import read from still exists,"
            " importing it again is the better move -- that records only what"
            " really lay in it."
        ),
    )
    a_adopt.add_argument(
        "--name",
        required=True,
        metavar="NAME",
        help="Record them under this name, e.g. docuware-2019 or orphaned",
    )
    a_adopt.add_argument(
        "--dry-run",
        action="store_true",
        help="Only count what would be recorded: nothing is written",
    )

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

    asub.add_parser(
        "migrate",
        help="Bring an archive written by an earlier version up to date",
        description=(
            "Bring an archive of any earlier shape up to the layout this version"
            " writes, and say what was lifted. This is the first thing to do"
            " after upgrading: until it has run, every other command refuses the"
            " archive rather than looking for the mail where it no longer is."
            " Nothing is deleted, a run that is interrupted is simply picked up"
            " by the next one, and on an archive that is already current it reads"
            " one small file and stops."
        ),
    )

    asub.add_parser(
        "compact",
        help="Consolidate the metadata log, dropping duplicate entries",
        description=(
            "Merge the many small metadata-log files an archive accumulates -- one"
            " per folder per backup, with entries repeated wherever a folder was"
            " read in full -- into one file per mailbox/folder holding each"
            " observation once. Lossless and safe to run at any time; the originals"
            " are removed only after the consolidated files are written and verified."
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

    build_db_parser(sub)
    return parser


def build_db_parser(sub: argparse._SubParsersAction) -> None:
    """The `db` command group: the archive's optional, throwaway query database.

    Its own group and not a corner of `archive`, because the database is not part
    of the archive in the way the mail and the log are. It holds nothing that is
    not already in there, it is built on demand, and every command here is free
    to say "that does not fit, build it again" -- which is exactly what no
    command touching the archive itself may ever say.
    """
    p_db = sub.add_parser(
        "db",
        help="Build, update, search and remove the archive's query database",
        description=(
            "The archive's query database: everything it knows about its mail --"
            " sender, recipients, subject, date, and which folder of which mailbox"
            " each message was seen in -- in a form that can be searched. It is"
            " optional and it is a copy, built from the archive and thrown away"
            " without loss; the archive itself never depends on it. It lives in the"
            f" archive as {commands.DEFAULT_DB_NAME}, and a backup keeps it in step"
            " when the configuration or --index-db asks for it."
        ),
    )
    dsub = p_db.add_subparsers(dest="db_command", metavar="<subcommand>", required=True)

    d_create = dsub.add_parser(
        "create",
        help="Build the query database from the archive",
        description=(
            "Build the query database from the metadata log and the messages it"
            " names: the log says where each was seen, the message itself supplies"
            " sender, subject and date. This is the expensive one -- every message"
            " is opened -- so it says how far it has got as it goes. Mail the log"
            " names nowhere is not in it; `archive check` reports such messages and"
            " `archive adopt` gives them a place. An existing database is refused:"
            " `db update` brings one up to date at a fraction of the cost."
        ),
    )
    d_create.add_argument(
        "--force",
        action="store_true",
        help="Build it again from scratch even if there is one, replacing it",
    )
    d_create.add_argument(
        "--temp-dir",
        type=pathlib.Path,
        metavar="DIR",
        help=(
            "Build the database under DIR and copy it in when it is done."
            " Worth it when the archive is on a network share, where writing the"
            " database as it grows costs more than reading the mail; on a local"
            " disk it is not. Named rather than guessed, because where there is"
            " somewhere fast with room is not something this program can know"
        ),
    )

    dsub.add_parser(
        "update",
        help="Bring the query database up to date with the archive",
        description=(
            "Take in what the archive has recorded since the database was last"
            " brought up to date, which costs a few small reads rather than a pass"
            " over every message. Builds one if there is none. A database written"
            " by another version of mailvault is left alone and reported, because"
            " it cannot be read -- build that one again."
        ),
    )

    dsub.add_parser(
        "drop",
        help="Delete the query database",
        description=(
            "Remove the query database from the archive. Nothing is lost that"
            " `db create` cannot produce again -- it holds no fact the archive"
            " does not -- which is why this asks nothing and takes no --force."
        ),
    )

    d_search = dsub.add_parser(
        "search",
        help="Find messages in the query database",
        description=(
            "Find archived messages by who sent them, who received them, what they"
            " are about, when they were sent, or where they were kept. Every filter"
            " given has to match; text matches anywhere in the value and ignores"
            " case. Prints the message ids `archive export` takes, so a search and"
            " an export make a pipeline."
        ),
    )
    d_search.add_argument("--from", dest="sender", metavar="TEXT", help="Sender address")
    d_search.add_argument("--to", dest="recipient", metavar="TEXT", help="Recipient address")
    d_search.add_argument("--subject", metavar="TEXT", help="Subject")
    d_search.add_argument("--mailbox", metavar="TEXT", help="Mailbox it was seen in")
    d_search.add_argument("--folder", metavar="TEXT", help="Folder it was seen in")
    d_search.add_argument(
        "--since",
        metavar="YYYY-MM-DD",
        help="Sent on this day or later; a message with no readable date matches neither",
    )
    d_search.add_argument(
        "--until",
        metavar="YYYY-MM-DD",
        help="Sent on this day or earlier",
    )
    d_search.add_argument(
        "--limit",
        type=int,
        metavar="N",
        help="Stop after this many, oldest first",
    )
    output = d_search.add_mutually_exclusive_group()
    output.add_argument(
        "--ids",
        action="store_true",
        help="Print message ids alone, one per line, for feeding to another command",
    )
    output.add_argument("--csv", action="store_true", help="Print as CSV, with a header row")
    output.add_argument("--json", action="store_true", help="Print as a JSON array")


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
        elif args.command == "db":
            exit_code = commands.run_db(args)
        # A report the size of a screen never leaves the buffer on its own, and a
        # buffer emptied after `main` has returned is emptied where nothing can
        # answer for it. Here it is still this run's business.
        sys.stdout.flush()
    except KeyboardInterrupt:
        log.warning("Interrupted!")
        exit_code = 130
    except BrokenPipeError:
        # `| head`, `| less` quit on the first page: nothing worth a word at any
        # level. Only the leftovers need somewhere to go, because the interpreter
        # flushes them on its way out, past everything here, and says `Exception
        # ignored` when they are refused. 141 is what SIGPIPE would have left
        # behind if Python let the signal through.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        exit_code = 141
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
