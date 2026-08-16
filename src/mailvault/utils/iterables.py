"""Working through the items of an iterable"""

from __future__ import annotations

import collections.abc
import sys
from typing import TypeVar

# What `batched` was handed, so that what it yields says so too. Without it a
# caller batching `list[int]` got `list[Any]` back, and every use of an item
# after that was invisible to the checker.
T = TypeVar("T")

# `itertools.batched` arrived in Python 3.12; below that the stand-in below does
# the same work. Gate on the version rather than catching ImportError, so a type
# checker analysing an older interpreter does not try to resolve a name that is
# not there -- same reasoning as the zstd facade in `mailvault.store.zstd`.
if sys.version_info >= (3, 12):
    from itertools import batched as _batched
else:

    def _batched(
        items: collections.abc.Iterable[T],
        n: int,
    ) -> collections.abc.Generator[tuple[T, ...], None, None]:
        """Stand in for `itertools.batched`, yielding tuples exactly as it does.

        Including what it does about `n`: the standard library raises for a
        batch size below one, and a stand-in that quietly collected everything
        into a single batch instead would behave one way on 3.11 and another on
        3.12 -- which is the one thing a stand-in must never do.
        """
        if n < 1:
            raise ValueError("n must be at least one")
        batch: list[T] = []
        for item in items:
            batch.append(item)
            if len(batch) == n:
                yield tuple(batch)
                batch = []
        if batch:
            yield tuple(batch)


def batched(
    items: collections.abc.Iterable[T],
    n: int,
) -> collections.abc.Generator[list[T], None, None]:
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
