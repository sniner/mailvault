"""Append-only record of where each archived message was seen.

Of everything the metadata database holds, only one thing cannot be recovered
from the archived `.eml` files: the place a message was seen -- which mailbox,
and which folder within it. Subject, sender, recipients and date are all in the
message itself. And the archive is usually the only copy left: mailvault exists
to move mail out of a mailbox, so "fetch it from the server again" is not a
recovery path.

So that one fact is written a second time, into files that are never modified.
The database becomes a projection that can be thrown away and rebuilt from here.

**One file is one place.** A log file's header names a mailbox and a folder, and
its lines name the messages that were seen there. Nothing else is needed: the
question "which folder of which mailbox" is answered by the file a line sits in,
not by the line. That is what keeps a message belonging to several places from
being ambiguous -- it simply appears in several files.

Folders, not labels. Gmail calls them labels and allows several per message,
IMAP calls them folders and allows one; that is a difference in cardinality, not
in kind, and modelling it as two concepts is what made the metadata database
lose the pairing in the first place. Here a message's location is just the set of
(mailbox, folder) pairs it was observed in, however many that is.

File layout:

    meta/2026-08-01T18-02-21.758307Z.jsonl

The timestamp sorts lexicographically, which gives replay order for free -- no
index file is needed, the directory listing is the log. The folder name lives in
the header, never in the filename: names like `Archiv/2016` and `\\Sent` would
otherwise have to be escaped for the filesystem.

File content -- a header line, then one line per message:

    {"version":1,"mailbox":"mail.example.org","folder":"INBOX","date":"...","messages":2}
    {"store_id":"df3823f1..."}
    {"store_id":"60f57aa7..."}

`folder` may be null: the mailbox is known but which folder it was in is not.
That happens when importing from a database written before this log existed,
where the pairing was never recorded. It is deliberately representable rather
than guessed -- an archive should not invent a location it cannot know.

A torn write costs the last line of one file, which is skipped on read. A file
whose header is unreadable costs that one place of that one run.
"""

from __future__ import annotations

import collections.abc
import dataclasses
import json
import logging
import pathlib
import time
from datetime import UTC, datetime

from mailvault.store import atomic

log = logging.getLogger(__name__)

# Default directory of the metadata log inside a store directory.
DEFAULT_LOG_DIR = "meta"

# Marker written once the bootstrap export has finished. Its absence -- not the
# absence of log files -- is what makes the export run: an export interrupted
# halfway leaves files behind, and "some files exist" would then be read as
# "nothing to do", freezing the archive at partial coverage forever.
BOOTSTRAP_MARKER = ".bootstrap"

# Payload format version. Readers reject what they do not know rather than
# misread it; a file with an unknown version is skipped with a warning.
LOG_VERSION = 1


@dataclasses.dataclass
class LogFile:
    """One place, and the messages observed there."""

    path: pathlib.Path
    mailbox: str | None
    folder: str | None
    date: str | None
    store_ids: list[str]


def as_text(value: object) -> str:
    """Coerce a folder name to text.

    Gmail reports its folder names as raw bytes over `X-GM-LABELS`, which is why
    `MessageMetadata.folders` is deliberately not typed `list[str]`. JSON has no
    bytes, so a byte name is decoded here rather than crashing the run.
    """
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _timestamp(date: datetime) -> str:
    """Format a timestamp for a filename: sortable, and free of path characters.

    Microseconds are part of the name, zero padded, so the plain lexicographic
    order of the directory is still chronological order.
    """
    if date.tzinfo is None:
        date = date.replace(tzinfo=UTC)
    return date.astimezone(UTC).strftime("%Y-%m-%dT%H-%M-%S.%fZ")


def _free_path(root: pathlib.Path) -> pathlib.Path:
    """Return a log path that does not exist yet.

    Named after the moment it is written, to the microsecond, which is precise
    enough that two files practically never collide. When they do, waiting out a
    millisecond is cheaper than carrying a counter in every filename forever.
    """
    while True:
        candidate = root / (_timestamp(datetime.now(UTC)) + ".jsonl")
        if not candidate.exists():
            return candidate
        time.sleep(0.001)


def log_files(root: pathlib.Path) -> list[pathlib.Path]:
    """Return the log files below `root` in replay order.

    Ordering is by filename, which is chronological by construction. Transient
    files and the bootstrap marker carry a different suffix and are skipped.
    """
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.suffix == ".jsonl" and p.is_file())


def has_logs(root: pathlib.Path) -> bool:
    """True when at least one log file exists."""
    return bool(log_files(root))


def bootstrap_done(root: pathlib.Path) -> bool:
    """True when a bootstrap export has run to completion for this archive."""
    return (root / BOOTSTRAP_MARKER).exists()


def mark_bootstrap_done(root: pathlib.Path, date: datetime) -> None:
    """Record that the bootstrap export finished, after every file was sealed."""
    atomic.write_text(root / BOOTSTRAP_MARKER, date.isoformat() + "\n")


