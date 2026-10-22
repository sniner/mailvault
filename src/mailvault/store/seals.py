"""Seals: the log files an archive held at one moment, under a name that proves it.

A seal lists every log file in `meta/` when it is made. Its name is the hash of
its content, each log file is named by the hash of its own, and each log file
names messages by the hash of theirs. So the name of a seal, the seal id, fixes
everything below it. Whoever keeps the id outside the archive can later show that
the archive still holds every log file and every message it held then, unchanged.

Seals lie in `meta/` next to the log, as `<id>.seal`, written through a second
content-addressed store on the same directory. Everything that reads the log
looks for `*/*.jsonl` (`metalog.log_files`), so a seal is invisible to it, and to
older versions of mailvault as well.

A seal is never replaced or removed by mailvault. While one exists, nothing may
remove or rename what it records: `refuse_if_sealed` is the check for that.

File content, a header line and then one line per log file, sorted:

    {"version": 1, "date": "2026-10-22T02:15:07+00:00", "hash": "sha256"}
    {"log": "14782a294b60681337721378b2db06a03dd03dc9338a617d68724fac56cdb39c"}
"""

from __future__ import annotations

import collections.abc
import dataclasses
import enum
import json
import logging
import pathlib
from datetime import datetime

from mailvault import utils
from mailvault.store import cas

log = logging.getLogger(__name__)

SUFFIX = ".seal"

# A reader rejects a version it does not know rather than misread it, the same
# rule the log follows.
SEAL_VERSION = 1
SUPPORTED_SEAL_VERSIONS = (SEAL_VERSION,)


class Unusable(enum.Enum):
    """Why a seal cannot be used.

    The three call for different moves. A seal that is gone or damaged means the
    archive is no longer what the seal recorded; one written by a newer version
    means this version is too old to read it.
    """

    GONE = "not in this archive"
    DAMAGED = "damaged"
    NEWER = "written by a newer mailvault"


@dataclasses.dataclass(frozen=True)
class Seal:
    """One seal: its id, when it was made, and the log files it records."""

    seal_id: str
    date: datetime
    hash_name: str
    logs: frozenset[str]

    @property
    def when(self) -> str:
        """The date as every report prints it, to the second and with its zone."""
        return self.date.strftime("%Y-%m-%d %H:%M:%S %Z")


@dataclasses.dataclass(frozen=True)
class SealFile:
    """A file in `meta/` that is a seal, and what could be read from it."""

    path: pathlib.Path
    seal: Seal | None
    unusable: Unusable | None

    @property
    def seal_id(self) -> str:
        """The id the file claims, which for an unusable seal is all there is."""
        return self.path.name.removesuffix(SUFFIX)


class Sealed(Exception):
    """Raised instead of removing or renaming what a seal records.

    Its message is the refusal the user reads, whichever command ran into it,
    the way a format error's is: this module is what knows why.
    """

    def __init__(self, action: str, where: str, count: int, newest: Seal | None):
        newest_part = f", the newest of {newest.when}" if newest is not None else ""
        super().__init__(
            f"this archive is sealed ({utils.counted(count, 'seal')}{newest_part}):"
            f" {action} would change what the seals record, and their ids could no"
            f" longer be checked. To go ahead anyway, delete the .seal files in {where}"
            f" first; every seal id given out so far then fails its check"
        )
        self.count = count
        self.newest = newest


def open_store(meta_root: pathlib.Path) -> cas.ContentAddressedStorage:
    """Open the store seals are written to: the log's directory, another suffix."""
    return cas.ContentAddressedStorage(meta_root, suffix=SUFFIX, depth=1)


def seal_files(meta_root: pathlib.Path) -> list[pathlib.Path]:
    """Every seal file below `meta_root`, in a stable order."""
    if not meta_root.is_dir():
        return []
    return sorted(p for p in meta_root.glob(f"*/*{SUFFIX}") if p.is_file())


def write(
    meta_root: pathlib.Path,
    logs: collections.abc.Iterable[str],
    date: datetime,
) -> Seal:
    """Write a seal over the log files named `logs`, made at `date`, and return it.

    The date is cut to the second: every verdict about the seal prints it, and
    nobody checking a seal has a use for microseconds. It must carry its zone,
    or the time it names is not a time.
    """
    if date.tzinfo is None:
        raise ValueError("a seal date needs a time zone")
    date = date.replace(microsecond=0)
    names = frozenset(logs)
    _status, seal_id, _path = open_store(meta_root).add(_serialize(names, date))
    return Seal(seal_id, date, cas.current_hash(), names)


