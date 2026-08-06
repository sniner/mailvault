"""Content-addressed storage: files named by the hash of their content.

The discipline underneath both the mail store (the `.eml` files) and the metadata
log. A file's name is its own hash, so it is written once, never modified, and
carries its own integrity check; adding the same bytes twice is a no-op. Entries
can be transparently zstd-compressed (through `mailvault.store.zstd`), and the
header of a stored message can be read without pulling the whole body.

Three rules keep an entry from ever being seen half-written. It is written to a
transient file and renamed into place only once complete -- a rename within one
directory is atomic on every filesystem in practical use, so a reader sees the
whole entry or nothing at all. That transient file's name belongs to one writer
alone, so two runs storing the same message at the same time cannot write into
the same file and rename the mixture into place. And the content reaches the
device before the rename, the directory entry naming it after it, so a power cut
cannot publish a name whose content never arrived.
"""

from __future__ import annotations

import collections.abc
import contextlib
import dataclasses
import hashlib
import io
import itertools
import logging
import os
import pathlib
import re
import time
from typing import Any

from mailvault.store import atomic, zstd

log = logging.getLogger(__name__)

# The hash entries are named after, unless a caller picks another one. Defined
# here because the store owns that decision: the metadata log checks its files
# against the same algorithm, and a second copy of the choice is a breakage
# waiting for the day the first one changes.
DEFAULT_HASH: collections.abc.Callable[..., hashlib._Hash] = hashlib.sha384

# Suffix of the transient file an entry is written to, shared with
# `mailvault.store.atomic` so both write disciplines look the same on disk.
TEMP_SUFFIX = atomic.TEMP_SUFFIX

# A hash is hexadecimal and nothing else. Anything else is a mix-up, and one
# that would otherwise be cut into directory names and followed.
_HEX = re.compile(r"[0-9a-fA-F]+\Z")

# Distinguishes the transient files of one process. Reading it is atomic, so
# threads of one process get distinct names too.
_serial = itertools.count()

# A name collision means a transient file of an earlier run with this process's
# pid is still lying there. Retrying a few times gets past it.
_TEMP_ATTEMPTS = 100

# How long a transient file has to have been lying around before a cleanup pass
# may remove it. One is open for as long as a single entry takes to write, so
# even a very large message is orders of magnitude below this; anything this old
# belongs to a run that is over. Generous on purpose -- the cost of waiting a day
# is a few kilobytes, the cost of being wrong is another writer's entry.
TRANSIENT_MIN_AGE = 24 * 60 * 60


def is_hashval(value: str) -> bool:
    """True when `value` has the shape of a name entries are filed under.

    The lenient counterpart to `normalize_hashval`, for a value that comes out
    of a file which is allowed to be damaged. The metadata log is such a file:
    a store id it cannot offer is a line to skip, not a caller to correct.
    """
    return _HEX.match(value) is not None


def normalize_hashval(hashval: str) -> str:
    """Check that `hashval` is a hash and return it in the form entries use.

    Store ids come back from the database, from the metadata log and from the
    command line, and a path is derived from one by cutting it into directory
    names. Something that is not a hash has no business becoming a path:
    `../..` would cut into components that climb out of the store entirely.
    Rejected here, at the one place such a value enters.
    """
    if not is_hashval(hashval):
        raise ValueError(f"not a hash: {hashval!r}")
    return hashval.lower()


def _checked_depth(
    depth: int,
    hashfactory: collections.abc.Callable[..., hashlib._Hash],
) -> int:
    """A shard depth this hash can actually be cut into.

    A depth deeper than the hash is long would otherwise be accepted here and
    fail on every single write; a negative one used to be silently turned into
    the default, which hides a caller's mistake rather than reporting it.
    """
    if depth < 0:
        raise ValueError(f"depth must not be negative: {depth}")
    available = len(hashfactory().hexdigest())
    if depth * 2 > available:
        raise ValueError(
            f"depth {depth} needs {depth * 2} hex characters,"
            f" but this hash produces only {available}"
        )
    return depth


