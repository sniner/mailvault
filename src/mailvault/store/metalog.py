"""Append-only record of where each archived message was seen.

Of everything the metadata database holds, only two things cannot be recovered
from the archived `.eml` files: which mailbox a message came from, and which
folder or label it carried there. Subject, sender, recipients and date are all in
the message itself. And the archive is usually the only copy left -- mailvault
exists to move mail out of a mailbox, so "just fetch it from the server again" is
not a recovery path.

So that attribution is written a second time, into a log that is never modified.
Each folder of each run produces one JSONL file under `meta/`, written once and
then left alone. The database becomes a projection that can be thrown away and
rebuilt from the log.

Why one file per folder rather than one per message: a content-addressed archive
stores a message once, but the same message legitimately belongs to several
mailboxes and folders at once. A per-message file would therefore have to be
reopened and merged by the next job -- read-modify-write, the very thing this is
meant to avoid. A log file instead records one *observation* ("during this run,
in this mailbox, in this folder, these messages were seen"). Two jobs seeing the
same message write two lines in two different files and never collide; the merge
happens when the log is replayed.

File layout:

    meta/2026-08-01T18-02-21Z_001.jsonl

The timestamp sorts lexicographically, which gives replay order for free -- no
index file is needed, the directory listing is the log. The folder name goes in
the header line, never in the filename: names like `Archiv/2016` and `\\Sent`
would otherwise have to be escaped for the filesystem.

File content -- a header line, then one line per message:

    {"version":1,"mailbox":"mail.example.org","folder":"INBOX","date":"...","complete":true}
    {"store_id":"df3823f1...","labels":["INBOX"]}

The mailbox of a message is the header's unless the line overrides it with its
own `mailboxes` list, which is what the bootstrap export of an existing database
does: that database knows which mailboxes and which labels a message has, but not
which label belonged to which mailbox, and inventing that pairing would be worse
than recording it as it is.

A torn write costs the last line of one file, which is skipped on read. A file
whose header is unreadable costs that one folder of that one run.
"""

from __future__ import annotations

import collections.abc
import dataclasses
import itertools
import json
import logging
import pathlib
from datetime import UTC, datetime

from mailvault.store import atomic

log = logging.getLogger(__name__)

# Default directory of the metadata log inside a store directory.
DEFAULT_LOG_DIR = "meta"

# Payload format version. Readers reject what they do not know rather than
# misread it; a file with an unknown version is skipped with a warning.
LOG_VERSION = 1


@dataclasses.dataclass
class LogEntry:
    """One observation: a message, and where it was seen."""

    store_id: str
    mailboxes: list[str]
    labels: list[str]


@dataclasses.dataclass
class LogFile:
    """The readable content of one log file."""

    path: pathlib.Path
    mailbox: str | None
    folder: str | None
    date: str | None
    complete: bool
    entries: list[LogEntry]


def as_text(value: object) -> str:
    """Coerce a label to text.

    Gmail reports its labels as raw bytes, which is why `MessageMetadata.labels`
    is deliberately not typed `list[str]`. JSON has no bytes, so a byte label is
    decoded here rather than crashing the run that found it.
    """
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _timestamp(date: datetime) -> str:
    """Format a timestamp for a filename: sortable, and free of path characters."""
    if date.tzinfo is None:
        date = date.replace(tzinfo=UTC)
    return date.astimezone(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")


def _free_path(root: pathlib.Path, date: datetime) -> pathlib.Path:
    """Return a log path that does not exist yet.

    The counter disambiguates the folders of one run, which usually share a
    second, and it also keeps two runs that started in the same second apart.
    """
    stamp = _timestamp(date)
    for counter in itertools.count(1):
        candidate = root / f"{stamp}_{counter:03d}.jsonl"
        if not candidate.exists():
            return candidate
    raise AssertionError("unreachable")  # pragma: no cover


def log_files(root: pathlib.Path) -> list[pathlib.Path]:
    """Return the log files below `root` in replay order.

    Ordering is by filename, which is chronological by construction. Transient
    files carry a different suffix and are therefore skipped.
    """
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.suffix == ".jsonl" and p.is_file())


