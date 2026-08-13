import pytest

from mailvault import mailutils


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
Received: from mx.test by srv.test for <hidden@example.com>; Wed, 20 Feb 2026 12:00:00 +0100
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
        b'Content-Type: multipart/mixed; boundary="BOUNDARY"\r\n'
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


def test_a_journal_item_carrying_non_ascii_is_written_back_out_whole():
    """The unencoded 8-bit that Exchange hands over, through unwrapping and back.

    Raw non-ASCII in a header and in the body, declared 8bit -- the shape that
    used to send `as_bytes` down a fallback chain. It has to come back byte for
    byte in the body, because what is written out here is what the archive keeps.
    """
    body = "Grüße aus München -- ölig, süß\r\n".encode()
    journal = (
        b"From: journal@example.com\r\n"
        b"Subject: Journal\r\n"
        b'Content-Type: multipart/mixed; boundary="BOUNDARY"\r\n'
        b"\r\n"
        b"--BOUNDARY\r\n"
        b"Content-Type: text/plain\r\n"
        b"\r\n"
        b"Journal envelope\r\n"
        b"--BOUNDARY\r\n"
        b"Content-Type: message/rfc822\r\n"
        b"\r\n"
        b"From: real-sender@example.com\r\n"
        b"Subject: " + "Ölwechsel".encode() + b"\r\n"
        b'Content-Type: text/plain; charset="utf-8"\r\n'
        b"Content-Transfer-Encoding: 8bit\r\n"
        b"\r\n" + body + b"--BOUNDARY--\r\n"
    )
    result = mailutils.unwrap_exchange_journal_item(journal)
    assert result is not None
    assert body.rstrip(b"\r\n") in result


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


def test_metadata_record():
    md = mailutils.metadata(mailbox="mb", folder="INBOX", store_id="deadbeef")
    assert md.mailbox == "mb"
    assert md.store_id == "deadbeef"
    assert md.folders == ["INBOX"]


def test_metadata_explicit_folders():
    md = mailutils.metadata(mailbox="mb", folder="INBOX", store_id="x", folders=["A", "B"])
    assert md.folders == ["A", "B"]


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


def test_date_decodes_an_rfc2047_encoded_header():
    """Nineties mail sometimes encodes the whole Date, comment and all."""
    raw = (
        b"From: a@example.com\r\n"
        b"Date: =?iso-8859-1?Q?Thu=2C_18_Dec_1997_22=3A03=3A34_+0100_=28=28ME?="
        b" =?iso-8859-1?Q?Z=29_Mitteleurop=E4ische_Zeit=29?=\r\n"
        b"\r\n"
    )
    header = mailutils.decode_email_header(raw)

    parsed = mailutils.date(header)

    assert parsed is not None
    assert parsed.year == 1997 and parsed.month == 12 and parsed.day == 18


def test_date_returns_none_for_a_header_beyond_repair(caplog):
    raw = b"From: a@example.com\r\nDate: yesterday afternoon\r\n\r\n"
    header = mailutils.decode_email_header(raw)

    assert mailutils.date(header) is None
    assert "Unreadable Date header" in caplog.text


def test_group_address_yields_no_empty_recipient():
    """'Undisclosed recipients:;' is legal RFC 5322 and names nobody."""
    raw = b"From: a@example.com\r\nTo: Undisclosed recipients:;\r\n\r\n"
    header = mailutils.decode_email_header(raw)

    _from_addrs, to_addrs = mailutils.addresses(header)

    assert to_addrs == set()


def test_group_members_are_collected():
    raw = b"From: a@example.com\r\nTo: Team: b@example.com, c@example.com;\r\n\r\n"
    header = mailutils.decode_email_header(raw)

    _from_addrs, to_addrs = mailutils.addresses(header)

    assert to_addrs == {"b@example.com", "c@example.com"}


