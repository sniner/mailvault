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
share without transferring the message is the Message-ID. That is the same key
`verify` has always compared on, and the same bias applies: a message counts as
missing whenever its Message-ID is not archived *for this folder*, so an absent
or duplicated one is fetched needlessly. Deliberate -- the storage discards the
redundant copy, while the reverse mistake would cost the message.
"""

from __future__ import annotations

import collections.abc
import dataclasses
import logging
import pathlib
from datetime import UTC, datetime

from mailvault import mailutils
from mailvault.backend import base
from mailvault.jobs.common import _seal_log
from mailvault.store import cas, metalog

log = logging.getLogger(__name__)

# How often the two long passes say where they are. Both read one item at a
# time with nothing to show for it until they finish, so without this a large
# archive looks like a hung process -- and the local one is usually the slower
# of the two, because it is thousands of small reads over whatever the archive
# is mounted on.
ARCHIVE_PROGRESS_EVERY = 10_000
SERVER_PROGRESS_EVERY = 5_000


@dataclasses.dataclass
class ReconcileResult:
    """Outcome of comparing one server folder against the local archive."""

    folder: str
    on_server: int = 0
    missing: int = 0
    restored: int = 0
    failed: int = 0

    @property
    def complete(self) -> bool:
        """True when nothing was left unaccounted for."""
        return self.failed == 0


def places_from_log(log_root: pathlib.Path) -> dict[tuple[str, str | None], set[str]]:
    """Read the whole log once into `(mailbox, folder) -> store ids`."""
    places: dict[tuple[str, str | None], set[str]] = {}
    for logfile in metalog.read_all(log_root):
        if logfile.mailbox is None:
            continue
        places.setdefault((logfile.mailbox, logfile.folder), set()).update(logfile.store_ids)
    return places


def archived_message_ids(
    store: cas.ContentAddressedStorage,
    store_ids: collections.abc.Iterable[str],
    log_ctx: str = "",
) -> set[str]:
    """Return the normalised Message-IDs of the given archived messages.

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
    known: set[str] = set()
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
            log.warning("%s: unreadable, not counted as archived: %s", path, exc)
            continue
        known.add(mailutils.normalize_message_id(mailutils.message_id(header)))
    known.discard("")
    return known


def reconcile_folder(
    mb: base.MailboxClient,
    store: cas.ContentAddressedStorage,
    log_root: pathlib.Path,
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
    known = mailutils.MessageIdIndex(archived_message_ids(store, archived, log_ctx=ctx))
    log.info("%s: %s message(s) in archive", ctx, f"{len(known):,}")

    result = ReconcileResult(folder=folder)
    missing: list[base.MessageRef] = []
    log.info("%s: listing the folder on the server", ctx)
    for ref in mb.message_index(folder):
        result.on_server += 1
        if mailutils.normalize_message_id(ref.message_id) not in known:
            missing.append(ref)
        if result.on_server % SERVER_PROGRESS_EVERY == 0:
            log.info("%s: %s message(s) indexed", ctx, f"{result.on_server:,}")
    result.missing = len(missing)

    log.info(
        "%s: %s of %s message(s) on the server are not archived",
        ctx,
        f"{result.missing:,}",
        f"{result.on_server:,}",
    )
    if not repair or not missing:
        return result

    # A fetched message is new archive content, so its location has to reach the
    # log as well -- otherwise nothing records where it belongs.
    log_writer = metalog.LogWriter(log_root)
    for ref in missing:
        label = ref.message_id or ref.msg_id
        try:
            msg = mb.fetch_message(ref.msg_id, folder)
        except Exception as exc:
            log.error("%s: download failed for %s: %s", ctx, label, exc)
            result.failed += 1
            continue
        try:
            status, store_id, _path = store.add(msg)
            log_writer.add(job_name, [folder], store_id)
        except Exception as exc:
            log.exception("%s: storing %s failed: %s", ctx, label, exc)
            result.failed += 1
            continue
        log.info("%s: restored %s: %s id=%s", ctx, label, status, store_id)
        result.restored += 1

    _seal_log(log_writer, datetime.now(UTC))
    return result
