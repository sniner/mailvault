"""The `FORMAT` file: which layout generation an archive is written in.

An archive can be recognised by its structure -- shards here, `meta/` there --
but only *backwards*. A layout this version has never seen looks familiar in
exactly the wrong way: the directories it knows are all present, so it reads the
archive and is wrong about it. Recognition by structure can only ever identify
formats that already existed when the reader was written.

That is not a constructed worry. An archive on a network share is opened by more
than one installation, and those drift; the older binary meeting an archive a
newer one has already lifted is the likeliest format error there is.

The second thing it answers is newer: since a command takes the directory it is
standing in as the archive, "is this an archive at all?" became a question
somebody can get wrong by mistyping a `cd`.

And the third is about not accumulating rules. Structure could answer the
question today -- `store.db` present means very old, `state.json` present means
before `heads/` -- but every generation adds one more rule, and all of them
describe what an archive *happens to contain* rather than what it *claims*.

## What this is not

**Not a replacement for the version numbers inside the files.** That looks like
duplication and is not: the archive is content-addressed and immutable, so it
holds files of several vintages by construction -- a `meta/` file from 2021 next
to one from today. A single number at the root cannot describe how to read both.

- **the mark** answers "may this binary touch this archive at all", once, before
  anything else. It moves when the *layout* changes
- **a file header** answers "how do I read *this* file". It moves when a *file
  format* changes

## The form

One self-explanatory line, so that a `cat` five years from now answers the
question without this code:

    mailvault archive format 1

Not JSON, because there is nothing to parse. `FORMAT` and not `VERSION`, because
"version" is already taken three times over in this project -- the product, the
resume state, the log header -- and this one says which of them is meant.

Counting starts at 1. No file means generation 0, the layout as it was before
there was a mark; a file saying `0` and a missing file would have meant almost
the same thing and needed two rules.

The number does not follow the product version. They change for different
reasons and tying them would mean bumping one for the other's sake.
"""

from __future__ import annotations

import logging
import pathlib
import re

from mailvault.store import atomic

log = logging.getLogger(__name__)

# Name of the mark in an archive's root.
FORMAT_NAME = "FORMAT"

# What this version writes and reads. Generation 1 is the first to carry a mark
# at all: messages under `mail/`, resume points and log chain heads under
# `heads/`, the metadata log under `meta/`.
CURRENT_FORMAT = 1

# Generation 0 is not written anywhere -- it is what an archive without a mark
# is, by definition.
UNMARKED = 0

_LINE = re.compile(r"mailvault\s+archive\s+format\s+(?P<generation>\d+)\s*\Z")


class FormatError(Exception):
    """The archive says something about its format that cannot be acted on."""


def read(archive: pathlib.Path) -> int:
    """The generation an archive says it is written in, or `UNMARKED`.

    A mark that is there but unreadable raises rather than falling back to
    guessing. Guessing is precisely what this file exists to stop, and a reader
    that quietly returns to it takes away the only thing the file was for.
    """
    path = archive / FORMAT_NAME
    try:
        text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return UNMARKED
    except (OSError, UnicodeDecodeError) as exc:
        raise FormatError(f"{path}: cannot be read, so this archive cannot be opened: {exc}")

    match = _LINE.match(text)
    if match is None:
        raise FormatError(
            f"{path}: does not say what it should say. Expected a line like"
            f" {describe(CURRENT_FORMAT)!r}, found {text!r}"
        )
    return int(match.group("generation"))


def describe(generation: int) -> str:
    """The line a mark of this generation holds."""
    return f"mailvault archive format {generation}"


def write(archive: pathlib.Path, generation: int = CURRENT_FORMAT) -> None:
    """Mark an archive as written in `generation`, replacing any earlier mark.

    Written **last** by whatever brings an archive up to a generation. An
    interrupted migration then still says the older number, so the next attempt
    picks the work up again -- where a mark written first would claim a layout
    that only half exists.
    """
    atomic.write_text(archive / FORMAT_NAME, describe(generation) + "\n")
    log.debug("%s: marked as %s", archive, describe(generation))


def check_readable(archive: pathlib.Path) -> int:
    """Return the generation, refusing one this version cannot read.

    The message names what to do, because a number on its own leaves a reader
    with no move to make.
    """
    generation = read(archive)
    if generation > CURRENT_FORMAT:
        raise FormatError(
            f"{archive}: written by a newer version of mailvault (archive format"
            f" {generation}, this one reads {CURRENT_FORMAT}). Upgrade mailvault"
            f" -- reading it with this version would misread it."
        )
    return generation