def find(meta_root: pathlib.Path, seal_id: str) -> tuple[Seal | None, Unusable | None]:
    """The seal with this id, or why there is none to use."""
    store = open_store(meta_root)
    path = store.locate(seal_id, exists=True)
    if path is None:
        return None, Unusable.GONE
    return _examine(store, path)


def all_seals(meta_root: pathlib.Path) -> list[SealFile]:
    """Every seal file, the usable ones oldest first, the others after them.

    A seal that cannot be used is listed rather than left out: a list that drops
    one would hide exactly the kind of change seals are there to show.
    """
    files = seal_files(meta_root)
    if not files:
        return []
    store = open_store(meta_root)
    found = [SealFile(path, *_examine(store, path)) for path in files]
    usable = sorted(
        ((seal, f) for f in found if (seal := f.seal) is not None),
        key=lambda pair: (pair[0].date, pair[0].seal_id),
    )
    return [f for _seal, f in usable] + [f for f in found if f.seal is None]


def refuse_if_sealed(meta_root: pathlib.Path, action: str) -> None:
    """Raise Sealed when `meta_root` holds a seal; `action` names what was refused.

    A damaged seal counts as well. Somebody may hold its id, and what it recorded
    is exactly what must not change underneath it.
    """
    found = all_seals(meta_root)
    if not found:
        return
    usable = [f.seal for f in found if f.seal is not None]
    newest = usable[-1] if usable else None
    raise Sealed(action, f"{meta_root.name}/", len(found), newest)


def _serialize(logs: frozenset[str], date: datetime) -> bytes:
    header = {"version": SEAL_VERSION, "date": date.isoformat(), "hash": cas.current_hash()}
    body = json.dumps(header) + "\n"
    body += "".join(json.dumps({"log": name}) + "\n" for name in sorted(logs))
    return body.encode("utf-8")


def _examine(
    store: cas.ContentAddressedStorage,
    path: pathlib.Path,
) -> tuple[Seal | None, Unusable | None]:
    """Read one seal file, and say why when it cannot be used.

    The content is held against the name before anything in it is read. The id
    covers every byte, so a seal whose content does not match it is damaged as a
    whole, and nothing in it is trusted, its date included.
    """
    seal_id = store.hashval_of(path)
    if seal_id is None:
        return None, Unusable.DAMAGED
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None, Unusable.GONE
    except OSError as exc:
        log.warning("%s: unreadable: %s", store.where(path), exc)
        return None, Unusable.DAMAGED
    if cas.digest(raw) != seal_id:
        return None, Unusable.DAMAGED
    return _parse(seal_id, raw)


def _parse(seal_id: str, raw: bytes) -> tuple[Seal | None, Unusable | None]:
    """Decode a seal whose content matches its name.

    A matching seal that cannot be decoded was written that way, by another
    program or a newer version. Only an unknown version says which: anything
    else is damage.
    """
    try:
        lines = raw.decode("utf-8").splitlines()
        header = json.loads(lines[0]) if lines else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, Unusable.DAMAGED
    if not isinstance(header, dict):
        return None, Unusable.DAMAGED
    if header.get("version") not in SUPPORTED_SEAL_VERSIONS:
        return None, Unusable.NEWER
    try:
        date = datetime.fromisoformat(header["date"])
        hash_name = header["hash"]
        logs = frozenset(_log_name(line) for line in lines[1:])
    except (KeyError, TypeError, ValueError):
        return None, Unusable.DAMAGED
    if date.tzinfo is None or hash_name not in cas.HASHES:
        return None, Unusable.DAMAGED
    return Seal(seal_id, date, hash_name, logs), None


def _log_name(line: str) -> str:
    """The log file one line of a seal names. Raises ValueError for anything else."""
    entry = json.loads(line)
    name = entry["log"] if isinstance(entry, dict) else None
    if not isinstance(name, str) or not cas.is_hashval(name):
        raise ValueError(f"not a log file name: {line!r}")
    return name
