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

The files of one place do form a chain, though -- each header names the file that
held that place before it, and the newest is named by that place's head in
`heads/`. **The chain is the check, never the enumeration.** Reading still goes
through the glob above, so a broken link hides nothing: it is a finding, not a
loss. What it buys is narrow and worth stating exactly. A log file that vanishes
usually announces itself already, because its messages turn up in `archive check`
as having no provenance at all. That does not happen when the same message is
also recorded elsewhere -- a Gmail message filed under three labels lives in
three files -- and then the loss of one of its places is completely silent. That
is the gap the chain closes.

File content -- a header line, then one line per message:

    {"version":1,"mailbox":"mail.example.org","folder":"INBOX","date":"...","messages":2}
    {"store_id":"df3823f1..."}
    {"store_id":"60f57aa7..."}

`folder` may be null: the mailbox is known but which folder it was in is not.
That happens when importing from a database written before this log existed,
where the pairing was never recorded. It is deliberately representable rather
than guessed -- an archive should not invent a location it cannot know.

`mailbox` may be null too, and it means something else: there is no mailbox, not
that it was forgotten. That is what `archive import` writes -- the name it was
given goes in `folder`, and the empty mailbox is what says this mail came from
somewhere nobody can be asked about again. It also keeps that name out of the way
of every reader that looks a mailbox up by a job's name: a job always has one.

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

from mailvault import utils
from mailvault.store import cas, heads

log = logging.getLogger(__name__)

# Default directory of the metadata log inside a store directory.
DEFAULT_LOG_DIR = "meta"

# Payload format version. Readers reject what they do not know rather than
# misread it; a file with an unknown version is skipped with a warning.
#
# Version 2 added `prev` to the header: the name of the file that held the same
# place before this one, so a place's files form a chain rather than a heap. See
# the module docstring. Version 1 is still read -- an archive is full of files
# written over years, and refusing the older ones would make most of a long-lived
# log unreadable for the sake of a field. What a version 1 file means is simply
# "carries no chain information".
LOG_VERSION = 2
LEGACY_LOG_VERSION = 1
SUPPORTED_LOG_VERSIONS = (LEGACY_LOG_VERSION, LOG_VERSION)


# A place: which mailbox, and which folder within it. `None` for the folder is
# "the mailbox is known, which folder is not" -- representable rather than
# guessed, see the module docstring.
Place = tuple[str | None, str | None]


@dataclasses.dataclass
class LogFile:
    """One place, and the messages observed there."""

    path: pathlib.Path
    mailbox: str | None
    folder: str | None
    date: str | None
    store_ids: list[str]
    # The file that held this place before this one, or None for the first. A
    # version 1 file has no such field, and there is nothing to distinguish
    # "first of its place" from "written before the chain existed" -- which is
    # why `check` reports what no chain reaches instead of assuming.
    prev: str | None = None

    @property
    def place(self) -> Place:
        return (self.mailbox, self.folder)

    @property
    def hashval(self) -> str:
        """The name this file is filed under, which is the hash of its content."""
        return self.path.name.removesuffix(".jsonl")


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


def where(path: pathlib.Path) -> str:
    """A log file as it reads inside the archive: `meta/a1/a1b2….jsonl`.

    The archive is named once at the start of a run, so a file inside it is
    named the way it reads inside it. The log knows the directory it lives in
    and nothing above that, which is all `under_dir` asks for.
    """
    return utils.under_dir(DEFAULT_LOG_DIR, path)


def has_logs(root: pathlib.Path) -> bool:
    """True when at least one log file exists."""
    return bool(log_files(root))


