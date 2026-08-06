"""Tests for the CLI front-end: how it reports a failed job and a partial conversion."""

from __future__ import annotations

import argparse
import logging
import pathlib
from datetime import UTC, datetime
from typing import Any

import pytest

from mailvault import conf, jobs
from mailvault.backend import base
from mailvault.cli import commands
from mailvault.store import cas, metalog, state

WHEN = datetime(2026, 8, 1, 18, 2, 21, tzinfo=UTC)

# A directory that does not exist, which is what an archive nobody has written
# into looks like -- the mailbox guard has nothing to compare against and lets
# every job through.
NEW_ARCHIVE = pathlib.Path("/nonexistent-archive")


def _args(**overrides: Any) -> argparse.Namespace:
    defaults: dict[str, Any] = dict(
        command="backup",
        config="mailvault.toml",
        allow_exec=False,
        job=None,
        destination=NEW_ARCHIVE,
        allow_new_mailbox=False,
        compress=False,
        index_db=False,
        full=False,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _one_job(monkeypatch, failure: Exception) -> None:
    """Make `run_mailbox` see a single job whose run raises `failure`."""
    config = conf.Config(jobs=[conf.JobConfig(name="proton.me")])
    monkeypatch.setattr(conf, "load", lambda *a, **kw: config)

    def _fail(*_a, **_kw):
        raise failure

    monkeypatch.setattr(commands, "_run_job", _fail)


class TestFullFlag:
    """`--full` vetoes the configured default rather than adding to it."""

    @staticmethod
    def _incremental_of(monkeypatch, args, config) -> bool:
        seen: dict[str, Any] = {}
        monkeypatch.setattr(jobs, "backup", lambda *a, **kw: seen.update(kw))
        commands._run_job(conf.JobConfig(name="proton.me"), args, config, NEW_ARCHIVE)
        return seen["incremental"]

    def test_full_switches_the_incremental_run_off(self, monkeypatch):
        config = conf.Config(incremental=True)

        assert self._incremental_of(monkeypatch, _args(full=True), config) is False

    def test_without_it_the_config_still_decides(self, monkeypatch):
        config = conf.Config(incremental=True)

        assert self._incremental_of(monkeypatch, _args(), config) is True

    def test_a_config_that_says_false_needs_no_flag(self, monkeypatch):
        config = conf.Config(incremental=False)

        assert self._incremental_of(monkeypatch, _args(), config) is False


class TestJobFailureReporting:
    """A diagnosed failure is one line; only a surprise is worth a traceback."""

    def test_a_refused_login_is_reported_without_a_traceback(self, monkeypatch, caplog):
        _one_job(monkeypatch, base.MailboxError("login refused for 'user': no such user"))

        with caplog.at_level(logging.DEBUG):
            assert commands.run_mailbox(_args()) == 1

        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 1
        assert "proton.me" in errors[0].getMessage()
        assert "no such user" in errors[0].getMessage()
        assert errors[0].exc_info is None

    def test_a_broken_config_is_reported_the_same_way(self, monkeypatch, caplog):
        _one_job(monkeypatch, conf.ConfigError("job 'proton.me': no such backend"))

        assert commands.run_mailbox(_args()) == 1

        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 1
        assert errors[0].exc_info is None

    def test_verbose_still_gets_the_traceback(self, monkeypatch, caplog):
        """Nothing is lost: -v turns the detail back on for the rare case it helps."""
        _one_job(monkeypatch, base.MailboxError("login refused"))

        with caplog.at_level(logging.DEBUG):
            commands.run_mailbox(_args())

        debug = [r for r in caplog.records if r.levelno == logging.DEBUG]
        assert any(r.exc_info for r in debug)

    def test_an_unexpected_error_keeps_its_traceback(self, monkeypatch, caplog):
        _one_job(monkeypatch, ValueError("something nobody thought of"))

        assert commands.run_mailbox(_args()) == 1

        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 1
        assert errors[0].exc_info is not None

    def test_the_remaining_jobs_still_run(self, monkeypatch, caplog):
        config = conf.Config(jobs=[conf.JobConfig(name="broken"), conf.JobConfig(name="fine")])
        monkeypatch.setattr(conf, "load", lambda *a, **kw: config)
        seen: list[str] = []

        def _run(job, *_a, **_kw):
            seen.append(job.name)
            if job.name == "broken":
                raise base.MailboxError("login refused")

        monkeypatch.setattr(commands, "_run_job", _run)

        assert commands.run_mailbox(_args()) == 1
        assert seen == ["broken", "fine"]


class TestArchivePath:
    """Where the archive comes from when the config names one as well."""

    def test_the_configured_one_is_used_when_the_command_line_is_silent(self):
        config = conf.Config(destination=pathlib.Path("/archive/private"))

        assert commands.archive_path(_args(destination=None), config) == config.destination

    def test_the_command_line_wins_and_says_so(self, caplog):
        config = conf.Config(destination=pathlib.Path("/archive/private"))
        args = _args(destination=pathlib.Path("/archive/scratch"))

        with caplog.at_level(logging.WARNING):
            assert commands.archive_path(args, config) == pathlib.Path("/archive/scratch")

        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any("overriding" in w for w in warnings)

    def test_naming_the_same_archive_twice_is_not_an_override(self, tmp_path, caplog):
        """Same place, written differently -- what a shell completion produces."""
        config = conf.Config(destination=tmp_path / "archive")
        args = _args(destination=tmp_path / "sub" / ".." / "archive")

        with caplog.at_level(logging.WARNING):
            commands.archive_path(args, config)

        assert not [r for r in caplog.records if r.levelno == logging.WARNING]

    def test_neither_of_them_is_an_error(self):
        with pytest.raises(conf.ConfigError, match="no archive directory"):
            commands.archive_path(_args(destination=None), conf.Config())


class TestMailboxGuard:
    """A run into an archive the jobs do not belong to is stopped beforehand."""

    @staticmethod
    def _archive(tmp_path, *mailboxes: str) -> pathlib.Path:
        s = state.SnapshotState(tmp_path / state.DEFAULT_STATE_NAME)
        for name in mailboxes:
            s.record(name, "INBOX", last_run=WHEN, resume=None)
        s.save()
        return tmp_path

    @staticmethod
    def _jobs_run(monkeypatch, jobnames: list[str]) -> list[str]:
        """Let `run_mailbox` see these jobs, and collect the ones that ran."""
        config = conf.Config(jobs=[conf.JobConfig(name=n) for n in jobnames])
        monkeypatch.setattr(conf, "load", lambda *a, **kw: config)
        seen: list[str] = []
        monkeypatch.setattr(commands, "_run_job", lambda job, *a, **kw: seen.append(job.name))
        return seen

    def test_a_configuration_that_never_wrote_here_is_refused(self, monkeypatch, tmp_path):
        archive = self._archive(tmp_path, "gmail.com", "posteo.de")
        ran = self._jobs_run(monkeypatch, ["ruhlgroup.com"])

        with pytest.raises(jobs.JobError, match="wrong configuration"):
            commands.run_mailbox(_args(destination=archive))

        assert ran == []

    def test_nothing_is_touched_before_the_check(self, monkeypatch, tmp_path):
        """The point of the exercise: it fails before the first job, not during."""
        archive = self._archive(tmp_path, "gmail.com")
        ran = self._jobs_run(monkeypatch, ["gmail.com", "ruhlgroup.com"])

        with pytest.raises(jobs.JobError):
            commands.run_mailbox(_args(destination=archive))

        assert ran == []

    def test_the_flag_lets_a_genuinely_new_job_through(self, monkeypatch, tmp_path):
        archive = self._archive(tmp_path, "gmail.com")
        ran = self._jobs_run(monkeypatch, ["posteo.de"])

        assert commands.run_mailbox(_args(destination=archive, allow_new_mailbox=True)) == 0
        assert ran == ["posteo.de"]

    def test_a_known_job_needs_no_flag(self, monkeypatch, tmp_path):
        archive = self._archive(tmp_path, "gmail.com", "posteo.de")
        ran = self._jobs_run(monkeypatch, ["gmail.com"])

        assert commands.run_mailbox(_args(destination=archive)) == 0
        assert ran == ["gmail.com"]

    def test_a_new_job_among_known_ones_reads_as_a_new_job(self, monkeypatch, tmp_path):
        """Not the same complaint: one unfamiliar name is rarely the wrong file."""
        archive = self._archive(tmp_path, "gmail.com")
        self._jobs_run(monkeypatch, ["gmail.com", "posteo.de"])

        with pytest.raises(jobs.JobError, match="posteo.de has not written here before"):
            commands.run_mailbox(_args(destination=archive))

    def test_a_job_the_config_no_longer_has_is_nobodys_business(self, monkeypatch, tmp_path):
        """Removing a job cannot put a message anywhere, so it is not reported."""
        archive = self._archive(tmp_path, "gmail.com", "posteo.de", "proton.me")
        ran = self._jobs_run(monkeypatch, ["gmail.com"])

        assert commands.run_mailbox(_args(destination=archive)) == 0
        assert ran == ["gmail.com"]

    def test_an_empty_archive_takes_anything(self, monkeypatch, tmp_path):
        ran = self._jobs_run(monkeypatch, ["gmail.com"])

        assert commands.run_mailbox(_args(destination=tmp_path / "new")) == 0
        assert ran == ["gmail.com"]

    def test_folders_needs_no_archive_at_all(self, monkeypatch):
        """It only ever talks to the server, so there is nothing to guard."""
        ran = self._jobs_run(monkeypatch, ["gmail.com"])

        assert commands.run_mailbox(_args(command="folders", destination=None)) == 0
        assert ran == ["gmail.com"]


@pytest.mark.parametrize(
    "error",
    [conf.ConfigError("x"), jobs.JobError("x"), base.MailboxError("x")],
)
def test_every_expected_error_is_recognised_as_one(error):
    assert isinstance(error, commands.EXPECTED_ERRORS)


def test_archive_decompress_reports_what_it_could_not_convert(tmp_path, capsys):
    """A conversion pass that left files behind must not report success.

    The pass keeps going when one entry fails, so the exit status is the only
    thing a script has to go on -- and a run that converted all but one file is
    exactly the case worth noticing.
    """
    root = tmp_path / "cas"
    store = cas.ContentAddressedStorage(root_dir=root, suffix=".eml", compress=True)
    _, _, good = store.add(b"a real message")
    _, _, broken = store.add(b"about to be corrupted")
    broken.write_bytes(b"this is not a zstd frame")

    exit_code = commands.run_archive(
        argparse.Namespace(archive_command="decompress", source=root)
    )

    assert exit_code == 1
    out = capsys.readouterr().out
    assert "1 files decompressed" in out
    assert str(broken) in out
    assert "1 file(s) failed" in out
    assert store.read(good.with_suffix("")) == b"a real message"
    assert broken.exists(), "what could not be converted is left as it is"


def test_archive_decompress_that_works_exits_zero(tmp_path, capsys):
    root = tmp_path / "cas"
    store = cas.ContentAddressedStorage(root_dir=root, suffix=".eml", compress=True)
    store.add(b"a real message")

    exit_code = commands.run_archive(
        argparse.Namespace(archive_command="decompress", source=root)
    )

    assert exit_code == 0
    assert "failed" not in capsys.readouterr().out


def _check_args(source, **overrides):
    defaults = dict(
        archive_command="check", source=source, no_integrity_check=False, quarantine=False
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _archive_with_a_log(root, extra_store_ids=()):
    store = cas.ContentAddressedStorage(root_dir=root, suffix=".eml")
    _status, store_id, _path = store.add(b"a real message")
    writer = metalog.LogWriter(root / metalog.DEFAULT_LOG_DIR)
    for known in (store_id, *extra_store_ids):
        writer.add("job", ["INBOX"], known)
    writer.seal(datetime(2026, 8, 1, tzinfo=UTC))
    return store, store_id


def test_archive_check_that_finds_nothing_exits_zero(tmp_path, capsys):
    _archive_with_a_log(tmp_path)

    exit_code = commands.run_archive(_check_args(tmp_path, no_integrity_check=True))

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "1 message(s) stored" in out
    assert "the integrity check was skipped" in out, "a run that did not look must say so"


def test_a_check_that_read_the_contents_says_so_rather_than_just_exiting_zero(tmp_path, capsys):
    _archive_with_a_log(tmp_path)

    exit_code = commands.run_archive(_check_args(tmp_path))

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "sound -- every message was read and matches its checksum" in out
    assert "as far as this went" not in out, "that is the other kind of clean run"


def test_archive_check_exits_non_zero_when_the_archive_is_not_what_it_claims(tmp_path, capsys):
    _archive_with_a_log(tmp_path, extra_store_ids=["aa" * 48])

    exit_code = commands.run_archive(_check_args(tmp_path))

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "1 message(s) referenced in the log are missing" in out
    assert "NOT sound -- 1 finding(s)" in out, "the verdict, not just the exit code"


def test_archive_check_quarantine_without_the_integrity_check_is_refused(tmp_path):
    _archive_with_a_log(tmp_path)

    with pytest.raises(jobs.JobError, match="cannot be combined with --no-integrity-check"):
        commands.run_archive(_check_args(tmp_path, quarantine=True, no_integrity_check=True))
