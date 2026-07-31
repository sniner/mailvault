"""Manage and maintain the local email archive (import, compress, statistics)."""

from __future__ import annotations

import argparse
import logging
import pathlib

from mailvault import archive, cas, jobs
from mailvault.cli import get_version, setup_logger

log = logging.getLogger(__name__)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=__doc__,
    )

    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {get_version()}",
    )
    parser.add_argument("--logfile", type=pathlib.Path, help="Log file path")
    parser.add_argument("--verbose", action="store_true", help="Set log level to DEBUG")

    subparsers = parser.add_subparsers(dest="subcommand")

    stats_parser = subparsers.add_parser("stats", description="Show stats of email archive")
    stats_parser.add_argument(
        "--docuware",
        action="store_true",
        help="Email archive is Docuware archive",
    )
    stats_parser.add_argument("source", type=pathlib.Path, help="Email archive directory")

    import_parser = subparsers.add_parser(
        "import",
        description="Import emails from source archive to destination",
    )
    import_parser.add_argument(
        "--docuware",
        action="store_true",
        help="Source archive is a Docuware email archive",
    )
    import_parser.add_argument(
        "--move",
        action="store_true",
        help="Move emails, i.e. remove emails from source after backup",
    )
    import_parser.add_argument(
        "--compress",
        action="store_true",
        help="Compress stored emails with zstd",
    )
    import_parser.add_argument(
        "source",
        type=pathlib.Path,
        help="Directory from which you want to copy/move the mails",
    )
    import_parser.add_argument("destination", type=pathlib.Path, help="Archive directory")

    addr_parser = subparsers.add_parser(
        "addresses",
        description="Show mail addresses of all emails in archive",
    )
    addr_parser.add_argument(
        "--docuware",
        action="store_true",
        help="Directory is a Docuware archive",
    )
    addr_parser.add_argument(
        "source",
        type=pathlib.Path,
        help="Archive directory",
    )

    compress_parser = subparsers.add_parser(
        "compress",
        description="Compress uncompressed files in archive with zstd",
    )
    compress_parser.add_argument(
        "source",
        type=pathlib.Path,
        help="Email archive directory",
    )

    decompress_parser = subparsers.add_parser(
        "decompress",
        description="Decompress compressed files in archive",
    )
    decompress_parser.add_argument(
        "source",
        type=pathlib.Path,
        help="Email archive directory",
    )

    dbupd_parser = subparsers.add_parser(
        "db-from-archive",
        description="Update metadata database by reading all archive items",
    )
    dbupd_parser.add_argument(
        "--mailbox",
        type=str,
        help="Mailbox identifier (matching a job file entry)",
    )
    dbupd_parser.add_argument(
        "source",
        type=pathlib.Path,
        help="Email archive directory",
    )

    args = parser.parse_args()
    if args.subcommand is None:
        parser.print_help()
        raise SystemExit(2)
    return args


def run() -> None:
    args = parse_arguments()

    setup_logger(
        logfile=args.logfile,
        loglevel=logging.DEBUG if args.verbose else logging.INFO,
    )
    log.info("START")

    def _archive(path: pathlib.Path) -> archive.MailArchive:
        docuware = getattr(args, "docuware", False)
        return (archive.DocuwareMailArchive if docuware else archive.MailArchive)(path)

    if args.subcommand == "stats":
        count, size = _archive(args.source).stats()
        units = ["bytes", "KiB", "MiB", "GiB", "TiB"]
        hr_size = float(size)
        unit = units[0]
        for unit in units:
            if hr_size < 1024 or unit == units[-1]:
                break
            hr_size /= 1024
        print(f"{args.source}: {count:,} emails, {hr_size:.1f} {unit} total")
    elif args.subcommand == "addresses":
        for where, addr in _archive(args.source).addresses():
            print(where, addr)
    elif args.subcommand == "import":
        source = _archive(args.source)
        destination = cas.ContentAddressedStorage(
            args.destination, suffix=".eml", compress=args.compress
        )
        source.archive_to_cas(destination, move=args.move)
    elif args.subcommand == "compress":
        store = cas.ContentAddressedStorage(args.source, suffix=".eml")
        compressed, skipped = store.compress_all()
        print(f"{args.source}: {compressed:,} files compressed, {skipped:,} already compressed")
    elif args.subcommand == "decompress":
        store = cas.ContentAddressedStorage(args.source, suffix=".eml")
        decompressed, skipped = store.decompress_all()
        print(f"{args.source}: {decompressed:,} files decompressed, {skipped:,} already plain")
    elif args.subcommand == "db-from-archive":
        jobs.update_db_from_archive(args.source, mailbox=args.mailbox)


def main() -> int:
    try:
        run()
    except KeyboardInterrupt:
        log.warning("Interrupted!")
        return 130
    except Exception as exc:
        log.exception("Fatal error: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())