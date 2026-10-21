"""Which server messages a place's archived copies account for by Message-ID alone.

The bookkeeping one reconciliation pass keeps while it compares a folder listing
with the archive. It lives here rather than among the message-reading helpers
because it reads nothing: it is handed normalised Message-IDs and counts, and
answers which server messages need no download.
"""

from __future__ import annotations

import bisect
import collections.abc
import enum


class Match(enum.Enum):
    """What the Message-ID alone says about one server message.

    Only `ARCHIVED` lets a message go without a download. The other two are
    reasons to fetch it and compare its content, and they are kept apart because
    the log names them: a reader watching hundreds of downloads on an archive
    that is complete is owed the reason.
    """

    # A copy under this id is archived here and no other server message
    # competes for it.
    ARCHIVED = "archived"
    # No archived copy under this id.
    UNKNOWN = "unknown"
    # The server lists this id more often than the archive holds it.
    OUTNUMBERED = "outnumbered"


TRUNCATION_THRESHOLD = 128
"""Length above which a server value may be a truncated Message-ID.

Exchange caps the Message-ID it reports in folder listings at around 255
characters, so a long value can arrive as a strict prefix of the archived one.
Well below that cap, which only keeps the prefix search small.
"""


def same_message_id(value: str, key: str) -> bool:
    """Whether the server's `value` can stand for the archived `key`.

    Both normalised. Equal, or `value` long enough to have been truncated and a
    prefix of `key`.
    """
    if not value:
        return False
    return key == value or (len(value) > TRUNCATION_THRESHOLD and key.startswith(value))


def match_by_message_id(
    counts: collections.abc.Mapping[str, int],
    values: collections.abc.Sequence[str],
) -> list[Match]:
    """Match server messages against archived copies by Message-ID, one per copy.

    `counts` is how many archived copies a place holds per Message-ID, `values`
    the server's Message-IDs in listing order, all normalised with
    `normalize_message_id`. The answer has one entry per value.

    Counted rather than looked up, because the archive is addressed by content:
    two messages that share a Message-ID and differ in their bytes are two
    archived objects, and only a count notices the server listing that id twice
    where one copy is archived.

    Where the server lists an id more often than the archive holds it, every
    server message under that id is `OUTNUMBERED`, not only the excess. The id
    cannot say which of the server's copies is the archived one, and handing the
    archived copy to whichever comes first in the listing leaves a different
    version that comes first unfetched for good.
    """
    ledger = _Ledger(counts)
    claims = [ledger.claim(value) for value in values]
    contested = {key for taken, keys in claims if not taken for key in keys}
    matches: list[Match] = []
    for taken, keys in claims:
        if not keys:
            matches.append(Match.UNKNOWN)
        elif not taken or contested.intersection(keys):
            matches.append(Match.OUTNUMBERED)
        else:
            matches.append(Match.ARCHIVED)
    return matches


class _Ledger:
    """Archived copies per Message-ID, claimed one by one in listing order."""

    def __init__(self, counts: collections.abc.Mapping[str, int]):
        self._left = {mid: n for mid, n in counts.items() if mid and n > 0}
        self._long = sorted(mid for mid in self._left if len(mid) > TRUNCATION_THRESHOLD)

    def claim(self, value: str) -> tuple[bool, tuple[str, ...]]:
        """Claim one archived copy of `value`.

        Returns whether a copy was left, and the archived ids involved: the one a
        copy was taken from, or every one `value` reached and found used up.
        None reached means the archive has no copy under this id at all.

        A count that has run down to zero keeps its entry. That is what lets a
        later copy of the same Message-ID be told from one nobody archived.
        """
        if not value:
            return False, ()
        left = self._left.get(value)
        if left is not None:
            if left > 0:
                self._left[value] = left - 1
                return True, (value,)
            return False, (value,)
        if len(value) <= TRUNCATION_THRESHOLD:
            return False, ()
        # In a sorted list, any entry starting with `value` sits at or after the
        # first one that is >= `value`. Scan on from there: an earlier match may
        # have been claimed already, and a later one may still have a copy.
        reached: list[str] = []
        for candidate in self._long[bisect.bisect_left(self._long, value) :]:
            if not candidate.startswith(value):
                break
            if self._left[candidate] > 0:
                self._left[candidate] -= 1
                return True, (candidate,)
            reached.append(candidate)
        return False, tuple(reached)
