"""Errors and helpers shared across the job commands."""

from __future__ import annotations

import logging
from datetime import datetime

from mailvault.store import metalog

log = logging.getLogger(__name__)


class JobError(Exception):
    pass


def _seal_log(writer: metalog.LogWriter, date: datetime) -> bool:
    """Write out a pass over a folder; return whether the log is now durable.

    A log that cannot be written is reported but does not abort the run -- the
    messages themselves are archived, and a failed seal simply does not advance
    what depends on it. In particular it gates deletion: a job that deletes after
    export must not remove a message from the server whose location was not
    recorded, so a False return keeps those messages in place to be re-fetched
    next run. An empty pass records nothing and is durable by definition, so it
    returns True.
    """
    recorded, places = len(writer), writer.places
    try:
        paths = writer.seal(date)
    except OSError as exc:
        log.error("%s: metadata log not written: %s", writer.root, exc)
        return False
    if paths:
        log.info("%s: %s message(s) recorded in %s place(s)", writer.root, recorded, places)
    return True
