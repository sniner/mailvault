"""Looking a stored message up by the id a report printed.

The lookup both readers of the archive rest on: `get` on the command line and
the MCP server hand a message out by an id as the reports print it, whole or
begun, resolved to the one entry it names or refused for naming several. It
lives here rather than with either frontend because it is the same question in
both mouths, and an answer that drifted between them would let the same id name
a message in one and nothing in the other.
"""

from __future__ import annotations

import logging
import pathlib

from mailvault import utils
from mailvault.jobs.common import JobError
from mailvault.store import cas

log = logging.getLogger(__name__)


def entry_path(store: cas.ContentAddressedStorage, wanted: str) -> pathlib.Path:
    """Find the entry a message id names, given whole or only begun.

    A message id is ninety-six characters and the reports print the first twelve
    of them, so that is what a reader has to hand and that is what this takes --
    as long as it belongs to one message and not to several.

    A path is accepted too, and only its file name is looked at -- someone who
    has been in the directory with `ls` should not be sent away. But an id is
    what the reports print and what every other command takes; where an entry
    lies is the store's business and no part of the interface.
    """
    # A store id has no suffix, so `hashval_of` declines it and the fallback
    # takes over; only what the directories say is ignored, which is why a path
    # copied from a report written on another machine still finds its entry.
    hashval = store.hashval_of(pathlib.Path(wanted))
    if hashval is None:
        if not cas.is_hashval(wanted):
            raise JobError(f"{wanted}: neither a store id nor the path of an entry")
        hashval = cas.normalize_hashval(wanted)
    # How much of an id to ask for when too little was given, derived from the
    # store rather than fixed: the shard directory is `depth * 2` characters and
    # is the shortest prefix the store can look anything up by, but it names the
    # whole shard, which in an archive of any size is several messages -- asking
    # for exactly that sends the reader straight back for more. One byte beyond
    # it is where a prefix starts naming one message: at the store's depth of two
    # that is six characters, and 16^6 is 16.7 million, which 131,000 messages
    # meet less than once in a hundred.
    #
    # Derived, because both lookups below cut the shard directory out of the
    # prefix -- `_subdirs` raises a bare ValueError under `depth * 2`, and
    # `locate` reaches it first -- and a number written down here would have to be
    # kept above that by hand. At two more than they need, that cannot happen
    # whatever the store is sharded into.
    minimum = store.depth * 2 + 2
    if len(hashval) < minimum:
        raise JobError(
            f"{wanted}: too little of an id to look one up -- give at least its"
            f" first {minimum} characters"
        )
    found = store.locate(hashval, exists=True)
    if found is not None:
        return found
    matches = store.matching(hashval)
    if not matches:
        raise JobError(f"{hashval}: not in this archive")
    if len(matches) > 1:
        log.debug("%s: begins %s", hashval, ", ".join(matches))
        raise JobError(
            f"{hashval}: the beginning of {utils.counted(len(matches), 'message id')}"
            f" in this archive -- name the one you mean by its whole id"
        )
    entry = store.locate(matches[0], exists=True)
    if entry is None:
        raise JobError(f"{matches[0]}: not in this archive")
    return entry
