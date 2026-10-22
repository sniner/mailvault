import base64
import email
import email.message
import email.policy
import logging
import random

import pytest

from mailvault import mailutils
from mailvault.mailutils import journal


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
    message = (
        b"From: real-sender@example.com\r\n"
        b"Subject: " + "Ölwechsel".encode() + b"\r\n"
        b'Content-Type: text/plain; charset="utf-8"\r\n'
        b"Content-Transfer-Encoding: 8bit\r\n"
        b"\r\n" + "Grüße aus München -- ölig, süß".encode()
    )
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
        b"\r\n" + message + b"\r\n--BOUNDARY--\r\n"
    )
    assert mailutils.unwrap_exchange_journal_item(journal) == message


def test_unwrap_exchange_journal_not_a_journal():
    plain = b"""From: a@b.com
To: c@d.com
Subject: Plain email

Just a plain email, no RFC822 attachments.
"""
    result = mailutils.unwrap_exchange_journal_item(plain)
    assert result is None


def _journal_report(message: bytes, nl: bytes = b"\r\n") -> bytes:
    """A journal report as Microsoft 365 sends it: the report, then the message."""
    lines = [
        b"From: Microsoft Outlook <journal-sender@example.com>",
        b"To: journal@example.org",
        b"Subject: Quarterly report",
        b"X-MS-Journal-Report:",
        b"MIME-Version: 1.0",
        b'Content-Type: multipart/mixed; boundary="_journal_"',
        b"",
        b"--_journal_",
        b'Content-Type: text/plain; charset="us-ascii"',
        b"Content-Transfer-Encoding: 7bit",
        b"",
        b"Sender: sender@example.com",
        b"Subject: Quarterly report",
        b"Message-Id: <abc@example.com>",
        b"To: recipient@example.com",
        b"--_journal_",
        b"Content-Type: message/rfc822",
        b"",
    ]
    return nl.join(lines) + nl + message + nl + b"--_journal_--" + nl


_SIGNED = (
    b"Message-ID: <signed@example.com>\r\n"
    b"MIME-Version: 1.0\r\n"
    b'Content-Type: multipart/signed; protocol="application/pkcs7-signature";'
    b' micalg=sha-256; boundary="S"\r\n'
    b"\r\n"
    b"--S\r\n"
    b"Content-Type: text/plain\r\n"
    b"Content-Description: s\xe4\r\n"
    b"\r\n"
    b"signed text\r\n"
    b"--S\r\n"
    b"Content-Type: application/pkcs7-signature\r\n"
    b"\r\n"
    b"AAAA\r\n"
    b"--S--\r\n"
)

_FORWARDED = (
    b"Message-ID: <outer@example.com>\r\n"
    b'Content-Type: multipart/mixed; boundary="F"\r\n'
    b"\r\n"
    b"--F\r\n"
    b"Content-Type: text/plain\r\n"
    b"\r\n"
    b"see below\r\n"
    b"--F\r\n"
    b"Content-Type: message/rfc822\r\n"
    b"\r\n"
    b"Message-ID: <inner@example.com>\r\n"
    b"\r\n"
    b"forwarded\r\n"
    b"--F--\r\n"
)

_JOURNALLED = {
    "onedrive id": (
        b"Message-ID: <Share-0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b,"
        b"rcid_0123456789abcdef0123@odspnotify>\r\nSubject: s\r\n\r\nbody\r\n"
    ),
    "8-bit subject": b"Message-ID: <a@example.com>\r\nSubject: s\xe4\r\n\r\nbody\r\n",
    # Exchange adds headers like this to every message; written back, each
    # became an encoded word.
    "long exchange header": (
        b"X-MS-Exchange-Organization-MessageLatency: SRV=" + b"A" * 140 + b";1\r\n"
        b"Subject: s\r\n\r\nbody\r\n"
    ),
    "long address line": (
        b"To: Foo <foo@example.com>, " + b"x" * 60 + b"@example.com\r\n\r\nbody\r\n"
    ),
    "malformed address": b"From: Foo [Bar] <foo@example.com>\r\n\r\nbody\r\n",
    "odd folding": b"Subject: a\r\n\t  b\r\n  c\r\nX-Empty:\r\n\r\nbody",
    "signed, 8-bit in the signed part": _SIGNED,
    "forwards a message of its own": _FORWARDED,
}


@pytest.mark.parametrize("message", _JOURNALLED.values(), ids=_JOURNALLED.keys())
def test_a_journal_item_gives_the_message_byte_for_byte(message):
    """What the archive keeps is the message as it was received, not a rewrite of it."""
    assert mailutils.unwrap_exchange_journal_item(_journal_report(message)) == message


