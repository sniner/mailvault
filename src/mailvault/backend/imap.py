"""The IMAP mailbox backend.

Implements the `MailboxClient` interface over `imapclient`: listing folders,
reading a folder read-only for backup, indexing messages by Message-ID for
`verify`, and -- separately from the read pass -- purging archived messages once
their location is durable. It also carries the Gmail-specific label handling and
the Exchange-journal unwrapping path.
"""

from __future__ import annotations

import collections.abc
import imaplib
import logging
import re
import ssl
import sys
import typing
from typing import Any

import imapclient
import imapclient.imap_utf7

if sys.version_info >= (3, 14):
    # Monkeypatch for imapclient 3.1.0 on Python 3.14:
    # IMAP4WithTimeout.open explicitly sets self.file which is now a property without a setter.
    # Removing the open method makes it fallback to imaplib.IMAP4.open which works fine.
    try:
        import imapclient.imap4

        if hasattr(imapclient.imap4.IMAP4WithTimeout, "open"):
            del imapclient.imap4.IMAP4WithTimeout.open
    except Exception as _patch_exc:
        # The patch is tied to imapclient 3.1.0: a moved module or a renamed
        # class makes it miss, and the consequence surfaces much later as a
        # connection failure that sounds like a server problem. The logger is
        # fetched by hand because `log` is not defined until below the imports.
        logging.getLogger(__name__).debug(
            "imapclient %s: the 3.14 open() patch did not apply (%s);"
            " connecting may fail on this combination",
            getattr(imapclient, "__version__", "of unknown version"),
            _patch_exc,
        )

from mailvault import conf, mailutils, utils
from mailvault.backend import base
from mailvault.backend.base import BackupResult, MailboxError, MessageRef
from mailvault.store import cas

log = logging.getLogger(__name__)

# Index page size for message_index(); only envelope metadata is fetched, no bodies.
INDEX_CHUNK_SIZE = 500

# The `kind` this backend stamps on the resume points it produces. A point
# carrying any other kind belongs to a different backend and is read in full.
UID_RESUME_KIND = "imap-uid"

# How a whole message is asked for, and what the server calls it in the answer.
# RFC822 is the deprecated spelling of the same thing, and iCloud answers a
# fetch for it with a response that simply leaves it out -- the message never
# arrives, and nothing says why. `BODY[]` is the spelling of RFC 3501, which
# every server has to understand; the PEEK form of it also keeps a backup from
# marking as read every message it passes over.
WHOLE_MESSAGE_ITEM = "BODY.PEEK[]"
WHOLE_MESSAGE_KEY = b"BODY[]"

# Where a Gmail message is, asked for in the same FETCH as its body. It used to
# be a call of its own per message -- `get_gmail_labels`, which is this item and
# nothing else -- so a folder cost one round trip per chunk for the bodies plus
# one per message for the labels.
#
# Only asked for where the server advertises the extension: a server that does
# not know the item answers the whole FETCH with an error, and the body would go
# down with the item nobody needed.
GMAIL_LABELS_ITEM = "X-GM-LABELS"
GMAIL_LABELS_KEY = b"X-GM-LABELS"


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

    def __init__(self, previous: dict[str, Any] | None):
        self._given = previous is not None
        self._previous = _uid_point(previous)
        self.uidvalidity: int | None = None
        self.highest: int | None = None
        # A point was handed in and could not be read: that is reported, not
        # worked around. Being given none in the first place is not a loss.
        self.lost = self._given and self._previous is None

    def accept(self, folder_info: dict[bytes, Any], ctx: str) -> int | None:
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
            log.warning("%s: no UIDVALIDITY in the SELECT response", ctx)
            self.lost = self._given
            return None
        if self._previous is None:
            return None
        validity, uid = self._previous
        if validity != self.uidvalidity:
            log.info(
                "%s: UIDVALIDITY changed (%s -> %s), the resume point is void",
                ctx,
                validity,
                self.uidvalidity,
            )
            self._previous = None
            self.lost = True
            return None
        return uid

    def saw(self, uid: int) -> None:
        """Note an archived message's UID, keeping the highest.

        Only for messages that were actually stored: one that failed must not
        raise the watermark over itself.
        """
        if self.highest is None or uid > self.highest:
            self.highest = uid

    def token(self) -> dict[str, Any] | None:
        """The point this pass earned, or None if it archived nothing."""
        if self.highest is None or self.uidvalidity is None:
            return None
        return {
            "kind": UID_RESUME_KIND,
            "uidvalidity": self.uidvalidity,
            "uid": self.highest,
        }


