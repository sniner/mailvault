"""Tests for mailvault.mailbox with mocked IMAPClient."""

from __future__ import annotations

import imaplib
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest

from mailvault import cas, conf, mailbox

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
    mb = mailbox.Mailbox(job=job)
    mb._conn = conn
    client = mailbox.ImapClient(mb)
    return client


# ---------------------------------------------------------------------------
# Mailbox context manager
# ---------------------------------------------------------------------------


class TestMailboxContextManager:
    @patch("mailvault.mailbox.imapclient.IMAPClient")
    def test_enter_creates_connection_with_tls(self, mock_imap_cls):
        mock_conn = _make_mock_conn()
        mock_imap_cls.return_value = mock_conn
        job = _make_job()

        with mailbox.Mailbox(job=job) as client:
            assert client is not None
            mock_imap_cls.assert_called_once()
            kwargs = mock_imap_cls.call_args
            assert kwargs.kwargs["host"] == "imap.example.com"
            assert kwargs.kwargs["port"] == 993
            assert kwargs.kwargs["ssl"] is True
            assert kwargs.kwargs["ssl_context"] is not None
            mock_conn.login.assert_called_once_with("user", "pass")

    @patch("mailvault.mailbox.imapclient.IMAPClient")
    def test_exit_calls_logout(self, mock_imap_cls):
        mock_conn = _make_mock_conn()
        mock_imap_cls.return_value = mock_conn
        job = _make_job()

        with mailbox.Mailbox(job=job):
            pass
        mock_conn.logout.assert_called_once()

    @patch("mailvault.mailbox.imapclient.IMAPClient")
    def test_no_tls(self, mock_imap_cls):
        mock_conn = _make_mock_conn()
        mock_imap_cls.return_value = mock_conn
        job = _make_job(tls=False)

        with mailbox.Mailbox(job=job):
            kwargs = mock_imap_cls.call_args
            assert kwargs.kwargs["ssl"] is False
            assert kwargs.kwargs["ssl_context"] is None

    @patch("mailvault.mailbox.imapclient.IMAPClient")
    def test_tls_no_hostname_check(self, mock_imap_cls):
        mock_conn = _make_mock_conn()
        mock_imap_cls.return_value = mock_conn
        job = _make_job(tls_check_hostname=False, tls_verify_cert=True)

        with mailbox.Mailbox(job=job):
            ssl_ctx = mock_imap_cls.call_args.kwargs["ssl_context"]
            assert ssl_ctx is not None
            assert ssl_ctx.check_hostname is False


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
        assert mailbox.ImapClient._isfoldertype(folder, "Junk") == "Junk"

    def test_isfoldertype_no_match(self):
        folder = ([b"\\HasNoChildren"], b"/", "INBOX")
        assert mailbox.ImapClient._isfoldertype(folder, "Junk", "Trash") is None

    def test_isfoldertype_case(self):
        # capitalize() is applied, so "junk" becomes "Junk" -> b"\\Junk"
        folder = ([b"\\Junk"], b"/", "Spam")
        assert mailbox.ImapClient._isfoldertype(folder, "junk") == "Junk"

    def test_isfoldername_match(self):
        folder = ([b"\\HasNoChildren"], b"/", "Notes")
        assert mailbox.ImapClient._isfoldername(folder, "Not.*") == "Not.*"

    def test_isfoldername_no_match(self):
        folder = ([b"\\HasNoChildren"], b"/", "INBOX")
        assert mailbox.ImapClient._isfoldername(folder, "Notes") is None


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
        conn.delete_messages.assert_not_called()

    def test_delete_on_success(self):
        conn = _make_mock_conn()
        msg_date = datetime(2026, 2, 20, tzinfo=UTC)
        conn.fetch.return_value = {
            1: {b"RFC822": DUMMY_EML, b"INTERNALDATE": msg_date},
        }
        client = _make_client(conn=conn)

        list(client._walk_folder("INBOX", [1], delete=True))
        conn.delete_messages.assert_called_once_with([1])

    def test_no_delete_on_fetch_error(self):
        conn = _make_mock_conn()
        conn.fetch.side_effect = OSError("connection reset")
        client = _make_client(conn=conn)

        list(client._walk_folder("INBOX", [1], delete=True))
        conn.delete_messages.assert_not_called()


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

    def test_delete_after_export(self):
        conn = _make_mock_conn()
        msg_date = datetime(2026, 2, 20, tzinfo=UTC)
        conn.select_folder.return_value = {b"EXISTS": 1}
        conn.search.return_value = [1]
        conn.fetch.return_value = {
            1: {b"RFC822": DUMMY_EML, b"INTERNALDATE": msg_date},
        }
        client = _make_client(delete_after_export=True, conn=conn)

        list(client._iter_folder("INBOX"))
        conn.select_folder.assert_called_with("INBOX", readonly=False)
        conn.delete_messages.assert_called_once_with(1)
        conn.expunge.assert_called_once()

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
        assert md["folder"] == "INBOX"
        assert md["email_id"] == "<abc123@example.com>"
        assert "sender@example.com" in md["sender"]
        assert "recipient@example.com" in md["recipients"]
        assert md["subject"] == "Hello World"

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

    def test_exchange_journal_skip_non_journal(self, tmp_path):
        conn = _make_mock_conn()
        msg_date = datetime(2026, 2, 20, tzinfo=UTC)
        conn.select_folder.return_value = {b"EXISTS": 1}
        conn.search.return_value = [1]
        conn.fetch.return_value = {
            1: {b"RFC822": DUMMY_EML, b"INTERNALDATE": msg_date},
        }
        # No MOVE capability, so error_folder won't be set
        conn = _make_mock_conn(capabilities=[b"IMAP4rev1"])
        conn.select_folder.return_value = {b"EXISTS": 1}
        conn.search.return_value = [1]
        conn.fetch.return_value = {
            1: {b"RFC822": DUMMY_EML, b"INTERNALDATE": msg_date},
        }
        client = _make_client(exchange_journal=True, conn=conn)
        store = cas.ContentAddressedStorage(tmp_path, suffix=".eml")

        result = client.folder_backup("INBOX", store)
        assert result.stored == 0  # skipped because not a journal item
        # A deliberate skip is not a failure: it must not block the snapshot.
        assert result.complete

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
        # An archived journal item is deleted from the server.
        conn.delete_messages.assert_called_once_with(1)
        conn.expunge.assert_called_once()

    def test_exchange_journal_non_journal_not_deleted(self, tmp_path):
        # Regression: a non-journal item under delete_after_export must NOT be
        # deleted from the server while it is being skipped -- otherwise it is
        # lost unarchived. No MOVE capability, so no error_folder is configured.
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
# full_backup
# ---------------------------------------------------------------------------