def _header_end(buf: bytes | bytearray, start: int = 0) -> int | None:
    """Index just past the blank line that ends a message's headers, or None."""
    ends = [
        found + len(separator)
        for separator in (b"\r\n\r\n", b"\n\n")
        if (found := buf.find(separator, start)) >= 0
    ]
    return min(ends) if ends else None


def _open_transient(destination: pathlib.Path) -> tuple[pathlib.Path, io.BufferedWriter]:
    """Create a file next to `destination` that no other writer can be using."""
    for _ in range(_TEMP_ATTEMPTS):
        tmp_path = destination.with_name(
            f"{destination.name}.{os.getpid()}-{next(_serial)}{TEMP_SUFFIX}"
        )
        try:
            return tmp_path, tmp_path.open("xb")
        except FileExistsError:
            continue
    raise OSError(f"{destination}: no unused transient file name")


@contextlib.contextmanager
def _writing_to(destination: pathlib.Path) -> collections.abc.Iterator[io.BufferedWriter]:
    """Yield a file to write, and rename it onto `destination` on success.

    The transient file is created in the destination's own directory, because a
    rename is only atomic within one filesystem, and under a name that belongs
    to this process and this call alone. A name derived from the destination
    would be enough for a single writer; two runs storing the same message at
    the same time would write into one file and rename the mixture into place.

    Both halves of the durability are unconditional, and their order is the
    point. The content reaches the device *before* the rename, or a power cut
    can publish a file under a name that claims to be the hash of bytes which
    never arrived -- and nothing would ever find out, because the store answers
    "is this entry here?" by looking at names. The directory entry follows the
    rename, or the bytes survive with nothing pointing at them.

    Neither is optional, because neither failure is recoverable by fetching the
    message again: an entry that is there but wrong is indistinguishable from a
    good one, and an entry that is gone was already accounted for by the run
    that stored it.

    One gap is left open deliberately. When this entry is the first of its
    shard, the directory holding it is itself a new name in the directory above,
    and that one is not flushed here -- what is synced is the shard, not its
    parent. Every filesystem in practical use commits the creation along with
    the transaction this sync forces; POSIX does not say it has to. Closing it
    properly would mean a second sync per shard, for a window that only exists
    for the first entry to land in one of 65,536 directories.

    Nothing is left behind when the write fails: the transient file goes and
    the destination is untouched.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp_path, f = _open_transient(destination)
    renamed = False
    try:
        with f:
            yield f
            f.flush()
            os.fsync(f.fileno())
        tmp_path.replace(destination)
        renamed = True
    finally:
        if not renamed:
            log.debug("%s: write failed, removing transient file", destination)
            tmp_path.unlink(missing_ok=True)

    atomic.sync_directory(destination.parent)


@dataclasses.dataclass
class ConversionResult:
    """What a pass of `compress_all` or `decompress_all` did.

    `failed` names the entries the pass could not convert. One unreadable file
    does not stop a maintenance run over a whole archive, but it must not
    disappear into the log either: a caller that only counts what worked
    reports success over a store it has just failed to convert.
    """

    converted: int = 0
    skipped: int = 0
    failed: list[pathlib.Path] = dataclasses.field(default_factory=list)


class ContentAddressedStorage:
    """A hash-named store under `root_dir`, sharded `depth` levels deep.

    `add` writes new content, returning EXISTS for content already present;
    `read` and `read_header` read it back, decompressing `.zst` entries
    transparently; `verify` checks an entry against the name it is filed under.
    """

    def __init__(
        self,
        root_dir: str | pathlib.Path = ".",
        suffix: str | None = None,
        depth: int = 2,
        compress: bool = False,
        hashfactory: collections.abc.Callable[..., hashlib._Hash] | None = None,
    ):
        self.root_dir = pathlib.Path(root_dir)
        pathlib.Path.mkdir(self.root_dir, parents=True, exist_ok=True)
        self.compress = compress
        self.hashfactory = hashfactory if hashfactory else DEFAULT_HASH
        self.depth = _checked_depth(depth, self.hashfactory)
        self.suffix = suffix
        self.blocksize = 16384
        if self.compress:
            zstd.require()  # fail fast if no zstd implementation is available

    @property
    def suffix(self) -> str:
        return self._suffix

    @suffix.setter
    def suffix(self, value: str | None) -> None:
        if value:
            self._suffix = value.strip()
            if not self._suffix.startswith("."):
                self._suffix = "." + self._suffix
        else:
            self._suffix = ".dat"

    def _subdirs(self, hashval: str) -> list[str]:
        if len(hashval) < self.depth * 2:
            raise ValueError(f"hash string to short, {self.depth * 2} characters required")
        return [hashval[i : i + 2] for i in range(0, self.depth * 2, 2)]

    def _path(self, hashval: str) -> pathlib.Path:
        return pathlib.Path(self.root_dir, *self._subdirs(hashval))

    def _reader(self, data: io.IOBase | bytes) -> io.IOBase:
        if isinstance(data, bytes):
            reader = io.BytesIO(data)
        elif isinstance(data, io.IOBase):
            if data.seekable():
                reader = data
                reader.seek(0)
            else:
                blob = data.read()
                if not isinstance(blob, bytes):
                    raise TypeError("read() has to return bytes")
                reader = io.BytesIO(blob)
        else:
            raise TypeError("instance of bytes or io.IOBase expected")
        return reader

    def _copy(self, reader: Any, write: collections.abc.Callable[[bytes], object]) -> None:
        """Pump everything from `reader` into `write`, block by block.

        Takes the bound method rather than the object, so the same loop feeds a
        file, a compressor and a hasher -- which spell it `write` and `update`.
        """
        while block := reader.read(self.blocksize):
            write(block)

    def _hashval(self, reader: io.IOBase) -> str:
        m = self.hashfactory()
        self._copy(reader, m.update)
        reader.seek(0)
        return m.hexdigest()

    def hashval(self, data: bytes) -> str:
        """The name this content would be stored under, without storing it."""
        return self.hashfactory(data).hexdigest()

    def hashval_of(self, path: pathlib.Path) -> str | None:
        """The hash a file name claims, or None when the file is not an entry."""
        for tail in (self.suffix + ".zst", self.suffix):
            if path.name.endswith(tail):
                stem = path.name[: -len(tail)]
                return stem.lower() if _HEX.match(stem) else None
        return None

    def _destination(self, hashval: str) -> tuple[pathlib.Path, str]:
        filename = hashval + self.suffix
        if self.compress:
            filename += ".zst"
        path = self._path(hashval)
        return path, filename

    def _find_existing(self, hashval: str) -> pathlib.Path | None:
        """Find an existing file for this hash, regardless of compression."""
        path = self._path(hashval)
        for candidate in (
            path / (hashval + self.suffix),
            path / (hashval + self.suffix + ".zst"),
        ):
            if candidate.is_file():
                return candidate
        return None

    def add(self, data: io.IOBase | bytes) -> tuple[str, str, pathlib.Path]:
        reader = self._reader(data)
        hashval = self._hashval(reader)
        existing = self._find_existing(hashval)
        if existing:
            log.debug(f"{existing}: already exists")
            return "EXISTS", hashval, existing
        path, filename = self._destination(hashval)
        file = path / filename
        with _writing_to(file) as f:
            if self.compress:
                # The compressor is closed -- its frame flushed into f -- while f
                # is still open, so the flush covers the whole compressed file.
                with zstd.open_writer(f) as compressor:
                    self._copy(reader, compressor.write)
            else:
                self._copy(reader, f.write)
        log.debug(f"{file}: new entry")
        return "NEW", hashval, file

    @contextlib.contextmanager
    def _reading(self, path: pathlib.Path) -> collections.abc.Iterator[Any]:
        """Yield a reader over an entry, decompressing `.zst` transparently."""
        if path.suffix == ".zst":
            with path.open("rb") as f, zstd.open_reader(f) as reader:
                yield reader
        else:
            with path.open("rb") as f:
                yield f

    def _read_until_header_end(self, reader: Any, limit: int) -> bytes:
        """Pull blocks off `reader` until the headers are complete."""
        buf = bytearray()
        while len(buf) < limit:
            block = reader.read(self.blocksize)
            if not block:
                break
            # Resume the search three bytes back so a separator split across two
            # blocks is still found.
            start = max(0, len(buf) - 3)
            buf += block
            end = _header_end(buf, start)
            if end is not None:
                return bytes(buf[:end])
        # A message with no blank line in it is cut at the limit rather than
        # wherever the last block happened to end, so the caller gets what it
        # asked for and not up to one block more.
        return bytes(buf[:limit])

    def read_header(self, path: pathlib.Path, limit: int = 1 << 20) -> bytes:
        """Read only the header block of a stored message.

        Headers are a few kilobytes; the message behind them can be tens of
        megabytes. Anything that only needs the headers -- matching a Message-ID,
        building a database -- would otherwise pull whole attachments off the disk
        or over the network to throw them away. On the reference archive the
        headers are 4.8% of the bytes.

        Stops at the blank line that separates headers from body, or at `limit`
        for a message that has no such line.
        """
        with self._reading(path) as reader:
            return self._read_until_header_end(reader, limit)

    def read(self, path: pathlib.Path) -> bytes:
        """Read file content, decompressing transparently if needed."""
        with self._reading(path) as reader:
            data: bytes = reader.read()
        return data

    def verify(self, path: pathlib.Path) -> bool:
        """True when an entry's content still matches the name it is filed under.

        The guarantee the whole design exists for, and the one thing no syntax
        check can give: a name that is a hash of the content catches bit rot, a
        truncated write and a botched restore alike, without a second copy or a
        checksum file to keep in sync. Reads the entry in blocks, so verifying a
        large message costs no more memory than storing one did.

        Raises ValueError for a file that is not an entry of this store -- there
        is no name to check the content against.
        """
        claimed = self.hashval_of(path)
        if claimed is None:
            raise ValueError(f"not an entry of this store: {path}")
        m = self.hashfactory()
        with self._reading(path) as reader:
            self._copy(reader, m.update)
        return m.hexdigest() == claimed

    def locate(
        self,
        data: io.IOBase | bytes | str,
        exists: bool = False,
    ) -> pathlib.Path | None:
        if isinstance(data, str):
            hashval = normalize_hashval(data)
        else:
            hashval = self._hashval(self._reader(data))
        if exists:
            return self._find_existing(hashval)
        path, filename = self._destination(hashval)
        return path / filename

    def _convert_all(
        self,
        skip_suffix: str,
        target_fn: collections.abc.Callable[[pathlib.Path], pathlib.Path],
        converter: collections.abc.Callable[..., object],
        operation: str,
    ) -> ConversionResult:
        """Convert all files in the store."""
        zstd.require()  # one clear failure rather than one per entry
        result = ConversionResult()
        for path in self.walk():
            if path.suffix == skip_suffix:
                result.skipped += 1
                continue
            try:
                with path.open("rb") as src, _writing_to(target_fn(path)) as dst:
                    converter(src, dst)
                path.unlink()
            except Exception as exc:
                # Deliberately broad: a codec error is not an OSError, and one
                # damaged entry must not stop a pass over a whole archive. The
                # file is named in the result, so the caller can still tell.
                log.error(f"{path}: {operation} failed: {exc}")
                result.failed.append(path)
            else:
                result.converted += 1
        return result

    def compress_all(self) -> ConversionResult:
        """Compress all uncompressed files in the store."""
        return self._convert_all(
            skip_suffix=".zst",
            target_fn=lambda p: p.with_suffix(p.suffix + ".zst"),
            converter=zstd.compress_stream,
            operation="compression",
        )

    def decompress_all(self) -> ConversionResult:
        """Decompress all compressed files in the store."""
        return self._convert_all(
            skip_suffix=self.suffix,
            target_fn=lambda p: p.with_suffix(""),
            converter=zstd.decompress_stream,
            operation="decompression",
        )

    def _transient_origin(self, path: pathlib.Path) -> str | None:
        """The store id a transient file was being written for, or None.

        What makes a leftover this store's to remove. A directory tree collects
        other people's transient files too -- `state.json` and `index.db` are
        replaced through one of their own -- and a store that swept every name
        ending in the suffix would delete files it never wrote.
        """
        name = path.name
        if not name.endswith(TEMP_SUFFIX):
            return None
        destination, _, serial = name[: -len(TEMP_SUFFIX)].rpartition(".")
        if not destination or not serial:
            return None
        return self.hashval_of(path.with_name(destination))

    def prune_transient_files(self, min_age: float = TRANSIENT_MIN_AGE) -> int:
        """Remove this store's leftover transient files, if they are old enough.

        A write that is interrupted leaves one behind, and because the name
        belongs to one writer, no later run reuses or overwrites it -- so
        nothing ever removes them. They are inert (`walk` does not yield them,
        `add` never looks at them), which is exactly why they would go on
        collecting unseen.

        Only ones old enough that no live writer can still hold them. Age is the
        only criterion available: the pid in the name says nothing about a
        process on another host, and an archive is reachable from more than one.

        Returns how many went away -- worth reporting rather than doing
        quietly, because such a file is the one trace an interrupted write
        leaves behind.
        """
        removed = 0
        cutoff = time.time() - min_age
        for path, _, files in os.walk(self.root_dir):
            for fname in files:
                entry = pathlib.Path(path, fname)
                if self._transient_origin(entry) is None:
                    continue
                try:
                    if entry.stat().st_mtime > cutoff:
                        continue
                    entry.unlink()
                except OSError as exc:
                    log.debug(f"{entry}: kept: {exc}")
                    continue
                log.info(f"{entry}: leftover of an interrupted write, removed")
                removed += 1
        return removed

    def prune_empty_dirs(self) -> int:
        """Remove the shard directories that no longer hold anything.

        A shard directory is created the first time an entry hashes into it, and
        nothing removes it when the last entry leaves again. A store that only
        grows never notices; one whose entries can go -- the metadata log, after
        compaction -- is left with a skeleton of empty directories.

        Cleanup only, and one that cannot take data with it: `rmdir` refuses a
        directory that still holds anything, so a shard is removed exactly when
        it is empty, including when it went empty between the walk and the call.
        The root itself always stays. Returns how many directories went away.
        """
        removed = 0
        for path, _, _ in os.walk(self.root_dir, topdown=False):
            directory = pathlib.Path(path)
            if directory == self.root_dir:
                continue
            try:
                directory.rmdir()
            except OSError as exc:
                log.debug(f"{directory}: kept: {exc}")
                continue
            removed += 1
        return removed

    def walk(self) -> collections.abc.Generator[pathlib.Path, None, None]:
        """Yield every entry in the store.

        Only files whose name really is a hash plus this store's suffix. A store
        is a directory tree, and directory trees collect things: a `.DS_Store`, a
        message somebody copied in by hand under its subject, the transient file
        of a run that was interrupted. Whoever walks the store turns the name back
        into a store id -- `create-db` does -- so a file that is not an entry
        would become a database row pointing at a message that does not exist.
        """
        for path, _, files in os.walk(self.root_dir):
            for fname in files:
                entry = pathlib.Path(path, fname)
                if self.hashval_of(entry) is not None:
                    yield entry
