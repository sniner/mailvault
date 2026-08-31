"""`mcp` -- serving the archive's query layer to an AI client.

The checks a server owes its operator before it opens: that this is an archive,
that the query database is there and readable, and that an address anyone can
reach is never served by accident. The server itself lives in
`mailvault.mcpserver`, behind the optional `mcp` extra; this module runs
without it, so the refusals here arrive whether or not the extra is installed.
"""

from __future__ import annotations

import argparse
import ipaddress
import logging

from mailvault import jobs
from mailvault.cli.common import (
    DEFAULT_DB_NAME,
    archive_path,
    require_archive,
)

log = logging.getLogger(__name__)


def parse_listen(value: str) -> tuple[str, int]:
    """HOST:PORT as `--listen` takes it, IPv6 in the usual brackets."""
    host, sep, port_text = value.rpartition(":")
    if not sep or not host:
        raise jobs.JobError(
            f"--listen {value}: not HOST:PORT -- e.g. 127.0.0.1:56789, or [::1]:56789 for IPv6"
        )
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    try:
        port = int(port_text)
    except ValueError:
        port = -1
    if not 0 < port < 65536:
        raise jobs.JobError(
            f"--listen {value}: {port_text} is not a port -- a number from 1 to 65535"
        )
    return host, port


def is_loopback(host: str) -> bool:
    """Whether an address stays on this machine.

    A hostname other than localhost may resolve anywhere, so it counts as not
    loopback: the flag this gates errs toward asking rather than serving.
    """
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host.lower() == "localhost"


def run(args: argparse.Namespace) -> int:
    """Run `mcp`: check what the server needs, then hand over to it.

    Everything that can be refused is refused here, before the SDK is even
    imported -- say no before the expensive part, and before a client is
    listening on the other end of stdout.
    """
    archive = archive_path(args)
    require_archive(archive)

    db_path = archive / DEFAULT_DB_NAME
    state = jobs.freshness(archive, db_path)
    complaint = state.complaint(db_path.name)
    if complaint and not state.is_usable:
        # No database, another version's database, an unmigrated archive: the
        # server answers searches from this file, so without it there is
        # nothing to serve. The complaint already names the state and the move.
        raise jobs.JobError(complaint)
    if complaint:
        # Behind the archive is not a reason to refuse: the server says so in
        # every search result. Said once here too, for the operator who starts
        # the server and never sees those results.
        log.warning("%s", complaint)

    listen: tuple[str, int] | None = None
    if args.listen is not None:
        listen = parse_listen(args.listen)
        if not is_loopback(listen[0]) and not args.allow_remote:
            raise jobs.JobError(
                f"--listen {args.listen}: not a loopback address -- anyone who"
                f" reaches this port can read your mail, because the server"
                f" itself asks for no authentication. Pass --allow-remote when"
                f" something in front of it (a reverse proxy, a firewall)"
                f" guards it"
            )

    try:
        # The one lazy import in the CLI, and the point of it: this is the
        # probe for the optional extra. At the top of the module it would take
        # every other command down with it on an install without the extra.
        from mailvault import mcpserver
    except ImportError:
        raise jobs.JobError(
            "the MCP server needs the Python package 'mcp', which this install"
            " does not carry -- reinstall with the extra:"
            " uv tool install 'mailvault[mcp]' (pipx and pip take the same name)"
        ) from None

    if listen is None:
        log.info("MCP server speaking on stdin/stdout")
    else:
        log.info("MCP server listening on http://%s:%d/mcp", *listen)
        if not is_loopback(listen[0]):
            log.warning(
                "serving without authentication to everyone who can reach"
                " %s:%d -- --allow-remote says something in front guards it",
                *listen,
            )
    mcpserver.serve(archive, listen)
    return 0