def test_a_journal_item_with_lf_line_ends_gives_the_message_unchanged():
    message = b"Message-ID: <a,b@example.com>\nSubject: s\xe4\n\nbody\n"
    report = _journal_report(message, nl=b"\n")

    assert mailutils.unwrap_exchange_journal_item(report) == message


def test_the_standard_library_rewrites_a_message_it_writes_back():
    """Before and after: why the attached message is cut out rather than written out.

    The first assertion pins the behaviour of the standard library, not of
    mailvault. If it fails, `as_bytes()` has started to write a parsed message
    back unchanged, which does not make it the right tool here.
    """
    message = _JOURNALLED["onedrive id"]
    report = _journal_report(message)
    parsed = email.message_from_bytes(report, policy=email.policy.default)
    attached = next(p for p in parsed.walk() if p.get_content_type() == "message/rfc822")
    inner = attached.get_payload(0)
    assert isinstance(inner, email.message.Message)
    written_back = inner.as_bytes(policy=email.policy.SMTP)

    assert not written_back.startswith(message.split(b"\r\n")[0] + b"\r\n")
    assert mailutils.unwrap_exchange_journal_item(report) == message


def _undeliverable(report: bytes) -> bytes:
    """A bounce as Exchange Online returns a journal report it could not deliver.

    A delivery report with three parts: the text for people, the delivery
    status, and the journal report as it was sent, mail and all.
    """
    return (
        b"Received: from mail.example.org\r\n"
        b"Subject: Undeliverable: Quarterly report\r\n"
        b"Auto-Submitted: auto-replied\r\n"
        b'Content-Type: multipart/report; report-type="delivery-status"; boundary="R"\r\n'
        b"\r\n"
        b"--R\r\n"
        b'Content-Type: multipart/alternative; boundary="A"\r\n'
        b"\r\n"
        b"--A\r\n"
        b"Content-Type: text/plain\r\n"
        b"\r\n"
        b"Your message could not be delivered.\r\n"
        b"--A\r\n"
        b"Content-Type: text/html\r\n"
        b"\r\n"
        b"<p>Your message could not be delivered.</p>\r\n"
        b"--A--\r\n"
        b"--R\r\n"
        b"Content-Type: message/delivery-status\r\n"
        b"\r\n"
        b"Reporting-MTA: dns; mail.example.org\r\n"
        b"\r\n"
        b"Final-Recipient: rfc822; journal@example.org\r\n"
        b"Action: failed\r\n"
        b"--R\r\n"
        b"Content-Type: message/rfc822\r\n"
        b"\r\n" + report + b"\r\n"
        b"--R--\r\n"
    )


def test_an_undeliverable_bounce_gives_the_mail_inside_the_journal_report():
    """The bounce returns the whole journal report; the mail is one level further down."""
    message = b"Received: from a.example.com\r\nMessage-ID: <a@example.com>\r\n\r\nbody\r\n"

    assert (
        mailutils.unwrap_exchange_journal_item(_undeliverable(_journal_report(message)))
        == message
    )


def test_a_bounced_journal_report_without_a_mail_is_no_journal_item():
    report = _journal_report(b"").replace(b"\r\n\r\n--_journal_--", b"\r\n--_journal_--")

    assert mailutils.unwrap_exchange_journal_item(_undeliverable(report)) is None


def test_a_journalled_mail_that_forwards_a_mail_is_not_taken_for_a_bounce():
    """Only a journal report is unwrapped twice, whatever a mail's first header is."""
    message = (
        b'Content-Type: multipart/mixed; boundary="F"\r\n'
        b"Message-ID: <outer@example.com>\r\n"
        b"\r\n"
        b"--F\r\n"
        b"Content-Type: message/rfc822\r\n"
        b"\r\n"
        b"Message-ID: <inner@example.com>\r\n\r\nforwarded\r\n"
        b"--F--\r\n"
    )

    assert mailutils.unwrap_exchange_journal_item(_journal_report(message)) == message


def test_an_attached_message_sent_in_base64_is_decoded():
    """Not allowed by RFC 2046, and still the message as it was sent once decoded."""
    message = b"Message-ID: <a@example.com>\r\nSubject: s\xe4\r\n\r\nbody\r\n"
    report = (
        b'Content-Type: multipart/mixed; boundary="B"\r\n'
        b"\r\n"
        b"--B\r\n"
        b"Content-Type: message/rfc822\r\n"
        b"Content-Transfer-Encoding: base64\r\n"
        b"\r\n" + base64.encodebytes(message) + b"--B--\r\n"
    )

    assert mailutils.unwrap_exchange_journal_item(report) == message


