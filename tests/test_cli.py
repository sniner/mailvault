"""Tests for the CLI front-end: how it reports a failed job and a partial conversion."""

from __future__ import annotations

import argparse
import logging
from typing import Any

import pytest

from mailvault import conf, jobs
from mailvault.backend import base
from mailvault.cli import commands
from mailvault.store import cas


def _args(**overrides: Any) -> argparse.Namespace:
    defaults: dict[str, Any] = dict(
        command="backup",
        config="mailvault.toml",
        allow_exec=False,
        job=None,
        destination=None,
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
        commands._run_job(conf.JobConfig(name="proton.me"), args, config)
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