def test_date_separates_a_timezone_glued_to_the_time():
    raw = b"From: a@example.com\r\nDate: Tue, 04 Apr 00 06:41:03EST\r\n\r\n"
    header = mailutils.decode_email_header(raw)

    parsed = mailutils.date(header)

    assert parsed is not None
    assert (parsed.year, parsed.month, parsed.day) == (2000, 4, 4)


def test_date_drops_an_impossible_utc_offset():
    """+9752 is not a timezone by any reading; the local time still is one."""
    raw = b"From: a@example.com\r\nDate: Fri, 8 Aug 7048 10:02:45 +9752\r\n\r\n"
    header = mailutils.decode_email_header(raw)

    parsed = mailutils.date(header)

    assert parsed is not None
    assert (parsed.year, parsed.hour) == (7048, 10)


def test_date_keeps_a_valid_offset_untouched():
    raw = b"From: a@example.com\r\nDate: Wed, 20 Feb 2026 12:00:00 -0500\r\n\r\n"
    header = mailutils.decode_email_header(raw)

    parsed = mailutils.date(header)

    assert parsed is not None
    offset = parsed.utcoffset()
    assert offset is not None
    assert offset.total_seconds() == -5 * 3600


class TestDateByConvention:
    """The last rung: repairs that lean on a convention, tried after all others.

    Every case here comes out of the reference archive, where 110 of 131,000
    Date headers could not be read at all.
    """

    @staticmethod
    def _date(value: str):
        raw = f"From: a@example.com\r\nDate: {value}\r\n\r\n".encode()
        return mailutils.date(mailutils.decode_email_header(raw))

    def test_a_weekday_no_parser_knows_is_dropped(self):
        """`Thur` is nobody's abbreviation, and the weekday says nothing anyway."""
        parsed = self._date("Thur, 11 Dec 1997")

        assert parsed is not None
        assert (parsed.year, parsed.month, parsed.day) == (1997, 12, 11)

    def test_a_month_spelled_out_in_full_is_read(self):
        parsed = self._date("Thur, 26 June 1997")

        assert parsed is not None
        assert (parsed.year, parsed.month, parsed.day) == (1997, 6, 26)

    def test_a_german_month_becomes_the_english_one(self):
        parsed = self._date("Sa, 14 Dez 2002 00:49:11 +0100")

        assert parsed is not None
        assert (parsed.year, parsed.month, parsed.day) == (2002, 12, 14)
        assert (parsed.hour, parsed.minute) == (0, 49)

    def test_a_german_month_written_out(self):
        parsed = self._date("Do, 5 März 2003 08:15:00 +0100")

        assert parsed is not None
        assert (parsed.year, parsed.month, parsed.day) == (2003, 3, 5)

    def test_a_dotted_date_is_read_when_it_cannot_be_a_month(self):
        parsed = self._date("27.11.2002 12:03:27")

        assert parsed is not None
        assert (parsed.year, parsed.month, parsed.day) == (2002, 11, 27)

    def test_an_ambiguous_dotted_date_stays_unread(self, caplog):
        """05.03.2002 is March to half the world and May to the other half."""
        assert self._date("05.03.2002 12:03:27") is None
        assert "Unreadable Date header" in caplog.text

    def test_a_date_with_no_time_gets_midnight(self):
        parsed = self._date("Mon, 11 Mar 2002 PST")

        assert parsed is not None
        assert (parsed.year, parsed.month, parsed.day) == (2002, 3, 11)
        assert (parsed.hour, parsed.minute) == (0, 0)

    def test_a_date_with_no_year_stays_unread(self, caplog):
        """A year is never filled in -- it would be this year, not the message's."""
        assert self._date("Wed, 17 Sep   GMT Daylight Time") is None
        assert "Unreadable Date header" in caplog.text

    def test_an_ordinary_header_never_reaches_any_of_this(self):
        """The plain reading wins, so a repair cannot touch what already parses."""
        parsed = self._date("Wed, 20 Feb 2026 12:00:00 +0100")

        assert parsed is not None
        assert (parsed.month, parsed.day, parsed.hour) == (2, 20, 12)
