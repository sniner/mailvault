"""The IMAP mailbox backend.

Implements the `MailboxClient` interface over `imapclient`: listing folders,
reading a folder read-only for backup, indexing messages by Message-ID for
`verify`, and -- separately from the read pass -- purging archived messages once
their location is durable. It also carries the Gmail-specific label handling and
the Exchange-journal unwrapping path.
"""

from __future__ import annotations

import collections.abc
import functools
import imaplib
import logging
import re
import ssl
import sys
import threading
from typing import Any

import imapclient

if sys.version_info >= (3, 14):
    # Monkeypatch for imapclient 3.1.0 on Python 3.14:
    # IMAP4WithTimeout.open explicitly sets self.file which is now a property without a setter.
    # Removing the open method makes it fallback to imaplib.IMAP4.open which works fine.
    try:
        import imapclient.imap4

        if hasattr(imapclient.imap4.IMAP4WithTimeout, "open"):
            del imapclient.imap4.IMAP4WithTimeout.open
    except Exception:
        pass

from mailvault import conf, mailutils, utils
from mailvault.backend import base
from mailvault.backend.base import BackupResult, MailboxError, MessageRef
from mailvault.store import cas

log = logging.getLogger(__name__)

# Stands in for Gmail's "All Mail", which holds every message that is not in
# Trash or Spam and is therefore the place a message is in when it carries no
# label of its own. Gmail has no canonical name for it -- the IMAP folder is
# localised (`[Gmail]/All Mail`, `[Google Mail]/Alle Nachrichten`) -- so this
# name follows the convention of the system labels it sits next to. Gmail never
# reports a user label with a leading backslash, so it cannot collide with one.
GMAIL_ALL_MAIL = "\\All"


# Index page size for message_index(); only envelope metadata is fetched, no bodies.
INDEX_CHUNK_SIZE = 500

# The `kind` this backend stamps on the resume points it produces. A point
# carrying any other kind belongs to a different backend and is read in full.
UID_RESUME_KIND = "imap-uid"


def _as_int(value: object) -> int | None:
    """An int, or None -- and a bool is not an int here, whatever Python says."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class _UidResume:
    """Where an incremental pass over one IMAP folder carries on.

    A UID is assigned when a message enters a folder and never changes while it
    stays there, so asking for everything above the highest one already archived
    is literally "what has arrived since". Including a message copied or moved
    in: it keeps its old INTERNALDATE but gets a *new* UID, which is exactly the
    case a date filter cannot see and the reason for preferring UIDs to dates.

    `UIDVALIDITY` is the server stating whether its UID space is still the same
    one. When it differs, every remembered UID is meaningless -- an explicit
    signal rather than something to guess at -- and the folder is read in full,
    which is the same answer this backend gives to a point it cannot read at all.
    """

    def __init__(self, previous: dict | None):
        self._previous = _uid_point(previous)
        self.uidvalidity: int | None = None
        self.highest: int | None = None

    def accept(self, folder_info: dict, ctx: str) -> int | None:
        """Take the SELECT response; return the UID to resume above, or None.

        None means read the folder in full, and it is the answer whenever there
        is any doubt at all about the remembered point.
        """
        self.uidvalidity = _as_int(folder_info.get(b"UIDVALIDITY"))
        if self.uidvalidity is None:
            # RFC 3501 makes UIDVALIDITY mandatory in a SELECT response, so this
            # is a broken server rather than an old one. Without it there is
            # nothing to check a remembered UID against, and one that belongs to
            # a rebuilt UID space would silently skip the whole folder.
            log.warning("%s: no UIDVALIDITY in the SELECT response, reading in full", ctx)
            return None
        if self._previous is None:
            return None
        validity, uid = self._previous
        if validity != self.uidvalidity:
            log.info(
                "%s: UIDVALIDITY changed (%s -> %s), reading the folder in full",
                ctx,
                validity,
                self.uidvalidity,
            )
            self._previous = None
            return None
        return uid

    def saw(self, uid: int) -> None:
        """Note an archived message's UID, keeping the highest.

        Only for messages that were actually stored: one that failed must not
        raise the watermark over itself.
        """
        if self.highest is None or uid > self.highest:
            self.highest = uid

    def token(self) -> dict | None:
        """The point this pass earned, or None if it archived nothing."""
        if self.highest is None or self.uidvalidity is None:
            return None
        return {
            "kind": UID_RESUME_KIND,
            "uidvalidity": self.uidvalidity,
            "uid": self.highest,
        }


def _uid_point(resume: dict | None) -> tuple[int, int] | None:
    """Read this backend's own resume point, or None for anything else."""
    if resume is None:
        return None
    if resume.get("kind") != UID_RESUME_KIND:
        log.info("resume point of kind %r is not ours, reading in full", resume.get("kind"))
        return None
    validity = _as_int(resume.get("uidvalidity"))
    uid = _as_int(resume.get("uid"))
    if validity is None or uid is None:
        log.warning("resume point %r is incomplete, reading in full", resume)
        return None
    return validity, uid