class TestFullBackup:
    def test_iterates_all_folders(self, tmp_path):
        folders = [
            ([b"\\HasNoChildren"], b"/", "INBOX"),
            ([b"\\HasNoChildren"], b"/", "Sent"),
        ]
        conn = _make_mock_conn(folders=folders)
        conn.select_folder.return_value = {b"EXISTS": 0}
        conn.search.return_value = []
        client = _make_client(conn=conn)
        store = cas.ContentAddressedStorage(tmp_path, suffix=".eml")

        client.full_backup(store)

        # Should have selected both folders
        folder_calls = [c[0][0] for c in conn.select_folder.call_args_list]
        assert "INBOX" in folder_calls
        assert "Sent" in folder_calls

    def test_continues_on_folder_error(self, tmp_path):
        folders = [
            ([b"\\HasNoChildren"], b"/", "INBOX"),
            ([b"\\HasNoChildren"], b"/", "Sent"),
        ]
        conn = _make_mock_conn(folders=folders)
        call_count = [0]

        def select_side_effect(name, readonly=True):
            call_count[0] += 1
            if name == "INBOX":
                raise Exception("folder error")
            return {b"EXISTS": 0}

        conn.select_folder.side_effect = select_side_effect
        conn.search.return_value = []
        client = _make_client(conn=conn)
        store = cas.ContentAddressedStorage(tmp_path, suffix=".eml")

        # Should not raise despite INBOX error
        client.full_backup(store)
        assert call_count[0] >= 2  # both folders attempted


# ---------------------------------------------------------------------------
# get_messages
# ---------------------------------------------------------------------------


class TestGetMessages:
    def test_yields_messages(self):
        conn = _make_mock_conn()
        msg_date = datetime(2026, 2, 20, tzinfo=UTC)
        conn.select_folder.return_value = {b"EXISTS": 1}
        conn.search.return_value = [1]
        conn.fetch.return_value = {
            1: {b"RFC822": DUMMY_EML, b"INTERNALDATE": msg_date},
        }
        client = _make_client(conn=conn)

        results = list(client.get_messages("INBOX"))
        assert len(results) == 1
        msg_id, date, msg = results[0]
        assert msg_id == 1
        assert date == msg_date
        assert msg == DUMMY_EML


# ---------------------------------------------------------------------------
# save_message / move_message / delete_message
# ---------------------------------------------------------------------------


