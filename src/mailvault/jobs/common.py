"""Errors and helpers shared across the job commands, and with the importer.

`seal_log` is here rather than in one of them because all three write the same
log and owe it the same discipline: what was not recorded must not be let go of.
A backup holds the deletion on the server back, an import holds back the removal
of its source files.
"""

from __future__ import annotations

import logging
from datetime import datetime

from mailvault.store import metalog

log = logging.getLogger(__name__)


class JobError(Exception):
    pass


def seal_log(writer: metalog.LogWriter, date: datetime) -> bool:
    """Write out a pass over a folder; return whether the log is now durable.

    A log that cannot be written is reported but does not abort the run -- the
    messages themselves are archived, and a failed seal simply does not advance
    what depends on it. In particular it gates deletion, in both directions mail
    comes from: a job that deletes after export must not remove a message from
    the server whose location was not recorded, and an import with `--move` must
    not remove the file it read a message out of. A False return leaves both
    where they are, to be fetched or read again. An empty pass records nothing
    and is durable by definition, so it returns True.

    What a failed seal leaves standing is deliberate too. `LogWriter.seal` clears
    what it wrote only after writing it, so a caller that keeps going accumulates
    the failed batch into the next one, and a later seal that succeeds carries
    all of it. Nothing has to be replayed by hand.

    Neither line names the directory it wrote into. There is one metadata log in
    an archive, the archive is named once at the start of the run, and what was
    left standing here was its absolute path -- over a share routinely longer
    than the sentence behind it. `metadata log:` says the same thing in words, as
    the reports have said it all along.
    """
    recorded, places = len(writer), writer.places
    try:
        paths = writer.seal(date)
    except OSError as exc:
        log.error("the metadata log was not written: %s", exc)
        return False
    if paths:
        log.info("metadata log: %s message(s) recorded in %s place(s)", recorded, places)
    return True
