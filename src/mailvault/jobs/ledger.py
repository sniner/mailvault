"""How many archived copies of a Message-ID a place still has to offer.

The bookkeeping one reconciliation pass keeps while it walks a folder listing.
It lives here rather than among the message-reading helpers because it reads
nothing: it is handed normalised Message-IDs and counts, and answers what is
left. Its subject is a comparison, not a message.
"""

from __future__ import annotations

import bisect
import collections.abc
import enum


class Claim(enum.Enum):
    """What a place's ledger had left for a Message-ID the server offered.

    Three outcomes and not two, because the two ways of coming away empty mean
    entirely different things to whoever reads the report. `ABSENT` is a gap: the
    archive has never seen this Message-ID at this place. `EXHAUSTED` is a
    message that *is* archived, of which the server holds more copies than the
    archive holds objects -- and the commonest reason for that is a byte-identical
    duplicate, which a content-addressed store cannot hold twice no matter how
    often it is fetched.

    Telling them apart is what keeps a report honest. Counted together they made
    a folder with two thousand duplicates report two thousand missing messages,
    on every run, for good -- and a number that always stands there and never
    means anything is one a reader learns to skip past.
    """

    TAKEN = "taken"
    EXHAUSTED = "exhausted"
    ABSENT = "absent"


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
    would otherwise be missing for good. What it no longer does is *call* them
    missing: `claim` says which of the two kinds of empty it is, and the counts
    part ways from there.

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

    def claim(self, value: str) -> Claim:
        """Claim one archived copy of `value`, and say what was there to claim.

        A count that has run down to zero keeps its entry rather than being
        removed, which is the whole trick: it is what lets a later copy of the
        same Message-ID be reported as one copy too many instead of as a message
        nobody ever archived.
        """
        if not value:
            return Claim.ABSENT
        left = self._left.get(value)
        if left is not None:
            if left > 0:
                self._left[value] = left - 1
                return Claim.TAKEN
            return Claim.EXHAUSTED
        if len(value) <= self.TRUNCATION_THRESHOLD:
            return Claim.ABSENT
        # In a sorted list, any entry starting with `value` sits at or after the
        # first one that is >= `value`. Scan on from there: an earlier match may
        # have been claimed already, and a later one may still have a copy.
        found = False
        for candidate in self._long[bisect.bisect_left(self._long, value) :]:
            if not candidate.startswith(value):
                break
            found = True
            if self._left.get(candidate, 0) > 0:
                self._left[candidate] -= 1
                return Claim.TAKEN
        return Claim.EXHAUSTED if found else Claim.ABSENT

    def __len__(self) -> int:
        """How many archived copies are still unclaimed."""
        return sum(self._left.values())
