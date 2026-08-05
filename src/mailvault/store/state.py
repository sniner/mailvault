"""Where the next incremental run picks up, kept as a small JSON file.

Two things are recorded per folder, and keeping them apart is the point of this
module. `last_run` is the wall clock of the last pass over that folder -- a
statement about the *run*, useful for answering "did the nightly job touch this
at all", and nothing else reads it. `resume` is the point the next pass carries
on from -- a statement about *coverage*, and the only one that decides what gets
fetched.

The resume point is opaque here. Its shape belongs to the backend that made it
(a UID watermark for IMAP, a delta link for Graph), and this module neither
interprets nor validates beyond "it is an object and it names its kind". A
backend that does not recognise a `kind` reads the folder in full, which makes
one rule cover an upgrade from an older format, a job whose backend was swapped,
and a token from a version that does not exist yet: **a resume point is either
understood and valid, or there is none.**

These used to live in a SQLite database inside the archive -- a file rewritten in
place, where over SMB or NFS a torn write can take the whole file with it, and
with it the record of what has already been fetched. That is why they are kept
here instead, and why `state.json` is only ever replaced atomically: write a
temporary file, flush it to disk, then rename it over the old one. A rename
within a directory is atomic on every filesystem in practical use, so a reader
sees either the previous state or the new one, never a half-written mixture. The
worst case is losing the most recent update, which costs bandwidth on the next
run -- the content-addressed storage discards the redundant downloads.

The file is not a second source of truth: what it holds can be recovered by
reading a folder in full. It exists so that starting a run does not have to.

Single writer assumed, matching the rest of mailvault: two runs writing the same
archive concurrently can lose one another's updates.
"""

from __future__ import annotations

import collections.abc
import dataclasses
import json
import logging
import pathlib
from datetime import datetime

from mailvault.store import atomic

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

    def to_payload(self) -> dict:
        payload: dict = {}
        if self.last_run is not None:
            payload["last_run"] = self.last_run
        if self.resume is not None:
            payload["resume"] = self.resume
        return payload

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
            log.warning("%s: unreadable, starting from empty state: %s", path, exc)
            return cls(path)

        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            log.warning("%s: not valid JSON, starting from empty state: %s", path, exc)
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
            log.warning("%s: expected a JSON object, ignoring content", path)
            return {}
        version = payload.get("version")
        if version not in SUPPORTED_STATE_VERSIONS:
            log.warning(
                "%s: unknown state version %r (expected one of %s), ignoring content",
                path,
                version,
                ", ".join(str(v) for v in SUPPORTED_STATE_VERSIONS),
            )
            return {}
        raw = payload.get("snapshots")
        if not isinstance(raw, dict):
            log.warning("%s: 'snapshots' is not an object, ignoring content", path)
            return {}

        if version == LEGACY_STATE_VERSION:
            log.info(
                "%s: state written by an older version -- its timestamps are kept as a "
                "record, but they are not resume points, so every folder is read in full "
                "once",
                path,
            )

        folders: dict[str, dict[str, FolderState]] = {}
        for mailbox, entries in raw.items():
            if not isinstance(mailbox, str) or not isinstance(entries, dict):
                log.warning("%s: skipping malformed entry for %r", path, mailbox)
                continue
            valid: dict[str, FolderState] = {}
            for folder, value in entries.items():
                if not isinstance(folder, str):
                    continue
                parsed = SnapshotState._folder_state(path, mailbox, folder, value)
                if parsed is not None:
                    valid[folder] = parsed
            if len(valid) != len(entries):
                log.warning("%s: dropped malformed folder entries of %r", path, mailbox)
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
            log.warning("%s: %s::%s has a non-string last_run, dropped", path, mailbox, folder)
            last_run = None

        resume = value.get("resume")
        if resume is not None and not _is_usable_resume(resume):
            # Not an error worth failing over: an unusable resume point means the
            # folder is read in full, which is the safe outcome anyway.
            log.warning(
                "%s: %s::%s has an unusable resume point, the folder is read in full",
                path,
                mailbox,
                folder,
            )
            resume = None

        parsed = FolderState(last_run=last_run, resume=resume)
        return None if parsed.is_empty() else parsed

    def is_empty(self) -> bool:
        """True when no folder is recorded, i.e. a new or unusable state file."""
        return not self._folders

    def mailboxes(self) -> set[str]:
        """The mailboxes recorded here -- who has written into this archive.

        Answers "does this job belong to this archive" without touching anything
        else, which is why it is worth having: the metadata log knows the same
        thing, but only by opening every file it holds.
        """
        return set(self._folders)

    def _state(self, mailbox: str, folder: str) -> FolderState | None:
        return self._folders.get(mailbox, {}).get(folder)

    def resume(self, mailbox: str, folder: str) -> dict | None:
        """Return the folder's resume point, or None to read it in full.

        The value is handed to the backend as it was stored. Nothing here knows
        what a `uid` or a `delta_link` means, and nothing here should.
        """
        entry = self._state(mailbox, folder)
        return None if entry is None else entry.resume

    def last_run(self, mailbox: str, folder: str) -> datetime | None:
        """Return when a run last read this folder, or None if it never did.

        A value without a timezone is read as local time, because that is what it
        was: those entries were written by a version that used `datetime.now()`
        rather than `datetime.now(UTC)`.
        """
        entry = self._state(mailbox, folder)
        if entry is None or entry.last_run is None:
            return None
        try:
            parsed = datetime.fromisoformat(entry.last_run)
        except ValueError:
            log.warning(
                "%s: %s::%s has an unparsable timestamp %r, treating as unknown",
                self.path,
                mailbox,
                folder,
                entry.last_run,
            )
            return None
        return parsed if parsed.tzinfo is not None else parsed.astimezone()

    def record(
        self,
        mailbox: str,
        folder: str,
        *,
        last_run: datetime,
        resume: dict | None,
    ) -> None:
        """Note a pass over one folder. Call save() to persist.

        `resume` of None leaves whatever was there untouched rather than clearing
        it -- a pass that archived nothing has no new point to offer, and
        forgetting the old one would throw away coverage that still holds.
        """
        entry = self._folders.setdefault(mailbox, {}).setdefault(folder, FolderState())
        entry.last_run = last_run.isoformat()
        if resume is not None:
            entry.resume = resume

    def entries(self) -> collections.abc.Iterator[tuple[str, str, FolderState]]:
        """Yield (mailbox, folder, state) for every recorded folder."""
        for mailbox in sorted(self._folders):
            for folder in sorted(self._folders[mailbox]):
                yield mailbox, folder, self._folders[mailbox][folder]

    def save(self) -> None:
        """Write the state to disk atomically, replacing any previous version."""
        snapshots = {
            mailbox: {folder: entry.to_payload() for folder, entry in sorted(folders.items())}
            for mailbox, folders in sorted(self._folders.items())
        }
        payload = {"version": STATE_VERSION, "snapshots": snapshots}
        body = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        atomic.write_text(self.path, body)
        log.debug("%s: resume state written", self.path)


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
