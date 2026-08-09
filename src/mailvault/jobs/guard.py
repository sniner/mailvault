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

An archive that knows no mailboxes at all is the case that needs care, because
"no names" and "nothing here" are not the same thing. A fresh archive accepts
anything, rightly: there is nothing to contaminate, and every archive has to
start somewhere. But an archive filled by `archive import` holds mail and no
names whatsoever -- an import writes messages, not a metadata log, because
nobody told it which mailbox and folder they came from. Reading that as "empty"
waved every configuration through into an archive that was anything but, and
with `delete_after_export` the server copies went afterwards.

So the question is asked in two parts: are there names, and failing that, is
there mail. Only both answered no is an empty archive.
"""

from __future__ import annotations

import logging
import pathlib

from mailvault import conf
from mailvault.jobs.common import JobError
from mailvault.store import cas, heads, metalog

log = logging.getLogger(__name__)


def known_mailboxes(store_path: pathlib.Path) -> set[str]:
    """The mailbox names an archive has already seen.

    `heads/` answers this on its own and is what any archive written by a
    current version has. The metadata log is the fallback, and the reason there
    is one: an archive whose heads were lost, or which has not been migrated
    yet, still knows perfectly well who wrote into it, and a guard that waved
    everything through in exactly that case would be worth little.

    The names cannot be read off the head file names -- a slug is lossy and the
    identity is a hash -- so the files themselves are asked. That is cheap:
    there are as many as there are folders, not as there are messages.
    """
    names = heads.mailboxes(store_path / heads.DEFAULT_HEADS_DIR)
    if names:
        return names
    return metalog.mailboxes(store_path / metalog.DEFAULT_LOG_DIR)


def holds_messages(store_path: pathlib.Path) -> bool:
    """Whether the archive holds any mail at all.

    Only ever asked when no mailbox name could be read, and answered by the first
    entry the walk turns up rather than by counting: the question is whether
    there is anything here, and one message settles it. That matters over a
    network share, where the cheap answer is the difference between a guard and
    a delay -- an empty archive costs the walk of an empty tree, and a full one
    stops at its first shard.
    """
    return next(iter(cas.mail_store(store_path).walk()), None) is not None


def _refuse_nameless_archive(store_path: pathlib.Path, allow_new: bool) -> None:
    """Stop a run aimed at an archive that holds mail under no name at all.

    The archive built by `archive import` is the one this is for. It is as full
    as any other and says nothing about whose mail it is, so the guard has
    nothing to compare a job against -- and the wrong configuration is exactly
    as plausible here as anywhere else. Refused rather than waved through,
    because the flag that says "yes, really" costs one run and the mix-up costs
    an untangling.
    """
    if allow_new:
        log.warning(
            "%s: the archive holds mail but records no mailbox, allowed explicitly",
            store_path,
        )
        return
    raise JobError(
        f"{store_path}: the archive holds mail but records no mailbox at all, so there"
        f" is nothing here to tell the right configuration from the wrong one. Mail"
        f" brought in with `archive import` is always like this. Check that the"
        f" configuration and the archive belong together, then pass --allow-new-mailbox"
        f" to go ahead"
    )


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
        if not holds_messages(store_path):
            log.debug("no mailboxes recorded yet, every job may write here")
            return
        _refuse_nameless_archive(store_path, allow_new)
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
