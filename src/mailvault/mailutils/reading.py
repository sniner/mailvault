"""Turning the bytes of a message into something with headers on it.

The way in. Everything else in this package takes the `EmailMessage` these
produce; nothing else in mailvault parses a message itself.
"""

from __future__ import annotations

import email.message
import email.parser
import email.policy
import io


def mail_reader(msg: io.IOBase | bytes) -> io.IOBase:
    """A stream over the message, whichever of the two forms it arrives in.

    A stream that has already been read is rewound where it can be: the callers
    hand the same open file to more than one of these, and a parser that starts
    wherever the last one stopped sees a message with no headers.
    """
    if isinstance(msg, io.IOBase):
        reader = msg
        if reader.seekable():
            reader.seek(0)
    else:
        reader = io.BytesIO(msg)
    return reader


def decode_email(
    msg: io.IOBase | bytes,
    headersonly: bool = False,
) -> email.message.EmailMessage:
    reader = mail_reader(msg)
    return email.parser.BytesParser(policy=email.policy.default).parse(
        reader,  # type: ignore
        headersonly=headersonly,
    )


def decode_email_header(msg: io.IOBase | bytes) -> email.message.EmailMessage:
    return decode_email(msg, headersonly=True)
