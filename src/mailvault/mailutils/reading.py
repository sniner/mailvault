"""Turning the bytes of a message into something with headers on it.

The way in. Everything else in this package takes the `EmailMessage` these
produce; nothing else in mailvault parses a message itself.
"""

from __future__ import annotations

import email.headerregistry
import email.message
import email.parser
import email.policy
import io
import typing


class MessageIDHeader(email.headerregistry.UnstructuredHeader, email.headerregistry.BaseHeader):
    """The Message-ID as written, unfolded, with nothing parsed out of it.

    The standard library's own header class parses the value by the msg-id
    grammar of RFC 5322 and keeps only the part it understood, without an error:
    `<Share-0f1e,rcid_abc@odspnotify>` becomes `<Share-0f1e`. Archive and server
    are compared by Message-ID, and the servers report the value as written, so
    the archive side has to read it as written too.

    Not `UnstructuredHeader` as it is: that decodes encoded words, which a
    Message-ID cannot hold and no server decodes. Raw 8-bit bytes come out as
    U+FFFD, one per byte, the same as the IMAP backend makes of them.
    """

    @classmethod
    def parse(cls, value: str, kwds: dict[str, object]) -> None:
        super().parse(value, kwds)
        kwds["decoded"] = " ".join(value.split())


_HEADERS = email.headerregistry.HeaderRegistry()
_HEADERS.map_to_type("message-id", MessageIDHeader)

POLICY = email.policy.default.clone(header_factory=_HEADERS)
"""`email.policy.default`, with the Message-ID read by `MessageIDHeader`."""


def mail_reader(msg: typing.BinaryIO | bytes) -> typing.BinaryIO:
    """A stream over the message, whichever of the two forms it arrives in.

    A stream that has already been read is rewound where it can be: the callers
    hand the same open file to more than one of these, and a parser that starts
    wherever the last one stopped sees a message with no headers.
    """
    if isinstance(msg, bytes):
        return io.BytesIO(msg)
    if msg.seekable():
        msg.seek(0)
    return msg


def decode_email(
    msg: typing.BinaryIO | bytes,
    headersonly: bool = False,
) -> email.message.EmailMessage:
    parsed = email.parser.BytesParser(policy=POLICY).parse(
        mail_reader(msg),
        headersonly=headersonly,
    )
    # `BytesParser` is annotated as returning `Message`, and returns whatever the
    # policy makes -- a clone of `email.policy.default` makes an `EmailMessage`,
    # which is the whole reason it is passed. Narrowed once, here, so nothing
    # downstream has to know that.
    return typing.cast(email.message.EmailMessage, parsed)


def decode_email_header(msg: typing.BinaryIO | bytes) -> email.message.EmailMessage:
    return decode_email(msg, headersonly=True)
