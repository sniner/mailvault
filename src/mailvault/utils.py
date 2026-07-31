from __future__ import annotations

import collections.abc
import logging
from typing import Any

log = logging.getLogger(__name__)


def chunks(items: list[Any], n: int) -> collections.abc.Generator[list[Any], None, None]:
    """Yield successive n-sized chunks from items. Reference: https://stackoverflow.com/a/312464"""
    for i in range(0, len(items), n):
        yield items[i : i + n]
