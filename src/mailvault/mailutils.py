from __future__ import annotations

import bisect
import collections.abc
import email.message
import email.parser
import email.policy
import email.utils
import io
import logging
import re
from datetime import datetime

from mailvault import fixedpolicy


def _mail_reader(msg: io.IOBase | bytes) -> io.IOBase:
    if isinstance(msg, io.IOBase):
        reader = msg
        if reader.seekable():
            reader.seek(0)
    else:
        reader = io.BytesIO(msg)
    return reader


def decode_email(
    msg: io.IOBase | bytes, headersonly: bool = False
) -> email.message.EmailMessage:
    reader = _mail_reader(msg)
    return email.parser.BytesParser(policy=email.policy.default).parse(
        reader,  # type: ignore
        headersonly=headersonly,
    )


def decode_email_header(msg: io.IOBase | bytes) -> email.message.EmailMessage:
    return decode_email(msg, headersonly=True)


def addresses(msg: email.message.EmailMessage) -> tuple[set[str], set[str]]:
    """Extract from/to addresses from message. Returns tuple of lists (from, to)."""

    def received_for() -> collections.abc.Generator[str, None, None]:
        for field in msg.get_all("Received", []):
            m = re.search(
                r"\bfor\s+\<?([\w\-\.]+@[\w\-\.]+\w)\>?\b", field, flags=re.IGNORECASE
            )
            if m:
                yield m[1].lower()

    def addr_field(label: str) -> set[str]:
        try:
            addrs = email.utils.getaddresses(msg.get_all(label, []))
        except Exception as exc:
            logging.warning("Failed to parse addresses for %s: %s", label, exc)
            addrs = []
        return {a[1].lower() for a in addrs}

    to_addrs = addr_field("To").union(received_for())
    from_addrs = addr_field("From")
    cc_addrs = addr_field("CC")
    return from_addrs, to_addrs.union(cc_addrs)


def date(msg: email.message.EmailMessage) -> datetime | None:
    date = msg.get("Date")
    if date:
        date = email.utils.parsedate_to_datetime(date)
    return date


def message_id(msg: email.message.EmailMessage) -> str:
    return msg.get("Message-Id") or ""


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


class MessageIdIndex:
    """Lookup of archived Message-IDs that tolerates server-side truncation.

    Exchange caps the Message-ID it reports in folder listings at around 255
    characters, so a long value can reach us as a strict prefix of the one stored
    in the archive. Exact hits are answered from a set; only values long enough to
    have been truncated fall back to a prefix search, and only over those archived
    IDs that are long enough to be affected -- normally a mere handful.

    All values are expected to be normalised with normalize_message_id().
    """

    # Well below the observed 255 character cap: only keeps the prefix search
    # small, so its exact value is not critical.
    TRUNCATION_THRESHOLD = 128

    def __init__(self, message_ids: collections.abc.Iterable[str]):
        self._exact = {mid for mid in message_ids if mid}
        self._long = sorted(mid for mid in self._exact if len(mid) > self.TRUNCATION_THRESHOLD)

    def __contains__(self, value: str) -> bool:
        if not value:
            return False
        if value in self._exact:
            return True
        if len(value) <= self.TRUNCATION_THRESHOLD:
            return False
        # In a sorted list, if any entry starts with `value`, the first entry
        # that is >= `value` is one of them.
        pos = bisect.bisect_left(self._long, value)
        return pos < len(self._long) and self._long[pos].startswith(value)

    def __len__(self) -> int:
        return len(self._exact)


def subject(msg: email.message.EmailMessage) -> str:
    return msg.get("Subject") or ""


def metadata(
    msg: bytes,
    mailbox: str,
    folder: str,
    store_id: str,
    labels: list[str] | None = None,
) -> dict:
    """Extract the metadata record that the storage database is fed with."""
    header = decode_email_header(msg)
    from_addrs, to_addrs = addresses(header)
    return {
        "mailbox": mailbox,
        "folder": folder,
        "email_id": message_id(header),
        "store_id": store_id,
        "labels": labels if labels is not None else [folder],
        "sender": from_addrs,
        "recipients": to_addrs,
        "date": date(header),
        "subject": subject(header),
    }


def unwrap_exchange_journal_item(msg: io.IOBase | bytes) -> bytes | None:
    """Returns None if not a journal item. Binary RFC822 message otherwise."""

    def as_bytes(m):
        try:
            return m.as_bytes(policy=email.policy.SMTP)
        except UnicodeEncodeError:
            logging.debug("as_bytes: email.policy.SMTP failed")
        try:
            return m.as_bytes(policy=email.policy.SMTPUTF8)
        except UnicodeEncodeError:
            logging.debug("as_bytes: email.policy.SMTPUTF8 failed")
        # FIXME: fixedpolicy
        try:
            return m.as_bytes(policy=fixedpolicy.SMTP)
        except UnicodeEncodeError:
            logging.debug("as_bytes: fixedpolicy.SMTP failed")
        try:
            return m.as_bytes(policy=fixedpolicy.SMTPUTF8)
        except UnicodeEncodeError:
            logging.debug("as_bytes: fixedpolicy.SMTPUTF8 failed")
        try:
            return m.as_bytes(policy=fixedpolicy.compat32)
        except UnicodeEncodeError:
            logging.debug("as_bytes: fixedpolicy.compat32 failed")
        return None

    def rfc822_attachment(parts, idx):
        submsgs = [as_bytes(m) for m in parts[idx].get_payload()]
        if len(submsgs) == 1:
            return submsgs[0]
        return None

    reader = _mail_reader(msg)
    cover = email.parser.BytesParser(policy=email.policy.default).parse(reader)  # type: ignore
    parts = [part for part in cover.walk() if part.get_content_type() == "message/rfc822"]

    # WORKAROUND: Microsoft Exchange sends journal messages using the original
    # sender address. When DKIM/SPF is configured for the sender's domain, the
    # receiving SMTP server rejects the message. Microsoft then wraps the
    # original journal entry in an "Undeliverable: <SUBJECT>" bounce, which
    # contains two RFC822 attachments instead of one.
    if len(parts) > 0:
        # When multiple RFC822 attachments are present, check whether this is
        # an "Undeliverable" bounce. In that case the first attachment is a
        # malformed delivery status (starts with "Content-Type:") and the
        # actual journal message is the second attachment.
        submsg = rfc822_attachment(parts, 0)
        if submsg and submsg.startswith(b"Content-Type:"):
            submsg = rfc822_attachment(parts, 1)
            if submsg:
                logging.warning("Message was rescued from 'Undeliverable' stupidity")
        return submsg  # type: ignore
    return None
