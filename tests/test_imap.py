"""Tests for mailvault.backend.imap with mocked IMAPClient."""

from __future__ import annotations

import imaplib
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest

from mailvault import conf
from mailvault.backend import imap
from mailvault.store import cas

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DUMMY_EML = b"""From: sender@example.com
To: recipient@example.com
Subject: Hello World
Message-ID: <abc123@example.com>
Date: Wed, 20 Feb 2026 12:00:00 +0100

Body text.
"""


def _make_job(**overrides: Any) -> conf.JobConfig:
    defaults: dict[str, Any] = dict(
        name="test-mailbox",
        server="imap.example.com",
        port=993,
        username="user",
        password="pass",
        tls=True,
        tls_check_hostname=True,
        tls_verify_cert=True,
    )
    defaults.update(overrides)
    return conf.JobConfig(**defaults)


def _make_mock_conn(capabilities=None, folders=None):
    """Create a mock IMAPClient with sensible defaults."""
    conn = MagicMock()
    conn.capabilities.return_value = capabilities or [b"IMAP4rev1"]
    conn.list_folders.return_value = folders or []
    conn.folder_exists.return_value = True
    conn.select_folder.return_value = {b"EXISTS": 0}
    conn.search.return_value = []
    conn.fetch.return_value = {}
    return conn


def _make_client(job=None, conn=None, **job_overrides):
    """Create a MailboxClient with mocked connection."""
    if job is None:
        job = _make_job(**job_overrides)
    if conn is None:
        conn = _make_mock_conn()
    return imap.ImapClient(conn, job)


# ---------------------------------------------------------------------------
# ImapClient.connect / close
# ---------------------------------------------------------------------------


class TestConnect:
    @patch("mailvault.backend.imap.imapclient.IMAPClient")
    def test_creates_connection_with_tls(self, mock_imap_cls):
        mock_conn = _make_mock_conn()
        mock_imap_cls.return_value = mock_conn
        job = _make_job()

        client = imap.ImapClient.connect(job)

        assert client is not None
        mock_imap_cls.assert_called_once()
        kwargs = mock_imap_cls.call_args
        assert kwargs.kwargs["host"] == "imap.example.com"
        assert kwargs.kwargs["port"] == 993
        assert kwargs.kwargs["ssl"] is True
        assert kwargs.kwargs["ssl_context"] is not None
        mock_conn.login.assert_called_once_with("user", "pass")

    @patch("mailvault.backend.imap.imapclient.IMAPClient")
    def test_close_calls_logout(self, mock_imap_cls):
        mock_conn = _make_mock_conn()
        mock_imap_cls.return_value = mock_conn

        client = imap.ImapClient.connect(_make_job())
        client.close()

        mock_conn.logout.assert_called_once()

    @patch("mailvault.backend.imap.imapclient.IMAPClient")
    def test_no_tls(self, mock_imap_cls):
        mock_conn = _make_mock_conn()
        mock_imap_cls.return_value = mock_conn
        job = _make_job(tls=False)

        imap.ImapClient.connect(job)

        kwargs = mock_imap_cls.call_args
        assert kwargs.kwargs["ssl"] is False
        assert kwargs.kwargs["ssl_context"] is None

    @patch("mailvault.backend.imap.imapclient.IMAPClient")
    def test_tls_no_hostname_check(self, mock_imap_cls):
        mock_conn = _make_mock_conn()
        mock_imap_cls.return_value = mock_conn
        job = _make_job(tls_check_hostname=False, tls_verify_cert=True)

        imap.ImapClient.connect(job)

        ssl_ctx = mock_imap_cls.call_args.kwargs["ssl_context"]
        assert ssl_ctx is not None
        assert ssl_ctx.check_hostname is False

    @patch("mailvault.backend.imap.imapclient.IMAPClient")
    def test_refused_credentials_are_reported_not_raised_raw(self, mock_imap_cls):
        """Wrong password: the server's wording, without an imapclient traceback."""
        mock_conn = _make_mock_conn()
        mock_conn.login.side_effect = imaplib.IMAP4.error("no such user")
        mock_imap_cls.return_value = mock_conn

        with pytest.raises(imap.MailboxError, match="login refused for 'user': no such user"):
            imap.ImapClient.connect(_make_job())

        # And the socket of the refused connection does not stay open.
        mock_conn.shutdown.assert_called_once()

    @patch("mailvault.backend.imap.imapclient.IMAPClient")
    def test_an_unreachable_server_names_host_and_port(self, mock_imap_cls):
        mock_imap_cls.side_effect = OSError("connection refused")

        with pytest.raises(imap.MailboxError, match="imap.example.com:993: connection refused"):
            imap.ImapClient.connect(_make_job())


