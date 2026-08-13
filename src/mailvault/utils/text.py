"""Wording that has to agree with a number"""

from __future__ import annotations


def counted(count: int, singular: str, plural: str | None = None) -> str:
    """How many of something there are: `counted(1, "message")` -> "1 message".

    Every report and log line in this tool counts something, and for a long
    while they all wrote `message(s)` -- never wrong, and unreadable by the time
    a noun does not simply take an `s`: `log entry/entries`, `copy/copies`. This
    picks the right word instead, and takes the plural form where it is not the
    singular plus `s`: `counted(n, "log entry", "log entries")`.

    The two forms are what the line reads as for one and for many, which is not
    always only the noun -- `counted(n, "message belongs", "messages belong")`
    is the way to get a verb after it to agree. Where a sentence would need
    that in several places, it is usually shorter to write it so it does not.

    The number comes back grouped (`1,729 messages`), because a count worth
    printing is worth reading, and the call sites that formatted it themselves
    were the ones that forgot.
    """
    noun = singular if count == 1 else plural or f"{singular}s"
    return f"{count:,} {noun}"
