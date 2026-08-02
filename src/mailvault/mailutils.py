"""Reading fields out of a raw email: headers, dates, addresses, Message-ID.

Everything the rest of mailvault needs to know about a message is parsed here,
from bytes -- subject, sender and recipients, date, Message-ID -- along with the
`MessageMetadata` record a backup carries and the unwrapping of Exchange journal
envelopes. The archive stores the message itself, so these are read back out of
it on demand rather than kept in a database.
"""

from __future__ import annotations

import bisect
import collections.abc
import dataclasses
import email.header
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
        logging.warning("Unreadable %s header: %s", label, exc)
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
            logging.warning("Unusable address in %s: %s", label, exc)
    return found


def addresses(msg: email.message.EmailMessage) -> tuple[set[str], set[str]]:
    """Extract from/to addresses from message. Returns tuple of sets (from, to)."""

    def received_for() -> collections.abc.Generator[str, None, None]:
        for field in msg.get_all("Received", []):
            m = re.search(
                r"\bfor\s+\<?([\w\-\.]+@[\w\-\.]+\w)\>?\b", field, flags=re.IGNORECASE
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
        logging.warning("Unreadable %s header: %s", name, exc)
        return ""
    return str(value) if value else ""


# A timezone abbreviation stuck to the time with no space: "06:41:03EST".
_GLUED_ZONE = re.compile(r"(\d:\d{2}(?::\d{2})?)([A-Za-z]{2,5})\b")
# A numeric UTC offset, so an impossible one can be told from a valid one.
_UTC_OFFSET = re.compile(r"\s*([+-])(\d{2})(\d{2})\b")


def _repair_date(value: str) -> str:
    """Apply the mechanical repairs to a Date header, language-independently.

    Deliberately only what cannot produce a *wrong* date. Separating a timezone
    that is glued to the time changes nothing about the value, and an offset of
    more than 24 hours (`+9752` occurs) is not a timezone by any reading, so
    dropping it leaves the local time it was attached to.

    Everything else that turns up -- German month names, `27.11.2002`, a weekday
    spelled `Thur`, a date with no time at all -- needs either a table for one
    language or a value the message never carried. Both are refused: a wrong date
    is worse than a missing one, because a missing one is visible.
    """
    repaired = _GLUED_ZONE.sub(r"\1 \2", value)
    return _UTC_OFFSET.sub(lambda m: "" if int(m.group(2)) >= 24 else m.group(0), repaired)


def _date_candidates(value: str) -> collections.abc.Iterator[str]:
    """Yield the readings of a Date header worth trying, plainest first."""
    seen = {value}
    yield value
    try:
        decoded = str(email.header.make_header(email.header.decode_header(value)))
    except (ValueError, LookupError, UnicodeDecodeError):
        decoded = value
    for candidate in (decoded, _repair_date(value), _repair_date(decoded)):
        if candidate not in seen:
            seen.add(candidate)
            yield candidate


def date(msg: email.message.EmailMessage) -> datetime | None:
    """Return the message's Date, or None when the header cannot be read at all.

    Old mail carries dates the parser refuses outright. Nineties mail sometimes
    RFC 2047-encodes the whole header, comment and all:

        =?iso-8859-1?Q?Thu=2C_18_Dec_1997_22=3A03=3A34_+0100_=28=28ME?=
        =?iso-8859-1?Q?Z=29_Mitteleurop=E4ische_Zeit=29?=

    which decodes to an entirely ordinary `Thu, 18 Dec 1997 22:03:34 +0100
    ((MEZ) Mitteleuropäische Zeit)`. Others glue the timezone to the time or
    carry an offset of `+9752`. Each reading is tried in turn, plainest first.

    What still cannot be read yields None, never an exception. Walking an archive
    must not stop at one bad header -- and None means "unknown", which is a
    truthful thing to store. An epoch date instead would sort these messages in
    among real ones from the seventies and hide them from `WHERE date IS NULL`.
    """
    value = header_text(msg, "Date")
    if not value:
        return None
    for candidate in _date_candidates(value):
        try:
            return email.utils.parsedate_to_datetime(candidate)
        except (ValueError, TypeError):
            continue
    logging.warning("Unreadable Date header %r", value)
    return None


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
    return header_text(msg, "Subject")


@dataclasses.dataclass(frozen=True)
class MessageMetadata:
    """The metadata record a backend extracts from a message when archiving it.

    Produced by :func:`metadata` in the backends and handed to the backup's
    callback, which records the message's location in the metadata log. Being a
    dataclass rather than a bare dict, a mistyped field name is caught statically
    instead of surfacing as a runtime KeyError in the middle of a backup run.

    ``folder`` is where the message was looked for; ``folders`` is where it
    actually turned out to be. The two differ for Gmail, which reports every
    folder a message is in no matter which one it was fetched from, so a message
    found in the inbox may report three folders.

    ``folders`` may contain ``bytes`` as well as ``str`` because Gmail reports
    its folder names as raw bytes; hence it is deliberately not annotated
    ``list[str]``.
    """

    mailbox: str
    folder: str
    store_id: str
    email_id: str
    date: datetime | None
    subject: str
    folders: list
    sender: set[str]
    recipients: set[str]


def metadata(
    msg: bytes,
    mailbox: str,
    folder: str,
    store_id: str,
    folders: list | None = None,
) -> MessageMetadata:
    """Extract the metadata record a backend hands to the backup callback."""
    header = decode_email_header(msg)
    from_addrs, to_addrs = addresses(header)
    return MessageMetadata(
        mailbox=mailbox,
        folder=folder,
        store_id=store_id,
        email_id=message_id(header),
        date=date(header),
        subject=subject(header),
        folders=folders if folders is not None else [folder],
        sender=from_addrs,
        recipients=to_addrs,
    )


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
