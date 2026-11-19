import collections.abc
import pathlib
import sys

import pytest

from mailvault.store import cas

_RECORD = """\
import sys

with open(sys.argv[1], "a") as f:
    f.write("x\\n")
if len(sys.argv) > 2:
    print(sys.argv[2])
"""


@pytest.fixture(autouse=True)
def _run_hash() -> collections.abc.Iterator[None]:
    """Put the run's hash back after every test.

    A command sets it from the archive's configuration and nothing sets it back,
    because a run works on one archive. A test suite works on hundreds, and one
    test's sha256 archive must not decide what the next one hashes with.
    """
    yield
    cas.use_hash(cas.LEGACY_HASH)


@pytest.fixture
def record_command(
    tmp_path: pathlib.Path,
) -> collections.abc.Callable[[pathlib.Path, str | None], str]:
    """A `_cmd` command that adds a line to a file and prints a value, in any shell.

    `touch` and `echo` would do on POSIX, but Windows runs the command through
    `cmd.exe`. The command quotes its paths; put it into TOML as a literal
    string, where a Windows path keeps its backslashes.
    """
    script = tmp_path / "record.py"
    script.write_text(_RECORD)

    def command(path: pathlib.Path, value: str | None = None) -> str:
        parts = [f'"{sys.executable}"', f'"{script}"', f'"{path}"']
        if value is not None:
            parts.append(value)
        return " ".join(parts)

    return command


@pytest.fixture
def dummy_eml_bytes() -> bytes:
    return b"""From: test@example.com
To: recipient@example.com
Subject: Test Email
Date: Wed, 20 Feb 2026 12:00:00 +0100

This is a test email body.
"""
