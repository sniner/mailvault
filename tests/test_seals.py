"""Tests for mailvault.store.seals: the seal file, and that the log never sees it."""

from __future__ import annotations

import json
import pathlib
from datetime import UTC, datetime, timedelta

import pytest

from mailvault.jobs.check import check
from mailvault.store import cas, heads, metalog, seals
from tests.tamper import tamper

WHEN = datetime(2026, 10, 22, 2, 15, 7, 123456, tzinfo=UTC)


def _log(archive: pathlib.Path, *store_ids: str) -> str:
    """Write one log file into `archive` and return its name, which is its hash."""
    writer = metalog.LogWriter(
        archive / metalog.DEFAULT_LOG_DIR,
        archive / heads.DEFAULT_HEADS_DIR,
    )
    for store_id in store_ids:
        writer.add("job", ["INBOX"], store_id)
    (path,) = writer.flush(WHEN)
    return path.name.removesuffix(".jsonl")


def _meta(archive: pathlib.Path) -> pathlib.Path:
    return archive / metalog.DEFAULT_LOG_DIR


def _path(meta: pathlib.Path, seal: seals.Seal) -> pathlib.Path:
    return meta / seal.seal_id[:2] / f"{seal.seal_id}{seals.SUFFIX}"


class TestWriting:
    def test_a_seal_is_named_by_its_content(self, tmp_path):
        meta = _meta(tmp_path)
        seal = seals.write(meta, [_log(tmp_path, "aa")], WHEN)

        path = _path(meta, seal)
        assert path.is_file()
        assert cas.digest(path.read_bytes()) == seal.seal_id

    def test_the_content_is_a_header_and_the_log_files_sorted(self, tmp_path):
        meta = _meta(tmp_path)
        logs = [_log(tmp_path, "aa"), _log(tmp_path, "bb"), _log(tmp_path, "cc")]

        seal = seals.write(meta, logs, WHEN)

        lines = _path(meta, seal).read_text(encoding="utf-8").splitlines()
        assert json.loads(lines[0]) == {
            "version": seals.SEAL_VERSION,
            "date": "2026-10-22T02:15:07+00:00",
            "hash": cas.current_hash(),
        }
        assert [json.loads(line)["log"] for line in lines[1:]] == sorted(logs)

    def test_the_date_is_cut_to_the_second_and_keeps_its_zone(self, tmp_path):
        seal = seals.write(_meta(tmp_path), [_log(tmp_path, "aa")], WHEN)

        assert seal.date.microsecond == 0
        assert seal.when == "2026-10-22 02:15:07 UTC"

    def test_a_date_without_a_zone_is_refused(self, tmp_path):
        with pytest.raises(ValueError):
            seals.write(_meta(tmp_path), [_log(tmp_path, "aa")], WHEN.replace(tzinfo=None))

    def test_the_same_log_files_at_the_same_second_are_one_seal(self, tmp_path):
        meta = _meta(tmp_path)
        logs = [_log(tmp_path, "aa")]

        first = seals.write(meta, logs, WHEN)
        second = seals.write(meta, logs, WHEN + timedelta(microseconds=1))

        assert first.seal_id == second.seal_id
        assert len(seals.seal_files(meta)) == 1


class TestFinding:
    def test_finds_what_was_written(self, tmp_path):
        meta = _meta(tmp_path)
        seal = seals.write(meta, [_log(tmp_path, "aa")], WHEN)

        assert seals.find(meta, seal.seal_id) == (seal, None)

    def test_an_id_with_no_seal_is_gone(self, tmp_path):
        meta = _meta(tmp_path)
        _log(tmp_path, "aa")

        assert seals.find(meta, cas.digest(b"never written")) == (None, seals.Unusable.GONE)

    def test_a_changed_seal_is_damaged(self, tmp_path):
        meta = _meta(tmp_path)
        seal = seals.write(meta, [_log(tmp_path, "aa")], WHEN)

        tamper(_path(meta, seal), _path(meta, seal).read_bytes().replace(b"2026", b"2025"))

        assert seals.find(meta, seal.seal_id) == (None, seals.Unusable.DAMAGED)

    def test_a_seal_of_a_newer_version_says_so(self, tmp_path):
        meta = _meta(tmp_path)
        header = {"version": seals.SEAL_VERSION + 1, "date": WHEN.isoformat(), "hash": "x"}
        _status, seal_id, _path = seals.open_store(meta).add(json.dumps(header).encode())

        assert seals.find(meta, seal_id) == (None, seals.Unusable.NEWER)

    def test_an_intact_seal_naming_no_log_file_is_damaged(self, tmp_path):
        """Content and name agree, so it was written that way, and it is still no seal."""
        meta = _meta(tmp_path)
        header = {"version": seals.SEAL_VERSION, "date": WHEN.isoformat(), "hash": "sha256"}
        content = json.dumps(header) + "\n" + json.dumps({"log": "../../etc"}) + "\n"
        _status, seal_id, _path = seals.open_store(meta).add(content.encode())

        assert seals.find(meta, seal_id) == (None, seals.Unusable.DAMAGED)