def has_logs(root: pathlib.Path) -> bool:
    """True when at least one log file exists."""
    return bool(log_files(root))


class LogWriter:
    """Collects the observations of one folder run and seals them into one file.

    Nothing is written until `seal`, so an interrupted run leaves no partial file
    behind and the log never contains a half-observed folder. Entries are
    serialised as they arrive rather than kept as objects, which keeps even a
    whole-database export to roughly the size of the file it will produce.
    """

    def __init__(
        self,
        root: pathlib.Path,
        mailbox: str | None = None,
        folder: str | None = None,
    ):
        self.root = root
        self.mailbox = mailbox
        self.folder = folder
        self._lines: list[str] = []

    def __len__(self) -> int:
        return len(self._lines)

    def add(
        self,
        store_id: str,
        labels: collections.abc.Iterable[object],
        mailboxes: collections.abc.Iterable[str] | None = None,
    ) -> None:
        """Record one message. `mailboxes` overrides the header's mailbox."""
        entry: dict[str, object] = {
            "store_id": store_id,
            "labels": [as_text(label) for label in labels],
        }
        if mailboxes is not None:
            entry["mailboxes"] = list(mailboxes)
        self._lines.append(json.dumps(entry, ensure_ascii=False))

    def seal(self, date: datetime, complete: bool = True) -> pathlib.Path | None:
        """Write the collected entries to a new file and return its path.

        Returns None when nothing was observed: an incremental run over an
        unchanged folder has nothing to record, and writing an empty file for
        every folder of every run would bury the log in noise.

        A folder whose downloads partly failed is still written, with `complete`
        false. The messages that *were* stored need their attribution recorded --
        it is only the snapshot that must not advance.
        """
        if not self._lines:
            return None
        header = {
            "version": LOG_VERSION,
            "mailbox": self.mailbox,
            "folder": self.folder,
            "date": date.isoformat(),
            "complete": complete,
            "messages": len(self._lines),
        }
        body = json.dumps(header, ensure_ascii=False) + "\n"
        body += "\n".join(self._lines) + "\n"
        self.root.mkdir(parents=True, exist_ok=True)
        path = _free_path(self.root, date)
        atomic.write_text(path, body)
        log.debug("%s: %s entries sealed", path, len(self._lines))
        self._lines = []
        return path


def _parse_entry(
    path: pathlib.Path, number: int, line: str, default_mailbox: str | None
) -> LogEntry | None:
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
    raw_labels = data.get("labels")
    labels = [as_text(label) for label in raw_labels] if isinstance(raw_labels, list) else []
    raw_mailboxes = data.get("mailboxes")
    if isinstance(raw_mailboxes, list):
        mailboxes = [as_text(mailbox) for mailbox in raw_mailboxes]
    else:
        mailboxes = [default_mailbox] if default_mailbox else []
    return LogEntry(store_id=store_id, mailboxes=mailboxes, labels=labels)


def read_log(path: pathlib.Path) -> LogFile | None:
    """Read one log file, returning None when it cannot be used at all.

    Individual damaged lines are skipped; only an unreadable header discards the
    whole file, because without it the lines have no mailbox to belong to.
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
            "%s: unknown log version %r (expected %d), skipped",
            path,
            header.get("version"),
            LOG_VERSION,
        )
        return None

    mailbox = header.get("mailbox")
    mailbox = mailbox if isinstance(mailbox, str) else None
    folder = header.get("folder")
    folder = folder if isinstance(folder, str) else None
    date = header.get("date")
    date = date if isinstance(date, str) else None

    entries = []
    for number, line in enumerate(lines[1:], start=2):
        entry = _parse_entry(path, number, line, mailbox)
        if entry is not None:
            entries.append(entry)

    return LogFile(
        path=path,
        mailbox=mailbox,
        folder=folder,
        date=date,
        complete=bool(header.get("complete", True)),
        entries=entries,
    )


def read_all(root: pathlib.Path) -> collections.abc.Iterator[LogFile]:
    """Yield every readable log file below `root`, in replay order."""
    for path in log_files(root):
        entry = read_log(path)
        if entry is not None:
            yield entry
