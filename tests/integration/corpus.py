"""The mail these tests put on the server, and where it may come from instead.

Generated rather than copied from an archive, for three reasons in this order.
Real mail cannot go into a public repository at all. A generated corpus is the
same bytes on every run, and a store id *is* the hash of those bytes, so the
tests can assert on ids instead of only counting rows. And it can be made to
hold the awkward cases on purpose -- a subject that is RFC 2047 encoded, a date
with no zone, a message with no Message-ID, the same message in two folders --
which real mail contains only by luck.

Real mail still has a use, and `MAILVAULT_TEST_MAIL` is it: point it at a
directory of `.eml` files and the same scenarios run over those instead. That is
a soak, not a fixture -- it is not reproducible, it is not committed, and nothing
in CI depends on it.
"""

from __future__ import annotations

import os
import pathlib

# A place with a character that does not survive a naive round trip: on the wire
# an IMAP folder name is modified UTF-7, `Pers&APY-nlich` here, and only the
# library's decoding makes it readable again. What the archive records has to be
# the readable one -- the same class of mistake that nearly went into the log
# when the Gmail label read was rewritten.
UMLAUT_FOLDER = "Persönlich"

MAIL_DIR_ENV = "MAILVAULT_TEST_MAIL"


def _message(
    number: int,
    subject: str,
    date: str | None = "Wed, 20 Feb 2026 12:00:00 +0100",
    message_id: str | None = None,
    body: str = "body text",
) -> bytes:
    """One message, spelled out so the bytes never move between runs."""
    headers = [
        f"From: sender{number}@example.com",
        "To: recipient@example.com",
        f"Subject: {subject}",
    ]
    if message_id is not None:
        headers.append(f"Message-ID: {message_id}")
    if date is not None:
        headers.append(f"Date: {date}")
    return ("\r\n".join(headers) + "\r\n\r\n" + body + "\r\n").encode("utf-8")


# One message per awkwardness, and the plain ones to have something ordinary
# beside them. Every one of these has been through the parser in the unit tests;
# what is new here is that it goes over the wire first.
PLAIN = [
    _message(1, "First", message_id="<1@example.com>"),
    _message(2, "Second", message_id="<2@example.com>"),
    _message(3, "Third", message_id="<3@example.com>"),
]

AWKWARD = [
    # RFC 2047 in the subject: what the index has to end up holding is `Grüße`.
    _message(4, "=?utf-8?q?Gr=C3=BC=C3=9Fe?=", message_id="<4@example.com>"),
    # A date with no zone at all. Unknown is the truthful answer, and the archive
    # is not allowed to invent one from the machine it happens to run on.
    _message(5, "No zone", date="Wed, 20 Feb 2026 12:00:00", message_id="<5@example.com>"),
    # No Message-ID. Everything that matches a message against the server has to
    # keep working without one -- `verify` above all.
    _message(6, "No message id", message_id=None),
    # A date the parser refuses outright, which must cost the date and not the
    # message.
    _message(
        7, "Unreadable date", date="Wed, 17 Sep GMT Daylight Time", message_id="<7@example.com>"
    ),
]

# Byte-identical, and deliberately so: put in two folders it is one entry in the
# store and two places in the log, which is the archive's central promise.
IN_TWO_PLACES = _message(8, "Filed twice", message_id="<8@example.com>")


def everything() -> list[bytes]:
    """The whole synthetic corpus, in a fixed order."""
    return [*PLAIN, *AWKWARD, IN_TWO_PLACES]


def from_the_environment() -> list[bytes] | None:
    """Real messages to run the same scenarios over, when a directory was named.

    Read in sorted order so that a soak run is at least repeatable against the
    same directory. Nothing here is committed and nothing in CI reaches it.
    """
    named = os.environ.get(MAIL_DIR_ENV)
    if not named:
        return None
    directory = pathlib.Path(named)
    if not directory.is_dir():
        raise RuntimeError(f"{MAIL_DIR_ENV}={named} is not a directory")
    found = sorted(directory.rglob("*.eml"))
    if not found:
        raise RuntimeError(f"{MAIL_DIR_ENV}={named} holds no .eml files")
    return [path.read_bytes() for path in found]


def messages() -> list[bytes]:
    """What to fill the server with: the corpus, or what the environment named."""
    return from_the_environment() or everything()
