"""Tests for reading back what earlier runs stored: what is read, what is set aside."""

from __future__ import annotations

import json
import os
import pathlib
import time
from datetime import UTC, datetime

from mailvault.jobs import readback
from mailvault.store import cas, heads, metalog
from tests.tamper import tamper

WHEN = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)
LATER = time.time() + readback.MIN_AGE + 60
RESUME = {"kind": "imap", "uidvalidity": 7, "uid": 42}


def _heads(archive: pathlib.Path) -> pathlib.Path:
    return archive / heads.DEFAULT_HEADS_DIR


def _record(
    archive: pathlib.Path,
    messages: list[bytes],
    job: str | None = "job",
    folder: str = "INBOX",
) -> list[pathlib.Path]:
    """Store `messages` without a flush and record them in one log file, as a pass does."""
    store = cas.mail_store(archive)
    writer = metalog.LogWriter(archive / metalog.DEFAULT_LOG_DIR, _heads(archive))
    paths = []
    for data in messages:
        _status, store_id, path = store.add(data)
        writer.add(job, [folder], store_id)
        paths.append(path)
    writer.flush(WHEN)
    return paths


def _resume(archive: pathlib.Path, job: str, folder: str) -> None:
    head = heads.read(_heads(archive), job, folder) or heads.Head(job=job, folder=folder)
    head.resume = RESUME
    heads.write(_heads(archive), head)


def _head(archive: pathlib.Path, job: str | None = "job", folder: str = "INBOX") -> heads.Head:
    head = heads.read(_heads(archive), job, folder)
    assert head is not None
    return head


def _mail(n: int) -> bytes:
    return f"Message-Id: <{n}@example.org>\r\n\r\nbody {n}\r\n".encode()


class TestWhatIsReadBack:
    def test_the_messages_recorded_since_once_they_are_old_enough(self, tmp_path):
        _record(tmp_path, [_mail(1), _mail(2)])

        result = readback.read_back(tmp_path, now=LATER)

        assert result.read == 2
        assert result.damaged == []
        head = _head(tmp_path)
        assert head.checked == head.log
        assert readback.read_back(tmp_path, now=LATER).read == 0, "once is enough"

    def test_nothing_that_was_written_within_the_hour(self, tmp_path):
        """Read back so soon, the bytes would come from memory and prove nothing."""
        _record(tmp_path, [_mail(1)])

        assert readback.read_back(tmp_path).read == 0
        assert _head(tmp_path).checked is None

    def test_only_the_older_files_of_a_chain(self, tmp_path):
        _record(tmp_path, [_mail(1)])
        older = _head(tmp_path).log
        assert older is not None
        for path in metalog.log_files(tmp_path / metalog.DEFAULT_LOG_DIR):
            os.utime(path, (0, time.time() - readback.MIN_AGE - 60))
        _record(tmp_path, [_mail(2)])

        result = readback.read_back(tmp_path)

        assert result.read == 1
        assert _head(tmp_path).checked == older

    def test_nothing_in_a_head_from_a_version_that_flushed_every_message(self, tmp_path):
        _record(tmp_path, [_mail(1)])
        path = heads.head_path(_heads(tmp_path), "job", "INBOX")
        payload = json.loads(path.read_text())
        del payload["checked"]
        path.write_text(json.dumps(payload))

        assert readback.read_back(tmp_path, now=LATER).read == 0


class TestADamagedMessage:
    def test_is_set_aside_and_its_folder_loses_its_resume_point(self, tmp_path):
        """The incremental backup asks only past it, and would never fetch it again."""
        _resume(tmp_path, "job", "INBOX")
        _resume(tmp_path, "job", "Sent")
        victim, _intact = _record(tmp_path, [_mail(1), _mail(2)])
        tamper(victim, b"")

        result = readback.read_back(tmp_path, now=LATER)

        assert len(result.damaged) == 1
        assert (result.folders, result.jobs) == (["job::INBOX"], set())
        assert not victim.exists()
        assert victim.with_name(victim.name + ".corrupt").exists()
        assert _head(tmp_path).resume is None
        assert _head(tmp_path, "job", "Sent").resume == RESUME
        head = _head(tmp_path)
        assert head.checked == head.log

    def test_recorded_only_under_labels_takes_every_resume_point_of_its_job(self, tmp_path):
        """Gmail: read from `[Gmail]/All Mail`, recorded under a label nobody configured."""
        _resume(tmp_path, "job", "[Gmail]/All Mail")
        _resume(tmp_path, "job", "INBOX")
        _resume(tmp_path, "other", "INBOX")
        (victim,) = _record(tmp_path, [_mail(1)], folder="Work")
        tamper(victim, b"")

        result = readback.read_back(tmp_path, now=LATER)

        assert (result.folders, result.jobs) == ([], {"job"})
        assert _head(tmp_path, "job", "[Gmail]/All Mail").resume is None
        assert _head(tmp_path, "job", "INBOX").resume is None
        assert _head(tmp_path, "other", "INBOX").resume == RESUME

    def test_one_the_archive_no_longer_holds_is_fetched_again_too(self, tmp_path):
        _resume(tmp_path, "job", "INBOX")
        (victim,) = _record(tmp_path, [_mail(1)])
        victim.unlink()

        result = readback.read_back(tmp_path, now=LATER)

        assert len(result.damaged) == 1
        assert _head(tmp_path).resume is None

    def test_of_an_import_names_the_import(self, tmp_path):
        (victim,) = _record(tmp_path, [_mail(1)], job=None, folder="docuware-2019")
        tamper(victim, b"cut sh")

        result = readback.read_back(tmp_path, now=LATER)

        assert result.imports == {"docuware-2019"}
        assert result.jobs == set()


class TestWhatIsLeftForNextTime:
    def test_a_place_with_a_message_that_could_not_be_read(self, tmp_path, monkeypatch):
        _record(tmp_path, [_mail(1)])

        def refuse(self, hashval):
            raise PermissionError("permission denied")

        monkeypatch.setattr(cas.ContentAddressedStorage, "verify_entry", refuse)

        result = readback.read_back(tmp_path, now=LATER)

        assert result.unreadable == 1
        assert _head(tmp_path).checked is None

    def test_a_head_a_running_backup_moved_meanwhile(self, tmp_path, monkeypatch):
        """The backup's write stands; what it added is read back next time."""
        _record(tmp_path, [_mail(1)])
        real = cas.ContentAddressedStorage.verify_entry
        moved: list[str | None] = []

        def verify_while_a_backup_writes(self, hashval):
            if not moved:
                _record(tmp_path, [_mail(2)])
                moved.append(_head(tmp_path).log)
            return real(self, hashval)

        monkeypatch.setattr(
            cas.ContentAddressedStorage, "verify_entry", verify_while_a_backup_writes
        )

        readback.read_back(tmp_path, now=LATER)

        head = _head(tmp_path)
        assert head.log == moved[0]
        assert head.checked is None


def test_compact_keeps_what_was_read_back(tmp_path):
    _record(tmp_path, [_mail(1)])
    _record(tmp_path, [_mail(2)])
    readback.read_back(tmp_path, now=LATER)

    metalog.compact(tmp_path / metalog.DEFAULT_LOG_DIR, _heads(tmp_path))

    head = _head(tmp_path)
    assert head.checked == head.log
    assert readback.read_back(tmp_path, now=LATER).read == 0