class LogWriter:
    """Collects observations and seals them into one file per (mailbox, folder).

    Nothing is written until `seal`, so an interrupted pass leaves no partial
    file behind and the log never contains a half-observed place. Entries are
    held as bare store ids, so even a whole-archive export costs roughly what the
    files it produces will cost.
    """

    def __init__(self, root: pathlib.Path):
        self.root = root
        self._places: dict[tuple[str | None, str | None], list[str]] = {}

    def __len__(self) -> int:
        return sum(len(ids) for ids in self._places.values())

    @property
    def places(self) -> int:
        """How many distinct (mailbox, folder) pairs are pending."""
        return len(self._places)

    def add(
        self,
        mailbox: str | None,
        folders: collections.abc.Iterable[object],
        store_id: str,
    ) -> None:
        """Record one message as seen in each of `folders` of `mailbox`.

        An empty `folders` records the message as seen in the mailbox without a
        known folder, rather than dropping it: knowing less is not the same as
        knowing nothing.
        """
        names: list[str | None] = [as_text(f) for f in folders]
        for name in names or [None]:
            self._places.setdefault((mailbox, name), []).append(store_id)

    def seal(self, date: datetime) -> list[pathlib.Path]:
        """Write one file per pending place and return their paths.

        Returns an empty list when nothing was observed: an incremental run over
        an unchanged folder has nothing to record, and writing an empty file for
        every folder of every run would bury the log in noise.

        A pass whose downloads partly failed is written just the same. The
        messages that *were* stored need their location recorded; it is only the
        snapshot that must not advance. Nothing marks the pass as partial,
        because a log of observations never claims to be exhaustive anyway --
        the messages recorded at a place are always a lower bound.
        """
        written: list[pathlib.Path] = []
        for (mailbox, folder), store_ids in sorted(
            self._places.items(), key=lambda item: (item[0][0] or "", item[0][1] or "")
        ):
            header = {
                "version": LOG_VERSION,
                "mailbox": mailbox,
                "folder": folder,
                "date": date.isoformat(),
                "messages": len(store_ids),
            }
            body = json.dumps(header, ensure_ascii=False) + "\n"
            body += "".join(
                json.dumps({"store_id": s}, ensure_ascii=False) + "\n" for s in store_ids
            )
            self.root.mkdir(parents=True, exist_ok=True)
            path = _free_path(self.root)
            atomic.write_text(path, body)
            log.debug("%s: %s message(s) in %s::%s", path, len(store_ids), mailbox, folder)
            written.append(path)
        self._places = {}
        return written


def _parse_store_id(path: pathlib.Path, number: int, line: str) -> str | None:
    """Decode one message line, returning None when it is unusable."""
    if not line.strip():
        return None
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        # The expected shape of a torn write: the file ends mid-line.
        log.warning("%s:%d: incomplete line, skipped", path, number)
        return None
    if not isinstance(data, dict):
        log.warning("%s:%d: not an object, skipped", path, number)
        return None
    store_id = data.get("store_id")
    if not isinstance(store_id, str) or not store_id:
        log.warning("%s:%d: no usable store_id, skipped", path, number)
        return None
    return store_id


def read_log(path: pathlib.Path) -> LogFile | None:
    """Read one log file, returning None when it cannot be used at all.

    Individual damaged lines are skipped; only an unreadable header discards the
    whole file, because without it the lines have no place to belong to.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        log.warning("%s: unreadable, skipped: %s", path, exc)
        return None
    if not lines:
        log.warning("%s: empty, skipped", path)
        return None
    try:
        header = json.loads(lines[0])
    except json.JSONDecodeError as exc:
        log.warning("%s: unreadable header, skipped: %s", path, exc)
        return None
    if not isinstance(header, dict):
        log.warning("%s: header is not an object, skipped", path)
        return None
    if header.get("version") != LOG_VERSION:
        log.warning(
            "%s: log version %r is not %d, skipped -- re-run 'archive bootstrap-log'"
            " to regenerate it",
            path,
            header.get("version"),
            LOG_VERSION,
        )
        return None

    mailbox = header.get("mailbox")
    folder = header.get("folder")
    date = header.get("date")
    store_ids = []
    for number, line in enumerate(lines[1:], start=2):
        store_id = _parse_store_id(path, number, line)
        if store_id is not None:
            store_ids.append(store_id)

    # The header's count is what catches a truncation that happens to end on a
    # line boundary: such a file parses cleanly and is still short. A torn line
    # already reports itself, this covers the case that otherwise passes unseen.
    declared = header.get("messages")
    if isinstance(declared, int) and declared != len(store_ids):
        log.warning(
            "%s: header declares %s message(s) but %s were readable, file is damaged",
            path,
            declared,
            len(store_ids),
        )

    return LogFile(
        path=path,
        mailbox=mailbox if isinstance(mailbox, str) else None,
        folder=folder if isinstance(folder, str) else None,
        date=date if isinstance(date, str) else None,
        store_ids=store_ids,
    )


def read_all(root: pathlib.Path) -> collections.abc.Iterator[LogFile]:
    """Yield every readable log file below `root`, in replay order."""
    for path in log_files(root):
        entry = read_log(path)
        if entry is not None:
            yield entry
