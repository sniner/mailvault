"""Turning the bytes of a message into something with headers on it.

The way in. Everything else in this package takes the `EmailMessage` these
produce; nothing else in mailvault parses a message itself.
"""

from __future__ import annotations

import email.message
import email.parser
import email.policy
import io
import typing


def mail_reader(msg: typing.BinaryIO | bytes) -> typing.BinaryIO:
    """A stream over the message, whichever of the two forms it arrives in.

    A stream that has already been read is rewound where it can be: the callers
    hand the same open file to more than one of these, and a parser that starts
    wherever the last one stopped sees a message with no headers.

    `BinaryIO` rather than `io.IOBase`, which is what stood here and is not what
    a parser can be handed: `IOBase` promises `seek` and not `read`. Every caller
    in the program passes bytes; the other half of the union is for the callers
    that do not exist yet, and it now says what they have to bring.
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
    parsed = email.parser.BytesParser(policy=email.policy.default).parse(
        mail_reader(msg),
        headersonly=headersonly,
    )
    # `BytesParser` is annotated as returning `Message`, and returns whatever the
    # policy makes -- `email.policy.default` makes an `EmailMessage`, which is
    # the whole reason it is passed. Narrowed once, here, so nothing downstream
    # has to know that.
    return typing.cast(email.message.EmailMessage, parsed)


def decode_email_header(msg: typing.BinaryIO | bytes) -> email.message.EmailMessage:
    return decode_email(msg, headersonly=True)