def verify_file(path: pathlib.Path) -> bool:
    """True when a file's content still matches the name it was stored under."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        log.warning("%s: unreadable: %s", where(path), exc)
        return False
    return cas.DEFAULT_HASH(raw).hexdigest() == path.name.removesuffix(".jsonl")


def _serialize(
    mailbox: str | None,
    folder: str | None,
    date: str | None,
    store_ids: list[str],
    prev: str | None = None,
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
        "prev": prev,
    }
    body = json.dumps(header, ensure_ascii=False) + "\n"
    body += "".join(json.dumps({"store_id": s}, ensure_ascii=False) + "\n" for s in store_ids)
    return body.encode("utf-8")


def _chain(head: heads.Head | None) -> str | None:
    """The file a new entry for this place follows, or None to start a chain."""
    return None if head is None else head.log


def _head_of(
    heads_root: pathlib.Path, mailbox: str | None, folder: str | None
) -> heads.Head | None:
    """The head of a place, or None where there cannot be one.

    A place has a head as soon as it can be told apart from another, and one
    half is enough for that: a mailbox whose folder was never recorded has one,
    and so does an import, which names a folder and no mailbox because there is
    no mailbox behind it. Only an entry that names *neither* has nothing to be
    the head of. That never comes from a backup -- the job name is always there
    -- and the type allows it, so it is answered rather than assumed away.
    """
    if mailbox is None and folder is None:
        return None
    return heads.read(heads_root, mailbox, folder) or heads.Head(job=mailbox, folder=folder)


def _move_head(heads_root: pathlib.Path, head: heads.Head | None, hashval: str) -> None:
    """Point a place's head at the file that now holds it.

    Written *after* the log file, never before: an interrupt between the two
    leaves a file no chain reaches, which `archive check` reports and which costs
    nothing -- the file is still read, because the glob and not the chain is how
    the log is enumerated. The other order would leave a head naming a file that
    was never written, which is the same shape as real loss.

    A head that cannot be written is logged and tolerated, for the same reason a
    resume point that cannot be written is: the observations are already safe in
    the log, and giving up here would cost the rest of the run for a failure that
    costs no mail.
    """
    if head is None:
        return
    head.log = hashval
    try:
        heads.write(heads_root, head)
    except OSError as exc:
        log.error(
            "%s: log chain head not written: %s",
            heads.place_name(head.job, head.folder),
            exc,
        )


class LogWriter:
    """Collects observations and seals them into one file per (mailbox, folder).

    Nothing is written until `seal`, so an interrupted pass leaves no partial
    file behind and the log never contains a half-observed place. Entries are
    held as bare store ids, so even a whole-archive export costs roughly what the
    files it produces will cost.

    `heads_root` is where each place's chain head is kept, so that sealing can
    link the new file to the one it follows and move the head on. It is a
    separate argument rather than derived from `root`, because the two are
    independent facts about an archive and guessing one from the other is how a
    layout ends up written down twice.
    """

    def __init__(self, root: pathlib.Path, heads_root: pathlib.Path):
        self.root = root
        self.heads_root = heads_root
        self._places: dict[Place, list[str]] = {}

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

        A name given twice counts once. Callers assemble the list from more than
        one source -- the IMAP backend adds the folder it is reading to the
        labels the server reported -- and the same place named twice would file
        the message twice in one file.
        """
        names: list[str | None] = []
        for folder in folders:
            name = as_text(folder)
            if name not in names:
                names.append(name)
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
            self._places.items(),
            key=lambda item: (item[0][0] or "", item[0][1] or ""),
        ):
            head = _head_of(self.heads_root, mailbox, folder)
            _status, hashval, path = store.add(
                _serialize(mailbox, folder, date.isoformat(), store_ids, prev=_chain(head))
            )
            log.debug(
                "%s: %s message(s) in %s",
                where(path),
                len(store_ids),
                heads.place_name(mailbox, folder),
            )
            _move_head(self.heads_root, head, hashval)
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
        log.warning("%s:%d: incomplete line, skipped", where(path), number)
        return None
    if not isinstance(data, dict):
        log.warning("%s:%d: not an object, skipped", where(path), number)
        return None
    store_id = data.get("store_id")
    if not isinstance(store_id, str) or not store_id:
        log.warning("%s:%d: no usable store_id, skipped", where(path), number)
        return None
    if not cas.is_hashval(store_id):
        # The store cuts a path out of a store id and refuses one that is not a
        # hash -- rightly, since `../..` would climb out of it. Here that value
        # came out of a file which is allowed to be damaged, so it is a line to
        # skip like any other unusable one. Letting it through would hand the
        # refusal to whoever asks the store next, and cost them the whole folder
        # they were reading for one broken line.
        log.warning("%s:%d: store_id is not a hash, skipped", where(path), number)
        return None
    return store_id


