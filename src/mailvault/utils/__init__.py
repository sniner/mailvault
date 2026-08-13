"""Small general-purpose helpers with no mailvault-specific knowledge.

Nothing here has an opinion about mail, archives or the store; this is the
handful of things several of them need and none of them owns. The package
presents them flat, so callers keep writing `utils.under(...)`.

- `iterables` -- working through what an iterable holds
- `path` -- what this archive does with paths beyond what `pathlib` does
- `fs` -- filesystem work that `pathlib` leaves to the caller
"""

from __future__ import annotations

from mailvault.utils.fs import remove_file, set_read_only
from mailvault.utils.iterables import batched
from mailvault.utils.path import under, under_dir

__all__ = [
    "batched",
    "remove_file",
    "set_read_only",
    "under",
    "under_dir",
]