# ---------------------------------------------------------------------------
# folders()
# ---------------------------------------------------------------------------


class TestFolders:
    def test_yields_folder_names(self):
        folders = [
            ([b"\\HasNoChildren"], b"/", "INBOX"),
            ([b"\\HasNoChildren"], b"/", "Sent"),
            ([b"\\HasNoChildren"], b"/", "Archive"),
        ]
        client = _make_client(conn=_make_mock_conn(folders=folders))
        result = list(client.folders())
        assert result == ["INBOX", "Sent", "Archive"]

    def test_filters_by_flags(self):
        folders = [
            ([b"\\HasNoChildren"], b"/", "INBOX"),
            ([b"\\Junk"], b"/", "Spam"),
            ([b"\\Trash"], b"/", "Deleted Items"),
            ([b"\\HasNoChildren"], b"/", "Sent"),
        ]
        job = _make_job(ignore_folder_flags=["Junk", "Trash"])
        client = _make_client(job=job, conn=_make_mock_conn(folders=folders))
        result = list(client.folders())
        assert result == ["INBOX", "Sent"]

    def test_filters_by_name_pattern(self):
        folders = [
            ([b"\\HasNoChildren"], b"/", "INBOX"),
            ([b"\\HasNoChildren"], b"/", "Notes"),
            ([b"\\HasNoChildren"], b"/", "Sent"),
        ]
        job = _make_job(ignore_folder_names=["Notes"])
        client = _make_client(job=job, conn=_make_mock_conn(folders=folders))
        result = list(client.folders())
        assert result == ["INBOX", "Sent"]

    def test_empty_folder_list(self):
        client = _make_client(conn=_make_mock_conn(folders=[]))
        result = list(client.folders())
        assert result == []


# ---------------------------------------------------------------------------
# _isfoldertype / _isfoldername (static methods)
# ---------------------------------------------------------------------------


class TestFolderHelpers:
    def test_isfoldertype_match(self):
        folder = ([b"\\Junk", b"\\HasNoChildren"], b"/", "Spam")
        assert imap.ImapClient._isfoldertype(folder, "Junk") == "Junk"

    def test_isfoldertype_no_match(self):
        folder = ([b"\\HasNoChildren"], b"/", "INBOX")
        assert imap.ImapClient._isfoldertype(folder, "Junk", "Trash") is None

    def test_isfoldertype_case(self):
        # capitalize() is applied, so "junk" becomes "Junk" -> b"\\Junk"
        folder = ([b"\\Junk"], b"/", "Spam")
        assert imap.ImapClient._isfoldertype(folder, "junk") == "Junk"

    def test_isfoldername_match(self):
        folder = ([b"\\HasNoChildren"], b"/", "Notes")
        assert imap.ImapClient._isfoldername(folder, "Not.*") == "Not.*"

    def test_isfoldername_no_match(self):
        folder = ([b"\\HasNoChildren"], b"/", "INBOX")
        assert imap.ImapClient._isfoldername(folder, "Notes") is None


# ---------------------------------------------------------------------------
# _walk_folder
# ---------------------------------------------------------------------------