def _parse_header(path: pathlib.Path, line: str) -> dict | None:
    """Decode a log file's first line, or None when it cannot be used.

    Without a usable header the lines below it have no place to belong to, so
    every caller here treats None as "skip this file".
    """
    try:
        header = json.loads(line)
    except json.JSONDecodeError as exc:
        log.warning("%s: unreadable header, skipped: %s", where(path), exc)
        return None
    if not isinstance(header, dict):
        log.warning("%s: header is not an object, skipped", where(path))
        return None
    if header.get("version") not in SUPPORTED_LOG_VERSIONS:
        log.warning(
            "%s: log version %r is not one of %s, skipped -- it was written by a"
            " different mailvault version; upgrade mailvault to read it",
            where(path),
            header.get("version"),
            ", ".join(str(v) for v in SUPPORTED_LOG_VERSIONS),
        )
        return None
    return header


def read_header(path: pathlib.Path) -> dict | None:
    """Read only a log file's header, without its message lines.

    For the questions the header alone answers -- which mailbox, which folder,
    when -- and where the message lines would be read and thrown away. No
    integrity check is possible this way: the file's name is the hash of all of
    it, so verifying means reading all of it. Use `read_log` where that matters.
    """
    try:
        with path.open("rb") as f:
            first = f.readline()
    except OSError as exc:
        log.warning("%s: unreadable, skipped: %s", where(path), exc)
        return None
    if not first:
        log.warning("%s: empty, skipped", where(path))
        return None
    try:
        line = first.decode("utf-8")
    except UnicodeDecodeError as exc:
        log.warning("%s: not valid UTF-8, skipped: %s", where(path), exc)
        return None
    return _parse_header(path, line)


def mailboxes(root: pathlib.Path) -> set[str]:
    """The mailbox names below `root`, from the headers alone.

    A log file names its mailbox in its first line, so "who has written into this
    archive" costs one line per file rather than the whole log.
    """
    names = set()
    for path in log_files(root):
        header = read_header(path)
        if header is None:
            continue
        mailbox = header.get("mailbox")
        if isinstance(mailbox, str) and mailbox:
            names.add(mailbox)
    return names


def read_log(path: pathlib.Path) -> LogFile | None:
    """Read one log file, returning None when it cannot be used at all.

    Individual damaged lines are skipped; only an unreadable header discards the
    whole file, because without it the lines have no place to belong to.
    """
    try:
        raw = path.read_bytes()
    except OSError as exc:
        log.warning("%s: unreadable, skipped: %s", where(path), exc)
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
        log.warning("%s: damaged -- content does not match its name", where(path))

    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        log.warning("%s: not valid UTF-8, skipped: %s", where(path), exc)
        return None
    if not lines:
        log.warning("%s: empty, skipped", where(path))
        return None
    header = _parse_header(path, lines[0])
    if header is None:
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
            where(path),
            declared,
            len(store_ids),
        )

    prev = header.get("prev")
    return LogFile(
        path=path,
        mailbox=mailbox if isinstance(mailbox, str) else None,
        folder=folder if isinstance(folder, str) else None,
        date=date if isinstance(date, str) else None,
        store_ids=store_ids,
        prev=prev if isinstance(prev, str) and cas.is_hashval(prev) else None,
    )


def read_all(root: pathlib.Path) -> collections.abc.Iterator[LogFile]:
    """Yield every readable log file below `root`, in replay order."""
    for path in log_files(root):
        entry = read_log(path)
        if entry is not None:
            yield entry


@dataclasses.dataclass
class PlaceSummary:
    """One place, as somebody asking what is in this archive wants it."""

    mailbox: str | None
    folder: str | None
    messages: int
    last_seen: str | None


