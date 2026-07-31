from __future__ import annotations

import collections.abc
import dataclasses
import imaplib
import logging
import pathlib
import time
from datetime import UTC, datetime
from typing import cast

from mailvault import cas, conf, mailutils, storedb
from mailvault.backend import base, imap

log = logging.getLogger(__name__)


class JobError(Exception):
    pass


@dataclasses.dataclass
class VerifyResult:
    """Outcome of comparing one server folder against the local archive."""

    folder: str
    on_server: int = 0
    missing: int = 0
    restored: int = 0
    failed: int = 0


def _metadata_writer(
    db: storedb.StoreDatabaseConnection, mailbox_id: int
) -> collections.abc.Callable[[dict], None]:
    """Build the callback that records a message's metadata in the store database."""

    def _store_metadata(email: dict) -> None:
        msg = db.add_message(
            email["store_id"], email["email_id"], email["date"], email["subject"]
        )
        db.assign_message_to_mailbox(msg, mailbox_id)
        db.add_message_labels(msg, *email["labels"])
        db.add_message_sender(msg, *email["sender"])
        db.add_message_recipients(msg, *email["recipients"])

    return _store_metadata


def backup(job: conf.JobConfig, store_path: pathlib.Path, compress: bool = False) -> None:
    with imap.Mailbox(job=job) as mb:
        store = cas.ContentAddressedStorage(store_path, suffix=".eml", compress=compress)
        if job.with_db:
            with storedb.StoreDatabase(path=store_path / "store.db") as db:
                mb_id = db.add_mailbox(job.name)
                _store_metadata = _metadata_writer(db, mb_id)
                folders = job.folders if job.folders else mb.folders()
                for folder in folders:
                    folder_id = db.add_label(folder)
                    start_date = (
                        db.get_snapshot_date(mb_id, folder_id) if job.incremental else None
                    )
                    snapshot_date = datetime.now(UTC)
                    result = mb.folder_backup(
                        folder, store, since=start_date, callback=_store_metadata
                    )
                    if result.complete:
                        db.set_snapshot(mb_id, folder_id, date=snapshot_date)
                    else:
                        # Advancing the snapshot now would push the failed messages
                        # out of every future date filter, losing them permanently.
                        log.warning(
                            "%s::%s: %s of %s message(s) failed, snapshot not advanced",
                            job.name,
                            folder,
                            result.failed,
                            result.total,
                        )
        else:
            if job.folders:
                for folder in job.folders:
                    mb.folder_backup(folder, store)
            else:
                mb.full_backup(store)


def _verify_folder(
    mb: base.MailboxClient,
    db: storedb.StoreDatabaseConnection,
    store: cas.ContentAddressedStorage,
    mailbox_id: int,
    job_name: str,
    folder: str,
    repair: bool = False,
) -> VerifyResult:
    """Compare one server folder against the archive and optionally refetch gaps.

    Matching is done by Message-ID, which is cheap: listing a folder's headers
    costs a handful of requests, while re-downloading it costs one request per
    message. A message counts as missing whenever its Message-ID is not archived
    for this folder, so messages with an absent or duplicated Message-ID may be
    fetched needlessly -- that is deliberate, since the content-addressed storage
    discards the redundant copy anyway. The reverse mistake, skipping a message
    that really is missing, is the one worth avoiding.
    """
    label_id = db.add_label(folder)
    known = mailutils.MessageIdIndex(db.get_known_message_ids(mailbox_id, label_id))
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

    store_metadata = _metadata_writer(db, mailbox_id)
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
            store_metadata(
                mailutils.metadata(msg, mailbox=job_name, folder=folder, store_id=store_id)
            )
        except Exception as exc:
            log.exception("%s::%s: storing %s failed: %s", job_name, folder, label, exc)
            result.failed += 1
            continue
        log.info("%s::%s: restored %s: %s id=%s", job_name, folder, label, status, store_id)
        result.restored += 1

    return result


