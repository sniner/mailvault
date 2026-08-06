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

File layout -- a content-addressed store, the same discipline the mail uses:

    meta/a1/a1b2c3....jsonl

The name is the hash of the content, so a file carries its own integrity check:
`sha384sum` against the name settles it, without knowing this format at all. It
also shards, which keeps a decade of runs from piling thousands of files into one
directory. Depth 1 is enough here -- the mail store uses 2 because it has to
carry hundreds of thousands of entries, the log has orders of magnitude fewer.

Nothing in the name orders the files, and nothing needs to: folders only ever
accumulate, so replaying in any order gives the same result. The `date` in the
header is what carries the chronology, for a reader that wants it and for any
future semantics where the newest observation has to win.

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
from datetime import datetime

from mailvault.store import cas

log = logging.getLogger(__name__)

# Default directory of the metadata log inside a store directory.
DEFAULT_LOG_DIR = "meta"

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


def open_store(root: pathlib.Path) -> cas.ContentAddressedStorage:
    """Open the log's content-addressed store.

    Depth 1 is enough. The mail store uses 2 because it has to carry hundreds of
    thousands of entries; the log has orders of magnitude fewer.
    """
    return cas.ContentAddressedStorage(root, suffix=".jsonl", depth=1)


def log_files(root: pathlib.Path) -> list[pathlib.Path]:
    """Return every log file below `root`, in a stable order.

    Sorted by path so a run is reproducible, not because the order carries
    meaning -- folders only accumulate, so a replay gives the same result in any
    order, and the chronology lives in each file's `date` header. Transient files
    do not match the `*/*.jsonl` pattern and are skipped.
    """
    if not root.is_dir():
        return []
    return sorted(p for p in root.glob("*/*.jsonl") if p.is_file())


def has_logs(root: pathlib.Path) -> bool:
    """True when at least one log file exists."""
    return bool(log_files(root))