def test_a_journal_item_without_its_close_delimiter_still_gives_the_message():
    message = b"Message-ID: <a@example.com>\r\n\r\nbody\r\n"
    report = _journal_report(message).removesuffix(b"\r\n--_journal_--\r\n")

    assert mailutils.unwrap_exchange_journal_item(report) == message


def test_a_line_that_begins_like_the_boundary_is_no_delimiter():
    """Only the boundary and, at most, whitespace may stand on a delimiter line."""
    message = (
        b"Message-ID: <a@example.com>\r\n\r\n"
        b"--_journal_x\r\n"
        b"--_journal_ not a delimiter\r\n"
        b"--_journal_--x\r\n"
        b"end\r\n"
    )

    assert mailutils.unwrap_exchange_journal_item(_journal_report(message)) == message


def test_a_preamble_and_an_epilogue_are_not_parts():
    message = b"Message-ID: <a@example.com>\r\n\r\nbody\r\n"
    report = (
        b'Content-Type: multipart/mixed; boundary="B"\r\n'
        b"\r\n"
        b"This is a multi-part message in MIME format.\r\n"
        b"--B\r\n"
        b"Content-Type: message/rfc822\r\n"
        b"\r\n" + message + b"\r\n"
        b"--B--\r\n"
        b"epilogue\r\n"
    )

    assert mailutils.unwrap_exchange_journal_item(report) == message


def test_a_delimiter_may_end_in_whitespace():
    message = b"Message-ID: <a@example.com>\r\n\r\nbody\r\n"
    report = (
        b'Content-Type: multipart/mixed; boundary="B"\r\n'
        b"\r\n"
        b"--B \t\r\n"
        b"Content-Type: message/rfc822\r\n"
        b"\r\n" + message + b"\r\n"
        b"--B-- \r\n"
    )

    assert mailutils.unwrap_exchange_journal_item(report) == message


def test_a_boundary_with_regex_metacharacters_is_taken_literally():
    message = b"Message-ID: <a@example.com>\r\n\r\nbody\r\n"
    report = (
        b'Content-Type: multipart/mixed; boundary="=_a.b+c(d)?"\r\n'
        b"\r\n"
        b"--=_a.b+c(d)?\r\n"
        b"Content-Type: message/rfc822\r\n"
        b"\r\n" + message + b"\r\n"
        b"--=_a.b+c(d)?--\r\n"
    )

    assert mailutils.unwrap_exchange_journal_item(report) == message


def test_a_boundary_declared_in_a_charset_marks_no_part():
    """RFC 2046 allows a boundary ASCII only; one that is not is no journal item."""
    report = (
        b"Content-Type: multipart/mixed; boundary*=utf-8''%C3%BC\r\n"
        b"\r\n"
        b"--\xc3\xbc\r\n"
        b"Content-Type: message/rfc822\r\n"
        b"\r\n"
        b"Message-ID: <a@example.com>\r\n\r\nbody\r\n"
        b"--\xc3\xbc--\r\n"
    )

    assert mailutils.unwrap_exchange_journal_item(report) is None


def test_a_message_whose_first_header_is_content_type_is_the_message():
    """A lone attachment is never the delivery status of a bounce, whatever it starts with."""
    message = b"Content-Type: text/plain\r\nMessage-ID: <a@example.com>\r\n\r\nbody\r\n"

    assert mailutils.unwrap_exchange_journal_item(_journal_report(message)) == message


def test_an_empty_attached_message_is_no_journal_item():
    assert mailutils.unwrap_exchange_journal_item(_journal_report(b"")) is None


def test_the_bounce_warning_names_the_message(caplog):
    message = b"Message-ID: <a@example.com>\r\n\r\nbody\r\n"
    bounce = _undeliverable(_journal_report(message))

    with caplog.at_level(logging.WARNING, logger="mailvault.mailutils.journal"):
        result = mailutils.unwrap_exchange_journal_item(bounce, log_ctx="job::INBOX[7]")

    assert result == message
    assert caplog.messages == [
        "job::INBOX[7]: journal message taken from an 'Undeliverable' bounce"
    ]


_BCHARS = "0123456789abcdefghijklmnopqrstuvwxyzABCDEF'()+_,-./:=?"


