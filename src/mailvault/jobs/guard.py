"""Refuse a run whose jobs do not belong to the archive they were aimed at.

The configuration file and the archive directory are two independent arguments,
and nothing about `-c archive-work.toml ~/mail/private` looks wrong until the
first message has been written. What follows is not a disaster -- the archive is
content-addressed, so nothing is overwritten and the metadata log records which
mailbox every message came from -- but untangling it afterwards is unpleasant,
and with `delete_after_export` the server copy is already gone.

So the run is stopped beforehand, using what the archive already knows about
itself: the mailbox names it has seen. A job whose name is among them has
written here before and is waved through; one that is not has to be confirmed.

The check is deliberately one-directional. A mailbox in the archive with no job
in the configuration is not reported -- removing a job, commenting one out or
picking a few with `--job` are all everyday things, and none of them can put a
message anywhere it does not belong. Only writing is capable of that, so only
writing is checked.

An archive that knows no mailboxes at all -- a new one, or a directory that does
not exist yet -- accepts anything: there is nothing there to contaminate, and
every archive has to start somewhere.
"""

from __future__ import annotations

import logging
import pathlib

from mailvault import conf
from mailvault.jobs.common import JobError
from mailvault.store import metalog, state

log = logging.getLogger(__name__)


def known_mailboxes(store_path: pathlib.Path) -> set[str]:
    """The mailbox names an archive has already seen.

    `state.json` answers this on its own and is what any archive written by a
    current version has. The metadata log is the fallback, and the reason there
    is one: an archive whose state file was lost, emptied or never written still
    knows perfectly well who wrote into it, and a guard that waved everything
    through in exactly that case would be worth little.
    """
    snapshot = state.SnapshotState.load(store_path / state.DEFAULT_STATE_NAME)
    names = snapshot.mailboxes()
    if names:
        return names
    return metalog.mailboxes(store_path / metalog.DEFAULT_LOG_DIR)


def check_jobs(
    store_path: pathlib.Path,
    jobs: list[conf.JobConfig],
    allow_new: bool = False,
) -> None:
    """Raise JobError when a job has never written into this archive.

    Nothing is run until this has passed, so a mismatched pair costs a message
    rather than a contaminated archive. `allow_new` is the deliberate override
    for the case the check cannot tell apart from a mix-up: a genuinely new job.
    """
    if not jobs:
        return
    known = known_mailboxes(store_path)
    if not known:
        log.debug("%s: no mailboxes recorded yet, every job may write here", store_path)
        return

    unknown = sorted({job.name for job in jobs} - known)
    if not unknown:
        return

    names = ", ".join(unknown)
    if allow_new:
        log.warning(
            "%s: %s has not written into this archive before, allowed explicitly",
            store_path,
            names,
        )
        return

    # No overlap at all is the signature of the mix-up this exists for: whatever
    # the configuration describes, this archive has never seen any of it. A
    # single unfamiliar name among familiar ones is far more likely to be a job
    # that was just added, so it is worth saying which of the two this looks like.
    if known.isdisjoint({job.name for job in jobs}):
        complaint = (
            f"none of its jobs ({names}) has ever written here -- this looks like the "
            f"wrong configuration for this archive"
        )
    else:
        complaint = f"{names} has not written here before"
    raise JobError(
        f"{store_path}: the archive holds {', '.join(sorted(known))}, and {complaint}. "
        f"Check that the configuration and the archive belong together, then pass "
        f"--allow-new-mailbox to go ahead"
    )
