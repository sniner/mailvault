import sys

import pytest

from mailvault import conf


def test_find():
    configs = [
        conf.JobConfig(name="Source", role="source"),
        conf.JobConfig(name="Dest", role="destination"),
    ]
    assert conf.find(configs, "role", "source") == configs[0]
    assert conf.find(configs, "role", "DESTINATION") == configs[1]  # Case insensitive
    assert conf.find(configs, "role", "other") is None


def test_find_tolerates_none_values():
    # Regression: a job without a role has role=None; find() must not crash
    # while scanning past it (None has no .casefold()).
    configs = [
        conf.JobConfig(name="NoRole"),  # role defaults to None
        conf.JobConfig(name="Source", role="source"),
    ]
    assert conf.find(configs, "role", "source") == configs[1]
    assert conf.find(configs, "role", "missing") is None


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def test_load_all_job_defaults(tmp_path):
    toml_file = tmp_path / "test.toml"
    toml_file.write_text('[[job]]\nname = "job1"\nusername = "user"\npassword = "pass"\n')
    config = conf.load(toml_file)
    assert config.compress is False
    job = config.jobs[0]
    assert job.server == "localhost"
    assert job.port == 993
    assert job.tls is True
    assert job.tls_check_hostname is True
    assert job.tls_verify_cert is True
    assert job.folders is None
    assert job.ignore_folder_flags == []
    assert job.ignore_folder_names == []
    assert job.delete_after_export is False
    assert job.exchange_journal is False
    assert job.with_metadata is True
    assert job.incremental is True
    assert job.move_to_archive is False


def test_load_unknown_job_fields_ignored(tmp_path):
    toml_file = tmp_path / "test.toml"
    toml_file.write_text('[[job]]\nname = "job1"\nserver = "test"\nunknown_field = 42\n')
    config = conf.load(toml_file)
    assert config.jobs[0].server == "test"
    assert not hasattr(config.jobs[0], "unknown_field")


def test_load_ignores_file_extension(tmp_path):
    # The content is always parsed as TOML, whatever the file is called.
    cfg_file = tmp_path / "test.conf"
    cfg_file.write_text('[[job]]\nname = "job1"\nserver = "imap.example.com"\n')
    config = conf.load(cfg_file)
    assert config.jobs[0].server == "imap.example.com"


def test_load_rejects_non_toml(tmp_path):
    # A leftover YAML config must fail loudly, not be silently misread.
    yaml_file = tmp_path / "test.yaml"
    yaml_file.write_text("job1:\n  server: a.example.com\n")
    with pytest.raises(conf.ConfigError, match="not a valid TOML"):
        conf.load(yaml_file)


def test_load_missing_file(tmp_path):
    # An unreadable config is a ConfigError too, so the CLI can report both
    # cases as a single error line instead of a traceback.
    with pytest.raises(conf.ConfigError, match="cannot read configuration"):
        conf.load(tmp_path / "does-not-exist.toml")


def test_expand_env(monkeypatch):
    monkeypatch.setenv("TEST_USER", "alice")
    assert conf._expand_env("${TEST_USER}") == "alice"
    assert conf._expand_env("user: ${TEST_USER}@example.com") == "user: alice@example.com"


def test_expand_env_default(monkeypatch):
    monkeypatch.delenv("UNSET_VAR", raising=False)
    assert conf._expand_env("${UNSET_VAR:-fallback}") == "fallback"


def test_expand_env_unset_no_default(monkeypatch):
    monkeypatch.delenv("UNSET_VAR", raising=False)
    # Unset var without default is kept as-is
    assert conf._expand_env("${UNSET_VAR}") == "${UNSET_VAR}"


def test_expand_env_no_pattern():
    assert conf._expand_env("plain string") == "plain string"


def _write_job(tmp_path, body: str):
    toml_file = tmp_path / "test.toml"
    toml_file.write_text(f'[[job]]\nname = "job1"\n{body}')
    return toml_file


def test_load_password_cmd_does_not_overwrite_explicit(tmp_path):
    """If both password and password_cmd exist, password_cmd wins (it resolves later)."""
    toml_file = _write_job(tmp_path, 'password = "old"\npassword_cmd = "echo new"\n')
    config = conf.load(toml_file, allow_exec=True)
    assert config.jobs[0].password == "new"


def test_load_with_failing_cmd(tmp_path):
    toml_file = _write_job(tmp_path, 'password_cmd = "false"\n')
    config = conf.load(toml_file, allow_exec=True)
    # Command fails, password stays at default (empty string)
    assert config.jobs[0].password == ""


def test_load_password_cmd_ignored_without_allow_exec(tmp_path):
    toml_file = _write_job(tmp_path, 'password_cmd = "echo s3cret"\n')
    config = conf.load(toml_file)
    assert config.jobs[0].password == ""


def test_non_string_values_unchanged(tmp_path):
    toml_file = _write_job(tmp_path, "port = 993\ntls = true\n")
    config = conf.load(toml_file)
    assert config.jobs[0].port == 993
    assert config.jobs[0].tls is True


def test_jobconfig_from_dict():
    job = conf.JobConfig.from_dict(
        "test",
        {
            "server": "imap.example.com",
            "port": 143,
            "username": "user",
            "password": "pass",
            "tls": False,
            "folders": ["INBOX", "Sent"],
            "ignore_folder_flags": ["Junk", "Trash"],
        },
    )
    assert job.name == "test"
    assert job.server == "imap.example.com"
    assert job.port == 143
    assert job.tls is False
    assert job.folders == ["INBOX", "Sent"]
    assert job.ignore_folder_flags == ["Junk", "Trash"]