class TestWalkFolder:
    def test_yields_messages_in_chunks(self):
        conn = _make_mock_conn()
        msg_date = datetime(2026, 2, 20, 12, 0, 0, tzinfo=UTC)
        conn.fetch.return_value = {
            1: {b"RFC822": DUMMY_EML, b"INTERNALDATE": msg_date},
            2: {b"RFC822": DUMMY_EML, b"INTERNALDATE": msg_date},
        }
        client = _make_client(conn=conn)

        results = list(client._walk_folder("INBOX", [1, 2], chunk_size=10))
        assert len(results) == 2
        assert results[0][0] == 1
        assert results[0][1] == DUMMY_EML
        assert results[0][2] == msg_date

    def test_chunked_fetching(self):
        conn = _make_mock_conn()
        msg_date = datetime(2026, 2, 20, tzinfo=UTC)

        def fake_fetch(ids, _fields):
            return {i: {b"RFC822": DUMMY_EML, b"INTERNALDATE": msg_date} for i in ids}

        conn.fetch.side_effect = fake_fetch
        client = _make_client(conn=conn)

        results = list(client._walk_folder("INBOX", [1, 2, 3, 4, 5], chunk_size=2))
        assert len(results) == 5
        assert conn.fetch.call_count == 3  # 2+2+1

    def test_fetch_error_continues(self):
        conn = _make_mock_conn()
        conn.fetch.side_effect = imaplib.IMAP4.error("fetch failed")
        client = _make_client(conn=conn)

        results = list(client._walk_folder("INBOX", [1, 2, 3]))
        assert results == []


# ---------------------------------------------------------------------------
# _iter_folder
# ---------------------------------------------------------------------------


class TestIterFolder:
    def test_basic_iteration(self):
        conn = _make_mock_conn()
        msg_date = datetime(2026, 2, 20, tzinfo=UTC)
        conn.select_folder.return_value = {b"EXISTS": 1}
        conn.search.return_value = [1]
        conn.fetch.return_value = {
            1: {b"RFC822": DUMMY_EML, b"INTERNALDATE": msg_date},
        }
        client = _make_client(conn=conn)

        results = list(client._iter_folder("INBOX"))
        assert len(results) == 1
        assert results[0][0] == 1
        conn.unselect_folder.assert_called_once()

    def test_since_filter(self):
        conn = _make_mock_conn()
        conn.select_folder.return_value = {b"EXISTS": 0}
        conn.search.return_value = []
        client = _make_client(conn=conn)

        since = datetime(2026, 2, 15, tzinfo=UTC)
        list(client._iter_folder("INBOX", since=since))

        search_query = conn.search.call_args[0][0]
        assert "NOT" in search_query
        assert "DELETED" in search_query
        assert "SINCE" in search_query

    def test_unselect_on_exception(self):
        conn = _make_mock_conn()
        conn.select_folder.return_value = {b"EXISTS": 1}
        conn.search.side_effect = Exception("search failed")
        client = _make_client(conn=conn)

        with pytest.raises(Exception, match="search failed"):
            list(client._iter_folder("INBOX"))
        conn.unselect_folder.assert_called_once()

    def test_read_only_even_when_deleting(self):
        # The read pass never deletes and never opens read-write, even under
        # delete_after_export: removal happens later, in purge, after the seal.
        conn = _make_mock_conn()
        msg_date = datetime(2026, 2, 20, tzinfo=UTC)
        conn.select_folder.return_value = {b"EXISTS": 1}
        conn.search.return_value = [1]
        conn.fetch.return_value = {
            1: {b"RFC822": DUMMY_EML, b"INTERNALDATE": msg_date},
        }
        client = _make_client(delete_after_export=True, conn=conn)

        list(client._iter_folder("INBOX"))
        conn.select_folder.assert_called_with("INBOX", readonly=True)
        conn.delete_messages.assert_not_called()
        conn.expunge.assert_not_called()

    def test_no_expunge_without_delete(self):
        conn = _make_mock_conn()
        conn.select_folder.return_value = {b"EXISTS": 0}
        conn.search.return_value = []
        client = _make_client(conn=conn)

        list(client._iter_folder("INBOX"))
        conn.expunge.assert_not_called()

    def test_readonly_without_delete(self):
        conn = _make_mock_conn()
        conn.select_folder.return_value = {b"EXISTS": 0}
        conn.search.return_value = []
        client = _make_client(conn=conn)

        list(client._iter_folder("INBOX"))
        conn.select_folder.assert_called_with("INBOX", readonly=True)


# ---------------------------------------------------------------------------
# folder_backup
# ---------------------------------------------------------------------------


