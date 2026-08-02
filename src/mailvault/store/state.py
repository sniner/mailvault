"""Where the next incremental run picks up, kept as a small JSON file.

The per-folder snapshot timestamps decide where the next incremental run picks
up. They used to live in a SQLite database inside the archive -- a file rewritten
in place, where over SMB or NFS a torn write can take the whole file with it, and
with it the record of what has already been fetched. That is why they are kept
here instead.

This module keeps that handful of timestamps in `state.json` in the archive and
only ever replaces that file atomically: write a temporary file,
flush it to disk, then rename it over the old one. A rename within a directory
is atomic on every filesystem in practical use, so a reader sees either the
previous state or the new one, never a half-written mixture. The worst case is
losing the most recent update, which costs bandwidth on the next run -- the
content-addressed storage discards the redundant downloads.

The file is not a second source of truth: an archive's snapshot state can be
reconstructed from the metadata log. It exists so that starting a run does not
require reading the whole log.

Single writer assumed, matching the rest of mailvault: two runs writing the same
archive concurrently can lose one another's updates.
"""

from __future__ import annotations

import collections.abc
import json
import logging
import pathlib
from datetime import datetime

from mailvault.store import atomic

log = logging.getLogger(__name__)

# Default filename of the resume state inside an archive.
DEFAULT_STATE_NAME = "state.json"

# Payload format version, so a future change can be recognised rather than
# guessed at. Readers reject what they do not know instead of misreading it.
STATE_VERSION = 1


class SnapshotState:
    """The per-mailbox, per-folder snapshot timestamps of one archive.

    Timestamps are held as ISO 8601 strings and converted on access, so a value
    that cannot be parsed costs one folder rather than the whole file.
    """

    def __init__(
        self,
        path: pathlib.Path,
        snapshots: dict[str, dict[str, str]] | None = None,
    ):
        self.path = path
        self._snapshots: dict[str, dict[str, str]] = snapshots if snapshots else {}

    @classmethod
    def load(cls, path: pathlib.Path) -> SnapshotState:
        """Read the state file, returning empty state when it is unusable.

        A missing file is the normal case for a new archive. A corrupt one is
        deliberately not an error either: the state is a cache of something the
        log can reproduce, and treating it as empty falls back to a full run --
        expensive, but never wrong. Anything unusable is reported as a warning so
        it does not pass unnoticed.
        """
        try:
            body = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return cls(path)
        except (OSError, UnicodeDecodeError) as exc:
            log.warning("%s: unreadable, starting from empty state: %s", path, exc)
            return cls(path)

        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            log.warning("%s: not valid JSON, starting from empty state: %s", path, exc)
            return cls(path)

        return cls(path, cls._extract(path, payload))

    @staticmethod
    def _extract(path: pathlib.Path, payload: object) -> dict[str, dict[str, str]]:
        """Pull the snapshot mapping out of a decoded payload, dropping junk.

        Validated field by field: the file lives in an archive that other tools
        may touch, so a wrong shape has to degrade into "unknown" rather than an
        AttributeError in the middle of a backup run.
        """
        if not isinstance(payload, dict):
            log.warning("%s: expected a JSON object, ignoring content", path)
            return {}
        version = payload.get("version")
        if version != STATE_VERSION:
            log.warning(
                "%s: unknown state version %r (expected %d), ignoring content",
                path,
                version,
                STATE_VERSION,
            )
            return {}
        raw = payload.get("snapshots")
        if not isinstance(raw, dict):
            log.warning("%s: 'snapshots' is not an object, ignoring content", path)
            return {}

        snapshots: dict[str, dict[str, str]] = {}
        for mailbox, folders in raw.items():
            if not isinstance(mailbox, str) or not isinstance(folders, dict):
                log.warning("%s: skipping malformed entry for %r", path, mailbox)
                continue
            valid = {
                folder: date
                for folder, date in folders.items()
                if isinstance(folder, str) and isinstance(date, str)
            }
            if len(valid) != len(folders):
                log.warning("%s: dropped malformed folder entries of %r", path, mailbox)
            if valid:
                snapshots[mailbox] = valid
        return snapshots

    def is_empty(self) -> bool:
        """True when no folder is recorded, i.e. a new or unusable state file."""
        return not self._snapshots

    def get_date(self, mailbox: str, folder: str) -> datetime | None:
        """Return the snapshot timestamp of one folder, or None when unknown.

        A value without a timezone is read as local time, because that is what it
        was: those entries were written by a version that used `datetime.now()`
        rather than `datetime.now(UTC)`. Leaving them naive would let a caller
        that stamps them `Z` read a local time as UTC -- an hour or two later than
        meant, and mail that arrived in that window is skipped once and never
        looked at again.
        """
        value = self._snapshots.get(mailbox, {}).get(folder)
        if value is None:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            log.warning(
                "%s: %s::%s has an unparsable timestamp %r, treating as unknown",
                self.path,
                mailbox,
                folder,
                value,
            )
            return None
        return parsed if parsed.tzinfo is not None else parsed.astimezone()

    def set_date(self, mailbox: str, folder: str, date: datetime) -> None:
        """Record the snapshot timestamp of one folder. Call save() to persist."""
        self._snapshots.setdefault(mailbox, {})[folder] = date.isoformat()

    def entries(self) -> collections.abc.Iterator[tuple[str, str, str]]:
        """Yield (mailbox, folder, timestamp) for every recorded folder."""
        for mailbox in sorted(self._snapshots):
            for folder in sorted(self._snapshots[mailbox]):
                yield mailbox, folder, self._snapshots[mailbox][folder]

    def save(self) -> None:
        """Write the state to disk atomically, replacing any previous version."""
        payload = {"version": STATE_VERSION, "snapshots": self._snapshots}
        body = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        atomic.write_text(self.path, body)
        log.debug("%s: snapshot state written", self.path)
