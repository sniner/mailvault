"""Bringing one folder's archive back in step with the server, by listing it.

There are two ways to read a folder in full, and they differ in what they trust.

The **real** full read downloads every message and lets the content-addressed
storage decide by hash what is new. It trusts nothing, which is why `--full`
uses it: someone asking for that is asking not to be second-guessed.

The **synthetic** full read -- this module -- lists the folder, compares against
what the archive already holds, and downloads only the difference. Listing costs
a handful of requests where downloading costs one per message, so it is the one
that can be reached for automatically: after an upgrade that left no resume
point, or when a source invalidates the point it gave us.

What it trades for that is the comparison key. The archive is addressed by the
hash of the bytes, which the server does not know, so the only key both sides
share without transferring the message is the Message-ID.

It is compared by *count*, not by presence. A place can hold two messages with
the same Message-ID and different bytes -- those are two objects here -- and a
plain "is it archived?" would let the second pass as already there. Counting
catches it: the server showing an id twice where one copy is archived means one
is missing.

The bias that leaves runs the safe way. Byte-identical duplicates on the server
collapse into a single object in the storage, so every copy after the first
finds nothing left to claim and is downloaded again to be discarded. On a real
folder that is a few thousand needless downloads -- bandwidth, on a command that
runs rarely, against a message that would otherwise be missing for good.

What those needless downloads must not do is *count* as missing mail, and that
is the difference between the two numbers this reports. A Message-ID the archive
has never seen at this place is a gap. One whose copies are merely outnumbered is
not: it is a message that is archived, of which the server holds more copies than
a content-addressed store can hold objects. Counted together, a folder with two
thousand duplicates reported two thousand missing messages after every run, for
good, on an archive that was not missing one -- and told its owner to run
`--repair`, which fetched them all and changed nothing. They are still fetched,
because from the outside a second copy might be the byte-different version that
really is absent; they are just no longer called missing.
"""

from __future__ import annotations

import collections
import collections.abc
import dataclasses
import logging
import pathlib
from datetime import UTC, datetime

from mailvault import mailutils
from mailvault.backend import base
from mailvault.jobs.common import _seal_log
from mailvault.jobs.ledger import Claim, MessageIdLedger
from mailvault.store import cas, metalog

log = logging.getLogger(__name__)

# How often the two long passes say where they are. Both read one item at a
# time with nothing to show for it until they finish, so without this a large
# archive looks like a hung process -- and the local one is usually the slower
# of the two, because it is thousands of small reads over whatever the archive
# is mounted on.
ARCHIVE_PROGRESS_EVERY = 10_000
SERVER_PROGRESS_EVERY = 5_000
# Downloading is the slowest of the three by an order of magnitude -- a round
# trip per message, measured at 131 ms over a real mailbox -- so it says where it
# is far more often than the passes that only read.
FETCH_PROGRESS_EVERY = 250


@dataclasses.dataclass
class ReconcileResult:
    """Outcome of comparing one server folder against the local archive.

    `missing` and `extra_copies` are the two ways a server message can fail to be
    accounted for, and they are kept apart because only the first is a gap. The
    second is a copy too many of something already archived -- usually one the
    store cannot hold twice, occasionally a byte-different version that really is
    absent, and never distinguishable until it has been fetched.

    `restored` counts the gaps a repair pass closed. `recovered_copies` counts the
    extra copies that turned out to be new content after all -- the case that
    justifies fetching them, and on a folder of byte-identical duplicates it
    stays at zero however many are fetched. Keeping it apart from `restored` is
    what stops a run from reporting more messages restored than were missing.
    """

    folder: str
    on_server: int = 0
    missing: int = 0
    extra_copies: int = 0
    restored: int = 0
    recovered_copies: int = 0
    failed: int = 0
    sealed: bool = True

    @property
    def complete(self) -> bool:
        """True when nothing was left unaccounted for.

        Says nothing about the log: a pass can download every missing message and
        still fail to write down where they belong. `sealed` answers that, and a
        caller that advances a resume point has to ask both -- see
        `backup._catch_up_folder`.
        """
        return self.failed == 0


