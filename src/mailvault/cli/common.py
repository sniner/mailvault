"""What every command group needs: which archive, and how a list is printed.

Deliberately small. What lives here is what more than one group asks for -- where
the archive is, whether that directory is one at all, and the two shapes every
report is built from. Anything one group alone needs stays with that group: a
helper in a common module is read by everybody and owned by nobody.
"""

from __future__ import annotations

import argparse
import logging
import pathlib

from mailvault import conf, jobs, utils
from mailvault.backend import base
from mailvault.store import marker

log = logging.getLogger(__name__)


# Failures that are already understood by the time they get here: a broken
# config, a refused operation, a mailbox that said no. There is nothing to
# debug in them, so they are reported as one line and the traceback is left to
# the errors nobody anticipated -- where the call stack is the only clue. The
# traceback is still there under `--verbose` for the rare case it is wanted.
EXPECTED_ERRORS = (conf.ConfigError, jobs.JobError, base.MailboxError, marker.FormatError)

# The configuration an archive carries. Named after the tool rather than after
# its purpose -- `config.toml` would be the better name inside an archive, where
# nothing else competes for it, but this is the same file in both roles, and the
# other role is the directory one happens to be standing in. That is a shared
# name space: a `config.toml` there belongs to whatever else lives in that
# directory, and reading one by accident is not a theoretical worry.
DEFAULT_CONFIG_NAME = "mailvault.toml"

# The query database, inside the archive. Named here as well as in `jobs.db`
# because the help texts talk about it and a help text that names a different
# file than the code writes is worse than one that names none.
DEFAULT_DB_NAME = jobs.DEFAULT_QUERY_DB_NAME


def archive_path(args: argparse.Namespace) -> pathlib.Path:
    """The archive a command works on: `--archive`, or the directory one is in.

    Two independent knobs, and nothing derived between them -- this is the only
    place an archive comes from. A configuration used to be able to name one,
    which cannot work across machines: the NAS hangs at a different path on each
    of them while the configuration sits in a home directory, so there is no
    path that is right on both. A configuration *inside* the archive has that
    distance by construction, and then there is nothing left for it to say.
    """
    if args.archive is not None:
        return args.archive
    return pathlib.Path.cwd()


def config_file(args: argparse.Namespace, archive: pathlib.Path) -> pathlib.Path:
    """The configuration to read: `--config`, or the one the archive carries."""
    if args.config is not None:
        return args.config
    return archive / DEFAULT_CONFIG_NAME


def require_archive(archive: pathlib.Path) -> None:
    """Stop a command that was pointed at something which is not an archive.

    The mark is the whole test, the way `.git` is for a repository. Before this,
    every command opened `<directory>/mail` and worked on whatever it found --
    which on an archive from before 0.10 is nothing at all, because the messages
    are still in the root. `archive check` then reported a healthy 131,000-message
    archive as a total loss, and `verify --repair` set about downloading the
    mailbox a second time.

    Both cases the mark cannot tell apart get named, because the answer differs:
    an older archive is lifted, a wrong directory is left alone.
    """
    if marker.is_archive(archive):
        return
    raise jobs.JobError(
        f"{archive}: not a mailvault archive. Make one here with"
        f" `mailvault archive init`. If it is an old mailvault archive,"
        f" migrate it with `mailvault archive migrate`"
    )


# --- folders / backup / verify -------------------------------------------------


# How many of a kind a report names before it stops listing them. A check on a
# damaged archive can find tens of thousands; the count is the finding, the
# names are there to give someone a place to start.
REPORT_LIMIT = 20


def report_items(
    items: list[str],
    singular: str,
    finding: str = "",
    plural: str | None = None,
) -> None:
    """Print a finding's count and the first few of whatever it found.

    What each line names depends on what the finding is about. A message is
    named by its id, because that is what every other command takes and the
    only handle its owner has any use for; where the file happens to lie is the
    store's business. A finding *about a file* -- one that is not a message at
    all, or a log file -- names the path, because there the file is the thing.

    The count and the noun come from `utils.counted`, so what follows has to
    read the same whether there is one of them or a thousand -- which is why
    these findings say "damaged" rather than "is damaged". Where that cannot be
    had, the finding is written out in both forms instead.
    """
    if not items:
        return
    print(f"{utils.counted(len(items), singular, plural)} {finding}".rstrip())
    for item in items[:REPORT_LIMIT]:
        print(f"  {item}")
    if len(items) > REPORT_LIMIT:
        print(f"  ... and {len(items) - REPORT_LIMIT:,} more")


def shorten(value: str | None, width: int) -> str:
    if not value:
        return ""
    collapsed = " ".join(value.split())
    if len(collapsed) <= width:
        return collapsed
    return collapsed[: width - 1] + "…"
