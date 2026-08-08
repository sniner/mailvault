"""Tests for the CLI front-end: how it reports a failed job and a partial conversion."""

from __future__ import annotations

import argparse
import logging
import pathlib
import tempfile
from datetime import UTC, datetime
from typing import Any

import pytest

from mailvault import cli, conf, jobs
from mailvault.backend import base
from mailvault.cli import commands
from mailvault.store import cas, heads, marker, metalog

WHEN = datetime(2026, 8, 1, 18, 2, 21, tzinfo=UTC)

# An archive nobody has written into yet: made by `archive init`, marked, and
# empty. The mailbox guard has nothing to compare against and lets every job
# through. Marked, because that is what makes a directory an archive -- every
# command asks before it does anything.
NEW_ARCHIVE = pathlib.Path(tempfile.mkdtemp(prefix="mailvault-empty-archive-"))
marker.write(NEW_ARCHIVE)


def _args(**overrides: Any) -> argparse.Namespace:
    defaults: dict[str, Any] = dict(
        command="backup",
        archive=NEW_ARCHIVE,
        config=None,
        allow_exec=False,
        job=None,
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


class TestArchiveAndConfig:
    """Two independent knobs: where the archive is, and which file describes it."""

    def test_the_archive_is_where_you_are_standing(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)

        assert commands.archive_path(_args(archive=None)) == tmp_path

    def test_or_wherever_the_option_points(self):
        assert commands.archive_path(_args(archive=NEW_ARCHIVE)) == NEW_ARCHIVE

    def test_the_configuration_comes_out_of_the_archive(self):
        wanted = NEW_ARCHIVE / commands.DEFAULT_CONFIG_NAME

        assert commands.config_file(_args(), NEW_ARCHIVE) == wanted

    def test_unless_one_is_named(self):
        named = pathlib.Path("/home/jd/private.toml")

        assert commands.config_file(_args(config=named), NEW_ARCHIVE) == named

    def test_a_named_configuration_without_an_archive_is_refused(self, monkeypatch, tmp_path):
        """Reaching elsewhere for the file says one is not standing in the archive.

        There is nothing left to derive the archive from -- a configuration
        names none any more -- and the directory one happens to be in is the
        last thing that should decide where the mail goes.
        """
        monkeypatch.chdir(tmp_path)
        args = _args(archive=None, config=pathlib.Path("/home/jd/private.toml"))

        with pytest.raises(conf.ConfigError, match="no archive"):
            commands.run_mailbox(args)

    def test_an_archive_without_a_configuration_says_which_rule_looked(
        self, monkeypatch, tmp_path
    ):
        """Naming the path alone leaves a reader wondering why that path."""
        marker.write(tmp_path)
        monkeypatch.chdir(tmp_path)

        with pytest.raises(conf.ConfigError, match="an archive carries its own"):
            commands.run_mailbox(_args(archive=None))

    def test_folders_needs_no_archive_and_so_takes_one_anyway(self, monkeypatch, tmp_path):
        """It only talks to the server, so nothing about it can go anywhere wrong."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(conf, "load", lambda *a, **kw: conf.Config())
        args = _args(command="folders", archive=None, config=pathlib.Path("/home/jd/p.toml"))

        assert commands.run_mailbox(args) == 0


class TestMailboxGuard:
    """A run into an archive the jobs do not belong to is stopped beforehand."""

    @staticmethod
    def _archive(tmp_path, *mailboxes: str) -> pathlib.Path:
        marker.write(tmp_path)
        for name in mailboxes:
            heads.write(
                tmp_path / heads.DEFAULT_HEADS_DIR,
                heads.Head(job=name, folder="INBOX", last_run=WHEN.isoformat()),
            )
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
            commands.run_mailbox(_args(archive=archive))

        assert ran == []

    def test_nothing_is_touched_before_the_check(self, monkeypatch, tmp_path):
        """The point of the exercise: it fails before the first job, not during."""
        archive = self._archive(tmp_path, "gmail.com")
        ran = self._jobs_run(monkeypatch, ["gmail.com", "ruhlgroup.com"])

        with pytest.raises(jobs.JobError):
            commands.run_mailbox(_args(archive=archive))

        assert ran == []

    def test_the_flag_lets_a_genuinely_new_job_through(self, monkeypatch, tmp_path):
        archive = self._archive(tmp_path, "gmail.com")
        ran = self._jobs_run(monkeypatch, ["posteo.de"])

        assert commands.run_mailbox(_args(archive=archive, allow_new_mailbox=True)) == 0
        assert ran == ["posteo.de"]

    def test_a_known_job_needs_no_flag(self, monkeypatch, tmp_path):
        archive = self._archive(tmp_path, "gmail.com", "posteo.de")
        ran = self._jobs_run(monkeypatch, ["gmail.com"])

        assert commands.run_mailbox(_args(archive=archive)) == 0
        assert ran == ["gmail.com"]

    def test_a_new_job_among_known_ones_reads_as_a_new_job(self, monkeypatch, tmp_path):
        """Not the same complaint: one unfamiliar name is rarely the wrong file."""
        archive = self._archive(tmp_path, "gmail.com")
        self._jobs_run(monkeypatch, ["gmail.com", "posteo.de"])

        with pytest.raises(jobs.JobError, match="posteo.de has not written here before"):
            commands.run_mailbox(_args(archive=archive))

    def test_a_job_the_config_no_longer_has_is_nobodys_business(self, monkeypatch, tmp_path):
        """Removing a job cannot put a message anywhere, so it is not reported."""
        archive = self._archive(tmp_path, "gmail.com", "posteo.de", "proton.me")
        ran = self._jobs_run(monkeypatch, ["gmail.com"])

        assert commands.run_mailbox(_args(archive=archive)) == 0
        assert ran == ["gmail.com"]

    def test_an_empty_archive_takes_anything(self, monkeypatch, tmp_path):
        ran = self._jobs_run(monkeypatch, ["gmail.com"])
        fresh = tmp_path / "new"
        fresh.mkdir()
        marker.write(fresh)

        assert commands.run_mailbox(_args(archive=fresh)) == 0
        assert ran == ["gmail.com"]

    def test_folders_needs_no_archive_at_all(self, monkeypatch):
        """It only ever talks to the server, so there is nothing to guard."""
        ran = self._jobs_run(monkeypatch, ["gmail.com"])

        assert commands.run_mailbox(_args(command="folders", archive=None)) == 0
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
    root.mkdir()
    marker.write(root)
    store = cas.mail_store(root, compress=True)
    _, _, good = store.add(b"a real message")
    _, _, broken = store.add(b"about to be corrupted")
    broken.write_bytes(b"this is not a zstd frame")

    exit_code = commands.run_archive(
        argparse.Namespace(archive_command="decompress", archive=root)
    )

    assert exit_code == 1
    out = capsys.readouterr().out
    assert "1 files decompressed" in out
    assert str(broken.relative_to(root)) in out, "named as it reads inside the archive"
    assert str(root) not in out, "the archive is named once, not on every line"
    assert "1 file(s) failed" in out
    assert store.read(good.with_suffix("")) == b"a real message"
    assert broken.exists(), "what could not be converted is left as it is"


def test_archive_decompress_that_works_exits_zero(tmp_path, capsys):
    root = tmp_path / "cas"
    root.mkdir()
    marker.write(root)
    store = cas.mail_store(root, compress=True)
    store.add(b"a real message")

    exit_code = commands.run_archive(
        argparse.Namespace(archive_command="decompress", archive=root)
    )

    assert exit_code == 0
    assert "failed" not in capsys.readouterr().out


def _check_args(archive, **overrides):
    defaults = dict(
        archive_command="check", archive=archive, no_integrity_check=False, quarantine=False
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _archive_with_a_log(root, extra_store_ids=()):
    marker.write(root)
    store = cas.mail_store(root)
    _status, store_id, _path = store.add(b"a real message")
    writer = metalog.LogWriter(root / metalog.DEFAULT_LOG_DIR, root / heads.DEFAULT_HEADS_DIR)
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


def test_archive_check_takes_the_directory_you_are_standing_in(tmp_path, monkeypatch, capsys):
    """The whole point: in the archive, `mailvault archive check` and nothing else."""
    _archive_with_a_log(tmp_path)
    monkeypatch.chdir(tmp_path)

    exit_code = commands.run_archive(_check_args(None))

    assert exit_code == 0
    assert "1 message(s) stored" in capsys.readouterr().out


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


class TestExport:
    """Getting a stored message back out, by store id or by the path a report printed."""

    @staticmethod
    def _archive(tmp_path, compress=False):
        marker.write(tmp_path)
        store = cas.mail_store(tmp_path, compress=compress)
        _status, store_id, path = store.add(b"From: a@b\r\nSubject: hello\r\n\r\nbody")
        return store_id, path

    @staticmethod
    def _export_args(archive, entry, output=None):
        return argparse.Namespace(
            archive_command="export", archive=archive, entry=entry, output=output
        )

    def test_a_store_id_goes_to_standard_output(self, tmp_path, capsysbinary):
        store_id, _path = self._archive(tmp_path)

        assert commands.run_archive(self._export_args(tmp_path, [store_id])) == 0

        assert capsysbinary.readouterr().out == b"From: a@b\r\nSubject: hello\r\n\r\nbody"

    def test_the_path_a_report_printed_works_just_as_well(self, tmp_path, capsysbinary):
        _store_id, path = self._archive(tmp_path)

        assert commands.run_archive(self._export_args(tmp_path, [str(path)])) == 0

        assert capsysbinary.readouterr().out == b"From: a@b\r\nSubject: hello\r\n\r\nbody"

    def test_a_compressed_entry_comes_out_as_it_went_in(self, tmp_path, capsysbinary):
        """The whole point over `cat`: the file on disk is a zstd frame."""
        store_id, path = self._archive(tmp_path, compress=True)
        assert path.suffix == ".zst"

        commands.run_archive(self._export_args(tmp_path, [store_id]))

        assert capsysbinary.readouterr().out == b"From: a@b\r\nSubject: hello\r\n\r\nbody"

    def test_output_writes_a_file(self, tmp_path):
        store_id, _path = self._archive(tmp_path)
        target = tmp_path / "out.eml"

        commands.run_archive(self._export_args(tmp_path, [store_id], output=target))

        assert target.read_bytes() == b"From: a@b\r\nSubject: hello\r\n\r\nbody"

    def test_several_messages_need_somewhere_to_go(self, tmp_path):
        store_id, _path = self._archive(tmp_path)

        with pytest.raises(jobs.JobError, match="need --output"):
            commands.run_archive(self._export_args(tmp_path, [store_id, store_id]))

    def test_a_store_id_the_archive_does_not_have(self, tmp_path):
        self._archive(tmp_path)

        with pytest.raises(jobs.JobError, match="not in this archive"):
            commands.run_archive(self._export_args(tmp_path, ["ab" * 48]))

    def test_something_that_is_neither_an_id_nor_a_path(self, tmp_path):
        self._archive(tmp_path)

        with pytest.raises(jobs.JobError, match="neither a store id nor the path"):
            commands.run_archive(self._export_args(tmp_path, ["Subject: hello"]))

    def test_a_bare_file_name_is_enough(self, tmp_path):
        """What is left after copying a path out of a report and cutting it short."""
        _store_id, path = self._archive(tmp_path)

        commands.run_archive(self._export_args(tmp_path, [path.name], output=tmp_path / "o"))

        assert (tmp_path / "o").read_bytes() == b"From: a@b\r\nSubject: hello\r\n\r\nbody"


class TestWhereTheOptionsLive:
    """An option sits on the level it applies to, and nowhere else.

    `--job` and the two permissions say what one command should do; only the
    archive, the configuration and how loud the run is are true of every command.
    The reading order follows from that -- `backup --job proton.me`, which is how
    everybody writes it anyway.
    """

    @staticmethod
    def _parse(argv: list[str]) -> argparse.Namespace:
        return cli.build_parser().parse_args(argv)

    def test_a_command_takes_its_own_options_after_it(self):
        args = self._parse(["backup", "--job", "proton.me", "--allow-exec"])

        assert args.job == ["proton.me"]
        assert args.allow_exec is True

    def test_the_option_may_be_repeated(self):
        assert self._parse(["backup", "--job", "a", "--job", "b"]).job == ["a", "b"]

    def test_it_is_not_a_global_option_any_more(self):
        with pytest.raises(SystemExit):
            self._parse(["--job", "proton.me", "backup"])

    def test_folders_selects_jobs_but_writes_nothing(self):
        args = self._parse(["folders", "--job", "proton.me"])

        assert args.job == ["proton.me"]
        assert not hasattr(args, "allow_new_mailbox")

    def test_a_command_that_writes_can_be_told_a_job_is_new(self):
        assert self._parse(["backup", "--allow-new-mailbox"]).allow_new_mailbox is True
        assert self._parse(["verify", "--allow-new-mailbox"]).allow_new_mailbox is True

    def test_an_archive_command_has_no_use_for_any_of_them(self):
        args = self._parse(["archive", "check"])

        assert not hasattr(args, "job")
        assert not hasattr(args, "allow_exec")

    def test_the_archive_stays_where_it_is(self):
        """Which archive is true of the whole run, so it keeps its place in front."""
        args = self._parse(["--archive", "/srv/mail", "archive", "check"])

        assert args.archive == pathlib.Path("/srv/mail")


class TestImportSource:
    """An import reads from somewhere else -- that is what makes it an import."""

    @staticmethod
    def _import_args(archive: pathlib.Path, source: pathlib.Path, move: bool = True):
        return argparse.Namespace(
            command="archive",
            archive_command="import",
            archive=archive,
            source=source,
            docuware=False,
            move=move,
            compress=False,
            dry_run=False,
        )

    def test_the_archive_itself_is_refused(self, tmp_path):
        """`cd <archive> && archive import --move .` used to empty the archive."""
        marker.write(tmp_path)
        store = cas.mail_store(tmp_path)
        store.add(b"Message-Id: <a@example.com>\r\n\r\nbody\r\n")

        with pytest.raises(jobs.JobError, match="this is the archive"):
            commands.run_archive(self._import_args(tmp_path, tmp_path))

        assert len(list((tmp_path / "mail").rglob("*.eml"))) == 1

    def test_a_directory_inside_the_archive_is_refused(self, tmp_path):
        marker.write(tmp_path)
        with pytest.raises(jobs.JobError, match="this is the archive"):
            commands.run_archive(self._import_args(tmp_path, tmp_path / "mail"))

    def test_an_archive_below_the_source_is_refused(self, tmp_path):
        """The other way round empties it just as thoroughly."""
        archive = tmp_path / "archive"
        cas.mail_store(archive)
        marker.write(archive)

        with pytest.raises(jobs.JobError, match="this is the archive"):
            commands.run_archive(self._import_args(archive, tmp_path))

    def test_refused_without_move_as_well(self, tmp_path):
        """One rule beats one that depends on a flag."""
        marker.write(tmp_path)
        with pytest.raises(jobs.JobError, match="this is the archive"):
            commands.run_archive(self._import_args(tmp_path, tmp_path, move=False))

    def test_a_source_elsewhere_is_imported(self, tmp_path):
        source = tmp_path / "elsewhere"
        source.mkdir()
        (source / "a.eml").write_bytes(b"Message-Id: <a@example.com>\r\n\r\nbody\r\n")
        archive = tmp_path / "archive"
        archive.mkdir()
        marker.write(archive)

        assert commands.run_archive(self._import_args(archive, source, move=True)) == 0
        assert len(list((archive / "mail").rglob("*.eml"))) == 1
        assert not (source / "a.eml").exists()


class TestAnArchiveIsAMarkedDirectory:
    """`FORMAT` answers "is this an archive", the way `.git` does for a repository.

    Before this, every command opened `<directory>/mail` and worked on whatever
    was there. On an archive from before 0.10 that is nothing -- the messages are
    still in the root -- so `archive check` called a healthy archive a total loss
    and `verify --repair` set about downloading the mailbox again.
    """

    @staticmethod
    def _old_archive(root: pathlib.Path) -> pathlib.Path:
        """An archive in the pre-0.10 layout: shards in the root, no mark."""
        old = cas.ContentAddressedStorage(root, suffix=".eml", depth=2)
        _status, store_id, _path = old.add(b"Message-Id: <a@example.com>\r\n\r\nbody\r\n")
        writer = metalog.LogWriter(
            root / metalog.DEFAULT_LOG_DIR, root / heads.DEFAULT_HEADS_DIR
        )
        writer.add("job", ["INBOX"], store_id)
        writer.seal(WHEN)
        return root

    def test_check_refuses_an_unmigrated_archive_instead_of_calling_it_broken(self, tmp_path):
        self._old_archive(tmp_path)

        with pytest.raises(jobs.JobError, match="archive migrate"):
            commands.run_archive(_check_args(tmp_path))

    def test_backup_refuses_it_too(self, monkeypatch, tmp_path):
        self._old_archive(tmp_path)
        monkeypatch.setattr(conf, "load", lambda *a, **kw: conf.Config(jobs=[]))

        with pytest.raises(jobs.JobError, match="not a mailvault archive"):
            commands.run_mailbox(_args(archive=tmp_path))

    def test_the_message_names_both_ways_out(self, tmp_path):
        """An older archive is lifted; a wrong directory is left alone."""
        (tmp_path / "not-an-archive.txt").write_text("hello")

        with pytest.raises(jobs.JobError) as caught:
            commands.run_archive(_check_args(tmp_path))

        assert "archive init" in str(caught.value)
        assert "archive migrate" in str(caught.value)

    def test_migrate_is_the_one_command_that_takes_an_unmarked_directory(self, tmp_path):
        self._old_archive(tmp_path)

        assert (
            commands.run_archive(
                argparse.Namespace(archive_command="migrate", archive=tmp_path)
            )
            == 0
        )
        assert marker.is_archive(tmp_path)

    def test_and_afterwards_check_is_happy(self, tmp_path):
        self._old_archive(tmp_path)
        commands.run_archive(argparse.Namespace(archive_command="migrate", archive=tmp_path))

        assert commands.run_archive(_check_args(tmp_path)) == 0


class TestArchiveInit:
    """What `git init` is: the directory becomes an archive, and says so."""

    @staticmethod
    def _init_args(archive: pathlib.Path):
        return argparse.Namespace(archive_command="init", archive=archive)

    def test_it_makes_the_three_directories_the_mark_and_a_configuration(self, tmp_path):
        assert commands.run_archive(self._init_args(tmp_path)) == 0

        assert marker.is_archive(tmp_path)
        for name in (cas.MAIL_DIR, metalog.DEFAULT_LOG_DIR, heads.DEFAULT_HEADS_DIR):
            assert (tmp_path / name).is_dir()
        assert (tmp_path / commands.DEFAULT_CONFIG_NAME).is_file()

    def test_the_archive_works_from_then_on(self, tmp_path):
        commands.run_archive(self._init_args(tmp_path))

        assert commands.run_archive(_check_args(tmp_path)) == 0

    def test_running_it_again_changes_nothing(self, tmp_path):
        commands.run_archive(self._init_args(tmp_path))
        (tmp_path / commands.DEFAULT_CONFIG_NAME).write_text("# mine\n")

        assert commands.run_archive(self._init_args(tmp_path)) == 0
        assert (tmp_path / commands.DEFAULT_CONFIG_NAME).read_text() == "# mine\n"

    def test_a_directory_with_something_in_it_is_refused(self, tmp_path):
        """Marking an unmigrated archive would claim a layout it is not in."""
        (tmp_path / "something").write_text("not mine to overwrite")

        with pytest.raises(jobs.JobError, match="archive migrate"):
            commands.run_archive(self._init_args(tmp_path))

        assert not marker.is_archive(tmp_path)


class TestExitCodes:
    """A pass that did not do what it was asked must not report success."""

    def test_a_format_error_is_a_message_and_not_a_traceback(self):
        """Two machines, one shared archive, one still on the old version."""
        assert isinstance(marker.FormatError("x"), commands.EXPECTED_ERRORS)

    def test_a_migration_that_did_not_finish_exits_non_zero(
        self, tmp_path, monkeypatch, capsys
    ):
        """The mark is written last, so carrying it means every step got through."""
        monkeypatch.setattr(
            metalog,
            "compact",
            lambda *a, **kw: metalog.CompactResult(files_before=2, verified=False),
        )

        exit_code = commands.run_archive(
            argparse.Namespace(archive_command="migrate", archive=tmp_path)
        )

        assert exit_code == 1
        assert "NOT marked" in capsys.readouterr().out

    def test_a_finished_migration_exits_zero(self, tmp_path):
        assert (
            commands.run_archive(
                argparse.Namespace(archive_command="migrate", archive=tmp_path)
            )
            == 0
        )

    def test_a_compaction_that_verified_nothing_exits_non_zero(self, tmp_path, monkeypatch):
        marker.write(tmp_path)
        monkeypatch.setattr(
            metalog,
            "compact",
            lambda *a, **kw: metalog.CompactResult(files_before=2, verified=False),
        )

        exit_code = commands.run_archive(
            argparse.Namespace(archive_command="compact", archive=tmp_path)
        )

        assert exit_code == 1

    def test_a_compaction_that_worked_exits_zero(self, tmp_path):
        marker.write(tmp_path)
        _archive_with_a_log(tmp_path)

        assert (
            commands.run_archive(
                argparse.Namespace(archive_command="compact", archive=tmp_path)
            )
            == 0
        )
