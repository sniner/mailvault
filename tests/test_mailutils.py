import pytest

from mailvault import mailutils
from .fixtures import dummy_eml_bytes


def test_decode_email_header(dummy_eml_bytes):
    msg = mailutils.decode_email_header(dummy_eml_bytes)
    assert msg is not None
    assert msg["Subject"] == "Test Email"


def test_decode_email_full(dummy_eml_bytes):
    msg = mailutils.decode_email(dummy_eml_bytes)
    assert msg is not None
    assert msg["Subject"] == "Test Email"
    assert msg.get_body() is not None


def test_addresses(dummy_eml_bytes):
    msg = mailutils.decode_email_header(dummy_eml_bytes)
    from_addrs, to_addrs = mailutils.addresses(msg)
    assert from_addrs == {"test@example.com"}
    assert "recipient@example.com" in to_addrs


def test_addresses_with_cc():
    eml = b"""From: alice@example.com
To: bob@example.com
CC: carol@example.com, dave@example.com
Subject: CC Test

Body.
"""
    msg = mailutils.decode_email_header(eml)
    from_addrs, to_addrs = mailutils.addresses(msg)
    assert from_addrs == {"alice@example.com"}
    assert to_addrs == {"bob@example.com", "carol@example.com", "dave@example.com"}


def test_addresses_with_received_for():
    eml = b"""From: sender@example.com
To: list@example.com
Received: from mx.example.com by server.example.com for <hidden@example.com>; Wed, 20 Feb 2026 12:00:00 +0100
Subject: Received For Test

Body.
"""
    msg = mailutils.decode_email_header(eml)
    _, to_addrs = mailutils.addresses(msg)
    assert "hidden@example.com" in to_addrs
    assert "list@example.com" in to_addrs


def test_addresses_lowercase():
    eml = b"""From: Alice@Example.COM
To: BOB@Example.Org
Subject: Case Test

Body.
"""
    msg = mailutils.decode_email_header(eml)
    from_addrs, to_addrs = mailutils.addresses(msg)
    assert "alice@example.com" in from_addrs
    assert "bob@example.org" in to_addrs


def test_subject(dummy_eml_bytes):
    msg = mailutils.decode_email_header(dummy_eml_bytes)
    assert mailutils.subject(msg) == "Test Email"


def test_subject_missing():
    eml = b"""From: a@b.com
To: c@d.com

No subject here.
"""
    msg = mailutils.decode_email_header(eml)
    assert mailutils.subject(msg) == ""


def test_message_id():
    eml = b"""From: a@b.com
To: c@d.com
Message-Id: <unique-id-123@example.com>
Subject: ID Test

Body.
"""
    msg = mailutils.decode_email_header(eml)
    assert mailutils.message_id(msg) == "<unique-id-123@example.com>"


def test_message_id_missing():
    eml = b"""From: a@b.com
To: c@d.com
Subject: No ID

Body.
"""
    msg = mailutils.decode_email_header(eml)
    assert mailutils.message_id(msg) == ""


def test_date(dummy_eml_bytes):
    msg = mailutils.decode_email_header(dummy_eml_bytes)
    dt = mailutils.date(msg)
    assert dt is not None
    assert dt.year == 2026


def test_date_missing():
    eml = b"""From: a@b.com
To: c@d.com
Subject: No Date

Body.
"""
    msg = mailutils.decode_email_header(eml)
    assert mailutils.date(msg) is None


def test_unwrap_exchange_journal_item():
    journal = (
        b"From: journal@example.com\r\n"
        b"To: archive@example.com\r\n"
        b"Subject: Journal\r\n"
        b"Content-Type: multipart/mixed; boundary=\"BOUNDARY\"\r\n"
        b"\r\n"
        b"--BOUNDARY\r\n"
        b"Content-Type: text/plain\r\n"
        b"\r\n"
        b"Journal envelope\r\n"
        b"--BOUNDARY\r\n"
        b"Content-Type: message/rfc822\r\n"
        b"\r\n"
        b"From: real-sender@example.com\r\n"
        b"To: real-recipient@example.com\r\n"
        b"Subject: Real Message\r\n"
        b"\r\n"
        b"Real body content.\r\n"
        b"--BOUNDARY--\r\n"
    )
    result = mailutils.unwrap_exchange_journal_item(journal)
    assert result is not None
    assert b"Real Message" in result


def test_unwrap_exchange_journal_not_a_journal():
    plain = b"""From: a@b.com
To: c@d.com
Subject: Plain email

Just a plain email, no RFC822 attachments.
"""
    result = mailutils.unwrap_exchange_journal_item(plain)
    assert result is None


