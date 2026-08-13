"""Tests for `archive adopt`: taking messages with no place into one."""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime

import pytest

from mailvault import jobs
from mailvault.jobs import adoption
from mailvault.store import cas, heads, metalog


def _orphans(archive: pathlib.Path, count: int) -> list[str]:
    """Mail in the store and in no log -- what an import before `--name` left."""
    store = cas.mail_store(archive)
    ids = []
    for number in range(count):
        _status, store_id, _path = store.add(
            f"From: someone\r\nSubject: {number}\r\n\r\nmessage {number}".encode()
        )
        ids.append(store_id)
    return ids


def _placed(archive: pathlib.Path, mailbox: str | None, folder: str, body: bytes) -> str:
    """One message with a place, the way a backup leaves it."""
    _status, store_id, _path = cas.mail_store(archive).add(body)
    writer = metalog.LogWriter(
        archive / metalog.DEFAULT_LOG_DIR, archive / heads.DEFAULT_HEADS_DIR
    )
    writer.add(mailbox, [folder], store_id)
    writer.seal(datetime.now(UTC))
    return store_id


def _log_of(archive: pathlib.Path, folder: str) -> metalog.LogFile:
    """The one log file of a place, so a failure lands on the assertion."""
    for path in metalog.log_files(archive / metalog.DEFAULT_LOG_DIR):
        logfile = metalog.read_log(path)
        if logfile is not None and logfile.folder == folder:
            return logfile
    raise AssertionError(f"no log file for {folder}")


class TestWhatItTakesIn:
    def test_every_message_with_no_place_lands_under_the_name(self, tmp_path):
        _orphans(tmp_path, 3)

        result = jobs.adopt(tmp_path, "docuware-2019")

        assert (result.found, result.recorded) == (3, 3)
        logfile = _log_of(tmp_path, "docuware-2019")
        assert logfile.mailbox is None, "the name is not a mailbox"
        assert len(logfile.store_ids) == 3

    def test_it_is_the_same_kind_of_place_an_import_writes(self, tmp_path):
        """Same statement, same shape -- and so it carries a head and a chain."""
        _orphans(tmp_path, 1)

        jobs.adopt(tmp_path, "orphaned")

        head = heads.read(tmp_path / heads.DEFAULT_HEADS_DIR, None, "orphaned")
        assert head is not None
        assert head.job is None
        assert head.log == _log_of(tmp_path, "orphaned").hashval

    def test_messages_that_have_a_place_are_left_alone(self, tmp_path):
        """This run knows nothing about where those were, so it says nothing."""
        placed = _placed(tmp_path, "gmail.com", "INBOX", b"From: a\r\n\r\nplaced")
        _orphans(tmp_path, 2)

        result = jobs.adopt(tmp_path, "docuware-2019")

        assert result.found == 2
        assert placed not in _log_of(tmp_path, "docuware-2019").store_ids
        assert _log_of(tmp_path, "INBOX").store_ids == [placed]

    def test_a_message_recorded_under_an_import_name_counts_as_placed(self, tmp_path):
        """An import name is a place like any other, and not this command's business."""
        imported = _placed(tmp_path, None, "docuware-2019", b"From: a\r\n\r\nimported")
        _orphans(tmp_path, 1)

        result = jobs.adopt(tmp_path, "orphaned")

        assert result.found == 1
        assert imported not in _log_of(tmp_path, "orphaned").store_ids

    def test_an_archive_that_is_whole_reports_nothing_to_do(self, tmp_path):
        _placed(tmp_path, "gmail.com", "INBOX", b"From: a\r\n\r\nplaced")

        result = jobs.adopt(tmp_path, "orphaned")

        assert (result.found, result.recorded) == (0, 0)

    def test_running_it_twice_finds_nothing_the_second_time(self, tmp_path):
        _orphans(tmp_path, 2)

        jobs.adopt(tmp_path, "docuware-2019")
        again = jobs.adopt(tmp_path, "docuware-2019")

        assert again.found == 0
        assert len(metalog.log_files(tmp_path / metalog.DEFAULT_LOG_DIR)) == 1

    def test_a_name_of_nothing_is_refused(self, tmp_path):
        _orphans(tmp_path, 1)

        with pytest.raises(jobs.JobError, match="--name"):
            jobs.adopt(tmp_path, "   ")

        assert metalog.log_files(tmp_path / metalog.DEFAULT_LOG_DIR) == []