class TestMessageOperations:
    def test_save_message(self):
        conn = _make_mock_conn()
        client = _make_client(conn=conn)
        msg_date = datetime(2026, 2, 20, tzinfo=UTC)

        client.save_message(DUMMY_EML, "Archive", date=msg_date)
        conn.append.assert_called_once_with("Archive", DUMMY_EML, msg_time=msg_date)

    def test_save_message_creates_folder(self):
        conn = _make_mock_conn()
        conn.folder_exists.return_value = False
        client = _make_client(conn=conn)

        client.save_message(DUMMY_EML, "NewFolder")
        conn.create_folder.assert_called_once_with("NewFolder")
        conn.append.assert_called_once()

    def test_move_message_with_capability(self):
        conn = _make_mock_conn(capabilities=[b"IMAP4rev1", b"MOVE"])
        client = _make_client(conn=conn)

        client.move_message(42, "Archive")
        conn.move.assert_called_once_with(42, "Archive")

    def test_move_message_creates_folder(self):
        conn = _make_mock_conn(capabilities=[b"IMAP4rev1", b"MOVE"])
        conn.folder_exists.return_value = False
        client = _make_client(conn=conn)

        client.move_message(42, "Archive")
        conn.create_folder.assert_called_once_with("Archive")

    def test_move_message_without_capability(self):
        conn = _make_mock_conn(capabilities=[b"IMAP4rev1"])
        client = _make_client(conn=conn)

        with pytest.raises(mailbox.MailboxError, match="MOVE"):
            client.move_message(42, "Archive")

    def test_delete_message(self):
        conn = _make_mock_conn()
        client = _make_client(conn=conn)

        client.delete_message(42)
        conn.delete_messages.assert_called_once_with(42)
        conn.expunge.assert_not_called()

    def test_delete_message_with_expunge(self):
        conn = _make_mock_conn()
        client = _make_client(conn=conn)

        client.delete_message(42, expunge=True)
        conn.delete_messages.assert_called_once_with(42)
        conn.expunge.assert_called_once_with(42)


# ---------------------------------------------------------------------------
# _collect_metadata
# ---------------------------------------------------------------------------


class TestCollectMetadata:
    def test_standard_metadata(self):
        conn = _make_mock_conn()
        client = _make_client(conn=conn)

        md = client._collect_metadata("INBOX", 1, "hash123", DUMMY_EML)
        assert md["mailbox"] == "test-mailbox"
        assert md["folder"] == "INBOX"
        assert md["store_id"] == "hash123"
        assert md["email_id"] == "<abc123@example.com>"
        assert md["labels"] == ["INBOX"]
        assert "sender@example.com" in md["sender"]
        assert "recipient@example.com" in md["recipients"]
        assert md["subject"] == "Hello World"

    def test_gmail_labels(self):
        conn = _make_mock_conn(capabilities=[b"IMAP4rev1", b"X-GM-EXT-1"])
        conn.get_gmail_labels.return_value = {1: [b"\\Important", b"Work"]}
        client = _make_client(conn=conn)

        md = client._collect_metadata("INBOX", 1, "hash123", DUMMY_EML)
        assert "INBOX" in md["labels"]
        assert b"\\Important" in md["labels"]
        assert b"Work" in md["labels"]

    def test_gmail_labels_google_mail_prefix(self):
        conn = _make_mock_conn(capabilities=[b"IMAP4rev1", b"X-GM-EXT-1"])
        conn.get_gmail_labels.return_value = {1: [b"\\Sent"]}
        client = _make_client(conn=conn)

        md = client._collect_metadata("[Google Mail]/Sent", 1, "hash123", DUMMY_EML)
        # folder_name starting with [Google Mail] should not be prepended
        assert "[Google Mail]/Sent" not in md["labels"]
        assert md["labels"] == [b"\\Sent"]


# ---------------------------------------------------------------------------
# select_folder
# ---------------------------------------------------------------------------


class TestSelectFolder:
    def test_creates_missing_folder(self):
        conn = _make_mock_conn()
        conn.folder_exists.return_value = False
        client = _make_client(conn=conn)

        client.select_folder("NewFolder")
        conn.create_folder.assert_called_once_with("NewFolder")
        conn.select_folder.assert_called_with("NewFolder", readonly=True)

    def test_existing_folder(self):
        conn = _make_mock_conn()
        conn.folder_exists.return_value = True
        client = _make_client(conn=conn)

        client.select_folder("INBOX", readonly=False)
        conn.create_folder.assert_not_called()
        conn.select_folder.assert_called_with("INBOX", readonly=False)


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

        with pytest.raises(mailbox.MailboxError):
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

        with pytest.raises(mailbox.MailboxError):
            client.fetch_message(7, "INBOX")
