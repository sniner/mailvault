"""Tests for `archive seal`: when a seal is made, reused or refused, and what it prints."""

from __future__ import annotations

import argparse
import json
import logging
import pathlib
from datetime import UTC, datetime, timedelta

import pytest

from mailvault import cli
from mailvault.cli.archive import run as run_archive
from mailvault.jobs.sealing import seal_archive
from mailvault.store import cas, heads, marker, metalog, seals
from tests.tamper import tamper

WHEN = datetime(2026, 10, 22, 2, 15, 7, tzinfo=UTC)


def _store(archive: pathlib.Path, *bodies: str) -> list[str]:
    """Store one message per body and record them in one log file, in one place."""
    store = cas.mail_store(archive)
    ids = [
        store.add(f"Message-Id: <{body}@example.com>\r\n\r\n{body}\r\n".encode())[1]
        for body in bodies
    ]
    writer = metalog.LogWriter(
        archive / metalog.DEFAULT_LOG_DIR,
        archive / heads.DEFAULT_HEADS_DIR,
    )
    for store_id in ids:
        writer.add("job", ["INBOX"], store_id)
    writer.flush(WHEN)
    return ids


def _meta(archive: pathlib.Path) -> pathlib.Path:
    return archive / metalog.DEFAULT_LOG_DIR


def _seal_path(archive: pathlib.Path, seal: seals.Seal) -> pathlib.Path:
    return _meta(archive) / seal.seal_id[:2] / f"{seal.seal_id}{seals.SUFFIX}"


class TestSealing:
    def test_seals_every_log_file_and_counts_what_they_record(self, tmp_path):
        _store(tmp_path, "a", "b")
        _store(tmp_path, "b", "c")

        result = seal_archive(tmp_path, now=WHEN)

        assert result.seal is not None
        assert result.seal.logs == {
            path.name.removesuffix(".jsonl") for path in metalog.log_files(_meta(tmp_path))
        }
        assert (result.log_files, result.messages, result.places) == (2, 3, 1)
        assert result.first
        assert not result.reused
        assert _seal_path(tmp_path, result.seal).is_file()

    def test_nothing_recorded_since_gives_the_same_seal(self, tmp_path):
        _store(tmp_path, "a")
        first = seal_archive(tmp_path, now=WHEN)

        again = seal_archive(tmp_path, now=WHEN + timedelta(hours=1))

        assert again.reused
        assert again.seal == first.seal
        assert len(seals.seal_files(_meta(tmp_path))) == 1

    def test_a_new_log_file_gives_a_new_seal(self, tmp_path):
        _store(tmp_path, "a")
        first = seal_archive(tmp_path, now=WHEN)
        _store(tmp_path, "b")

        second = seal_archive(tmp_path, now=WHEN + timedelta(days=1))

        assert second.seal is not None and first.seal is not None
        assert second.seal.seal_id != first.seal.seal_id
        assert second.seal.logs > first.seal.logs
        assert not second.first
        assert len(seals.seal_files(_meta(tmp_path))) == 2

    def test_a_dry_run_writes_nothing(self, tmp_path):
        _store(tmp_path, "a")

        result = seal_archive(tmp_path, dry_run=True, now=WHEN)

        assert result.seal is None
        assert result.first
        assert result.log_files == 1
        assert not seals.seal_files(_meta(tmp_path))

    def test_an_archive_without_a_log_file_is_not_sealed(self, tmp_path):
        result = seal_archive(tmp_path, now=WHEN)

        assert result.seal is None
        assert result.log_files == 0
        assert not result.refused