class TestFolderBackup:
    def test_stores_to_cas(self, tmp_path):
        conn = _make_mock_conn()
        msg_date = datetime(2026, 2, 20, tzinfo=UTC)
        conn.select_folder.return_value = {b"EXISTS": 1}
        conn.search.return_value = [1]
        conn.fetch.return_value = {
            1: {b"RFC822": DUMMY_EML, b"INTERNALDATE": msg_date},
        }
        client = _make_client(conn=conn)
        store = cas.ContentAddressedStorage(tmp_path, suffix=".eml")

        result = client.folder_backup("INBOX", store)
        assert result.stored == 1
        assert result.failed == 0
        assert result.complete
        assert len(list(store.walk())) == 1

    def test_callback_receives_metadata(self, tmp_path):
        conn = _make_mock_conn()
        msg_date = datetime(2026, 2, 20, tzinfo=UTC)
        conn.select_folder.return_value = {b"EXISTS": 1}
        conn.search.return_value = [1]
        conn.fetch.return_value = {
            1: {b"RFC822": DUMMY_EML, b"INTERNALDATE": msg_date},
        }
        client = _make_client(conn=conn)
        store = cas.ContentAddressedStorage(tmp_path, suffix=".eml")

        collected = []
        client.folder_backup("INBOX", store, callback=collected.append)

        assert len(collected) == 1
        md = collected[0]
        assert md.mailbox == "test-mailbox"
        assert md.store_id
        assert md.folders == ["INBOX"]

    def test_callback_error_continues(self, tmp_path):
        conn = _make_mock_conn()
        msg_date = datetime(2026, 2, 20, tzinfo=UTC)
        conn.select_folder.return_value = {b"EXISTS": 2}
        conn.search.return_value = [1, 2]
        conn.fetch.return_value = {
            1: {b"RFC822": DUMMY_EML, b"INTERNALDATE": msg_date},
            2: {b"RFC822": DUMMY_EML, b"INTERNALDATE": msg_date},
        }
        client = _make_client(conn=conn)
        store = cas.ContentAddressedStorage(tmp_path, suffix=".eml")

        def failing_callback(md):
            raise ValueError("callback error")

        # Should not raise, continues processing
        result = client.folder_backup("INBOX", store, callback=failing_callback)
        # The mails are in the store, but without metadata they count as failed,
        # so the run is incomplete and the snapshot must not advance.
        assert result.stored == 0
        assert result.failed == 2
        assert not result.complete

    def test_exchange_journal_unwrap(self, tmp_path):
        journal_eml = (
            b"From: journal@exchange.local\r\n"
            b"To: archive@exchange.local\r\n"
            b"Subject: Journal\r\n"
            b'Content-Type: multipart/mixed; boundary="boundary"\r\n'
            b"\r\n"
            b"--boundary\r\n"
            b"Content-Type: text/plain\r\n"
            b"\r\n"
            b"Journal envelope\r\n"
            b"--boundary\r\n"
            b"Content-Type: message/rfc822\r\n"
            b"\r\n"
            b"From: real@example.com\r\n"
            b"To: dest@example.com\r\n"
            b"Subject: Real Message\r\n"
            b"Message-ID: <real@example.com>\r\n"
            b"Date: Wed, 20 Feb 2026 12:00:00 +0100\r\n"
            b"\r\n"
            b"Real body\r\n"
            b"--boundary--\r\n"
        )
        conn = _make_mock_conn()
        msg_date = datetime(2026, 2, 20, tzinfo=UTC)
        conn.select_folder.return_value = {b"EXISTS": 1}
        conn.search.return_value = [1]
        conn.fetch.return_value = {
            1: {b"RFC822": journal_eml, b"INTERNALDATE": msg_date},
        }
        client = _make_client(exchange_journal=True, conn=conn)
        store = cas.ContentAddressedStorage(tmp_path, suffix=".eml")

        result = client.folder_backup("INBOX", store)
        assert result.stored == 1
        assert result.complete

    def _journal_conn(self, capabilities=None):
        """A mailbox holding one message that is not a journal envelope."""
        msg_date = datetime(2026, 2, 20, tzinfo=UTC)
        conn = _make_mock_conn(capabilities=capabilities or [b"IMAP4rev1"])
        conn.select_folder.return_value = {b"EXISTS": 1}
        conn.search.return_value = [1]
        conn.fetch.return_value = {
            1: {b"RFC822": DUMMY_EML, b"INTERNALDATE": msg_date},
        }
        return conn

    def test_exchange_journal_skip_non_journal(self, tmp_path):
        conn = self._journal_conn()
        client = _make_client(exchange_journal=True, conn=conn)
        store = cas.ContentAddressedStorage(tmp_path, suffix=".eml")

        result = client.folder_backup("INBOX", store)
        assert result.stored == 0  # skipped because not a journal item
        # A deliberate skip is not a failure: it must not block the snapshot.
        assert result.complete
        # Without an error folder the item stays exactly where it was.
        conn.move.assert_not_called()
        conn.copy.assert_not_called()

    def test_a_non_journal_item_lands_in_the_error_folder(self, tmp_path):
        conn = self._journal_conn(capabilities=[b"IMAP4rev1", b"MOVE"])
        client = _make_client(exchange_journal=True, error_folder="Errors", conn=conn)
        store = cas.ContentAddressedStorage(tmp_path, suffix=".eml")

        result = client.folder_backup("INBOX", store)

        assert result.stored == 0
        assert result.complete
        conn.move.assert_called_once_with([1], "Errors")

    def test_the_error_folder_works_without_move_capability(self, tmp_path):
        """Exchange's own IMAP service is exactly where MOVE tends to be absent."""
        conn = self._journal_conn(capabilities=[b"IMAP4rev1"])
        client = _make_client(exchange_journal=True, error_folder="Errors", conn=conn)
        store = cas.ContentAddressedStorage(tmp_path, suffix=".eml")

        client.folder_backup("INBOX", store)

        conn.copy.assert_called_once_with([1], "Errors")

    def test_relocation_happens_after_the_read_only_pass(self, tmp_path):
        """The pass holds the folder read-only, where a MOVE must be refused."""
        conn = self._journal_conn(capabilities=[b"IMAP4rev1", b"MOVE"])
        client = _make_client(exchange_journal=True, error_folder="Errors", conn=conn)
        store = cas.ContentAddressedStorage(tmp_path, suffix=".eml")

        order: list[str] = []
        conn.select_folder.side_effect = lambda f, readonly=True: (
            order.append(f"select(readonly={readonly})"),
            {b"EXISTS": 1},
        )[1]
        conn.unselect_folder.side_effect = lambda: order.append("unselect")
        conn.move.side_effect = lambda ids, dest: order.append("move")

        client.folder_backup("INBOX", store)

        # The read-only pass is fully closed before the folder is reopened.
        assert order == [
            "select(readonly=True)",
            "unselect",
            "select(readonly=False)",
            "move",
            "unselect",
        ]

    def test_exchange_journal_delete_after_successful_export(self, tmp_path):
        journal_eml = (
            b"From: journal@exchange.local\r\n"
            b"Subject: Journal\r\n"
            b'Content-Type: multipart/mixed; boundary="boundary"\r\n'
            b"\r\n"
            b"--boundary\r\n"
            b"Content-Type: text/plain\r\n"
            b"\r\n"
            b"Journal envelope\r\n"
            b"--boundary\r\n"
            b"Content-Type: message/rfc822\r\n"
            b"\r\n"
            b"From: real@example.com\r\n"
            b"Subject: Real Message\r\n"
            b"Message-ID: <real@example.com>\r\n"
            b"Date: Wed, 20 Feb 2026 12:00:00 +0100\r\n"
            b"\r\n"
            b"Real body\r\n"
            b"--boundary--\r\n"
        )
        conn = _make_mock_conn()
        msg_date = datetime(2026, 2, 20, tzinfo=UTC)
        conn.select_folder.return_value = {b"EXISTS": 1}
        conn.search.return_value = [1]
        conn.fetch.return_value = {
            1: {b"RFC822": journal_eml, b"INTERNALDATE": msg_date},
        }
        client = _make_client(exchange_journal=True, delete_after_export=True, conn=conn)
        store = cas.ContentAddressedStorage(tmp_path, suffix=".eml")

        result = client.folder_backup("INBOX", store)
        assert result.stored == 1
        # folder_backup does not delete -- it marks the archived item deletable,
        # and the runner removes it later, through purge, once the log is sealed.
        assert result.deletable == [1]
        conn.delete_messages.assert_not_called()

        client.purge("INBOX", result.deletable)
        conn.delete_messages.assert_called_once_with([1])
        conn.expunge.assert_called_once()

    def test_exchange_journal_non_journal_not_deletable(self, tmp_path):
        # Regression: a non-journal item under delete_after_export must NOT be
        # eligible for deletion while it is being skipped -- otherwise it is lost
        # unarchived. No MOVE capability, so no error_folder is configured.
        conn = _make_mock_conn(capabilities=[b"IMAP4rev1"])
        msg_date = datetime(2026, 2, 20, tzinfo=UTC)
        conn.select_folder.return_value = {b"EXISTS": 1}
        conn.search.return_value = [1]
        conn.fetch.return_value = {
            1: {b"RFC822": DUMMY_EML, b"INTERNALDATE": msg_date},
        }
        client = _make_client(exchange_journal=True, delete_after_export=True, conn=conn)
        store = cas.ContentAddressedStorage(tmp_path, suffix=".eml")

        result = client.folder_backup("INBOX", store)
        assert result.stored == 0
        assert result.deletable == []
        conn.delete_messages.assert_not_called()

    def test_gmail_clears_trash(self, tmp_path):
        conn = _make_mock_conn(capabilities=[b"IMAP4rev1", b"X-GM-EXT-1"])
        conn.select_folder.return_value = {b"EXISTS": 0}
        conn.search.return_value = []
        conn.get_gmail_labels.return_value = {}
        client = _make_client(
            conn=conn,
            trash_folder="[Google Mail]/Trash",
        )

        store = cas.ContentAddressedStorage(tmp_path, suffix=".eml")
        client.folder_backup("INBOX", store)

        # Trash folder should have been selected for clearing
        assert any(
            c == call("[Google Mail]/Trash", readonly=False)
            for c in conn.select_folder.call_args_list
        )


