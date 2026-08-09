"""Reading the `state.json` of an archive written before `heads/`.

**Nothing writes this format any more, and nothing reads it during a run.** The
resume points live in `heads/`, one file per place; see `mailvault.store.heads`
for why. What is left here is the reader, used exactly once per archive by the
import in `mailvault.jobs.migration`, after which the file is deleted. It is
migration code and goes out with 1.0.

Two versions have to be understood, and the difference between them is the
reason the format moved at all. Version 1 held a bare ISO timestamp per folder
and resumed from it -- a date, which a message copied into a folder slips behind
without ever being asked for again. Version 2 split that into `last_run`, a
statement about the *run*, and an opaque `resume`, a statement about *coverage*.
Only the second decides what gets fetched.

Both are read, and they are carried over differently: a version 2 file yields
`last_run` and `resume`, a version 1 file yields `last_run` alone. Adopting a
version 1 timestamp as a resume point would inherit exactly the gap it can hide,
so every folder of such an archive is read in full once instead.

The resume point stays opaque. Its shape belongs to the backend that made it (a
UID watermark for IMAP, a delta link for Graph), and this module neither
interprets nor validates beyond "it is an object and it names its kind". A
backend that does not recognise a `kind` reads the folder in full, which makes
one rule cover an upgrade, a job whose backend was swapped, and a token from a
version that does not exist yet: **a resume point is either understood and
valid, or there is none.**

Nothing here is a second source of truth: what it holds can be recovered by
reading a folder in full. It exists so that the first run after the import does
not have to.
"""

from __future__ import annotations

import collections.abc
import dataclasses
import json
import logging
import pathlib

log = logging.getLogger(__name__)

# Default filename of the resume state inside an archive.
DEFAULT_STATE_NAME = "state.json"

# Payload format version, so a change can be recognised rather than guessed at.
# Version 1 held a bare ISO timestamp per folder and resumed from it; version 2
# splits that into `last_run` and an opaque `resume`. A version 1 file is still
# read -- its timestamps become `last_run` -- but it yields no resume point, so
# the first run after the upgrade reads every folder in full. Deliberate: a date
# is not a resume point, and pretending otherwise is what version 2 exists to
# stop.
STATE_VERSION = 2
LEGACY_STATE_VERSION = 1

# Readers reject what they do not know instead of misreading it.
SUPPORTED_STATE_VERSIONS = (LEGACY_STATE_VERSION, STATE_VERSION)


@dataclasses.dataclass
class FolderState:
    """What is known about one folder: when it was read, and where to carry on.

    Both are optional and independent. A folder that was visited but had nothing
    to offer has a `last_run` and no `resume`; one adopted from an older format
    has the same, which is what sends the next pass over it in full.
    """

    last_run: str | None = None
    resume: dict | None = None

    def is_empty(self) -> bool:
        return self.last_run is None and self.resume is None