class TestTheDryRun:
    def test_it_counts_and_writes_nothing(self, tmp_path):
        _orphans(tmp_path, 4)

        result = jobs.adopt(tmp_path, "docuware-2019", dry_run=True)

        assert result.dry_run
        assert (result.found, result.recorded) == (4, 0)
        assert metalog.log_files(tmp_path / metalog.DEFAULT_LOG_DIR) == []
        assert heads.head_files(tmp_path / heads.DEFAULT_HEADS_DIR) == []

    def test_it_predicts_what_the_real_run_then_does(self, tmp_path):
        _orphans(tmp_path, 4)

        predicted = jobs.adopt(tmp_path, "docuware-2019", dry_run=True)
        actual = jobs.adopt(tmp_path, "docuware-2019")

        assert predicted.found == actual.found == actual.recorded


class TestWhenTheLogCannotBeWritten:
    def test_nothing_is_recorded_and_the_run_says_so(self, tmp_path, monkeypatch):
        _orphans(tmp_path, 2)

        def refuse(self, date):
            raise OSError("no space left on device")

        monkeypatch.setattr(metalog.LogWriter, "seal", refuse)
        result = jobs.adopt(tmp_path, "docuware-2019")

        assert (result.found, result.recorded) == (2, 0)
        assert metalog.log_files(tmp_path / metalog.DEFAULT_LOG_DIR) == []

    def test_it_is_tried_once_per_batch_and_not_once_per_message(self, tmp_path, monkeypatch):
        """A failed seal leaves the writer full, so "is it full" would retry forever."""
        monkeypatch.setattr(adoption, "SEAL_BATCH", 2)
        _orphans(tmp_path, 7)
        attempts = []

        def refuse(self, date):
            attempts.append(len(self))
            raise OSError("no space left on device")

        monkeypatch.setattr(metalog.LogWriter, "seal", refuse)
        jobs.adopt(tmp_path, "docuware-2019")

        assert attempts == [2, 4, 6, 7], "one attempt per batch, each carrying the last"


class TestBatches:
    def test_the_observations_go_out_in_batches(self, tmp_path, monkeypatch):
        monkeypatch.setattr(adoption, "SEAL_BATCH", 2)
        _orphans(tmp_path, 5)

        result = jobs.adopt(tmp_path, "docuware-2019")

        assert result.recorded == 5
        files = metalog.log_files(tmp_path / metalog.DEFAULT_LOG_DIR)
        assert len(files) == 3, "two full batches and the remainder"

    def test_and_form_one_chain(self, tmp_path, monkeypatch):
        monkeypatch.setattr(adoption, "SEAL_BATCH", 2)
        _orphans(tmp_path, 4)

        jobs.adopt(tmp_path, "docuware-2019")

        head = heads.read(tmp_path / heads.DEFAULT_HEADS_DIR, None, "docuware-2019")
        assert head is not None
        store = metalog.open_store(tmp_path / metalog.DEFAULT_LOG_DIR)
        walked = 0
        hashval = head.log
        while hashval is not None:
            path = store.locate(hashval)
            assert path is not None, "the chain names a file that is not there"
            logfile = metalog.read_log(path)
            assert logfile is not None
            walked += 1
            hashval = logfile.prev
        assert walked == 2


class TestTogetherWithTheCheck:
    def test_what_check_reports_is_what_adopt_takes(self, tmp_path):
        """The two must agree, or the report names a number the command misses."""
        _placed(tmp_path, "gmail.com", "INBOX", b"From: a\r\n\r\nplaced")
        _orphans(tmp_path, 3)

        before = jobs.check(tmp_path, contents=False)
        result = jobs.adopt(tmp_path, "orphaned")

        assert len(before.orphans) == result.found == 3

    def test_and_afterwards_the_archive_is_whole(self, tmp_path):
        _orphans(tmp_path, 3)

        jobs.adopt(tmp_path, "orphaned")
        after = jobs.check(tmp_path)

        assert after.orphans == []
        assert after.referenced == 3
        assert after.sound, f"findings: {after.missing} {after.broken_chains}"
