"""Check the archive for messages the server still has but the archive lacks.

A last resort, not part of the routine: a folder whose downloads partly failed
does not advance its snapshot, so the next backup fetches it again. What is left
for verify are archives from older versions, jobs that keep no state, and mail
moved into a folder with an internal date older than the snapshot.
"""

from __future__ import annotations

import collections.abc
import dataclasses
import logging
import pathlib
from datetime import UTC, datetime

from mailvault import conf, mailutils
from mailvault.backend import base, session
from mailvault.jobs.common import JobError, _seal_log
from mailvault.store import cas, metalog

log = logging.getLogger(__name__)


@dataclasses.dataclass
class VerifyResult:
    """Outcome of comparing one server folder against the local archive."""

    folder: str
    on_server: int = 0
    missing: int = 0
    restored: int = 0
    failed: int = 0


def _places_from_log(log_root: pathlib.Path) -> dict[tuple[str, str | None], set[str]]:
    """Read the whole log once into `(mailbox, folder) -> store ids`."""
    places: dict[tuple[str, str | None], set[str]] = {}
    for logfile in metalog.read_all(log_root):
        if logfile.mailbox is None:
            continue
        places.setdefault((logfile.mailbox, logfile.folder), set()).update(logfile.store_ids)
    return places


def _archived_message_ids(
    store: cas.ContentAddressedStorage, store_ids: collections.abc.Iterable[str]
) -> set[str]:
    """Return the normalised Message-IDs of the given archived messages.

    The log says which messages are at a place; the Message-ID itself is only in
    the message, so each one is parsed. That is a few thousand header reads for a
    folder -- `verify` is a once-in-a-blue-moon command, and the database it used
    to ask is no longer part of the archive.

    Messages without a usable Message-ID are omitted: they cannot serve as a
    comparison key and must count as "not present" so a verify run re-fetches
    them, which is harmless because the storage deduplicates by content.
    """
    known: set[str] = set()
    for store_id in store_ids:
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


def _verify_folder(
    mb: base.MailboxClient,
    store: cas.ContentAddressedStorage,
    log_root: pathlib.Path,
    archived: set[str],
    job_name: str,
    folder: str,
    repair: bool = False,
) -> VerifyResult:
    """Compare one server folder against the archive and optionally refetch gaps.

    Matching is done by Message-ID, which is the only key both sides share
    without transferring the message: listing a folder's headers costs a handful
    of requests, while re-downloading it costs one request per message. The
    content hash would be exact, but the server does not know it.

    A message counts as missing whenever its Message-ID is not archived for this
    folder, so messages with an absent or duplicated Message-ID may be fetched
    needlessly -- deliberate, since the storage discards the redundant copy. The
    reverse mistake, skipping a message that really is missing, is the one worth
    avoiding.
    """
    known = mailutils.MessageIdIndex(_archived_message_ids(store, archived))
    log.info("%s::%s: %s message(s) in archive", job_name, folder, len(known))

    result = VerifyResult(folder=folder)
    missing: list[base.MessageRef] = []
    for ref in mb.message_index(folder):
        result.on_server += 1
        if mailutils.normalize_message_id(ref.message_id) not in known:
            missing.append(ref)
        if result.on_server % 5000 == 0:
            log.info("%s::%s: %s message(s) indexed", job_name, folder, result.on_server)
    result.missing = len(missing)

    log.info(
        "%s::%s: %s of %s message(s) on the server are not archived",
        job_name,
        folder,
        result.missing,
        result.on_server,
    )
    if not repair or not missing:
        return result

    # A repaired message is new archive content, so its location has to reach the
    # log as well -- otherwise nothing records where it belongs.
    log_writer = metalog.LogWriter(log_root)
    for ref in missing:
        label = ref.message_id or ref.msg_id
        try:
            msg = mb.fetch_message(ref.msg_id, folder)
        except Exception as exc:
            log.error("%s::%s: download failed for %s: %s", job_name, folder, label, exc)
            result.failed += 1
            continue
        try:
            status, store_id, _path = store.add(msg)
            log_writer.add(job_name, [folder], store_id)
        except Exception as exc:
            log.exception("%s::%s: storing %s failed: %s", job_name, folder, label, exc)
            result.failed += 1
            continue
        log.info("%s::%s: restored %s: %s id=%s", job_name, folder, label, status, store_id)
        result.restored += 1

    _seal_log(log_writer, datetime.now(UTC))
    return result


def verify(
    job: conf.JobConfig,
    store_path: pathlib.Path,
    repair: bool = False,
    compress: bool = False,
) -> list[VerifyResult]:
    """Check the archive for messages the server still has but the archive lacks.

    Gaps are rare by design: a folder whose downloads partly failed does not
    advance its snapshot, so the next run fetches it again. What is left are
    archives from older versions, jobs that keep no state, and mail moved into a
    folder with an internal date older than the snapshot. This is a last resort,
    not part of the routine -- which is why it can afford to read the archive
    itself rather than keep an index alongside it.
    """
    if job.exchange_journal:
        raise JobError(
            f"{job.name}: verify does not support 'exchange_journal' jobs, because the"
            " archive holds the unwrapped message whose Message-ID differs from the"
            " journal envelope reported by the server"
        )

    results: list[VerifyResult] = []
    log_root = store_path / metalog.DEFAULT_LOG_DIR
    places = _places_from_log(log_root)
    with session.open_mailbox(job) as mb:
        store = cas.ContentAddressedStorage(store_path, suffix=".eml", compress=compress)
        folders = job.folders if job.folders else list(mb.folders())
        for folder in folders:
            try:
                results.append(
                    _verify_folder(
                        mb,
                        store,
                        log_root,
                        places.get((job.name, folder), set()),
                        job.name,
                        folder,
                        repair=repair,
                    )
                )
            except Exception as exc:
                log.error("%s::%s: verify failed: %s", job.name, folder, exc)
    return results