def places_from_log(log_root: pathlib.Path) -> dict[tuple[str, str | None], set[str]]:
    """Read the whole log once into `(mailbox, folder) -> store ids`."""
    places: dict[tuple[str, str | None], set[str]] = {}
    for logfile in metalog.read_all(log_root):
        if logfile.mailbox is None:
            continue
        places.setdefault((logfile.mailbox, logfile.folder), set()).update(logfile.store_ids)
    return places


class ArchivedPlaces:
    """What the archive already holds, read from the log at most once per run.

    One archive has one metadata log, and it names every place in it -- so
    reading it per job is reading the same 60 files five times to filter each
    time by a different mailbox. Measured on a real run: five jobs, five
    identical reads, five identical lines reporting 219,962 messages in 59
    places, 2.9 seconds of a 60-second `verify`. Identical output is the tell.

    Lazy, because in the steady state nobody asks: a backup only needs it for a
    folder without a resume point, and where every folder has one the log stays
    unread.

    Its scope is one run, and that is why the caller makes it rather than each
    job. A run that writes to the log leaves it stale afterwards, which costs
    nothing: what a job writes belongs to that job's own places, and every other
    job asks about its own.
    """

    def __init__(self, log_root: pathlib.Path):
        self._log_root = log_root
        self._places: dict[tuple[str, str | None], set[str]] | None = None

    def of(self, job_name: str, folder: str) -> set[str]:
        return self._all().get((job_name, folder), set())

    def _all(self) -> dict[tuple[str, str | None], set[str]]:
        if self._places is None:
            # Named without a job in front of it: the log belongs to the
            # archive, and the archive was named once when the run started.
            log.info("reading the metadata log")
            self._places = places_from_log(self._log_root)
            log.info(
                "%s message(s) recorded in %s place(s)",
                f"{sum(len(ids) for ids in self._places.values()):,}",
                len(self._places),
            )
        return self._places


def archived_message_counts(
    store: cas.ContentAddressedStorage,
    store_ids: collections.abc.Iterable[str],
    log_ctx: str = "",
) -> collections.Counter[str]:
    """Count the archived copies per normalised Message-ID.

    Counted rather than collected, because the archive is addressed by content:
    two messages sharing a Message-ID and differing in their bytes are two
    objects here, and only a count can tell that the server showing that id twice
    means one of them is missing.

    The log says which messages are at a place; the Message-ID itself is only in
    the message, so each one is parsed. That is one header read per archived
    message, and the database that used to answer this is no longer part of the
    archive.

    Reports its progress under `log_ctx` while doing so. On a large folder this
    is the longest silence in the whole operation, and it is spent on the *local*
    archive rather than on the server, which is not what one would guess from the
    outside.

    Messages without a usable Message-ID are omitted: they cannot serve as a
    comparison key and must count as "not present" so they are re-fetched, which
    is harmless because the storage deduplicates by content.
    """
    known: collections.Counter[str] = collections.Counter()
    read = 0
    for store_id in store_ids:
        read += 1
        if log_ctx and read % ARCHIVE_PROGRESS_EVERY == 0:
            log.info("%s: %s message(s) read from the archive", log_ctx, f"{read:,}")
        path = store.locate(store_id, exists=True)
        if path is None:
            continue
        try:
            header = mailutils.decode_email_header(store.read_header(path))
        except (OSError, ValueError) as exc:
            log.warning("%s: unreadable, not counted as archived: %s", store.where(path), exc)
            continue
        known[mailutils.normalize_message_id(mailutils.message_id(header))] += 1
    known.pop("", None)
    return known


