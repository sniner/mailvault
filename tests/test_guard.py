"""Tests for mailvault.jobs.guard (does this job belong to this archive?)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest

from mailvault import conf
from mailvault.jobs import guard
from mailvault.jobs.common import JobError
from mailvault.store import metalog, state

WHEN = datetime(2026, 8, 1, 18, 2, 21, tzinfo=UTC)
STORE_ID = "df3823f1cd1638d0f374745bb0e200e3"


def _with_state(root, *mailboxes: str):
    s = state.SnapshotState(root / state.DEFAULT_STATE_NAME)
    for name in mailboxes:
        s.record(name, "INBOX", last_run=WHEN, resume=None)
    s.save()


def _with_log(root, *mailboxes: str):
    writer = metalog.LogWriter(root / metalog.DEFAULT_LOG_DIR)
    for name in mailboxes:
        writer.add(name, ["INBOX"], STORE_ID)
    writer.seal(WHEN)


def _jobs(*names: str) -> list[conf.JobConfig]:
    return [conf.JobConfig(name=n) for n in names]


class TestKnownMailboxes:
    def test_a_directory_that_does_not_exist_knows_nobody(self, tmp_path):
        assert guard.known_mailboxes(tmp_path / "nothing-here") == set()

    def test_the_state_file_answers_it(self, tmp_path):
        _with_state(tmp_path, "gmail.com", "posteo.de")

        assert guard.known_mailboxes(tmp_path) == {"gmail.com", "posteo.de"}

    def test_the_log_answers_it_when_there_is_no_state_file(self, tmp_path):
        """The case the guard must not wave through: a lost state.json."""
        _with_log(tmp_path, "gmail.com", "posteo.de")

        assert guard.known_mailboxes(tmp_path) == {"gmail.com", "posteo.de"}

    def test_an_unusable_state_file_falls_back_to_the_log(self, tmp_path):
        (tmp_path / state.DEFAULT_STATE_NAME).write_text("{ not json", encoding="utf-8")
        _with_log(tmp_path, "gmail.com")

        assert guard.known_mailboxes(tmp_path) == {"gmail.com"}


class TestCheckJobs:
    def test_a_known_job_passes(self, tmp_path):
        _with_state(tmp_path, "gmail.com")

        guard.check_jobs(tmp_path, _jobs("gmail.com"))

    def test_an_unknown_job_is_refused(self, tmp_path):
        _with_state(tmp_path, "gmail.com")

        with pytest.raises(JobError, match="--allow-new-mailbox"):
            guard.check_jobs(tmp_path, _jobs("gmail.com", "ruhlgroup.com"))

    def test_the_message_names_both_sides(self, tmp_path):
        """Enough to decide without opening anything: who is here, who is not."""
        _with_state(tmp_path, "gmail.com")

        with pytest.raises(JobError) as excinfo:
            guard.check_jobs(tmp_path, _jobs("ruhlgroup.com"))

        message = str(excinfo.value)
        assert "gmail.com" in message
        assert "ruhlgroup.com" in message
        assert str(tmp_path) in message

    def test_the_override_passes_and_leaves_a_warning(self, tmp_path, caplog):
        _with_state(tmp_path, "gmail.com")

        with caplog.at_level(logging.WARNING):
            guard.check_jobs(tmp_path, _jobs("ruhlgroup.com"), allow_new=True)

        assert any("ruhlgroup.com" in r.getMessage() for r in caplog.records)

    def test_an_archive_nobody_wrote_into_takes_anything(self, tmp_path):
        guard.check_jobs(tmp_path, _jobs("gmail.com"))

    def test_no_jobs_left_to_run_is_not_a_mismatch(self, tmp_path):
        """`--job` may select nothing; that is reported elsewhere, not here."""
        _with_state(tmp_path, "gmail.com")

        guard.check_jobs(tmp_path, [])
