"""Bringing one folder's archive back in step with the server, by listing it.

There are two ways to read a folder in full, and they differ in what they trust.

The **real** full read downloads every message and lets the content-addressed
storage decide by hash what is new. It trusts nothing, which is why `--full`
uses it.

The **synthetic** full read, this module, lists the folder, compares the listing
with what the archive holds at that place, and downloads only what the
comparison cannot settle. Listing costs a handful of requests where downloading
costs one per message, so the backup uses it by itself: after an upgrade that
left no resume point, after a pass that broke off, or when a source voids the
point it gave us. `verify` uses it too.

The only key the server and the archive share without a download is the
Message-ID, and it is not reliable. The server's value can differ from the one
in the message (Graph's `internetMessageId`, common in Sent Items), the message
can carry none while the server lists one, and different messages can share
one. So the Message-ID decides only the plain case: an id the archive holds at
least as often as the server lists it. Everything else is downloaded and decided
by its hash, which is exact:

- an id the archive does not hold at this place
- an id the server lists more often than the archive holds it, every copy of it,
  because the id cannot say which of them is the archived one
- an id one of whose archived copies turned out to be a server message listed
  under another id: that copy no longer counts for its own id, which can leave
  the id outnumbered

A wrong guess here costs downloads, never mail. A backup pays them once and
starts a resume point afterwards; `verify` pays them on every run.
"""

from __future__ import annotations

import collections
import collections.abc
import dataclasses
import logging
import pathlib
from datetime import UTC, datetime
from typing import Any

from mailvault import mailutils, utils
from mailvault.backend import base
from mailvault.jobs.common import PassLog
from mailvault.jobs.ledger import Match, match_by_message_id, same_message_id
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

    `compared` counts the server messages downloaded because their Message-ID
    did not settle whether they are archived, and `missing` those of them whose
    content is not archived at this place. `restored` counts the missing ones a
    repair pass stored and recorded.

    `unchecked` counts the messages that could not be downloaded, so nothing is
    known about them. `failed` adds the missing ones a repair could not store;
    it is the count that holds a resume point back.

    `unfetched` is what `base.BackupResult.unfetched` is: the failed downloads,
    by backend id, that a backup may fetch again by themselves next run.
    """

    folder: str
    on_server: int = 0
    compared: int = 0
    missing: int = 0
    restored: int = 0
    unchecked: int = 0
    failed: int = 0
    sealed: bool = True
    unfetched: list[Any] = dataclasses.field(default_factory=list)

    @property
    def complete(self) -> bool:
        """True when nothing was left unaccounted for.

        Says nothing about the log: a pass can download every missing message and
        still fail to write down where they belong. `sealed` answers that, and a
        caller that advances a resume point has to ask both -- see
        `backup._catch_up_folder`.
        """
        return self.failed == 0

    @property
    def resumable(self) -> bool:
        """True if every failure is a message in `unfetched`."""
        return self.failed == len(self.unfetched)

    @property
    def outstanding(self) -> int:
        """Server messages not known to be archived at this place after the pass."""
        return self.missing - self.restored + self.unchecked


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
                "%s recorded in %s",
                utils.counted(sum(len(ids) for ids in self._places.values()), "message"),
                utils.counted(len(self._places), "place"),
            )
        return self._places


def archived_message_ids(
    store: cas.ContentAddressedStorage,
    store_ids: collections.abc.Iterable[str],
    log_ctx: str = "",
) -> dict[str, str]:
    """Read the normalised Message-ID of each archived message, by store id.

    A message without a usable Message-ID maps to "". A store id the store no
    longer holds, or cannot read, is left out: it does not count as archived, so
    a server message with that content is restored.

    The log says which messages are at a place; the Message-ID itself is only in
    the message, so each one is parsed. That is one header read per archived
    message, and the database that used to answer this is no longer part of the
    archive.

    Reports its progress under `log_ctx` while doing so. On a large folder this
    is the longest silence in the whole operation, and it is spent on the *local*
    archive rather than on the server, which is not what one would guess from the
    outside.

    Each entry is opened by its id rather than looked up first, which is one
    round trip instead of two -- the same reason `db._insert_message` does it
    that way. Asking `locate(exists=True)` where the file is costs up to two
    `stat()` calls (a store id names two candidates, `.eml` and `.eml.zst`)
    before the `open()` that actually reads, and every one of them goes over the
    share this pass is already the slowest part of.
    """
    known: dict[str, str] = {}
    for read, store_id in enumerate(store_ids, start=1):
        if log_ctx and read % ARCHIVE_PROGRESS_EVERY == 0:
            log.info("%s: %s read from the archive", log_ctx, utils.counted(read, "message"))
        try:
            header = mailutils.decode_email_header(store.read_header_of(store_id))
        except FileNotFoundError:
            # The log names it, the store no longer holds it. Expected rather
            # than wrong -- a message can have gone since it was recorded -- and
            # not counted as archived is exactly the answer the caller wants.
            continue
        except (OSError, ValueError) as exc:
            log.warning("%s: unreadable, not counted as archived: %s", store_id, exc)
            continue
        known[store_id] = mailutils.normalize_message_id(mailutils.message_id(header))
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
    """List one folder, compare it against the archive, and settle what is unclear.

    The messages the Message-ID does not settle are downloaded and compared by
    hash either way. With `repair` false nothing is written, which is what
    `verify` does on its own. With it true the missing messages are stored and
    their locations written to the log, which makes this a full read that skips
    what is already there.
    """
    ctx = f"{job_name}::{folder}"
    # Said before the work, not after it: this reads one header per archived
    # message and has nothing to report until every one of them is done.
    log.info("%s: reading %s", ctx, utils.counted(len(archived), "archived message"))
    present = archived_message_ids(store, archived, log_ctx=ctx)
    log.info("%s: %s in archive", ctx, utils.counted(len(present), "message"))

    result = ReconcileResult(folder=folder)
    log.info("%s: listing the folder on the server", ctx)
    listed = _list_folder(mb, folder, ctx)
    result.on_server = len(listed)

    if not repair:
        _Comparison(mb, store, job_name, folder, present, result).run(listed)
    else:
        # The log is sealed in batches and once more on the way out, however
        # the loop ends: a catch-up after an interrupted first backup can run
        # for hours too.
        with PassLog(log_root, heads_root, datetime.now(UTC)) as pass_log:
            comparison = _Comparison(mb, store, job_name, folder, present, result)
            comparison.repair_into(pass_log, set(archived))
            comparison.run(listed)
        # Whether the locations reached disk is the caller's business: a pass
        # that fetched every message and could not write down where any of them
        # belongs must not move a resume point past them.
        result.sealed = pass_log.sealed

    log.info(
        "%s: %s of %s on the server are not archived",
        ctx,
        f"{result.missing:,}",
        utils.counted(result.on_server, "message"),
    )
    return result


def _list_folder(
    mb: base.MailboxClient,
    folder: str,
    ctx: str,
) -> list[tuple[base.MessageRef, str]]:
    """The folder's messages with their normalised Message-IDs, in listing order."""
    listed: list[tuple[base.MessageRef, str]] = []
    for ref in mb.message_index(folder):
        listed.append((ref, mailutils.normalize_message_id(ref.message_id)))
        if len(listed) % SERVER_PROGRESS_EVERY == 0:
            log.info("%s: %s indexed", ctx, utils.counted(len(listed), "message"))
    return listed