def reconcile_folder(
    mb: base.MailboxClient,
    store: cas.ContentAddressedStorage,
    log_root: pathlib.Path,
    heads_root: pathlib.Path,
    archived: set[str],
    job_name: str,
    folder: str,
    repair: bool = False,
) -> ReconcileResult:
    """List one folder, compare it against the archive, optionally fetch the gap.

    With `repair` false this only reports, which is what `verify` does on its
    own. With it true the missing messages are downloaded and their locations
    written to the log, which is what makes this a full read that skips what is
    already there.
    """
    ctx = f"{job_name}::{folder}"
    # Said before the work, not after it: this reads one header per archived
    # message and has nothing to report until every one of them is done.
    log.info("%s: reading %s archived message(s)", ctx, f"{len(archived):,}")
    known = MessageIdLedger(archived_message_counts(store, archived, log_ctx=ctx))
    log.info("%s: %s message(s) in archive", ctx, f"{len(known):,}")

    result = ReconcileResult(folder=folder)
    # Each one carries which of the two it is: the downloader treats them alike,
    # and what the pass reports afterwards depends on telling them apart.
    wanted: list[tuple[base.MessageRef, Claim]] = []
    log.info("%s: listing the folder on the server", ctx)
    for ref in mb.message_index(folder):
        result.on_server += 1
        claim = known.claim(mailutils.normalize_message_id(ref.message_id))
        if claim is Claim.ABSENT:
            result.missing += 1
            wanted.append((ref, claim))
        elif claim is Claim.EXHAUSTED:
            result.extra_copies += 1
            wanted.append((ref, claim))
        if result.on_server % SERVER_PROGRESS_EVERY == 0:
            log.info("%s: %s message(s) indexed", ctx, f"{result.on_server:,}")

    log.info(
        "%s: %s of %s message(s) on the server are not archived",
        ctx,
        f"{result.missing:,}",
        f"{result.on_server:,}",
    )
    if result.extra_copies:
        # Said separately, and said at all: on a folder that holds duplicates
        # this is where the download count comes from, and a reader watching
        # thousands of messages being fetched after "0 not archived" would
        # otherwise have no way to account for them.
        log.info(
            "%s: %s further copy/copies of already-archived message(s), fetched to"
            " find out whether they differ",
            ctx,
            f"{result.extra_copies:,}",
        )
    if not repair or not wanted:
        return result

    # A fetched message whose place is not yet recorded needs its location in the
    # log -- otherwise nothing says it belongs here. One that *is* recorded needs
    # nothing: the log holds observations, a repeated observation says no more
    # than the first, and `compact` exists to take such repeats back out. Writing
    # them and undoing them later is work in both directions.
    #
    # It is what made a repair that recovered nothing still change the archive:
    # 1,729 duplicates fetched, 1,729 entries written, a new log file and a new
    # link in the chain, every time it ran.
    log_writer = metalog.LogWriter(log_root, heads_root)
    recorded = set(archived)
    log.info("%s: fetching %s message(s)", ctx, f"{len(wanted):,}")
    for fetched, (ref, claim) in enumerate(wanted, start=1):
        if fetched % FETCH_PROGRESS_EVERY == 0:
            log.info("%s: %s of %s fetched", ctx, f"{fetched:,}", f"{len(wanted):,}")
        label = ref.message_id or ref.msg_id
        try:
            msg = mb.fetch_message(ref.msg_id, folder)
        except Exception as exc:
            log.error("%s: download failed for %s: %s", ctx, label, exc)
            result.failed += 1
            continue
        try:
            status, store_id, _path = store.add(msg)
            if store_id not in recorded:
                log_writer.add(job_name, [folder], store_id)
                recorded.add(store_id)
        except Exception as exc:
            log.exception("%s: storing %s failed: %s", ctx, label, exc)
            result.failed += 1
            continue
        if claim is Claim.ABSENT:
            # A gap closed, whether or not the bytes were new here: the archive
            # may well hold this message under another folder, and what was
            # missing at *this* place is the record that it belongs here too.
            result.restored += 1
            log.info("%s: restored %s: %s id=%s", ctx, label, status, store_id)
        elif status == "NEW":
            # An extra copy that was not a duplicate after all -- the byte-
            # different second version, and the reason these get fetched.
            result.recovered_copies += 1
            log.info(
                "%s: %s: a further copy whose content differs, kept id=%s",
                ctx,
                label,
                store_id,
            )
        else:
            # The ordinary outcome for a further copy, and the reason it is not
            # said out loud: on a folder that holds thousands of duplicates this
            # is thousands of lines reporting that nothing happened -- under the
            # word "restored", which is what it was doing before.
            log.debug("%s: %s: a further copy, identical to the archived one", ctx, label)

    # Whether the locations reached disk is the caller's business: a pass that
    # fetched every message and could not write down where any of them belongs
    # must not move a resume point past them. `_seal_log` reports that through
    # its return value and nowhere else.
    result.sealed = _seal_log(log_writer, datetime.now(UTC))
    return result