# ---------------------------------------------------------------------------
# normalize_message_id
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "value,expected",
    [
        ("<abc@example.com>", "abc@example.com"),
        ("abc@example.com", "abc@example.com"),
        ("  <abc@example.com>  ", "abc@example.com"),
        ("<ABC@Example.COM>", "abc@example.com"),
        # Header folding turns up in raw MIME but never on the server side.
        ("<abc@example.com>\r\n", "abc@example.com"),
        # Unfolding leaves the space behind, which makes the value malformed;
        # the header parser then truncates it. Both sides do so identically.
        ("<abc\r\n @example.com>", "abc"),
        ("", ""),
        (None, ""),
        ("<>", ""),
    ],
)
def test_normalize_message_id(value, expected):
    assert mailutils.normalize_message_id(value) == expected


def test_normalize_message_id_matches_across_sources():
    """A header value and a server-reported value must compare equal."""
    eml = b"Message-ID: <ABC123@Example.com>\r\nSubject: x\r\n\r\nbody\r\n"
    from_header = mailutils.message_id(mailutils.decode_email_header(eml))
    from_server = "<abc123@example.com>"
    assert mailutils.normalize_message_id(from_header) == mailutils.normalize_message_id(
        from_server
    )


# ---------------------------------------------------------------------------
# metadata
# ---------------------------------------------------------------------------

def test_metadata_record(dummy_eml_bytes):
    md = mailutils.metadata(
        dummy_eml_bytes, mailbox="mb", folder="INBOX", store_id="deadbeef"
    )
    assert md["mailbox"] == "mb"
    assert md["folder"] == "INBOX"
    assert md["store_id"] == "deadbeef"
    assert md["labels"] == ["INBOX"]
    assert md["subject"] == "Test Email"
    assert "test@example.com" in md["sender"]
    assert "recipient@example.com" in md["recipients"]


def test_metadata_explicit_labels(dummy_eml_bytes):
    md = mailutils.metadata(
        dummy_eml_bytes, mailbox="mb", folder="INBOX", store_id="x", labels=["A", "B"]
    )
    assert md["labels"] == ["A", "B"]


@pytest.mark.parametrize(
    "server_value,archived_value",
    [
        # Python's header parser truncates at the second "@", so the value the
        # server reports and the value stored in the archive differ literally.
        ("<00a3$dec0@schneider@pcvisit.example>", "<00a3$dec0@schneider>"),
        ("<a@b@c@d>", "<a@b>"),
    ],
)
def test_normalize_message_id_matches_truncated_headers(server_value, archived_value):
    assert mailutils.normalize_message_id(server_value) == mailutils.normalize_message_id(
        archived_value
    )


@pytest.mark.parametrize(
    "value",
    [
        "<abc@example.com>",
        "4128136537",
        "<n=2eumh.22er6bf",
        "<a@b@c@d>",
        "870-LBG-312:0:56687@marketo.example",
    ],
)
def test_normalize_message_id_is_idempotent(value):
    once = mailutils.normalize_message_id(value)
    assert mailutils.normalize_message_id(once) == once


# ---------------------------------------------------------------------------
# MessageIdIndex
# ---------------------------------------------------------------------------

def _long_id(tail: str = "spnotify") -> str:
    """A Message-ID long enough to be affected by server-side truncation."""
    return "teamsmissedactivityemail-" + "a1b2c3d4-" * 20 + "@od" + tail


class TestMessageIdIndex:
    def test_exact_match(self):
        index = mailutils.MessageIdIndex({"a@example.com", "b@example.com"})
        assert "a@example.com" in index
        assert "c@example.com" not in index

    def test_empty_values_never_match(self):
        assert "" not in mailutils.MessageIdIndex({"a@example.com"})
        assert "" not in mailutils.MessageIdIndex(set())

    def test_empty_values_are_not_counted(self):
        assert len(mailutils.MessageIdIndex({"a@example.com", ""})) == 1

    def test_truncated_long_id_matches_by_prefix(self):
        """Exchange caps the reported Message-ID, so the server value is a prefix."""
        archived = _long_id()
        index = mailutils.MessageIdIndex({archived})
        assert archived[:255] in index

    def test_short_id_is_never_prefix_matched(self):
        """A short prefix must not silently match a longer archived ID."""
        index = mailutils.MessageIdIndex({"abcdef@example.com"})
        assert "abc" not in index

    def test_prefix_match_picks_the_right_entry(self):
        a, b = _long_id("aaa"), _long_id("bbb")
        index = mailutils.MessageIdIndex({a, b, "short@example.com"})
        assert a in index
        assert b in index
        assert a[:255] in index
        # A long value that is nobody's prefix stays unmatched.
        assert (_long_id("zzz") + "-x") not in index

    def test_longer_value_does_not_match_shorter_archived(self):
        archived = _long_id()
        index = mailutils.MessageIdIndex({archived})
        assert archived + "-more" not in index