def verify(
    job: conf.JobConfig,
    store_path: pathlib.Path,
    repair: bool = False,
    compress: bool = False,
) -> list[VerifyResult]:
    """Check the archive for messages the server still has but the archive lacks.

    Gaps arise when individual downloads fail during a backup run. The snapshot
    date of an incremental job hides them from every later run, so they need an
    explicit comparison to be found. With `repair` the missing messages are
    fetched and added to the archive. The snapshot dates are left untouched.
    """
    if not job.with_db:
        raise JobError(f"{job.name}: verify requires a job with 'with_db' enabled")
    if job.exchange_journal:
        raise JobError(
            f"{job.name}: verify does not support 'exchange_journal' jobs, because the"
            " archive holds the unwrapped message whose Message-ID differs from the"
            " journal envelope reported by the server"
        )

    results: list[VerifyResult] = []
    with imap.Mailbox(job=job) as mb:
        store = cas.ContentAddressedStorage(store_path, suffix=".eml", compress=compress)
        with storedb.StoreDatabase(path=store_path / "store.db") as db:
            mb_id = db.add_mailbox(job.name)
            folders = job.folders if job.folders else list(mb.folders())
            for folder in folders:
                try:
                    results.append(
                        _verify_folder(mb, db, store, mb_id, job.name, folder, repair=repair)
                    )
                except Exception as exc:
                    log.error("%s::%s: verify failed: %s", job.name, folder, exc)
    return results


def folder_list(job: conf.JobConfig) -> None:
    with imap.Mailbox(job=job) as mb:
        for folder in mb.folders():
            print(f"{job.name}::{folder}")


def update_db_from_archive(store_path: pathlib.Path, mailbox: str | None = None) -> None:
    store = cas.ContentAddressedStorage(store_path, suffix=".eml")
    with storedb.StoreDatabase(path=store_path / "store.db") as db:
        mb_id = db.add_mailbox(mailbox) if mailbox else None
        for path in store.walk():
            msg = store.read(path)
            header = mailutils.decode_email_header(msg)
            from_addrs, to_addrs = mailutils.addresses(header)
            store_id = path.name.split(".")[0]
            email_id = mailutils.message_id(header)
            date = mailutils.date(header)
            subject = mailutils.subject(header)
            log.debug("%s: message_id=%s, date=%s", store_id, email_id, date)

            msg_id = db.add_message(store_id, email_id, date, subject)
            if mb_id:
                db.assign_message_to_mailbox(msg_id, mb_id)
            db.add_message_sender(msg_id, *from_addrs)
            db.add_message_recipients(msg_id, *to_addrs)


def _format_archive_folder(template: str) -> str:
    now = datetime.now()
    return now.strftime(template)


def _copy_folder(
    mb_from: base.MailboxClient,
    mb_to: base.MailboxClient,
    folder: str,
    archive_folder: str | None = None,
) -> None:
    for msg_id, msg_date, msg in mb_from.get_messages(folder):
        mb_to.save_message(msg, folder, date=msg_date)
        if archive_folder:
            dest_folder = _format_archive_folder(archive_folder)
            log.info(
                "%s::%s: Moving message '%s' to folder '%s'",
                mb_from.job_name,
                folder,
                msg_id,
                dest_folder,
            )
            try:
                mb_from.move_message(msg_id, dest_folder)
            except imap.MailboxError:
                mb_from.save_message(msg, dest_folder, date=msg_date)
                mb_from.delete_message(msg_id, expunge=True)


def _copy(
    source: conf.JobConfig, destination: conf.JobConfig, archive_folder: str | None = None
) -> None:
    with imap.Mailbox(job=source) as mb_from:
        with imap.Mailbox(job=destination) as mb_to:
            folders = source.folders if source.folders else ["INBOX"]
            for folder in folders:
                _copy_folder(mb_from, mb_to, folder, archive_folder=archive_folder)


def _idle_copy(
    source: conf.JobConfig,
    folder_name: str,
    destination: conf.JobConfig,
    archive_folder: str | None = None,
) -> None:
    def _copy_to_dest(mb_from: base.MailboxClient):
        with imap.Mailbox(job=destination) as mb_to:
            _copy_folder(mb_from, mb_to, folder_name, archive_folder=archive_folder)

    backoff = 1
    while True:
        try:
            with imap.Mailbox(job=source) as mb_from:
                imap_client = cast(imap.ImapClient, mb_from)
                backoff = 1
                _copy_to_dest(imap_client)
                while True:
                    for _, _ in imap_client.watch_folder("INBOX"):
                        _copy_to_dest(imap_client)
        except (OSError, imaplib.IMAP4.abort):
            log.warning(
                "%s::%s: Connection lost, reconnecting in %ds",
                source.name,
                folder_name,
                backoff,
            )
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)


def copy(source: conf.JobConfig, destination: conf.JobConfig, idle: bool = False) -> None:
    if source.move_to_archive:
        if source.archive_folder:
            archive_folder = source.archive_folder
        else:
            raise JobError("Option 'move_to_archive' given, but 'archive_folder' missing")
    else:
        archive_folder = None

    if idle:
        # FIXME: currently only INBOX
        _idle_copy(source, "INBOX", destination, archive_folder=archive_folder)
    else:
        _copy(source, destination, archive_folder=archive_folder)