class ImapClient:
    """The IMAP implementation of `MailboxClient`, wrapping an `imapclient` connection."""

    def __init__(self, conn: imapclient.IMAPClient, job: conf.JobConfig):
        self.job = job
        self.conn = conn
        self.job_name = job.name
        self.lock = threading.RLock()
        self.capabilities = self.conn.capabilities()
        self.delete_after_export = job.delete_after_export
        self.exchange_journal = job.exchange_journal
        self.gmail = functools.reduce(
            lambda acc, c: acc or c.startswith(b"X-GM-"), self.capabilities, False
        )
        # MOVE is RFC 6851 (2013), UIDPLUS is RFC 4315 -- neither is in the
        # IMAP4rev1 base, and Exchange's IMAP service in particular is sparing
        # with both. Missing capabilities only pick a different route in
        # `_relocate`, they never disable the error folder.
        self.move_cap = b"MOVE" in self.capabilities
        self.uidplus_cap = b"UIDPLUS" in self.capabilities
        self.trash_folder = job.trash_folder
        self.error_folder = job.error_folder

    @classmethod
    def connect(cls, job: conf.JobConfig) -> ImapClient:
        """Open a TLS IMAP connection for `job`, log in, and wrap it in a client.

        A server that is unreachable or refuses the credentials raises
        `MailboxError`: both are ordinary, fully diagnosed outcomes -- the
        server said what was wrong -- and the caller reports them as one line
        rather than a traceback through `imapclient`.
        """
        if job.tls:
            tls_context = ssl.create_default_context()
            if not job.tls_check_hostname:
                log.warning("%s: TLS hostname check disabled", job.name)
                tls_context.check_hostname = False
            if not job.tls_verify_cert:
                log.warning("%s: TLS certificate verification disabled", job.name)
                tls_context.verify_mode = ssl.CERT_NONE
        else:
            log.warning("%s: TLS disabled, connection is unencrypted", job.name)
            tls_context = None

        try:
            conn = imapclient.IMAPClient(
                host=job.server,
                port=job.port,
                ssl=job.tls,
                ssl_context=tls_context,
            )
        except (OSError, imaplib.IMAP4.error) as exc:
            # OSError covers the lot below the protocol: DNS, refused
            # connections, timeouts, and ssl.SSLError for a certificate the
            # server could not prove.
            raise MailboxError(f"cannot connect to {job.server}:{job.port}: {exc}") from exc

        try:
            conn.login(job.username, job.password)
        except imaplib.IMAP4.error as exc:
            # imapclient's LoginError is one of these, and carries the server's
            # own wording ("no such user", "authentication failed") -- which is
            # the whole message worth reporting.
            try:
                conn.shutdown()
            except Exception as close_exc:
                log.debug("%s: closing the refused connection failed: %s", job.name, close_exc)
            raise MailboxError(f"login refused for '{job.username}': {exc}") from exc
        return cls(conn, job)

    def close(self) -> None:
        """Log out and close the underlying connection."""
        try:
            self.conn.logout()
        except Exception as exc:
            log.debug("%s: logout failed: %s", self.job_name, exc)

    @staticmethod
    def _isfoldertype(folder: tuple, *flags: str) -> str | None:
        folderflags = set(folder[0])
        bflags = [(b"\\" + f.encode(), f) for f in [f.capitalize() for f in flags]]
        for flag in bflags:
            if flag[0] in folderflags:
                return flag[1]
        return None

    @staticmethod
    def _isfoldername(folder: tuple, *patterns: str) -> str | None:
        foldername = folder[2]
        for pattern in patterns:
            if re.match(pattern, foldername):
                return pattern
        return None

    def folders(self) -> collections.abc.Generator[str, None, None]:
        with self.lock:
            for folder in self.conn.list_folders():
                if self._isfoldertype(folder, *self.job.ignore_folder_flags):
                    continue
                if self._isfoldername(folder, *self.job.ignore_folder_names):
                    continue
                yield folder[2]

    def _walk_folder(
        self,
        folder_name: str,
        message_ids: list[int],
        chunk_size: int = 10,
        result: BackupResult | None = None,
    ) -> collections.abc.Generator[tuple[int, bytes], None, None]:
        for msg_ids in utils.batched(message_ids, chunk_size):
            msg_ids_str = ", ".join([str(i) for i in msg_ids])
            log.debug("%s::%s: fetching %s", self.job_name, folder_name, msg_ids_str)
            msg_id = None
            try:
                for msg_id, msg_data in self.conn.fetch(msg_ids, ["RFC822"]).items():
                    yield msg_id, msg_data[b"RFC822"]  # type: ignore
            except (OSError, imaplib.IMAP4.error) as exc:
                log.exception(
                    "%s::%s[%s]: fetch failed: %s", self.job_name, folder_name, msg_id, exc
                )
                if result is not None:
                    # fetch() returns the whole chunk at once, so nothing of it
                    # was yielded before the failure.
                    result.failed += len(msg_ids)

    def _collect_metadata(
        self, folder_name: str, msg_id: Any, store_id: str
    ) -> mailutils.MessageMetadata:
        if self.gmail:
            # X-GM-LABELS reports every folder the message is in, in canonical
            # form (`\Sent`). The IMAP folder name is a localised view of the
            # same thing -- `[Google Mail]/Gesendet` on a German account,
            # `[Gmail]/Sent Mail` on an English one -- so taking it as well would
            # record one place twice, once in a spelling that differs per
            # account. Hence Gmail's own list is the only source here.
            folders = self.conn.get_gmail_labels(msg_id).get(msg_id, [])
            if not folders:
                # "All Mail" is not a label, so a message filed nowhere else
                # reports nothing at all. Name that place instead of leaving the
                # message without one -- it is where the message actually is.
                folders = [GMAIL_ALL_MAIL]
        else:
            folders = [folder_name]
        return mailutils.metadata(
            mailbox=self.job_name,
            folder=folder_name,
            store_id=store_id,
            folders=folders,
        )

    def _clear_folder(self, folder_name: str) -> None:
        with self.lock:
            try:
                self.conn.select_folder(folder_name, readonly=False)
                try:
                    message_ids = self.conn.search()
                    for msg_ids in utils.batched(message_ids, 10):
                        self.conn.delete_messages(msg_ids)
                except Exception as exc:
                    log.error("%s::%s: %s", self.job_name, folder_name, exc)
                finally:
                    self.conn.expunge()
                    self.conn.unselect_folder()
            except Exception as exc:
                log.error("%s::%s: %s", self.job_name, folder_name, exc)

    def _search_folder(self, above_uid: int | None = None) -> list[int]:
        """Search the selected folder, from a UID watermark where there is one."""
        if above_uid is None:
            return self.conn.search(["NOT", "DELETED"])  # type: ignore
        criteria = ["NOT", "DELETED", "UID", f"{above_uid + 1}:*"]
        found: list[int] = self.conn.search(criteria)  # type: ignore
        # RFC 3501 makes a UID range unordered, and says so explicitly: the last
        # message is included even when its UID is below the lower bound. So
        # `4711:*` on a folder whose newest message is 4700 comes back with 4700.
        # Dropping everything at or below the watermark is what keeps a quiet
        # folder from re-fetching its last message on every single run.
        return [uid for uid in found if uid > above_uid]

    def _iter_folder(
        self,
        folder_name: str,
        resume: _UidResume | None = None,
        result: BackupResult | None = None,
    ) -> collections.abc.Generator[tuple[int, bytes], None, None]:
        """Select the folder read-only, search and yield its messages.

        A read-only pass: nothing is deleted here. Messages are removed from the
        server later, by `purge`, and only once their archival has been made
        durable -- so the folder is opened read-only even when the job deletes
        after export, and a torn run can never remove mail whose location was
        never written down. The folder is always unselected on exit.
        """
        with self.lock:
            folder_info = self.conn.select_folder(folder_name, readonly=True)
            try:
                items_in_folder = folder_info[b"EXISTS"]
                above_uid = (
                    resume.accept(folder_info, f"{self.job_name}::{folder_name}")
                    if resume is not None
                    else None
                )
                message_ids = self._search_folder(above_uid)
                items_found = len(message_ids)
                if result is not None:
                    result.total = items_found
                if items_found != items_in_folder:
                    log.info(
                        "%s::%s: found %s/%s messages",
                        self.job_name,
                        folder_name,
                        items_found,
                        items_in_folder,
                    )
                else:
                    log.info(
                        "%s::%s: found %s messages", self.job_name, folder_name, items_found
                    )
                processed = 0
                for msg_id, msg in self._walk_folder(folder_name, message_ids, result=result):
                    yield msg_id, msg
                    processed += 1
                    if processed % 100 == 0:
                        log.info(
                            "%s::%s: %s/%s messages processed",
                            self.job_name,
                            folder_name,
                            processed,
                            items_found,
                        )
            except Exception as exc:
                log.error("%s::%s: %s", self.job_name, folder_name, exc)
                raise
            finally:
                self.conn.unselect_folder()

    def _relocate(self, folder_name: str, msg_ids: list[int], dest_folder: str) -> None:
        """Move the given messages of `folder_name` into `dest_folder`.

        Called after the read-only pass over the folder has finished, never
        during it: relocating changes the source mailbox, which a server must
        refuse while the folder is selected read-only. Same reasoning as
        `purge`, which is why this waits in the same way.

        Uses MOVE where the server has it (RFC 6851) and otherwise the older
        COPY + \\Deleted + EXPUNGE it replaced, which every IMAP4rev1 server can
        do. A failure is logged and costs only the relocation: the message stays
        where it is, unarchived and undeleted.
        """
        if not msg_ids:
            return
        with self.lock:
            self.conn.select_folder(folder_name, readonly=False)
            try:
                if not self.conn.folder_exists(dest_folder):
                    self.conn.create_folder(dest_folder)
                if self.move_cap:
                    self.conn.move(msg_ids, dest_folder)
                else:
                    self.conn.copy(msg_ids, dest_folder)
                    self.conn.delete_messages(msg_ids)
                    if self.uidplus_cap:
                        # Targeted: removes these messages and nothing else. A
                        # plain EXPUNGE would drop every \Deleted message in the
                        # folder, including ones another client marked, so
                        # without UIDPLUS the flag is left for the server or the
                        # mailbox owner to act on.
                        self.conn.uid_expunge(msg_ids)
                log.info(
                    "%s::%s: %s message(s) moved to '%s'",
                    self.job_name,
                    folder_name,
                    len(msg_ids),
                    dest_folder,
                )
            except Exception as exc:
                log.error(
                    "%s::%s: could not move %s message(s) to '%s': %s",
                    self.job_name,
                    folder_name,
                    len(msg_ids),
                    dest_folder,
                    exc,
                )
            finally:
                self.conn.unselect_folder()

    def folder_backup(
        self,
        folder_name: str,
        store: cas.ContentAddressedStorage,
        resume: dict | None = None,
        callback: collections.abc.Callable[[mailutils.MessageMetadata], None] | None = None,
    ) -> BackupResult:
        """Store a folder's messages read-only, recording each via `callback`.

        Nothing is deleted here: the ids of successfully stored messages are
        collected in `BackupResult.deletable`, and the caller purges them only
        after the metadata log is sealed.
        """
        result = BackupResult()
        watermark = _UidResume(resume)
        # Collected during the read-only pass and relocated once it is over --
        # a skip is not a failure, so the resume point may still advance either way.
        non_journal: list[int] = []
        for msg_id, msg in self._iter_folder(folder_name, watermark, result=result):
            if self.exchange_journal:
                msg = mailutils.unwrap_exchange_journal_item(msg)
                if msg is None:
                    log.warning(
                        "%s::%s[%s]: not a journal item, %s",
                        self.job_name,
                        folder_name,
                        msg_id,
                        "moving to the error folder" if self.error_folder else "kept on server",
                    )
                    non_journal.append(msg_id)
                    continue
            store_id = base.store_message(
                store,
                msg,
                result=result,
                log_ctx=f"{self.job_name}::{folder_name}[{msg_id}]",
                callback=callback,
                metadata_fn=lambda sid, mid=msg_id: self._collect_metadata(
                    folder_name=folder_name, msg_id=mid, store_id=sid
                ),
            )
            if store_id is None:
                continue
            # The UID, which is what the next pass will ask the server about.
            watermark.saw(msg_id)
            # Not deleted here: a message is removed from the server only after
            # the folder's log is sealed. A non-journal item skipped above never
            # reaches this point, so it can never be deleted unarchived.
            if self.delete_after_export:
                result.deletable.append(msg_id)
        if self.error_folder:
            self._relocate(folder_name, non_journal, self.error_folder)
        if self.gmail and self.trash_folder:
            self._clear_folder(self.trash_folder)
        result.resume = watermark.token()
        return result

    def purge(self, folder_name: str, msg_ids: collections.abc.Sequence[int]) -> None:
        """Delete the given messages from the server and expunge them.

        The folder is opened read-write only here, and only the backup runner
        calls it -- after the metadata log for the folder has been sealed. So a
        message is removed from its source only once the record of where it was
        seen is durable; a run interrupted before this point leaves every message
        in place, to be re-fetched (and deduplicated) next time.
        """
        if not msg_ids:
            return
        with self.lock:
            self.conn.select_folder(folder_name, readonly=False)
            try:
                self.conn.delete_messages(list(msg_ids))
                self.conn.expunge()
            finally:
                self.conn.unselect_folder()

    def message_index(
        self, folder_name: str
    ) -> collections.abc.Generator[MessageRef, None, None]:
        """List the folder's messages by Message-ID only, without fetching bodies."""
        with self.lock:
            self.conn.select_folder(folder_name, readonly=True)
            try:
                message_ids = self._search_folder()
                log.info(
                    "%s::%s: indexing %s messages",
                    self.job_name,
                    folder_name,
                    len(message_ids),
                )
                for chunk in utils.batched(message_ids, INDEX_CHUNK_SIZE):
                    try:
                        fetched = self.conn.fetch(chunk, ["ENVELOPE"])
                    except (OSError, imaplib.IMAP4.error) as exc:
                        raise MailboxError(f"{folder_name}: indexing failed: {exc}") from exc
                    for msg_id, msg_data in fetched.items():
                        envelope = msg_data[b"ENVELOPE"]
                        raw_id = getattr(envelope, "message_id", None)
                        yield MessageRef(
                            msg_id=msg_id,
                            message_id=raw_id.decode("ascii", "replace") if raw_id else "",
                            date=getattr(envelope, "date", None),
                        )
            finally:
                self.conn.unselect_folder()

    def fetch_message(self, msg_id: int, folder_name: str) -> bytes:
        """Fetch a single message by UID from the given folder."""
        with self.lock:
            self.conn.select_folder(folder_name, readonly=True)
            try:
                msg_data = self.conn.fetch([msg_id], ["RFC822"]).get(msg_id)
                if not msg_data or b"RFC822" not in msg_data:
                    raise MailboxError(f"{folder_name}[{msg_id}]: message not found")
                return msg_data[b"RFC822"]  # type: ignore[return-value]
            finally:
                self.conn.unselect_folder()
