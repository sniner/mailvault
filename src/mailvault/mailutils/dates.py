"""Reading the Date header, including the ones no parser will take as they stand.

Its own module because it is the one field of a message that arrives broken often
enough to need a strategy rather than a call. Thirty years of mail programs have
glued timezones to times, encoded the whole header, named months in German, and
left the time out altogether -- and an archive holds all of them at once.

The order of the repairs is the substance here, not the regular expressions: a
header that can be read as it stands never reaches a step that leans on a
convention, and no step is allowed to turn one date into a different one.
"""

from __future__ import annotations

import collections.abc
import email.header
import email.message
import email.utils
import logging
import re
from datetime import datetime

from mailvault.mailutils.headers import header_text

log = logging.getLogger(__name__)

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
    # Debug, not a warning: a run over an archive writes one of these per
    # message and they add up to a misleading number -- measured against the
    # reference archive, 16 of them for 110 messages that ended up with no date
    # at all. What a header said is worth having when one is looked into; what
    # the database came to hold is the count worth printing, and `db create`
    # asks it of the database rather than of the parser.
    log.debug("Unreadable Date header %r", value)
    return None