# ---------------------------------------------------------------------------
# purge
# ---------------------------------------------------------------------------


class TestPurge:
    def test_empty_list_is_a_no_op(self):
        conn = _make_mock_conn()
        client = _make_client(delete_after_export=True, conn=conn)

        client.purge("INBOX", [])

        conn.select_folder.assert_not_called()
        conn.delete_messages.assert_not_called()
        conn.expunge.assert_not_called()

    def test_deletes_the_batch_read_write_and_expunges(self):
        conn = _make_mock_conn()
        client = _make_client(delete_after_export=True, conn=conn)

        client.purge("INBOX", [1, 2, 3])

        conn.select_folder.assert_called_once_with("INBOX", readonly=False)
        conn.delete_messages.assert_called_once_with([1, 2, 3])
        conn.expunge.assert_called_once()
        conn.unselect_folder.assert_called_once()


# ---------------------------------------------------------------------------
# _relocate
# ---------------------------------------------------------------------------


class TestRelocate:
    def test_uses_move_when_the_server_has_it(self):
        conn = _make_mock_conn(capabilities=[b"IMAP4rev1", b"MOVE"])
        client = _make_client(conn=conn)

        client._relocate("INBOX", [42], "Errors")
        conn.move.assert_called_once_with([42], "Errors")
        conn.copy.assert_not_called()

    def test_falls_back_to_copy_and_delete_without_move(self):
        """MOVE is RFC 6851; the three-step dance it replaced works everywhere."""
        conn = _make_mock_conn(capabilities=[b"IMAP4rev1", b"UIDPLUS"])
        client = _make_client(conn=conn)

        client._relocate("INBOX", [42], "Errors")
        conn.move.assert_not_called()
        conn.copy.assert_called_once_with([42], "Errors")
        conn.delete_messages.assert_called_once_with([42])
        conn.uid_expunge.assert_called_once_with([42])

    def test_without_uidplus_the_flag_is_left_for_someone_else(self):
        # A plain EXPUNGE would drop every \Deleted message in the folder, not
        # just ours, so the copy is made and the flag simply stays.
        conn = _make_mock_conn(capabilities=[b"IMAP4rev1"])
        client = _make_client(conn=conn)

        client._relocate("INBOX", [42], "Errors")
        conn.copy.assert_called_once_with([42], "Errors")
        conn.delete_messages.assert_called_once_with([42])
        conn.uid_expunge.assert_not_called()
        conn.expunge.assert_not_called()

    def test_the_folder_is_reopened_writable(self):
        """The backup pass holds it read-only, where a server must refuse this."""
        conn = _make_mock_conn(capabilities=[b"IMAP4rev1", b"MOVE"])
        client = _make_client(conn=conn)

        client._relocate("INBOX", [42], "Errors")
        conn.select_folder.assert_called_once_with("INBOX", readonly=False)
        conn.unselect_folder.assert_called_once()

    def test_a_missing_destination_is_created(self):
        conn = _make_mock_conn(capabilities=[b"IMAP4rev1", b"MOVE"])
        conn.folder_exists.return_value = False
        client = _make_client(conn=conn)

        client._relocate("INBOX", [42], "Errors")
        conn.create_folder.assert_called_once_with("Errors")

    def test_nothing_to_move_touches_nothing(self):
        conn = _make_mock_conn(capabilities=[b"IMAP4rev1", b"MOVE"])
        client = _make_client(conn=conn)

        client._relocate("INBOX", [], "Errors")
        conn.select_folder.assert_not_called()

    def test_a_failure_costs_the_move_and_nothing_else(self):
        conn = _make_mock_conn(capabilities=[b"IMAP4rev1", b"MOVE"])
        conn.move.side_effect = OSError("connection reset")
        client = _make_client(conn=conn)

        client._relocate("INBOX", [42], "Errors")  # must not raise
        conn.unselect_folder.assert_called_once()