class TestAllSeals:
    def test_oldest_first_and_the_unusable_after_them(self, tmp_path):
        meta = _meta(tmp_path)
        logs = [_log(tmp_path, "aa")]
        newer = seals.write(meta, logs, WHEN)
        older = seals.write(meta, logs, WHEN - timedelta(days=1))
        broken = seals.write(meta, logs, WHEN - timedelta(days=2))
        tamper(_path(meta, broken), b"not a seal any more")

        found = seals.all_seals(meta)

        assert [f.seal for f in found] == [older, newer, None]
        assert found[-1].path == _path(meta, broken)
        assert found[-1].unusable == seals.Unusable.DAMAGED

    def test_no_log_directory_means_no_seals_and_creates_nothing(self, tmp_path):
        assert seals.all_seals(tmp_path / "meta") == []
        assert not (tmp_path / "meta").exists()


class TestRefuseIfSealed:
    def test_a_log_without_a_seal_passes(self, tmp_path):
        _log(tmp_path, "aa")

        seals.refuse_if_sealed(_meta(tmp_path), "compacting")

    def test_a_seal_stops_it_and_names_the_newest(self, tmp_path):
        meta = _meta(tmp_path)
        logs = [_log(tmp_path, "aa")]
        seals.write(meta, logs, WHEN - timedelta(days=1))
        newest = seals.write(meta, logs, WHEN)

        with pytest.raises(seals.Sealed) as raised:
            seals.refuse_if_sealed(meta, "compacting")

        assert raised.value.count == 2
        assert raised.value.newest == newest

    def test_the_refusal_says_what_and_how_to_go_ahead(self, tmp_path):
        meta = _meta(tmp_path)
        seals.write(meta, [_log(tmp_path, "aa")], WHEN)

        with pytest.raises(seals.Sealed) as raised:
            seals.refuse_if_sealed(meta, "compacting the metadata log")

        message = str(raised.value)
        assert "sealed (1 seal, the newest of 2026-10-22 02:15:07 UTC)" in message
        assert "compacting the metadata log would change what the seals record" in message
        assert "delete the .seal files in meta/ first" in message

    def test_a_damaged_seal_stops_it_too(self, tmp_path):
        meta = _meta(tmp_path)
        seal = seals.write(meta, [_log(tmp_path, "aa")], WHEN)
        tamper(_path(meta, seal), b"not a seal any more")

        with pytest.raises(seals.Sealed) as raised:
            seals.refuse_if_sealed(meta, "compacting")

        assert raised.value.count == 1
        assert raised.value.newest is None


class TestTheLogDoesNotSeeASeal:
    """Everything that reads the log, and every older version, must pass a seal by.

    That is what lets a seal lie in `meta/` without a new archive format: the
    readers look for `*.jsonl`, and the log's own store only knows its suffix.
    """

    def test_the_log_files_are_only_log_files(self, tmp_path):
        meta = _meta(tmp_path)
        name = _log(tmp_path, "aa")
        seals.write(meta, [name], WHEN)

        assert [p.name for p in metalog.log_files(meta)] == [f"{name}.jsonl"]
        assert [p.name for p in metalog.open_store(meta).walk()] == [f"{name}.jsonl"]

    def test_check_counts_no_seal_as_a_log_file_or_a_stray(self, tmp_path):
        store = cas.mail_store(tmp_path)
        _status, store_id, _path = store.add(b"Message-Id: <a@example.com>\r\n\r\nbody\r\n")
        seals.write(_meta(tmp_path), [_log(tmp_path, store_id)], WHEN)

        result = check(tmp_path)

        assert result.sound
        assert result.log_files == 1
        assert not result.foreign
