"""What this archive does with paths, beyond what `pathlib` already does."""

from __future__ import annotations

import pathlib


def under(root: pathlib.Path, path: pathlib.Path) -> str:
    """A path as it reads *inside* `root`: what lies below it, nothing more.

    Reports and log lines are about one archive, named once at the start of a
    run, and repeating its path on every line buries the statement behind it --
    over a network share the prefix is routinely longer than what it prefixes.

    A path that does not lie below `root` is returned whole. That is not a
    failure: `archive import` reads from somewhere else entirely, and shortening
    such a path against an archive it has nothing to do with would be a lie
    about where the file is.
    """
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def under_dir(name: str, path: pathlib.Path) -> str:
    """A path as it reads from outside the directory `name` it lies in.

    The same shortening as `under`, for the places that do not have the root to
    shorten against. A file deep in one of an archive's directories is handled by
    code that knows the directory it belongs to and nothing above it -- the
    metadata log knows `meta/`, the resume points know `heads/` -- while what a
    reader has in front of them is the archive those sit in.

    Found by name rather than by counting levels upwards, so how deep a store
    shards its files stays the store's own business. A path with no such
    directory above it is returned whole, the same way and for the same reason
    as in `under`.
    """
    for parent in path.parents:
        if parent.name == name:
            return under(parent.parent, path)
    return str(path)
