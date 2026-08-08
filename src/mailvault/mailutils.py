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
    msg: io.IOBase | bytes,
    headersonly: bool = False,
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
        logging.warning("Unreadable %s header: %s", name, exc)
        return ""
    return str(value) if value else ""


# A timezone abbreviation stuck to the time with no space: "06:41:03EST".
_GLUED_ZONE = re.compile(r"(\d:\d{2}(?::\d{2})?)([A-Za-z]{2,5})\b")
# A numeric UTC offset, so an impossible one can be told from a valid one.
_UTC_OFFSET = re.compile(r"\s*([+-])(\d{2})(\d{2})\b")


def _repair_date(value: str) -> str:
    """Apply the mechanical repairs to a Date header, language-independently.

    Only what cannot produce a *wrong* date. Separating a timezone that is glued
    to the time changes nothing about the value, and an offset of more than 24
    hours (`+9752` occurs) is not a timezone by any reading, so dropping it
    leaves the local time it was attached to.
    """
    repaired = _GLUED_ZONE.sub(r"\1 \2", value)
    return _UTC_OFFSET.sub(lambda m: "" if int(m.group(2)) >= 24 else m.group(0), repaired)


# The weekday a Date header opens with, if it has one.
_WEEKDAY = re.compile(r"\A\s*[^\W\d_]+\s*,\s*")
# A date written the German way, all numbers: "27.11.2002".
_DOTTED = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b")
# Any time at all, to tell a header that has one from a header that has none.
_ANY_TIME = re.compile(r"\d{1,2}:\d{2}")
# The year, which is where a missing time is inserted after.
_YEAR = re.compile(r"\b(\d{4})\b")

_MONTH_ABBR = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)

# German month names, for mail written by a program that did not translate them.
# One language, and the one this archive is full of. Another can be added beside
# it, but not carelessly: French `Jui` is June or July depending on which
# abbreviation somebody cut short, and that is where wrong dates start.
_GERMAN_MONTH_NAMES = (
    ("jan", "januar"),
    ("feb", "februar"),
    ("mrz", "mär", "märz", "maer", "maerz"),
    ("apr", "april"),
    ("mai",),
    ("jun", "juni"),
    ("jul", "juli"),
    ("aug", "august"),
    ("sep", "sept", "september"),
    ("okt", "oktober"),
    ("nov", "november"),
    ("dez", "dezember"),
)

