from __future__ import annotations

import collections.abc
import logging
from typing import Any

log = logging.getLogger(__name__)


def chunks(items: list[Any], n: int) -> collections.abc.Generator[list[Any], None, None]:
    """Yield successive n-sized chunks from items. Reference: https://stackoverflow.com/a/312464"""
    for i in range(0, len(items), n):
        yield items[i : i + n]


def batched(
    items: collections.abc.Iterable[Any], n: int
) -> collections.abc.Generator[list[Any], None, None]:
    """Yield successive n-sized batches from any iterable.

    Unlike `chunks` this does not need the sequence up front, so it can batch a
    walk over an archive without holding every path in memory first.
    (`itertools.batched` would do, but it needs Python 3.12.)
    """
    batch: list[Any] = []
    for item in items:
        batch.append(item)
        if len(batch) >= n:
            yield batch
            batch = []
    if batch:
        yield batch