class _Comparison:
    """Download what the Message-ID cannot settle, and decide it by hash.

    `present` maps the store ids archived at this place to the Message-IDs their
    headers carry. Without `repair_into` nothing is written.
    """

    def __init__(
        self,
        mb: base.MailboxClient,
        store: cas.ContentAddressedStorage,
        job_name: str,
        folder: str,
        present: dict[str, str],
        result: ReconcileResult,
    ):
        self._mb = mb
        self._store = store
        self._job_name = job_name
        self._folder = folder
        self._ctx = f"{job_name}::{folder}"
        self._present = present
        self._result = result
        self._pass_log: PassLog | None = None
        self._recorded: set[str] = set()

    def repair_into(self, pass_log: PassLog, recorded: set[str]) -> None:
        """Store the missing messages and record their places in `pass_log`.

        `recorded` holds the store ids already logged at this place and grows as
        messages are added.
        """
        self._pass_log = pass_log
        self._recorded = recorded

    def run(self, listed: list[tuple[base.MessageRef, str]]) -> None:
        """Compare in rounds until the Message-ID settles everything not downloaded.

        A round downloads every message the ids do not settle. One that turns
        out to be an archived message the server lists under another id takes
        that message out of the count for the id it carries, and the next round
        asks the ledger again about the messages not downloaded yet. Each round
        downloads at least one message, so the loop ends.
        """
        counts = collections.Counter(key for key in self._present.values() if key)
        withdrawn: set[str] = set()
        pending = listed
        first = True
        while pending:
            matches = match_by_message_id(counts, [key for _ref, key in pending])
            wanted = [item for item, m in zip(pending, matches) if m is not Match.ARCHIVED]
            if not wanted:
                return
            self._announce(matches, first)
            first = False
            pending = [item for item, m in zip(pending, matches) if m is Match.ARCHIVED]
            found = self._compare(wanted)
            if found is None:
                return
            for key, store_id in found:
                archived_key = self._present[store_id]
                if (
                    archived_key
                    and store_id not in withdrawn
                    and not same_message_id(key, archived_key)
                ):
                    withdrawn.add(store_id)
                    counts[archived_key] -= 1

    def _announce(self, matches: list[Match], first: bool) -> None:
        unknown = matches.count(Match.UNKNOWN)
        outnumbered = matches.count(Match.OUTNUMBERED)
        if not first:
            log.info(
                "%s: %s more to compare: an archived message with their Message-ID"
                " is listed on the server under another one",
                self._ctx,
                utils.counted(unknown + outnumbered, "message"),
            )
            return
        if unknown:
            log.info(
                "%s: %s under a Message-ID not archived here, downloaded to compare"
                " their content",
                self._ctx,
                utils.counted(unknown, "message"),
            )
        if outnumbered:
            # On a folder with duplicates most downloads come from here, and a
            # complete archive downloading thousands of messages needs a reason
            # in the log.
            log.info(
                "%s: %s under a Message-ID the server lists more often than the"
                " archive holds it, downloaded to compare their content",
                self._ctx,
                utils.counted(outnumbered, "message"),
            )

    def _compare(
        self,
        wanted: list[tuple[base.MessageRef, str]],
    ) -> list[tuple[str, str]] | None:
        """Download `wanted` and decide each by hash.

        Returns `(Message-ID, store id)` for each one archived at this place, or
        None when the connection was lost: every message after that one would
        fail the same way, each with an error line of its own.
        """
        found: list[tuple[str, str]] = []
        for position, (ref, key) in enumerate(wanted, start=1):
            if position % FETCH_PROGRESS_EVERY == 0:
                log.info(
                    "%s: %s of %s fetched",
                    self._ctx,
                    f"{position:,}",
                    f"{len(wanted):,}",
                )
            try:
                store_id = self._compare_one(ref)
            except base.ConnectionLost as exc:
                not_fetched = len(wanted) - position + 1
                utils.log_failure(
                    log,
                    exc,
                    "%s: %s not fetched",
                    self._ctx,
                    utils.counted(not_fetched, "message"),
                )
                self._result.unchecked += not_fetched
                self._result.failed += not_fetched
                return None
            if store_id is not None:
                found.append((key, store_id))
        return found

    def _compare_one(self, ref: base.MessageRef) -> str | None:
        """Download one message; return its store id if it is archived here."""
        label = ref.message_id or ref.msg_id
        try:
            fetched = self._mb.fetch_message(ref.msg_id, self._folder)
        except base.MessageGone:
            log.info("%s: %s is no longer in the folder", self._ctx, label)
            return None
        except base.ConnectionLost:
            raise
        except Exception as exc:
            log.error("%s: download failed for %s: %s", self._ctx, label, exc)
            self._result.unchecked += 1
            self._result.failed += 1
            self._result.unfetched.append(ref.msg_id)
            return None
        self._result.compared += 1
        store_id = self._store.hashval(fetched.body)
        if store_id in self._present:
            log.debug("%s: %s is archived, id=%s", self._ctx, label, store_id)
            return store_id
        self._result.missing += 1
        if self._pass_log is None:
            log.info("%s: not archived: %s", self._ctx, label)
        else:
            self._restore(label, fetched, self._pass_log)
        return None

    def _restore(self, label: str, fetched: base.Fetched, pass_log: PassLog) -> None:
        """Store a missing message and record where it was seen."""
        try:
            status, store_id, _path = self._store.add(fetched.body)
            # Recorded only where the place is not in the log yet. A message the
            # log names here and the store had lost needs its bytes back, not a
            # second entry saying the same thing.
            if store_id not in self._recorded:
                # The source's places, not the folder this pass happens to be
                # walking. A Gmail message carries labels, and a repair that
                # wrote `[folder]` put it back with the others stripped off.
                #
                # They came with the message, out of the same read: nothing here
                # goes to the network, so nothing after `store.add` can fail and
                # leave the bytes in the store with no log entry naming them.
                pass_log.add(self._job_name, fetched.places, store_id)
                self._recorded.add(store_id)
        except Exception as exc:
            utils.log_failure(log, exc, "%s: storing %s failed", self._ctx, label)
            self._result.failed += 1
            return
        self._result.restored += 1
        log.info("%s: restored %s: %s id=%s", self._ctx, label, status, store_id)