class TestRefusals:
    """Each one leaves the archive as it was: no seal is written over it."""

    def test_a_damaged_log_file(self, tmp_path):
        _store(tmp_path, "a")
        (path,) = metalog.log_files(_meta(tmp_path))
        tamper(path, path.read_bytes() + b'{"store_id": "ab"}\n')

        result = seal_archive(tmp_path, now=WHEN)

        assert result.refused
        assert result.damaged_logs == [path]
        assert not seals.seal_files(_meta(tmp_path))

    def test_a_log_file_a_seal_records_and_that_is_gone(self, tmp_path):
        _store(tmp_path, "a")
        sealed = seal_archive(tmp_path, now=WHEN).seal
        (path,) = metalog.log_files(_meta(tmp_path))
        path.unlink()
        _store(tmp_path, "b")

        result = seal_archive(tmp_path, now=WHEN + timedelta(days=1))

        assert result.refused
        assert result.gone_logs == {path: [sealed]}
        assert len(seals.seal_files(_meta(tmp_path))) == 1

    def test_a_damaged_seal(self, tmp_path):
        _store(tmp_path, "a")
        sealed = seal_archive(tmp_path, now=WHEN).seal
        assert sealed is not None
        tamper(_seal_path(tmp_path, sealed), b"not a seal any more")

        result = seal_archive(tmp_path, now=WHEN)

        assert result.refused
        assert result.damaged_seals == [_seal_path(tmp_path, sealed)]

    def test_a_seal_written_by_a_newer_version(self, tmp_path):
        _store(tmp_path, "a")
        header = {"version": seals.SEAL_VERSION + 1, "date": WHEN.isoformat(), "hash": "x"}
        _status, _id, path = seals.open_store(_meta(tmp_path)).add(json.dumps(header).encode())

        result = seal_archive(tmp_path, now=WHEN)

        assert result.refused
        assert result.newer_seals == [path]


def _run(archive: pathlib.Path, **options) -> int:
    namespace = argparse.Namespace(
        archive_command="seal",
        archive=archive,
        config=None,
        list=False,
        dry_run=False,
    )
    for name, value in options.items():
        setattr(namespace, name, value)
    return run_archive(namespace)


class TestTheCommand:
    def test_stdout_is_the_id_and_nothing_else(self, tmp_path, capsys):
        marker.write(tmp_path)
        _store(tmp_path, "a")

        assert _run(tmp_path) == 0

        (seal_file,) = seals.seal_files(_meta(tmp_path))
        assert capsys.readouterr().out == seal_file.name.removesuffix(seals.SUFFIX) + "\n"

    def test_the_same_id_when_nothing_was_recorded_since(self, tmp_path, capsys):
        marker.write(tmp_path)
        _store(tmp_path, "a")
        _run(tmp_path)
        first = capsys.readouterr().out

        assert _run(tmp_path) == 0
        assert capsys.readouterr().out == first

    def test_the_first_seal_says_what_it_changes(self, tmp_path, caplog):
        marker.write(tmp_path)
        _store(tmp_path, "a")

        with caplog.at_level(logging.INFO):
            _run(tmp_path)

        assert "from now on `archive compact` refuses to run" in caplog.text

    def test_a_refusal_prints_no_id_and_exits_non_zero(self, tmp_path, capsys, caplog):
        marker.write(tmp_path)
        _store(tmp_path, "a")
        (path,) = metalog.log_files(_meta(tmp_path))
        tamper(path, b"not a log file any more")

        assert _run(tmp_path) == 1

        assert capsys.readouterr().out == ""
        assert "is damaged, a seal over it could never be checked" in caplog.text
        assert "without a backup, delete all seals, run `archive compact`" in caplog.text

    def test_an_archive_without_mail_is_not_sealed(self, tmp_path, capsys):
        marker.write(tmp_path)

        assert _run(tmp_path) == 1
        assert capsys.readouterr().out == ""

    def test_a_dry_run_says_what_it_would_do(self, tmp_path, capsys):
        marker.write(tmp_path)
        _store(tmp_path, "a", "b")

        assert _run(tmp_path, dry_run=True) == 0

        out = capsys.readouterr().out
        assert "would seal 1 log file covering 2 messages in 1 place" in out
        assert "this would be the first seal of this archive" in out
        assert not seals.seal_files(_meta(tmp_path))

    def test_the_list_is_oldest_first_and_marks_what_cannot_be_read(self, tmp_path, capsys):
        marker.write(tmp_path)
        _store(tmp_path, "a")
        older = seal_archive(tmp_path, now=WHEN).seal
        _store(tmp_path, "b")
        newer = seal_archive(tmp_path, now=WHEN + timedelta(days=1)).seal
        _store(tmp_path, "c")
        broken = seal_archive(tmp_path, now=WHEN + timedelta(days=2)).seal
        assert older is not None and newer is not None and broken is not None
        tamper(_seal_path(tmp_path, broken), b"not a seal any more")

        assert _run(tmp_path, list=True) == 0

        assert capsys.readouterr().out.splitlines() == [
            f"2026-10-22 02:15:07 UTC  {older.seal_id}",
            f"2026-10-23 02:15:07 UTC  {newer.seal_id}",
            f"{'':23}  {broken.seal_id}  (damaged)",
        ]

    def test_the_modes_do_not_combine(self):
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args(["archive", "seal", "--list", "--dry-run"])
