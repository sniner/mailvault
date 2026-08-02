"""Copy mail from a source mailbox to a destination, optionally moving it aside.

Unlike backup, this does not touch the archive or its log: it streams messages
from one server to another, and -- when configured -- files the source copy into
a dated archive folder on the source server.
"""

from __future__ import annotations

import imaplib
import logging
import time
from datetime import datetime

from mailvault import conf
from mailvault.backend import base, imap, session
from mailvault.jobs.common import JobError

log = logging.getLogger(__name__)


def _format_archive_folder(template: str) -> str:
    # Local wall clock on purpose: an archive folder like "Archive/%Y" is a human
    # calendar label, not a UTC instant -- near a year/month boundary the user
    # expects their local date. Unlike the snapshot timestamps, this value is
    # never compared or stored, so there is nothing to normalise to UTC.
    now = datetime.now().astimezone()
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
    with session.open_mailbox(source) as mb_from:
        with session.open_mailbox(destination) as mb_to:
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
        with session.open_mailbox(destination) as mb_to:
            _copy_folder(mb_from, mb_to, folder_name, archive_folder=archive_folder)

    backoff = 1
    while True:
        try:
            with session.open_mailbox(source) as mb_from:
                # IDLE is IMAP-specific; the caller guarantees an IMAP source, but
                # assert it so a misuse fails loudly instead of as an AttributeError.
                if not isinstance(mb_from, imap.ImapClient):
                    raise JobError(f"{source.name}: --idle requires an IMAP source")
                backoff = 1
                _copy_to_dest(mb_from)
                while True:
                    for _, _ in mb_from.watch_folder("INBOX"):
                        _copy_to_dest(mb_from)
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
        if source.backend != "imap":
            raise JobError(
                f"{source.name}: --idle is only supported for IMAP sources, "
                f"not backend {source.backend!r}"
            )
        # FIXME: currently only INBOX
        _idle_copy(source, "INBOX", destination, archive_folder=archive_folder)
    else:
        _copy(source, destination, archive_folder=archive_folder)