def _random_mime_tree(rnd: random.Random, depth: int = 0) -> email.message.EmailMessage:
    """A message with random nesting: leaves, multipart/mixed, multipart/digest, message/rfc822.

    Leaf bodies hold lines that look like delimiters, and every multipart gets a
    boundary of its own. Every message carries a Message-ID, which is how the
    parts found are told apart.
    """
    kind = rnd.choice(["leaf", "leaf", "mixed", "digest", "rfc822"]) if depth < 3 else "leaf"
    msg = email.message.EmailMessage()
    msg["Message-ID"] = f"<{kind}-{rnd.randrange(10**9)}@example.com>"
    if kind == "leaf":
        lines = ["body\n", "--\n", "--B\n", "\n", "--_x_--\n", "line " * 20 + "\n"]
        body = "".join(rnd.choice(lines) for _ in range(rnd.randint(0, 5)))
        msg.set_content(body, cte=rnd.choice(["7bit", "8bit", "base64", "quoted-printable"]))
        return msg
    msg.make_mixed()
    msg.set_boundary("".join(rnd.choice(_BCHARS) for _ in range(rnd.randint(8, 30))))
    if kind == "rfc822":
        msg.add_attachment(_random_mime_tree(rnd, depth + 1))
        return msg
    if kind == "digest":
        msg.replace_header("Content-Type", f'multipart/digest; boundary="{msg.get_boundary()}"')
    for _ in range(rnd.randint(1, 3)):
        child = _random_mime_tree(rnd, depth + 1)
        if kind == "digest":
            # A digest part without a Content-Type, as RFC 2046 allows.
            part = email.message.EmailMessage()
            part.set_default_type("message/rfc822")
            part.set_payload([child])
            msg.attach(part)
        else:
            msg.attach(child)
    return msg


def _message_ids_of_attached(raw: bytes) -> list[str]:
    return [
        email.message_from_bytes(found, policy=email.policy.default)["Message-ID"]
        for found in journal._attached_messages(raw)
    ]


def _message_ids_walked(raw: bytes) -> list[str]:
    parsed = email.message_from_bytes(raw, policy=email.policy.default)
    ids: list[str] = []
    for part in parsed.walk():
        if part.get_content_type() == "message/rfc822":
            inner = part.get_payload(0)
            assert isinstance(inner, email.message.Message)
            ids.append(inner["Message-ID"])
    return ids


@pytest.mark.parametrize("seed", range(5))
def test_the_parts_found_are_the_ones_the_standard_library_parser_finds(seed):
    """Random MIME trees, written out by the standard library and cut up here.

    The content of a part is compared elsewhere, byte for byte; this test pins
    the structure: which message/rfc822 parts are found, and in what order.
    """
    rnd = random.Random(seed)
    for _ in range(40):
        tree = _random_mime_tree(rnd)
        for policy in (email.policy.SMTP, email.policy.default):
            raw = tree.as_bytes(policy=policy)
            if rnd.random() < 0.2:
                raw = raw.replace(b"\r\n", b"\n")
            assert _message_ids_of_attached(raw) == _message_ids_walked(raw)


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
        ("<abc\r\n @example.com>", "abc@example.com"),
        ("", ""),
        (None, ""),
        ("<>", ""),
        # The cases of the key() test in ossuary-mailvault.
        ("<Abc@Example.ORG>", "abc@example.org"),
        ("  <abc@\r\n example.org> (comment)", "abc@example.org"),
        ("abc@example.org", "abc@example.org"),
        ("<abc@example.org", "abc@example.org"),
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


@pytest.mark.parametrize(
    "written,key",
    [
        (b"<abc.def@example.com>", "abc.def@example.com"),
        (b"<a!#$%&'*+-/=?^_`{|}~b@example.com>", "a!#$%&'*+-/=?^_`{|}~b@example.com"),
        (b"<abc@[192.0.2.1]>", "abc@[192.0.2.1]"),
        (b"<" + b"x" * 200 + b"@mail.example.com>", "x" * 200 + "@mail.example.com"),
        (b"   <abc@example.com>", "abc@example.com"),
        (b"\r\n <abc@example.com>", "abc@example.com"),
        (b"<abc\r\n @example.com>", "abc@example.com"),
        (b"<abc@example.com> (comment)", "abc@example.com"),
        (b'<"a b"@example.com>', '"ab"@example.com'),
        # Not valid by RFC 5322. The standard library's parser keeps only what
        # comes before the first character it does not expect.
        (b"<Share-0f1e,rcid_abc@odspnotify>", "share-0f1e,rcid_abc@odspnotify"),
        (b"<a$b@host@domain.example>", "a$b@host@domain.example"),
        (b"<abc def@example.com>", "abcdef@example.com"),
        (b"<a@example.com> <b@example.com>", "a@example.com"),
        (b"abc@example.com", "abc@example.com"),
        (b"<=?utf-8?q?x?=@example.com>", "=?utf-8?q?x?=@example.com"),
        (b"<first\xe4,x@example.com>", "first�,x@example.com"),
    ],
)
def test_the_archive_and_the_server_give_the_same_key(written, key):
    """Nothing of a Message-ID is lost, however malformed, on any Python version.

    The archived value is read from a parsed message, the server value is the
    header as written, the way IMAP's ENVELOPE and Graph's internetMessageId
    report it. The IMAP backend decodes it as ASCII with replacement.
    """
    eml = b"Message-ID: " + written + b"\r\nSubject: x\r\n\r\nbody\r\n"
    archived = mailutils.message_id(mailutils.decode_email_header(eml))
    server = written.decode("ascii", "replace")

    assert archived == " ".join(server.split())
    assert mailutils.normalize_message_id(archived) == key
    assert mailutils.normalize_message_id(server) == key


