"""Getting the original message out of an Exchange journal envelope.

A journal item is a cover message with the real one attached, and what a backup
must archive is the message, not the envelope. Kept apart from the rest of the
package because none of it is about reading a field: it is one vendor's wrapper
and one vendor's bounce, and the workarounds belong where they can be read as
such.
"""

from __future__ import annotations

import base64
import binascii
import email.message
import logging
import quopri
import re
import typing

from mailvault.mailutils.reading import decode_email_header, mail_reader

log = logging.getLogger(__name__)

_LINE_END = re.compile(rb"\r?\n")
_BLANK_LINE = re.compile(rb"\r?\n\r?\n")


def unwrap_exchange_journal_item(
    msg: typing.BinaryIO | bytes,
    log_ctx: str = "journal item",
) -> bytes | None:
    """The message a journal item carries, byte for byte; None if it carries none.

    `log_ctx` is the `mailbox::folder[id]` prefix the caller's log lines carry.

    The attached message is cut out of the envelope, not parsed and written out
    again. Writing a parsed message back rebuilds long and 8-bit header lines
    through the header parser, which can change what they say: a malformed
    Message-ID loses its end, an 8-bit Subject becomes an encoded word, and a
    signature over the message fails. RFC 2046 allows a message/rfc822 part no
    transfer encoding but 7bit, 8bit and binary, so its body is the attached
    message as it was sent.
    """
    attached = _attached_messages(mail_reader(msg).read())
    if not attached:
        return None
    message = attached[0]

    # WORKAROUND: Microsoft Exchange sends a journal report from the original
    # sender's address. Where that domain publishes SPF or DKIM, the receiving
    # server rejects the report, and Exchange returns it in an "Undeliverable:"
    # bounce: a multipart/report whose message/rfc822 part is the journal
    # report itself, with the mail inside it. The report is unwrapped once
    # more. It is told from a mail by the header Exchange marks it with; it
    # also carries no Received header, so its bytes start with Content-Type,
    # which is what an earlier version of this check looked for.
    if _is_journal_report(message):
        inner = _attached_messages(message)
        message = inner[0] if inner else b""
        if message:
            log.warning("%s: journal message taken from an 'Undeliverable' bounce", log_ctx)
    # An empty part is not a message.
    return message or None


def _is_journal_report(entity: bytes) -> bool:
    headers, _ = _headers(entity)
    return "X-MS-Journal-Report" in headers


def _headers(entity: bytes) -> tuple[email.message.EmailMessage, bytes]:
    """The header block of `entity`, parsed, and its body as it stands."""
    head, body = _split(entity)
    return decode_email_header(head + b"\r\n\r\n"), body


def _attached_messages(entity: bytes, default_type: str = "text/plain") -> list[bytes]:
    """Every message/rfc822 body in `entity`, in the order `Message.walk()` finds them.

    An attached message is searched like the envelope, so one attached to it
    comes right after it.
    """
    headers, body = _headers(entity)
    headers.set_default_type(default_type)
    found: list[bytes] = []
    if headers.get_content_type() == "message/rfc822":
        message = _decoded(body, headers.get("Content-Transfer-Encoding"))
        found.append(message)
        found.extend(_attached_messages(message))
    elif headers.get_content_maintype() == "multipart":
        boundary = _boundary(headers)
        if boundary:
            # In a digest a part without a Content-Type is a message.
            child_type = (
                "message/rfc822" if headers.get_content_subtype() == "digest" else "text/plain"
            )
            for part in _parts(body, boundary):
                found.extend(_attached_messages(part, child_type))
    return found


def _boundary(headers: email.message.EmailMessage) -> bytes | None:
    """The boundary as it is written in the body; None if there is none.

    Raw 8-bit in the header arrives as surrogates and encodes back to its bytes.
    A boundary declared in a charset (RFC 2231) comes back as text, which no
    delimiter in the body can match: RFC 2046 allows a boundary ASCII only.
    """
    boundary = headers.get_boundary()
    if not boundary:
        return None
    try:
        return boundary.encode("ascii", "surrogateescape")
    except UnicodeEncodeError:
        return None


def _split(entity: bytes) -> tuple[bytes, bytes]:
    """The header block and the body, either side of the first empty line."""
    starts_empty = _LINE_END.match(entity)
    if starts_empty:
        return b"", entity[starts_empty.end() :]
    blank = _BLANK_LINE.search(entity)
    if blank is None:
        return entity, b""
    return entity[: blank.start()], entity[blank.end() :]


def _parts(body: bytes, boundary: bytes) -> list[bytes]:
    """The body parts of a multipart body, as they stand between its delimiters.

    The line end before a delimiter belongs to the delimiter (RFC 2046, section
    5.1.1), not to the part. Without a close delimiter the last part runs to the
    end, as the standard library's parser takes it.
    """
    delimiter = re.compile(
        rb"(?:\A|\r?\n)--" + re.escape(boundary) + rb"(--)?[ \t]*(?=\r?\n|\Z)"
    )
    parts: list[bytes] = []
    start: int | None = None
    for found in delimiter.finditer(body):
        if start is not None:
            parts.append(body[start : found.start()])
        if found.group(1):
            return parts
        line_end = _LINE_END.match(body, found.end())
        start = line_end.end() if line_end else found.end()
    if start is not None:
        parts.append(body[start:])
    return parts


def _decoded(body: bytes, encoding: str | None) -> bytes:
    """The attached message as sent, should it come with a transfer encoding after all."""
    cte = (encoding or "").strip().lower()
    try:
        if cte == "base64":
            return base64.b64decode(body)
        if cte == "quoted-printable":
            return quopri.decodestring(body)
    except binascii.Error as exc:
        log.warning("attached message declared %s and not decodable: %s", cte, exc)
    return body
