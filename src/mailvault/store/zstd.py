"""A small facade over zstd, so the store need not care where the codec comes from.

Python 3.14 added zstd to the standard library (PEP 784); before that it is the
third-party ``zstandard`` package. Both provide the two things the store needs --
a streaming writer and a streaming reader over an open file object -- but through
different APIs, so this hides the difference behind one and builds whole-file
compress/decompress on top of it.

The reader and writer wrap a file object the caller already opened, and never
close it: the caller's own ``with open(...)`` owns that.
"""

from __future__ import annotations

import shutil
import typing

_zstd: typing.Any
_BACKEND: str | None

try:
    from compression import zstd as _zstd  # Python 3.14+ (PEP 784)

    _BACKEND = "stdlib"
except ImportError:  # pragma: no cover - depends on the interpreter version
    try:
        import zstandard as _zstd

        _BACKEND = "package"
    except ImportError:
        _zstd = None
        _BACKEND = None


def available() -> bool:
    """True when some zstd implementation is importable."""
    return _BACKEND is not None


def require() -> None:
    """Raise a clear error when no zstd implementation is available.

    Called before anything compresses, so a missing dependency fails here with a
    readable message rather than as an ImportError deep inside a write.
    """
    if _BACKEND is None:
        raise RuntimeError(
            "zstd compression needs Python 3.14+ or the 'zstandard' package installed"
        )


def open_writer(fileobj: typing.BinaryIO) -> typing.Any:
    """A context-managed writer that compresses into an already-open file object.

    ``closefd=False`` keeps the package backend from closing the caller's file
    object on exit (the stdlib one never does), so both leave it to the caller.
    """
    require()
    if _BACKEND == "stdlib":
        return _zstd.ZstdFile(fileobj, "w")
    return _zstd.ZstdCompressor().stream_writer(fileobj, closefd=False)


def open_reader(fileobj: typing.BinaryIO) -> typing.Any:
    """A context-managed reader that decompresses from an already-open file object."""
    require()
    if _BACKEND == "stdlib":
        return _zstd.ZstdFile(fileobj, "r")
    return _zstd.ZstdDecompressor().stream_reader(fileobj, closefd=False)


def compress_stream(src: typing.BinaryIO, dst: typing.BinaryIO) -> None:
    """Compress all of ``src`` into ``dst``."""
    with open_writer(dst) as writer:
        shutil.copyfileobj(src, writer)


def decompress_stream(src: typing.BinaryIO, dst: typing.BinaryIO) -> None:
    """Decompress all of ``src`` into ``dst``."""
    with open_reader(src) as reader:
        shutil.copyfileobj(reader, dst)
