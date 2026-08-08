"""Small general-purpose helpers with no mailvault-specific knowledge."""

from __future__ import annotations

import collections.abc
import pathlib
import sys
from typing import Any

# `itertools.batched` arrived in Python 3.12; below that the stand-in below does
# the same work. Gate on the version rather than catching ImportError, so a type
# checker analysing an older interpreter does not try to resolve a name that is
# not there -- same reasoning as the zstd facade in `mailvault.store.zstd`.
if sys.version_info >= (3, 12):
    from itertools import batched as _batched
else:

    def _batched(
        items: collections.abc.Iterable[Any],
        n: int,
    ) -> collections.abc.Generator[tuple[Any, ...], None, None]:
        """Stand in for `itertools.batched`, yielding tuples exactly as it does."""
        batch: list[Any] = []
        for item in items:
            batch.append(item)
            if len(batch) == n:
                yield tuple(batch)
                batch = []
        if batch:
            yield tuple(batch)


def batched(
    items: collections.abc.Iterable[Any],
    n: int,
) -> collections.abc.Generator[list[Any], None, None]:
    """Yield successive n-sized batches from any iterable, the last one short.

    The sequence is not needed up front, so this batches a walk over an archive
    without holding every path in memory first -- and it takes a plain list just
    as happily, which is why there is only one of these.

    Callers always get lists, never the tuples the standard library yields. That
    is what makes the version gate invisible: when the baseline reaches 3.12 the
    fallback can simply be deleted, and no call site has to be looked at.
    """
    for batch in _batched(items, n):
        yield list(batch)


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
