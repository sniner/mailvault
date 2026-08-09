"""Reading single fields out of a parsed message, tolerating what they hold.

Every function here answers one question about one header and answers it for
mail that was written by anything, in any decade. What they share is the way
they fail: a field nobody can read costs that field and never the message.
Walking an archive of a hundred thousand messages means meeting every mistake a
mail program has ever made, and stopping at the first one is not an option.
"""

from __future__ import annotations

import collections.abc
import email.message
import email.policy
import email.utils
import logging
import re

log = logging.getLogger(__name__)


def address_field(msg: email.message.EmailMessage, label: str) -> set[str]:
    """Collect the addresses of one header, tolerating what it may contain.

    Read through the policy's own parsing rather than `email.utils.getaddresses`.
    The difference shows on group addresses -- `To: Undisclosed recipients:;` is
    perfectly legal RFC 5322 -- where `getaddresses` reports the group as the
    address pair `('', '')`. That empty string used to reach the archive as a
    recipient: 532 of 130,997 messages in the reference archive carried one, with
    no error anywhere to show for it. The policy reports the group as a group and
    the address list as empty, which is the truth.

    Anything the parser refuses costs this one header, not the message. Five
    messages in the same archive made `getaddresses` raise from inside CPython,
    and the handler that caught it was far enough away to have forgotten it was
    only reading a field.
    """
    try:
        headers = msg.get_all(label, [])
    except Exception as exc:
        log.warning("Unreadable %s header: %s", label, exc)
        return set()

    found: set[str] = set()
    for header in headers:
        try:
            parsed = getattr(header, "addresses", None)
            if parsed is not None:
                found.update(a.addr_spec.lower() for a in parsed if a.addr_spec)
            else:
                # A plain string: no policy parsing to lean on, so fall back.
                found.update(
                    addr.lower()
                    for _name, addr in email.utils.getaddresses([str(header)])
                    if addr
                )
        except Exception as exc:
            log.warning("Unusable address in %s: %s", label, exc)
    return found


def addresses(msg: email.message.EmailMessage) -> tuple[set[str], set[str]]:
    """Extract from/to addresses from message. Returns tuple of sets (from, to)."""

    def received_for() -> collections.abc.Generator[str, None, None]:
        for field in msg.get_all("Received", []):
            m = re.search(
                r"\bfor\s+\<?([\w\-\.]+@[\w\-\.]+\w)\>?\b",
                field,
                flags=re.IGNORECASE,
            )
            if m:
                yield m[1].lower()

    to_addrs = address_field(msg, "To").union(received_for())
    from_addrs = address_field(msg, "From")
    cc_addrs = address_field(msg, "CC")
    return from_addrs, to_addrs.union(cc_addrs)


def header_text(msg: email.message.EmailMessage, name: str) -> str:
    """Return one header as text, or "" when it cannot be read.

    `msg.get` parses the header on access, so a malformed encoded word or a
    broken address raises here rather than where the value is used. One
    unreadable field must not cost the message every other field.
    """
    try:
        value = msg.get(name)
    except Exception as exc:
        log.warning("Unreadable %s header: %s", name, exc)
        return ""
    return str(value) if value else ""


def subject(msg: email.message.EmailMessage) -> str:
    return header_text(msg, "Subject")


def message_id(msg: email.message.EmailMessage) -> str:
    return header_text(msg, "Message-Id")


def normalize_message_id(value: str | None) -> str:
    """Normalise a Message-ID so that values from different sources compare equal.

    Besides folding whitespace, angle brackets and case, the value is run through
    the same header parser that produced the archived Message-ID. That parser
    silently truncates malformed values -- "<a$b@host@domain.example>" becomes
    "<a$b@host>" -- so a value reported verbatim by a server would never match its
    archived counterpart unless both are treated identically.

    Returns an empty string for missing or unusable values, which callers must
    treat as "cannot be matched" rather than as a valid key.
    """
    if not value:
        return ""
    collapsed = " ".join(value.split())
    if not collapsed:
        return ""
    try:
        parsed = str(email.policy.default.header_fetch_parse("Message-Id", collapsed))
    except Exception:
        # CPython's email header parser raises HeaderParseError and, on some patch
        # levels (e.g. 3.11/3.12), IndexError on malformed values such as "<>".
        # Such a value cannot serve as a comparison key -- treat it as unusable.
        return ""
    return parsed.strip().strip("<>").strip().casefold()
