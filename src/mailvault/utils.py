"""Small general-purpose helpers with no mailvault-specific knowledge."""

from __future__ import annotations

import collections.abc
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