# Paired with the English abbreviations by position rather than by hand, so a
# month cannot end up beside the wrong one through a typo in a long table.
_GERMAN_MONTHS = {
    name: _MONTH_ABBR[number]
    for number, names in enumerate(_GERMAN_MONTH_NAMES)
    for name in names
}
_GERMAN_MONTH = re.compile(
    r"\b(" + "|".join(sorted(_GERMAN_MONTHS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)


def _repair_date_by_convention(value: str) -> str:
    """Repair a Date header the ways that lean on a convention rather than on form.

    The rung below `_repair_date`, tried only once every plainer reading has
    failed, and each step still chosen so that it cannot turn one date into a
    different one:

    - **The weekday goes.** It is optional in RFC 5322 and says nothing the rest
      of the header does not, so a `Thur` or a `Sa` that no parser knows costs
      nothing to drop -- and dropping it is what makes the weekday, of any
      language, stop being a problem without a table for any of them.
    - **German month names become English ones.** A table for one language, and
      `Dez` is December in every reading of it.
    - **`27.11.2002` becomes `27 Nov 2002`, but only when the first number
      cannot be a month.** Day-first is the convention wherever the dots are,
      yet `05.03.2002` is the fifth of March to half the world and the third of
      May to the other half, so that one stays unread rather than guessed.
    - **A date with no time at all gets midnight.** The one step here that adds
      something the message never carried, and the only one that can be wrong --
      in the time, never in the date. It buys the five `Mon, 11 Mar 2002 PST` of
      the reference archive a day that is right to sort and to filter by.
    """
    repaired = _WEEKDAY.sub("", value)
    repaired = _GERMAN_MONTH.sub(lambda m: _GERMAN_MONTHS[m.group(1).lower()], repaired)
    repaired = _DOTTED.sub(_dotted_date, repaired)
    if not _ANY_TIME.search(repaired):
        repaired = _YEAR.sub(r"\1 00:00:00", repaired, count=1)
    return repaired


def _dotted_date(match: re.Match[str]) -> str:
    """Rewrite `27.11.2002` as `27 Nov 2002`, or leave an ambiguous one alone."""
    day, month, year = (int(part) for part in match.groups())
    if day <= 12 or not 1 <= month <= 12:
        return match.group(0)
    return f"{day} {_MONTH_ABBR[month - 1]} {year}"


def _date_candidates(value: str) -> collections.abc.Iterator[str]:
    """Yield the readings of a Date header worth trying, plainest first.

    Two rungs of repair, and the order between them is the point: a header that
    parses as it stands, or after being decoded, never reaches the repairs that
    lean on a convention.
    """
    try:
        decoded = str(email.header.make_header(email.header.decode_header(value)))
    except (ValueError, LookupError, UnicodeDecodeError):
        decoded = value
    readings = [value, decoded, _repair_date(value), _repair_date(decoded)]
    readings += [_repair_date_by_convention(reading) for reading in readings]
    seen: set[str] = set()
    for reading in readings:
        if reading and reading not in seen:
            seen.add(reading)
            yield reading


def date(msg: email.message.EmailMessage) -> datetime | None:
    """Return the message's Date, or None when the header cannot be read at all.

    Old mail carries dates the parser refuses outright. Nineties mail sometimes
    RFC 2047-encodes the whole header, comment and all:

        =?iso-8859-1?Q?Thu=2C_18_Dec_1997_22=3A03=3A34_+0100_=28=28ME?=
        =?iso-8859-1?Q?Z=29_Mitteleurop=E4ische_Zeit=29?=

    which decodes to an entirely ordinary `Thu, 18 Dec 1997 22:03:34 +0100
    ((MEZ) Mitteleuropäische Zeit)`. Others glue the timezone to the time or
    carry an offset of `+9752`. Others again open with a weekday no parser knows,
    name their month in German, or leave the time out altogether. Each reading is
    tried in turn, plainest first, and the ones that lean on a convention come
    last -- see `_date_candidates`.

    What still cannot be read yields None, never an exception. Walking an archive
    must not stop at one bad header -- and None means "unknown", which is a
    truthful thing to store. An epoch date instead would sort these messages in
    among real ones from the seventies and hide them from `WHERE date IS NULL`.
    That is also why no reading here fills in a *year*: `Wed, 17 Sep GMT Daylight
    Time` stays unknown rather than being dated by whichever year the run happens
    to take place in.
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


class MessageIdLedger:
    """How many archived copies of each Message-ID a place holds, claimed one by one.

    Asking "is this Message-ID archived?" is not enough. A place can hold two
    messages that share a Message-ID and differ in their bytes -- the storage is
    addressed by content, so those are two objects, and a plain presence check
    lets the second one pass as already archived. Asking "how many" instead
    closes that: the server showing an id twice while the archive holds one copy
    means one is missing.

    So a match *consumes* a copy, which makes this a one-pass, order-dependent
    thing rather than a value that can be asked twice. That is the price, and it
    is why it is a ledger rather than an index.

    Where it errs, it errs towards claiming too little and therefore fetching too
    much: byte-identical duplicates on the server collapse into one object here,
    so each further copy finds nothing left to claim and gets downloaded again to
    be discarded. Bandwidth on a command that runs rarely, against a message that
    would otherwise be missing for good.

    Truncation is tolerated throughout. Exchange caps the Message-ID it reports
    in folder listings at around 255 characters, so a long value can arrive as a
    strict prefix of the archived one. Exact hits are answered from the counts;
    only values long enough to have been truncated fall back to a prefix search,
    and that has to scan forward -- the first archived id starting with the value
    may already be used up.

    All values are expected to be normalised with normalize_message_id().
    """

    # Well below the observed 255 character cap: only keeps the prefix search
    # small, so its exact value is not critical.
    TRUNCATION_THRESHOLD = 128

    def __init__(self, counts: collections.abc.Mapping[str, int]):
        self._left = {mid: n for mid, n in counts.items() if mid and n > 0}
        self._long = sorted(mid for mid in self._left if len(mid) > self.TRUNCATION_THRESHOLD)

    def take(self, value: str) -> bool:
        """Claim one archived copy of `value`; False when there is none left."""
        if not value:
            return False
        if self._left.get(value, 0) > 0:
            self._left[value] -= 1
            return True
        if len(value) <= self.TRUNCATION_THRESHOLD:
            return False
        # In a sorted list, any entry starting with `value` sits at or after the
        # first one that is >= `value`. Scan on from there: an earlier match may
        # have been claimed already, and a later one may still have a copy.
        for candidate in self._long[bisect.bisect_left(self._long, value) :]:
            if not candidate.startswith(value):
                break
            if self._left.get(candidate, 0) > 0:
                self._left[candidate] -= 1
                return True
        return False

    def __len__(self) -> int:
        """How many archived copies are still unclaimed."""
        return sum(self._left.values())


def subject(msg: email.message.EmailMessage) -> str:
    return header_text(msg, "Subject")


@dataclasses.dataclass(frozen=True)
class MessageMetadata:
    """Where an archived message was seen: its store id and the folders it is in.

    Produced by :func:`metadata` and handed to the backup's callback, which
    records it in the metadata log. That location is the only thing a backup needs
    to write about a message -- subject, sender and date are in the message itself.
    ``folders`` is the set of places it is in: one for IMAP, possibly several for
    Gmail, which reports every label a message carries no matter which folder it
    was fetched from. It may hold ``bytes`` as well as ``str`` (Gmail reports
    label names as raw bytes), so it is deliberately not annotated ``list[str]``.
    """

    mailbox: str
    store_id: str
    folders: list


def metadata(
    mailbox: str,
    folder: str,
    store_id: str,
    folders: list | None = None,
) -> MessageMetadata:
    """Build the location record a backend hands to the backup callback.

    `folders`, when given, is the exact set of places the message is in (Gmail
    labels); otherwise it defaults to the single `folder` it was fetched from.
    Nothing is read out of the message -- the log records only the location.
    """
    return MessageMetadata(
        mailbox=mailbox,
        store_id=store_id,
        folders=folders if folders is not None else [folder],
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
