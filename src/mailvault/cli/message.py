"""`get` -- handing a message out of the archive, or naming where it lies.

One command, and the lookup both of its answers rest on: an id as the reports
print it, whole or begun, resolved to the one entry it names or refused for
naming several.
"""

from __future__ import annotations

import argparse
import logging
import pathlib
import sys

from mailvault import jobs, utils
from mailvault.cli.common import (
    archive_path,
    require_archive,
)
from mailvault.store import cas

log = logging.getLogger(__name__)


# How much of an id `get` asks for when it was given too little. The
# store can look up four characters -- that is the shard directory -- but four
# names the whole shard, which in an archive of any size is several messages, so
# asking for four sends the reader back for more. Six is where a prefix starts
# naming one message: 16^6 is 16.7 million, and 131,000 messages meet one of them
# less than once in a hundred.
MIN_ID_PREFIX = 6


def _entry_path(store: cas.ContentAddressedStorage, wanted: str) -> pathlib.Path:
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
            raise jobs.JobError(f"{wanted}: neither a store id nor the path of an entry")
        hashval = cas.normalize_hashval(wanted)
    if len(hashval) < MIN_ID_PREFIX:
        raise jobs.JobError(
            f"{wanted}: too little of an id to look one up -- give at least its"
            f" first {MIN_ID_PREFIX} characters"
        )
    found = store.locate(hashval, exists=True)
    if found is not None:
        return found
    matches = store.matching(hashval)
    if not matches:
        raise jobs.JobError(f"{hashval}: not in this archive")
    if len(matches) > 1:
        log.debug("%s: begins %s", hashval, ", ".join(matches))
        raise jobs.JobError(
            f"{hashval}: the beginning of {utils.counted(len(matches), 'message id')}"
            f" in this archive -- name the one you mean by its whole id"
        )
    entry = store.locate(matches[0], exists=True)
    if entry is None:
        raise jobs.JobError(f"{matches[0]}: not in this archive")
    return entry


def export_entries(
    source: pathlib.Path,
    wanted: list[str],
    output: pathlib.Path | None,
) -> int:
    """Write out what an entry holds, decompressed, exactly as it was stored.

    The way to look at a message the reports can only name. What lies in the
    archive under that id may be a zstd frame in a sharded directory, and none
    of that is anyone's business outside the store -- this hands over the
    message.

    Raw and unmodified: whatever comes out here hashes back to the name it came
    from, so it is also the way to hand a message to another tool without the
    archive having an opinion about it.
    """
    store = cas.mail_store(source)
    paths = [_entry_path(store, one) for one in wanted]

    if output is None:
        if len(paths) > 1:
            raise jobs.JobError(
                "export: several messages need --output, or they would arrive as one"
                " stream with nothing between them"
            )
        sys.stdout.buffer.write(store.read(paths[0]))
        return 0

    if len(paths) > 1 or output.is_dir():
        if not output.is_dir():
            raise jobs.JobError(f"{output}: not a directory")
        for path in paths:
            target = output / path.name.removesuffix(".zst")
            target.write_bytes(store.read(path))
            print(f"{target}")
        return 0

    output.write_bytes(store.read(paths[0]))
    print(f"{output}")
    return 0


def locate_entries(source: pathlib.Path, wanted: list[str]) -> int:
    """Say where each message lies, one path per line.

    The answer a script wants: `export` hands over the message and this hands
    over its place in the archive, so that a run can be told which file to look
    at, back up, or hand to something else.

    What lies there is the entry, not a message ready to read -- it is
    write-protected and it may be a zstd frame. That is what the help says, and
    it is why this prints a path instead of pretending to be a cheaper `export`.
    """
    store = cas.mail_store(source)
    # Every id is looked up before the first line is printed. Half a list
    # followed by a refusal is the worst shape for something a script reads.
    paths = [_entry_path(store, one) for one in wanted]
    for path in paths:
        print(f"{path}")
    return 0


def run(args: argparse.Namespace) -> int:
    """Run `get`: hand over a message, or say where it lies.

    Its own command and not a corner of `archive`, because `archive` is where an
    archive is looked after -- taking a message out of one is using it, which is
    what `backup` and `verify` are, and it belongs beside them.
    """
    archive = archive_path(args)
    require_archive(archive)
    if args.path:
        return locate_entries(archive, args.entry)
    return export_entries(archive, args.entry, args.output)
