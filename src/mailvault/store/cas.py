"""Content-addressed storage: files named by the hash of their content.

The discipline underneath both the mail store (the `.eml` files) and the metadata
log. A file's name is its own hash, so it is written once, never modified, and
carries its own integrity check; adding the same bytes twice is a no-op. Entries
can be transparently zstd-compressed (through `mailvault.store.zstd`), and the
header of a stored message can be read without pulling the whole body.
"""

from __future__ import annotations

import collections.abc
import hashlib
import io
import logging
import os
import pathlib
from typing import Any

from mailvault.store import zstd

log = logging.getLogger(__name__)


def _header_end(buf: bytes | bytearray, start: int = 0) -> int | None:
    """Index just past the blank line that ends a message's headers, or None."""
    ends = [
        found + len(separator)
        for separator in (b"\r\n\r\n", b"\n\n")
        if (found := buf.find(separator, start)) >= 0
    ]
    return min(ends) if ends else None


class ContentAddressedStorage:
    """A hash-named store under `root_dir`, sharded `depth` levels deep.

    `add` writes new content, returning EXISTS for content already present;
    `read` and `read_header` read it back, decompressing `.zst` entries
    transparently.
    """

    def __init__(
        self,
        root_dir: str | pathlib.Path = ".",
        suffix: str | None = None,
        depth: int = 2,
        compress: bool = False,
        hashfactory: collections.abc.Callable[..., hashlib._Hash] | None = None,
        fsync: bool = False,
    ):
        self.root_dir = pathlib.Path(root_dir)
        pathlib.Path.mkdir(self.root_dir, parents=True, exist_ok=True)
        self.compress = compress
        self.hashfactory = hashfactory if hashfactory else hashlib.sha384
        self.depth = depth if depth >= 0 else 2
        self.suffix = suffix
        # Flush each entry to the device before it is renamed into place. Off by
        # default: for mail a lost entry is re-fetched on the next run, and one
        # fsync per message is a real cost over a large archive. Callers whose
        # entries are not re-fetchable that cheaply -- the metadata log -- turn
        # it on.
        self.fsync = fsync
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

    def _hashval(self, reader: io.IOBase) -> str:
        m = self.hashfactory()
        while True:
            block = reader.read(self.blocksize)
            if block is None or len(block) == 0:
                break
            m.update(block)
        reader.seek(0)
        return m.hexdigest()

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
            if candidate.exists():
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
        pathlib.Path.mkdir(path, parents=True, exist_ok=True)
        tmp_file = file.with_suffix("._tmp_")
        try:
            if self.compress:
                with open(tmp_file, "wb") as f, zstd.open_writer(f) as compressor:
                    while True:
                        block = reader.read(self.blocksize)
                        if block is None or len(block) == 0:
                            break
                        compressor.write(block)
            else:
                with open(tmp_file, "wb") as f:
                    while True:
                        block = reader.read(self.blocksize)
                        if block is None or len(block) == 0:
                            break
                        f.write(block)
                    if self.fsync:
                        f.flush()
                        os.fsync(f.fileno())
        except Exception as exc:
            log.error(f"{file}: error while writing file: {exc}")
            if tmp_file.exists():
                tmp_file.unlink()
            raise
        else:
            tmp_file.rename(file)
        log.debug(f"{file}: new entry")
        return "NEW", hashval, file

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
        return bytes(buf)

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
        if path.suffix == ".zst":
            with open(path, "rb") as f, zstd.open_reader(f) as reader:
                return self._read_until_header_end(reader, limit)
        with open(path, "rb") as f:
            return self._read_until_header_end(f, limit)

    def read(self, path: pathlib.Path) -> bytes:
        """Read file content, decompressing transparently if needed."""
        if path.suffix == ".zst":
            with open(path, "rb") as f, zstd.open_reader(f) as reader:
                return reader.read()
        else:
            return path.read_bytes()

    def locate(
        self, data: io.IOBase | bytes | str, exists: bool = False
    ) -> pathlib.Path | None:
        if isinstance(data, str):
            hashval = data
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
    ) -> tuple[int, int]:
        """Convert all files in the store. Returns (converted, skipped)."""
        converted = 0
        skipped = 0
        for path in self.walk():
            if path.suffix == skip_suffix:
                skipped += 1
                continue
            target = target_fn(path)
            tmp_file = target.with_suffix("._tmp_")
            try:
                with open(path, "rb") as src, open(tmp_file, "wb") as dst:
                    converter(src, dst)
                tmp_file.rename(target)
                path.unlink()
                converted += 1
            except Exception as exc:
                log.error(f"{path}: {operation} failed: {exc}")
                if tmp_file.exists():
                    tmp_file.unlink()
        return converted, skipped

    def compress_all(self) -> tuple[int, int]:
        """Compress all uncompressed files in the store. Returns (compressed, skipped)."""
        return self._convert_all(
            skip_suffix=".zst",
            target_fn=lambda p: p.with_suffix(p.suffix + ".zst"),
            converter=zstd.compress_stream,
            operation="compression",
        )

    def decompress_all(self) -> tuple[int, int]:
        """Decompress all compressed files in the store. Returns (decompressed, skipped)."""
        return self._convert_all(
            skip_suffix=self.suffix,
            target_fn=lambda p: p.with_suffix(""),
            converter=zstd.decompress_stream,
            operation="decompression",
        )

    def walk(self) -> collections.abc.Generator[pathlib.Path, None, None]:
        suffixes = {self.suffix, self.suffix + ".zst"}
        for path, _, files in os.walk(self.root_dir):
            for fname in files:
                if any(fname.endswith(s) for s in suffixes):
                    yield pathlib.Path(path, fname)