def verify_file(path: pathlib.Path) -> bool:
    """True when a file's content still matches the name it was stored under."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        log.warning("%s: unreadable: %s", path, exc)
        return False
    return cas.DEFAULT_HASH(raw).hexdigest() == path.name.removesuffix(".jsonl")


def _serialize(
    mailbox: str | None, folder: str | None, date: str | None, store_ids: list[str]
) -> bytes:
    """Serialize one place's observations into the on-disk JSONL form.

    Shared by `LogWriter.seal` and `compact` so the two produce byte-identical
    files for the same content -- which is what makes compaction idempotent.
    """
    header = {
        "version": LOG_VERSION,
        "mailbox": mailbox,
        "folder": folder,
        "date": date,
        "messages": len(store_ids),
    }
    body = json.dumps(header, ensure_ascii=False) + "\n"
    body += "".join(json.dumps({"store_id": s}, ensure_ascii=False) + "\n" for s in store_ids)
    return body.encode("utf-8")


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
        if not self._places:
            return []
        written: list[pathlib.Path] = []
        store = open_store(self.root)
        for (mailbox, folder), store_ids in sorted(
            self._places.items(), key=lambda item: (item[0][0] or "", item[0][1] or "")
        ):
            _status, _hashval, path = store.add(
                _serialize(mailbox, folder, date.isoformat(), store_ids)
            )
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
    if not cas.is_hashval(store_id):
        # The store cuts a path out of a store id and refuses one that is not a
        # hash -- rightly, since `../..` would climb out of it. Here that value
        # came out of a file which is allowed to be damaged, so it is a line to
        # skip like any other unusable one. Letting it through would hand the
        # refusal to whoever asks the store next, and cost them the whole folder
        # they were reading for one broken line.
        log.warning("%s:%d: store_id is not a hash, skipped", path, number)
        return None
    return store_id


def read_log(path: pathlib.Path) -> LogFile | None:
    """Read one log file, returning None when it cannot be used at all.

    Individual damaged lines are skipped; only an unreadable header discards the
    whole file, because without it the lines have no place to belong to.
    """
    try:
        raw = path.read_bytes()
    except OSError as exc:
        log.warning("%s: unreadable, skipped: %s", path, exc)
        return None

    # The name is the hash of the content, so the file carries its own integrity
    # check -- the same guarantee the mail store gives, and it catches what syntax
    # never could: a flipped bit inside an otherwise well-formed line.
    #
    # A mismatch is reported but does not discard the file. A log records
    # observations and never claims to be exhaustive, so whatever still parses is
    # a subset of the truth -- which is what every log file is anyway. Throwing
    # away 80,000 readable lines because the last one was cut short would be the
    # worse answer. The warning is what lets someone repair the archive.
    if cas.DEFAULT_HASH(raw).hexdigest() != path.name.removesuffix(".jsonl"):
        log.warning("%s: damaged -- content does not match its name", path)

    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        log.warning("%s: not valid UTF-8, skipped: %s", path, exc)
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
            "%s: log version %r is not %d, skipped -- it was written by a different"
            " mailvault version; upgrade mailvault to read it",
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


@dataclasses.dataclass
class CompactResult:
    """Outcome of consolidating the log."""

    files_before: int = 0
    files_after: int = 0
    places: int = 0
    entries_before: int = 0
    entries_after: int = 0
    verified: bool = True
    transient_removed: int = 0


def compact(root: pathlib.Path) -> CompactResult:
    """Consolidate the log into one file per place, dropping duplicate entries.

    Incremental backups overlap -- each run re-records the messages in its lookback
    window -- so a place accumulates many small files whose store ids repeat across
    them. This reads them all, writes one file per (mailbox, folder) holding the
    sorted union of that place's store ids, verifies the new files landed, and only
    then removes the originals.

    Crash-safe by ordering: on an interrupt the originals are still there, a read
    takes the union regardless so nothing is lost, and a rerun finishes the job. A
    file that cannot be read is left in place rather than folded away, so damaged
    data is never silently dropped. Producing byte-identical files for the same
    content (via `_serialize`) makes a second run a no-op.
    """
    result = CompactResult()
    originals = log_files(root)
    result.files_before = len(originals)
    if not originals:
        return result

    # Union each place's store ids, keeping the newest date it was seen.
    places: dict[tuple[str | None, str | None], set[str]] = {}
    dates: dict[tuple[str | None, str | None], str] = {}
    consumed: list[pathlib.Path] = []
    for path in originals:
        entry = read_log(path)
        if entry is None:
            continue
        consumed.append(path)
        key = (entry.mailbox, entry.folder)
        result.entries_before += len(entry.store_ids)
        places.setdefault(key, set()).update(entry.store_ids)
        existing = dates.get(key)
        if entry.date is not None and (existing is None or entry.date > existing):
            dates[key] = entry.date
    if not places:
        return result
    result.places = len(places)

    store = open_store(root)
    written: set[pathlib.Path] = set()
    for key in sorted(places, key=lambda k: (k[0] or "", k[1] or "")):
        mailbox, folder = key
        store_ids = sorted(places[key])
        result.entries_after += len(store_ids)
        _status, _hashval, path = store.add(
            _serialize(mailbox, folder, dates.get(key), store_ids)
        )
        written.add(path)

    # Verify the consolidated files landed before removing anything.
    if not all(verify_file(path) for path in written):
        log.error("%s: consolidated files did not verify, originals left in place", root)
        result.verified = False
        result.files_after = len(log_files(root))
        return result

    # Drop the originals we consolidated, but never one byte-identical to a file
    # just written (an already-compact place produces the same hash).
    for path in consumed:
        if path not in written:
            path.unlink(missing_ok=True)

    # Folding a hundred files into one empties most of the shard directories, and
    # nothing else ever removes them -- a store that only grows, like the mail, has
    # no reason to look.
    #
    # The same goes for what an interrupted write leaves behind. Only the log is
    # swept here, and only because this pass has it open anyway: the mail store
    # would mean walking a hundred thousand directories over whatever the archive
    # is mounted on, which belongs to a pass that walks it for its own reasons.
    result.transient_removed = store.prune_transient_files()
    store.prune_empty_dirs()

    result.files_after = len(log_files(root))
    return result
