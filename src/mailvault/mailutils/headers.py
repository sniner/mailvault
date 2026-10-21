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
                specs = [a.addr_spec for a in parsed]
            else:
                # A plain string: no policy parsing to lean on, so fall back.
                specs = [addr for _name, addr in email.utils.getaddresses([str(header)])]
            found.update(decode_raw_bytes(spec).lower() for spec in specs if spec)
        except Exception as exc:
            log.warning("Unusable address in %s: %s", label, exc)
    return found


def decode_raw_bytes(text: str) -> str:
    """Decode the raw 8-bit bytes the parser left in a header value.

    `BytesParser` reads a header as ASCII and keeps every other byte as a lone
    surrogate (U+DC80 to U+DCFF). A string holding one cannot be encoded as
    UTF-8, so SQLite rejects it. Bytes that form UTF-8, as RFC 6532 allows in an
    address, become the characters they encode; any other byte becomes U+FFFD.
    The policy does the same for the text of a header like Subject, but leaves
    `addr_spec` as it was parsed.
    """
    return text.encode("utf-8", "surrogateescape").decode("utf-8", "replace")


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
    """The Message-ID as written in the message, unfolded; "" when there is none.

    Only for a message parsed with `reading.POLICY`. The standard policy cuts a
    malformed value short.
    """
    return header_text(msg, "Message-Id")


def normalize_message_id(value: str | None) -> str:
    """The key a Message-ID is compared by, the same for every source.

    What stands between the first "<" and the next ">", or the whole value when
    there is no "<", without whitespace and in lower case. A comment after the
    ">" and a second id in the same header drop out; a value cut off before its
    ">" keeps everything after the "<". It is the rule of `key()` in
    ossuary-mailvault, so both tools compare the same way.

    No header parser is involved. The one in the standard library truncates a
    malformed value without an error, and different messages would end up under
    one key: `<Share-0f1e,rcid_abc@odspnotify>` becomes `<Share-0f1e`.

    Returns "" for a missing or empty value, which callers must treat as
    "cannot be matched" rather than as a key.
    """
    if not value:
        return ""
    start = value.find("<")
    if start < 0:
        inner = value
    else:
        rest = value[start + 1 :]
        end = rest.find(">")
        inner = rest if end < 0 else rest[:end]
    return "".join(inner.split()).lower()
