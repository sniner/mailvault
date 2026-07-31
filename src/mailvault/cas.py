from __future__ import annotations

import collections.abc
import hashlib
import io
import logging
import os
import pathlib

log = logging.getLogger(__name__)


class ContentAddressedStorage:
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
        self.hashfactory = hashfactory if hashfactory else hashlib.sha384
        self.depth = depth if depth >= 0 else 2
        self.suffix = suffix
        self.blocksize = 16384
        if self.compress:
            import zstandard  # noqa: F401 — fail fast if not installed

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
            block = reader.read(self.blocksize or 4096)
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
                import zstandard

                cctx = zstandard.ZstdCompressor()
                with open(tmp_file, "wb") as f:
                    with cctx.stream_writer(f) as compressor:
                        while True:
                            block = reader.read(self.blocksize or 4096)
                            if block is None or len(block) == 0:
                                break
                            compressor.write(block)
            else:
                with open(tmp_file, "wb") as f:
                    while True:
                        block = reader.read(self.blocksize or 4096)
                        if block is None or len(block) == 0:
                            break
                        f.write(block)
        except Exception as exc:
            log.error(f"{file}: error while writing file: {exc}")
            if tmp_file.exists():
                tmp_file.unlink()
            raise
        else:
            tmp_file.rename(file)
        log.debug(f"{file}: new entry")
        return "NEW", hashval, file

    def read(self, path: pathlib.Path) -> bytes:
        """Read file content, decompressing transparently if needed."""
        if path.suffix == ".zst":
            import zstandard

            dctx = zstandard.ZstdDecompressor()
            with open(path, "rb") as f:
                with dctx.stream_reader(f) as reader:
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
        import zstandard

        cctx = zstandard.ZstdCompressor()
        return self._convert_all(
            skip_suffix=".zst",
            target_fn=lambda p: p.with_suffix(p.suffix + ".zst"),
            converter=cctx.copy_stream,
            operation="compression",
        )

    def decompress_all(self) -> tuple[int, int]:
        """Decompress all compressed files in the store. Returns (decompressed, skipped)."""
        import zstandard

        dctx = zstandard.ZstdDecompressor()
        return self._convert_all(
            skip_suffix=self.suffix,
            target_fn=lambda p: p.with_suffix(""),
            converter=dctx.copy_stream,
            operation="decompression",
        )

    def walk(self) -> collections.abc.Generator[pathlib.Path, None, None]:
        suffixes = {self.suffix, self.suffix + ".zst"}
        for path, _, files in os.walk(self.root_dir):
            for fname in files:
                if any(fname.endswith(s) for s in suffixes):
                    yield pathlib.Path(path, fname)