@pytest.mark.parametrize(
    "written,cut_short",
    [
        (b"<Share-0f1e,rcid_abc@odspnotify>", "<Share-0f1e"),
        (b"<a$b@host@domain.example>", "<a$b@host>"),
        (b"<abc def@example.com>", "<abc"),
        (b"<a@example.com> <b@example.com>", "<a@example.com>"),
    ],
)
def test_the_standard_policy_still_cuts_a_malformed_message_id_short(written, cut_short):
    """Before and after: the standard policy against the one mailvault reads with.

    The first assertion pins a defect of the standard library, not of mailvault.
    If it fails, a Python version has changed what its parser makes of a
    malformed Message-ID. mailvault does not depend on that, but it is the
    reason `reading.MessageIDHeader` exists, which may then no longer be needed.
    """
    eml = b"Message-ID: " + written + b"\r\nSubject: x\r\n\r\nbody\r\n"
    standard = email.message_from_bytes(eml, policy=email.policy.default)

    assert str(standard["Message-ID"]).strip() == cut_short
    assert mailutils.message_id(mailutils.decode_email_header(eml)) == written.decode()


def test_two_onedrive_notifications_keep_two_keys():
    """Issue #16: the part before the comma repeats across notifications."""

    def archived(written: bytes) -> str:
        eml = b"Message-ID: " + written + b"\r\nSubject: x\r\n\r\nbody\r\n"
        return mailutils.message_id(mailutils.decode_email_header(eml))

    first = archived(b"<Share-0f1e,rcid_aaa@odspnotify>")
    second = archived(b"<Share-0f1e,rcid_bbb@odspnotify>")

    assert mailutils.normalize_message_id(first) != mailutils.normalize_message_id(second)


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

    with caplog.at_level(logging.DEBUG):
        assert mailutils.date(header) is None

    # Still reported, one level down: the value is worth having when a message
    # is looked into, and worthless as a count -- `db create` asks the finished
    # database how many came out with no date instead.
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


def test_raw_8bit_addresses_come_out_as_encodable_text():
    """No lone surrogate may reach a caller: SQLite cannot encode one."""
    raw = (
        b"From: j\xc3\xbcrgen@example.com\r\n"  # UTF-8, as RFC 6532 allows
        b"To: M\xfcller <m\xfcller@example.com>, plain@example.com\r\n"  # Latin-1
        b"\r\n"
    )
    header = mailutils.decode_email_header(raw)

    from_addrs, to_addrs = mailutils.addresses(header)

    assert from_addrs == {"jürgen@example.com"}
    assert to_addrs == {"m�ller@example.com", "plain@example.com"}


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
        with caplog.at_level(logging.DEBUG):
            assert self._date("05.03.2002 12:03:27") is None

        assert "Unreadable Date header" in caplog.text

    def test_a_date_with_no_time_gets_midnight(self):
        parsed = self._date("Mon, 11 Mar 2002 PST")

        assert parsed is not None
        assert (parsed.year, parsed.month, parsed.day) == (2002, 3, 11)
        assert (parsed.hour, parsed.minute) == (0, 0)

    def test_a_date_with_no_year_stays_unread(self, caplog):
        """A year is never filled in -- it would be this year, not the message's."""
        with caplog.at_level(logging.DEBUG):
            assert self._date("Wed, 17 Sep   GMT Daylight Time") is None

        assert "Unreadable Date header" in caplog.text

    def test_an_ordinary_header_never_reaches_any_of_this(self):
        """The plain reading wins, so a repair cannot touch what already parses."""
        parsed = self._date("Wed, 20 Feb 2026 12:00:00 +0100")

        assert parsed is not None
        assert (parsed.month, parsed.day, parsed.hour) == (2, 20, 12)
