"""What the operating systems mailvault runs on do differently.

The callers stay the same on every platform; what one of them needs on Windows
and not elsewhere is decided here, once.
"""

from __future__ import annotations

import collections.abc
import contextlib
import errno
import os
import sys
import typing


@contextlib.contextmanager
def _reader_gone_is_a_broken_pipe() -> collections.abc.Iterator[None]:
    try:
        yield
    except OSError as exc:
        if exc.errno != errno.EINVAL:
            raise
        raise BrokenPipeError(errno.EPIPE, os.strerror(errno.EPIPE)) from exc


class _WindowsStdout:
    """Standard output that raises BrokenPipeError when its reader has gone.

    POSIX raises it for a write into a pipe nobody reads any more (`| more`,
    quit early), and the CLI ends the run on it without a word. Windows raises
    OSError with EINVAL instead, and no `winerror` that would tell it apart from
    any other EINVAL. Translated here, on the one stream where it means that,
    and on its binary side as well, which `mailvault get` writes a message to.
    """

    def __init__(self, stream: typing.Any) -> None:
        self._stream = stream

    def __getattr__(self, name: str) -> typing.Any:
        return getattr(self._stream, name)

    @property
    def buffer(self) -> _WindowsStdout:
        return _WindowsStdout(self._stream.buffer)

    def write(self, data: typing.Any) -> int:
        with _reader_gone_is_a_broken_pipe():
            return self._stream.write(data)

    def flush(self) -> None:
        with _reader_gone_is_a_broken_pipe():
            self._stream.flush()


def standard_output(stream: typing.TextIO) -> typing.TextIO:
    """`stream`, to be put in place of `sys.stdout` for a run.

    Everything a run prints, writes through `csv` or `json`, or writes to the
    binary side ends in a pipe whose reader may leave. On POSIX that raises
    BrokenPipeError, and `stream` is returned as it is. On Windows it is
    wrapped so that it raises the same.
    """
    if sys.platform != "win32":
        return stream
    return typing.cast(typing.TextIO, _WindowsStdout(stream))
