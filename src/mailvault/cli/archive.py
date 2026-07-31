"""Handlers for the `archive` command group: local archive maintenance."""

from __future__ import annotations

import argparse
import logging

from mailvault import archive, cas, jobs

log = logging.getLogger(__name__)


def _archive(args: argparse.Namespace) -> archive.MailArchive:
    docuware = getattr(args, "docuware", False)
    cls = archive.DocuwareMailArchive if docuware else archive.MailArchive
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


def run(args: argparse.Namespace) -> int:
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
        jobs.update_db_from_archive(args.source, mailbox=args.mailbox)

    return 0
