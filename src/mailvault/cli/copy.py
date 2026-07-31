"""Handler for the `copy` command: copy mails between two mailboxes."""

from __future__ import annotations

import argparse
import logging
import sys

from mailvault import conf, jobs

log = logging.getLogger(__name__)


def run(args: argparse.Namespace) -> int:
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