# ---------------------------------------------------------------------------
# TOML loading
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.version_info < (3, 11), reason="tomllib requires Python 3.11+")
class TestTomlConfig:
    def test_load_toml_basic(self, tmp_path):
        toml_file = tmp_path / "test.toml"
        toml_file.write_text(
            "[global]\n"
            "compress = true\n"
            "\n"
            "[[job]]\n"
            'name = "gmail"\n'
            'server = "imap.gmail.com"\n'
            'username = "user@gmail.com"\n'
            'password = "secret"\n'
        )
        config = conf.load(toml_file)
        assert isinstance(config, conf.Config)
        assert config.compress is True
        assert len(config.jobs) == 1
        assert config.jobs[0].name == "gmail"
        assert config.jobs[0].server == "imap.gmail.com"

    def test_load_toml_multiple_jobs(self, tmp_path):
        toml_file = tmp_path / "test.toml"
        toml_file.write_text(
            "[[job]]\n"
            'name = "gmail"\n'
            'server = "imap.gmail.com"\n'
            "\n"
            "[[job]]\n"
            'name = "work"\n'
            'server = "imap.work.com"\n'
        )
        config = conf.load(toml_file)
        assert len(config.jobs) == 2
        assert config.jobs[0].name == "gmail"
        assert config.jobs[1].name == "work"

    def test_load_toml_defaults(self, tmp_path):
        toml_file = tmp_path / "test.toml"
        toml_file.write_text('[[job]]\nname = "test"\n')
        config = conf.load(toml_file)
        assert config.compress is False
        job = config.jobs[0]
        assert job.server == "localhost"
        assert job.port == 993
        assert job.tls is True

    def test_load_toml_no_global(self, tmp_path):
        toml_file = tmp_path / "test.toml"
        toml_file.write_text('[[job]]\nname = "test"\nserver = "imap.example.com"\n')
        config = conf.load(toml_file)
        assert config.compress is False
        assert len(config.jobs) == 1

    def test_load_toml_unknown_global_fields(self, tmp_path):
        toml_file = tmp_path / "test.toml"
        toml_file.write_text('[global]\nunknown_thing = 42\n\n[[job]]\nname = "test"\n')
        config = conf.load(toml_file)
        assert not hasattr(config, "unknown_thing")

    def test_load_toml_job_fields(self, tmp_path):
        toml_file = tmp_path / "test.toml"
        toml_file.write_text(
            "[[job]]\n"
            'name = "full"\n'
            'server = "imap.example.com"\n'
            "port = 143\n"
            'username = "user"\n'
            'password = "pass"\n'
            "tls = false\n"
            'folders = ["INBOX", "Sent"]\n'
            'ignore_folder_flags = ["Junk"]\n'
            "with_metadata = false\n"
            "incremental = false\n"
        )
        config = conf.load(toml_file)
        job = config.jobs[0]
        assert job.server == "imap.example.com"
        assert job.port == 143
        assert job.tls is False
        assert job.folders == ["INBOX", "Sent"]
        assert job.ignore_folder_flags == ["Junk"]
        assert job.with_metadata is False
        assert job.incremental is False

    def test_load_toml_env_expansion(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TEST_PASS", "s3cret")
        toml_file = tmp_path / "test.toml"
        toml_file.write_text('[[job]]\nname = "test"\npassword = "${TEST_PASS}"\n')
        config = conf.load(toml_file)
        assert config.jobs[0].password == "s3cret"

    def test_load_toml_password_cmd(self, tmp_path):
        toml_file = tmp_path / "test.toml"
        toml_file.write_text('[[job]]\nname = "test"\npassword_cmd = "echo s3cret"\n')
        config = conf.load(toml_file, allow_exec=True)
        assert config.jobs[0].password == "s3cret"

    def test_load_toml_copy_roles(self, tmp_path):
        toml_file = tmp_path / "test.toml"
        toml_file.write_text(
            "[[job]]\n"
            'name = "src"\n'
            'role = "source"\n'
            'server = "imap.src.com"\n'
            "\n"
            "[[job]]\n"
            'name = "dst"\n'
            'role = "destination"\n'
            'server = "imap.dst.com"\n'
        )
        config = conf.load(toml_file)
        source = conf.find(config.jobs, "role", "source")
        dest = conf.find(config.jobs, "role", "destination")
        assert source is not None
        assert source.name == "src"
        assert dest is not None
        assert dest.name == "dst"

    def test_load_toml_empty_jobs(self, tmp_path):
        toml_file = tmp_path / "test.toml"
        toml_file.write_text("[global]\ncompress = true\n")
        config = conf.load(toml_file)
        assert config.compress is True
        assert config.jobs == []


def test_with_db_still_works_and_says_so(tmp_path, caplog):
    """An old config must keep meaning what it said, not revert to a default."""
    path = tmp_path / "old.toml"
    path.write_text(
        '[[job]]\nname = "j"\nserver = "s"\nusername = "u"\npassword = "p"\nwith_db = false\n',
        encoding="utf-8",
    )

    config = conf.load(path)

    assert config.jobs[0].with_metadata is False
    assert "renamed to 'with_metadata'" in caplog.text


def test_the_new_name_wins_when_both_are_given(tmp_path):
    path = tmp_path / "both.toml"
    path.write_text(
        '[[job]]\nname = "j"\nserver = "s"\nusername = "u"\npassword = "p"\n'
        "with_db = false\nwith_metadata = true\n",
        encoding="utf-8",
    )

    assert conf.load(path).jobs[0].with_metadata is True