@dataclasses.dataclass
class LogSummary:
    """Every place the log knows, and how much mail it accounts for altogether.

    `messages` is not the sum of the places' counts and is usually smaller: a
    message filed under three Gmail labels lies at three places and is one
    message. Both numbers are wanted -- per place to know where to look, in total
    to hold against what the archive holds -- and a reader who adds the column up
    has to be told why it comes out higher.
    """

    places: list[PlaceSummary]
    messages: int


def summarize(root: pathlib.Path) -> LogSummary:
    """Read the whole log into one line per place.

    The counts are of *distinct* messages, which is why this reads the files
    rather than their headers. A header carries the count of its own file, and
    the same message is written again whenever a folder is read in full instead
    of resumed -- summing those would report an archive larger than it is, and a
    number that is quietly an upper bound is worse than no number.

    It holds the union of every place's store ids while it runs, the same
    structure `compact` holds and for the same reason. That is the one thing here
    that grows with the archive rather than with the number of places.

    What comes out are the places mail was *seen* in, which is what
    `db search --folder` matches. A place that has only a resume point and no
    observations is not among them -- Gmail has such places, where the folder a
    job polls and the label the server reports are two names for one thing.
    """
    ids: dict[Place, set[str]] = {}
    dates: dict[Place, str] = {}
    for logfile in read_all(root):
        ids.setdefault(logfile.place, set()).update(logfile.store_ids)
        seen = dates.get(logfile.place)
        if logfile.date is not None and (seen is None or logfile.date > seen):
            dates[logfile.place] = logfile.date
    places = [
        PlaceSummary(
            mailbox=mailbox,
            folder=folder,
            messages=len(ids[(mailbox, folder)]),
            last_seen=dates.get((mailbox, folder)),
        )
        # A place with no mailbox comes last: what an import or `archive adopt`
        # brought in is the exception, and the mailboxes are what an archive is
        # mostly made of.
        for mailbox, folder in sorted(ids, key=lambda p: (p[0] is None, p[0] or "", p[1] or ""))
    ]
    return LogSummary(places=places, messages=len(set().union(*ids.values())) if ids else 0)


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


def compact(root: pathlib.Path, heads_root: pathlib.Path) -> CompactResult:
    """Consolidate the log into one file per place, dropping duplicate entries.

    A place accumulates many small files -- one per backup that put mail there --
    and their store ids repeat wherever a folder was read in full rather than
    resumed, because such a pass records everything it finds. This reads them all,
    writes one file per (mailbox, folder) holding the sorted union of that place's
    store ids, verifies the new files landed, and only then removes the originals.

    Crash-safe by ordering: on an interrupt the originals are still there, a read
    takes the union regardless so nothing is lost, and a rerun finishes the job. A
    file that cannot be read is left in place rather than folded away, so damaged
    data is never silently dropped. Producing byte-identical files for the same
    content (via `_serialize`) makes a second run a no-op.

    Each consolidated file **starts its place's chain over**: it holds everything
    that came before, so pointing back at files that are about to be removed
    would name what is deliberately gone. The heads are moved on before the
    originals are dropped, so an interrupt in between leaves duplicates and a
    chain that reaches the new file -- never a head naming something that was
    never written.
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
    roots: list[tuple[Place, str]] = []
    for key in sorted(places, key=lambda k: (k[0] or "", k[1] or "")):
        mailbox, folder = key
        store_ids = sorted(places[key])
        result.entries_after += len(store_ids)
        _status, hashval, path = store.add(
            _serialize(mailbox, folder, dates.get(key), store_ids, prev=None)
        )
        written.add(path)
        roots.append((key, hashval))

    # Verify the consolidated files landed before removing anything.
    if not all(verify_file(path) for path in written):
        log.error("consolidated files did not verify, originals left in place")
        result.verified = False
        result.files_after = len(log_files(root))
        return result

    # And move each place's head onto its new root before anything is removed --
    # a head still naming a file that has just been deleted is the one state
    # worth avoiding here.
    for (mailbox, folder), hashval in roots:
        _move_head(heads_root, _head_of(heads_root, mailbox, folder), hashval)

    # Drop the originals we consolidated, but never one byte-identical to a file
    # just written (an already-compact place produces the same hash).
    for path in consumed:
        if path not in written:
            utils.remove_file(path, missing_ok=True)

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