# ---------------------------------------------------------------------------
# _collect_metadata
# ---------------------------------------------------------------------------


class TestCollectMetadata:
    def test_standard_metadata(self):
        conn = _make_mock_conn()
        client = _make_client(conn=conn)

        md = client._collect_metadata("INBOX", 1, "hash123")
        assert md.mailbox == "test-mailbox"
        assert md.store_id == "hash123"
        assert md.folders == ["INBOX"]

    def test_gmail_reports_its_own_folders_only(self):
        """The IMAP folder name is a localised view of what X-GM-LABELS already
        says, so taking both would record one place twice."""
        conn = _make_mock_conn(capabilities=[b"IMAP4rev1", b"X-GM-EXT-1"])
        conn.get_gmail_labels.return_value = {1: [b"\\Important", b"Work"]}
        client = _make_client(conn=conn)

        md = client._collect_metadata("INBOX", 1, "hash123")

        assert md.folders == [b"\\Important", b"Work"]
        assert "INBOX" not in md.folders

    def test_gmail_localised_pseudo_folder_is_not_recorded(self):
        conn = _make_mock_conn(capabilities=[b"IMAP4rev1", b"X-GM-EXT-1"])
        conn.get_gmail_labels.return_value = {1: [b"\\Sent"]}
        client = _make_client(conn=conn)

        md = client._collect_metadata("[Google Mail]/Sent", 1, "hash123")

        assert md.folders == [b"\\Sent"]

    def test_gmail_message_without_labels_lands_in_all_mail(self):
        """A message filed nowhere else is in All Mail, which is a place too."""
        conn = _make_mock_conn(capabilities=[b"IMAP4rev1", b"X-GM-EXT-1"])
        conn.get_gmail_labels.return_value = {}
        client = _make_client(conn=conn)

        md = client._collect_metadata("[Google Mail]/Alle Nachrichten", 1, "hash123")

        assert md.folders == [imap.GMAIL_ALL_MAIL]