def _uid_point(resume: dict[str, Any] | None) -> tuple[int, int] | None:
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
    """The IMAP implementation of `MailboxClient`, wrapping an `imapclient` connection.

    **One client, one connection, one thread.** Nothing here is safe to call
    from two threads at once, and no lock would make it so: `SELECT` is
    connection *state*, not an argument, so two threads reading different
    folders over one connection would read each other's mail whatever the
    calls were serialised against. Reading in parallel means a connection per
    thread, and that is a decision for the caller, not something this class
    can paper over.
    """

    def __init__(self, conn: imapclient.IMAPClient, job: conf.JobConfig):
        self.job = job
        self.conn = conn
        self.job_name = job.name
        self.capabilities = self.conn.capabilities()
        self.delete_after_export = job.delete_after_export
        self.exchange_journal = job.exchange_journal
        self.gmail = any(c.startswith(b"X-GM-") for c in self.capabilities)
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
        # An empty password is never a credential worth trying, and it is what
        # a `password_cmd` leaves behind when it was not allowed to run. Sending
        # it produces whatever the server makes of a login with nothing in it --
        # iCloud answers with its LOGIN syntax, which says nothing about the
        # cause. So the cause is named here instead.
        if not job.password:
            raise MailboxError(
                f"no password for '{job.username}': set 'password' in the job, "
                f"or pass --allow-exec so 'password_cmd' may run"
            )

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
                # Said out loud although it is imapclient's default, because
                # everything this backend remembers between runs rests on it.
                # Without it SEARCH and FETCH speak sequence numbers, which are
                # positions in the folder and shift as soon as a message is
                # deleted -- so a resume point written as a UID would name a
                # different message on the next run, and the messages it skipped
                # would sit below it for good. UIDVALIDITY would go on matching
                # and say nothing was wrong. The one guard against silently
                # skipped mail is that these ids are UIDs; that is not a default
                # to inherit quietly.
                use_uid=True,
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
        """The first of `flags` the folder carries, spelled as the caller wrote it.

        `imapclient` reports a folder as `(flags, delimiter, name)`.
        """
        folderflags, _delimiter, _name = folder
        carried = set(folderflags)
        for flag in flags:
            name = flag.capitalize()
            if b"\\" + name.encode() in carried:
                return name
        return None

    @staticmethod
    def _isfoldername(folder: tuple, *patterns: str) -> str | None:
        _flags, _delimiter, foldername = folder
        for pattern in patterns:
            if re.match(pattern, foldername):
                return pattern
        return None

    def folders(self) -> collections.abc.Generator[str, None, None]:
        for folder in self.conn.list_folders():
            if self._isfoldertype(folder, *self.job.ignore_folder_flags):
                continue
            if self._isfoldername(folder, *self.job.ignore_folder_names):
                continue
            _flags, _delimiter, name = folder
            yield name

    @property
    def _fetch_items(self) -> list[str]:
        """What one read of a message has to ask for, this source being what it is."""
        if self.gmail:
            return [WHOLE_MESSAGE_ITEM, GMAIL_LABELS_ITEM]
        return [WHOLE_MESSAGE_ITEM]

    def _places_from(self, msg_data: dict[bytes, Any], folder_name: str) -> list[str | bytes]:
        """Read the places out of one message's FETCH answer.

        The labels arrive in IMAP modified UTF-7 -- `Pers&APY-nlich` for a label
        called `Persönlich`. `get_gmail_labels`, which this replaces, decoded them
        on the way out and that is what every log written so far holds, so the
        same decoding happens here: verified against the live account, this
        answers identically to `get_gmail_labels` on the same UIDs.
        """
        labels = msg_data.get(GMAIL_LABELS_KEY) or ()
        return base.places_read_from(
            [imapclient.imap_utf7.decode(label) for label in labels], folder_name
        )

    def _walk_folder(
        self,
        folder_name: str,
        message_ids: list[int],
        chunk_size: int = 10,
        result: BackupResult | None = None,
    ) -> collections.abc.Generator[tuple[int, base.Fetched], None, None]:
        for msg_ids in utils.batched(message_ids, chunk_size):
            msg_ids_str = ", ".join([str(i) for i in msg_ids])
            log.debug("%s::%s: fetching %s", self.job_name, folder_name, msg_ids_str)
            msg_id = None
            try:
                for msg_id, msg_data in self.conn.fetch(msg_ids, self._fetch_items).items():
                    body = msg_data.get(WHOLE_MESSAGE_KEY)
                    if not isinstance(body, bytes):
                        # A FETCH the server answered without the message in it.
                        # It is not a failure of the connection -- the rest of
                        # the chunk arrives -- so this one message is counted
                        # lost and named, rather than ending the folder.
                        log.error(
                            "%s::%s[%s]: the server sent no message body",
                            self.job_name,
                            folder_name,
                            msg_id,
                        )
                        if result is not None:
                            result.failed += 1
                        continue
                    yield msg_id, base.Fetched(body, self._places_from(msg_data, folder_name))
            except (OSError, imaplib.IMAP4.error) as exc:
                log.exception(
                    "%s::%s[%s]: fetch failed: %s",
                    self.job_name,
                    folder_name,
                    msg_id,
                    exc,
                )
                if result is not None:
                    # fetch() returns the whole chunk at once, so nothing of it
                    # was yielded before the failure.
                    result.failed += len(msg_ids)

    def _collect_metadata(
        self,
        folder_name: str,
        places: list[str | bytes],
        store_id: str,
    ) -> mailutils.MessageMetadata:
        """The location record, out of what the read already brought back.

        Nothing is asked of the server here. The places came with the message
        (`_places_from`), so this cannot fail and cannot be slow -- which is what
        lets the backup record a location it has already paid for.
        """
        return mailutils.metadata(
            mailbox=self.job_name,
            folder=folder_name,
            store_id=store_id,
            folders=places,
        )

    def _clear_folder(self, folder_name: str) -> None:
        """Delete everything in a folder and expunge it, whatever goes wrong.

        The EXPUNGE is in the `finally` so that whatever was flagged still
        goes when the pass over the batches breaks off half way, and the
        UNSELECT is guarded apart from it: a connection handed back with a
        folder still selected makes the *next* SELECT look like the failure.
        """
        try:
            self.conn.select_folder(folder_name, readonly=False)
        except Exception as exc:
            log.error("%s::%s: %s", self.job_name, folder_name, exc)
            return
        try:
            for msg_ids in utils.batched(self.conn.search(), 10):
                self.conn.delete_messages(msg_ids)
        except Exception as exc:
            log.error("%s::%s: %s", self.job_name, folder_name, exc)
        finally:
            try:
                self.conn.expunge()
            except Exception as exc:
                log.error("%s::%s: not expunged: %s", self.job_name, folder_name, exc)
            try:
                self.conn.unselect_folder()
            except Exception as exc:
                log.error("%s::%s: not unselected: %s", self.job_name, folder_name, exc)

    def _search(self, criteria: list[str]) -> list[int]:
        """Run one SEARCH over the selected folder and return the UIDs.

        `imapclient.search` carries no annotations, so a checker reads
        `criteria: str` off its `"ALL"` default and offers
        `list[int] | list[bytes]` for the result. It takes a sequence of
        criteria items, and it answers in bytes only for a MODSEQ search.
        """
        found = self.conn.search(criteria)  # type: ignore[arg-type]
        return typing.cast(list[int], found)

    def _search_folder(self, above_uid: int | None = None) -> list[int]:
        """Search the selected folder, from a UID watermark where there is one."""
        if above_uid is None:
            return self._search(["NOT", "DELETED"])
        found = self._search(["NOT", "DELETED", "UID", f"{above_uid + 1}:*"])
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
    ) -> collections.abc.Generator[tuple[int, base.Fetched], None, None]:
        """Select the folder read-only, search and yield its messages.

        A read-only pass: nothing is deleted here. Messages are removed from the
        server later, by `purge`, and only once their archival has been made
        durable -- so the folder is opened read-only even when the job deletes
        after export, and a torn run can never remove mail whose location was
        never written down. The folder is always unselected on exit.
        """
        folder_info = self.conn.select_folder(folder_name, readonly=True)
        try:
            above_uid = (
                resume.accept(folder_info, f"{self.job_name}::{folder_name}")
                if resume is not None
                else None
            )
            if resume is not None and resume.lost:
                # Nothing is yielded and nothing is fetched: what to do with
                # a void point is the caller's call, not this backend's.
                return
            message_ids = self._search_folder(above_uid)
            items_found = len(message_ids)
            if result is not None:
                result.total = items_found
            # What this pass will work through, and nothing about how full the
            # folder is. The two were reported as `found 0 of 1` -- two answers
            # to two different questions, in the shape of a ratio that says one
            # was missed. A covered folder read that way on every run, for good.
            log.info(
                "%s::%s: found %s",
                self.job_name,
                folder_name,
                utils.counted(items_found, "message"),
            )
            for processed, (msg_id, fetched) in enumerate(
                self._walk_folder(folder_name, message_ids, result=result), 1
            ):
                yield msg_id, fetched
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
                "%s::%s: %s moved to '%s'",
                self.job_name,
                folder_name,
                utils.counted(len(msg_ids), "message"),
                dest_folder,
            )
        except Exception as exc:
            log.error(
                "%s::%s: could not move %s to '%s': %s",
                self.job_name,
                folder_name,
                utils.counted(len(msg_ids), "message"),
                dest_folder,
                exc,
            )
        finally:
            self.conn.unselect_folder()

    def folder_backup(
        self,
        folder_name: str,
        store: cas.ContentAddressedStorage,
        resume: dict[str, Any] | None = None,
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
        for msg_id, fetched in self._iter_folder(folder_name, watermark, result=result):
            msg = fetched.body
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
                metadata_fn=lambda sid, places=fetched.places: self._collect_metadata(
                    folder_name=folder_name,
                    places=places,
                    store_id=sid,
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
        if watermark.lost:
            return BackupResult(resume_lost=True)
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
        self.conn.select_folder(folder_name, readonly=False)
        try:
            self.conn.delete_messages(list(msg_ids))
            self.conn.expunge()
        finally:
            self.conn.unselect_folder()

    def empty_trash(self) -> None:
        """Empty the trash folder, where Gmail keeps what `purge` deleted.

        Gmail answers an EXPUNGE by moving the message to its trash folder rather
        than removing it, and the quota counts it there just the same. So the
        deletion this job asked for is only finished once that folder is emptied,
        and `trash_folder` is how the owner names it -- the name is localised, so
        only they know it.

        Once per job, after the last purge: a message reaches the trash
        *during* `purge`, so emptying at any earlier point clears what an
        earlier run left behind and leaves this run's mail sitting there.
        """
        if self.gmail and self.trash_folder:
            self._clear_folder(self.trash_folder)

    def resume_point(self, folder_name: str) -> dict[str, Any] | None:
        """The folder's current UID watermark, without fetching anything."""
        folder_info = self.conn.select_folder(folder_name, readonly=True)
        try:
            uidvalidity = _as_int(folder_info.get(b"UIDVALIDITY"))
            if uidvalidity is None:
                log.warning(
                    "%s::%s: no UIDVALIDITY in the SELECT response, no resume point",
                    self.job_name,
                    folder_name,
                )
                return None
            uids = self._search_folder()
            if not uids:
                return None
            return {
                "kind": UID_RESUME_KIND,
                "uidvalidity": uidvalidity,
                "uid": max(uids),
            }
        finally:
            self.conn.unselect_folder()

    def message_index(
        self,
        folder_name: str,
    ) -> collections.abc.Generator[MessageRef, None, None]:
        """List the folder's messages by Message-ID only, without fetching bodies."""
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

    def fetch_message(self, msg_id: int, folder_name: str) -> base.Fetched:
        """Fetch a single message by UID from the given folder, places included.

        The repair path's read. It asks for exactly what `_walk_folder` asks for,
        in one FETCH inside one selection -- where it used to fetch the body,
        give the folder back, and select the same folder again to ask where the
        message was.
        """
        self.conn.select_folder(folder_name, readonly=True)
        try:
            msg_data = self.conn.fetch([msg_id], self._fetch_items).get(msg_id)
            body = msg_data.get(WHOLE_MESSAGE_KEY) if msg_data else None
            if msg_data is None or not isinstance(body, bytes):
                raise MailboxError(f"{folder_name}[{msg_id}]: message not found")
            return base.Fetched(body, self._places_from(msg_data, folder_name))
        finally:
            self.conn.unselect_folder()