class SnapshotState:
    """The per-mailbox, per-folder resume state of one archive.

    Timestamps are held as ISO 8601 strings and converted on access, so a value
    that cannot be parsed costs one folder rather than the whole file.
    """

    def __init__(
        self,
        path: pathlib.Path,
        folders: dict[str, dict[str, FolderState]] | None = None,
    ):
        self.path = path
        self._folders: dict[str, dict[str, FolderState]] = folders if folders else {}

    @classmethod
    def load(cls, path: pathlib.Path) -> SnapshotState:
        """Read the state file, returning empty state when it is unusable.

        A missing file is the normal case for a new archive. A corrupt one is
        deliberately not an error either: everything here can be recovered by
        reading the folders in full, and treating it as empty falls back to
        exactly that -- expensive, but never wrong. Anything unusable is reported
        as a warning so it does not pass unnoticed.
        """
        try:
            body = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return cls(path)
        except (OSError, UnicodeDecodeError) as exc:
            log.warning("%s: unreadable, starting from empty state: %s", path.name, exc)
            return cls(path)

        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            log.warning("%s: not valid JSON, starting from empty state: %s", path.name, exc)
            return cls(path)

        return cls(path, cls._extract(path, payload))

    @staticmethod
    def _extract(path: pathlib.Path, payload: object) -> dict[str, dict[str, FolderState]]:
        """Pull the folder states out of a decoded payload, dropping junk.

        Validated field by field: the file lives in an archive that other tools
        may touch, so a wrong shape has to degrade into "unknown" rather than an
        AttributeError in the middle of a backup run.
        """
        if not isinstance(payload, dict):
            log.warning("%s: expected a JSON object, ignoring content", path.name)
            return {}
        version = payload.get("version")
        if version not in SUPPORTED_STATE_VERSIONS:
            log.warning(
                "%s: unknown state version %r (expected one of %s), ignoring content",
                path.name,
                version,
                ", ".join(str(v) for v in SUPPORTED_STATE_VERSIONS),
            )
            return {}
        raw = payload.get("snapshots")
        if not isinstance(raw, dict):
            log.warning("%s: 'snapshots' is not an object, ignoring content", path.name)
            return {}

        if version == LEGACY_STATE_VERSION:
            log.info(
                "%s: state written by an older version -- its timestamps are kept as a "
                "record, but they are not resume points, so every folder is read in full "
                "once",
                path.name,
            )

        folders: dict[str, dict[str, FolderState]] = {}
        for mailbox, entries in raw.items():
            if not isinstance(mailbox, str) or not isinstance(entries, dict):
                log.warning("%s: skipping malformed entry for %r", path.name, mailbox)
                continue
            valid: dict[str, FolderState] = {}
            for folder, value in entries.items():
                if not isinstance(folder, str):
                    continue
                parsed = SnapshotState._folder_state(path, mailbox, folder, value)
                if parsed is not None:
                    valid[folder] = parsed
            if len(valid) != len(entries):
                log.warning("%s: dropped malformed folder entries of %r", path.name, mailbox)
            if valid:
                folders[mailbox] = valid
        return folders

    @staticmethod
    def _folder_state(
        path: pathlib.Path,
        mailbox: str,
        folder: str,
        value: object,
    ) -> FolderState | None:
        """Decode one folder's entry, in either the version 1 or version 2 shape.

        Version 1 is a bare timestamp string. It becomes a `last_run` and nothing
        else: a date says when a run happened, not how far the archive reaches,
        and the whole reason for version 2 is that the two were confused.
        """
        if isinstance(value, str):
            return FolderState(last_run=value)
        if not isinstance(value, dict):
            return None

        last_run = value.get("last_run")
        if last_run is not None and not isinstance(last_run, str):
            log.warning(
                "%s: %s::%s has a non-string last_run, dropped", path.name, mailbox, folder
            )
            last_run = None

        resume = value.get("resume")
        if resume is not None and not _is_usable_resume(resume):
            # Not an error worth failing over: an unusable resume point means the
            # folder is read in full, which is the safe outcome anyway.
            log.warning(
                "%s: %s::%s has an unusable resume point, the folder is read in full",
                path.name,
                mailbox,
                folder,
            )
            resume = None

        parsed = FolderState(last_run=last_run, resume=resume)
        return None if parsed.is_empty() else parsed

    def is_empty(self) -> bool:
        """True when no folder is recorded, i.e. a missing or unusable file."""
        return not self._folders

    def entries(self) -> collections.abc.Iterator[tuple[str, str, FolderState]]:
        """Yield (mailbox, folder, state) for every recorded folder."""
        for mailbox in sorted(self._folders):
            for folder in sorted(self._folders[mailbox]):
                yield mailbox, folder, self._folders[mailbox][folder]


def _is_usable_resume(value: object) -> bool:
    """A resume point has to be an object that names its kind, and no more.

    Everything past `kind` belongs to the backend, so validating it here would
    only mean this module having to be changed every time a backend learns
    something. What is checked is what this module actually relies on.
    """
    if not isinstance(value, dict):
        return False
    kind = value.get("kind")
    return isinstance(kind, str) and bool(kind)