# ---------------------------------------------------------------------------
# _clear_folder
# ---------------------------------------------------------------------------


class TestClearFolder:
    def test_clears_all_messages(self):
        conn = _make_mock_conn()
        conn.search.return_value = [1, 2, 3]
        client = _make_client(conn=conn)

        client._clear_folder("Trash")
        conn.select_folder.assert_called_with("Trash", readonly=False)
        conn.delete_messages.assert_called()
        conn.expunge.assert_called_once()
        conn.unselect_folder.assert_called_once()

    def test_handles_error_gracefully(self):
        conn = _make_mock_conn()
        conn.select_folder.side_effect = Exception("folder not found")
        client = _make_client(conn=conn)

        # Should not raise
        client._clear_folder("NonExistent")


# ---------------------------------------------------------------------------
# Gmail detection
# ---------------------------------------------------------------------------


class TestCapabilityDetection:
    def test_gmail_detected(self):
        conn = _make_mock_conn(capabilities=[b"IMAP4rev1", b"X-GM-EXT-1"])
        client = _make_client(conn=conn)
        assert client.gmail is True

    def test_non_gmail(self):
        conn = _make_mock_conn(capabilities=[b"IMAP4rev1"])
        client = _make_client(conn=conn)
        assert client.gmail is False

    def test_move_capability(self):
        conn = _make_mock_conn(capabilities=[b"IMAP4rev1", b"MOVE"])
        client = _make_client(conn=conn)
        assert client.move_cap is True

    def test_no_move_capability(self):
        conn = _make_mock_conn(capabilities=[b"IMAP4rev1"])
        client = _make_client(conn=conn)
        assert client.move_cap is False
        assert client.error_folder is None  # disabled without MOVE


# ---------------------------------------------------------------------------
# message_index / fetch_message
# ---------------------------------------------------------------------------


class _Envelope:
    """Stand-in for imapclient's Envelope namedtuple."""

    def __init__(self, message_id, date=None):
        self.message_id = message_id
        self.date = date


class TestMessageIndex:
    def test_yields_refs_without_bodies(self):
        conn = _make_mock_conn()
        msg_date = datetime(2026, 2, 20, tzinfo=UTC)
        conn.search.return_value = [1, 2]
        conn.fetch.return_value = {
            1: {b"ENVELOPE": _Envelope(b"<a@example.com>", msg_date)},
            2: {b"ENVELOPE": _Envelope(b"<b@example.com>", msg_date)},
        }
        client = _make_client(conn=conn)

        refs = list(client.message_index("INBOX"))

        assert [r.msg_id for r in refs] == [1, 2]
        assert [r.message_id for r in refs] == ["<a@example.com>", "<b@example.com>"]
        assert refs[0].date == msg_date
        # Only envelopes are requested, never RFC822.
        assert conn.fetch.call_args[0][1] == ["ENVELOPE"]

    def test_missing_message_id_yields_empty_string(self):
        conn = _make_mock_conn()
        conn.search.return_value = [1]
        conn.fetch.return_value = {1: {b"ENVELOPE": _Envelope(None)}}
        client = _make_client(conn=conn)

        refs = list(client.message_index("INBOX"))
        assert refs[0].message_id == ""

    def test_folder_is_selected_readonly_and_released(self):
        conn = _make_mock_conn()
        conn.search.return_value = []
        client = _make_client(conn=conn)

        list(client.message_index("Archive"))

        conn.select_folder.assert_called_with("Archive", readonly=True)
        conn.unselect_folder.assert_called_once()

    def test_since_narrows_the_search(self):
        conn = _make_mock_conn()
        conn.search.return_value = []
        client = _make_client(conn=conn)

        list(client.message_index("INBOX", since=datetime(2026, 2, 20, tzinfo=UTC)))

        query = conn.search.call_args[0][0]
        assert "SINCE" in query

    def test_fetch_failure_is_reported(self):
        conn = _make_mock_conn()
        conn.search.return_value = [1]
        conn.fetch.side_effect = imaplib.IMAP4.error("connection lost")
        client = _make_client(conn=conn)

        with pytest.raises(imap.MailboxError):
            list(client.message_index("INBOX"))
        conn.unselect_folder.assert_called_once()


class TestFetchMessage:
    def test_returns_raw_message(self):
        conn = _make_mock_conn()
        conn.fetch.return_value = {7: {b"RFC822": DUMMY_EML}}
        client = _make_client(conn=conn)

        assert client.fetch_message(7, "INBOX") == DUMMY_EML
        conn.select_folder.assert_called_with("INBOX", readonly=True)
        conn.unselect_folder.assert_called_once()

    def test_unknown_uid_raises(self):
        conn = _make_mock_conn()
        conn.fetch.return_value = {}
        client = _make_client(conn=conn)

        with pytest.raises(imap.MailboxError):
            client.fetch_message(7, "INBOX")
