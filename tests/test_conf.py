import sys

import pytest

from imapbackup import conf


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
# YAML loading
# ---------------------------------------------------------------------------

def test_load_yaml(tmp_path):
    yaml_file = tmp_path / "test.yaml"
    yaml_file.write_text("job1:\n  server: a.example.com\njob2:\n  server: b.example.com\n")
    config = conf.load(yaml_file)
    assert isinstance(config, conf.Config)
    assert len(config.jobs) == 2
    assert config.jobs[0].name == "job1"
    assert config.jobs[0].server == "a.example.com"
    assert config.jobs[1].name == "job2"
    assert config.jobs[1].server == "b.example.com"


def test_load_yaml_defaults(tmp_path):
    yaml_file = tmp_path / "test.yaml"
    yaml_file.write_text("job1:\n  username: user\n  password: pass\n")
    config = conf.load(yaml_file)
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
    assert job.with_db is True
    assert job.incremental is True
    assert job.move_to_archive is False


def test_load_yaml_unknown_fields_ignored(tmp_path):
    yaml_file = tmp_path / "test.yaml"
    yaml_file.write_text("job1:\n  server: test\n  unknown_field: 42\n")
    config = conf.load(yaml_file)
    assert config.jobs[0].server == "test"
    assert not hasattr(config.jobs[0], "unknown_field")


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


def test_load_yaml_with_env_expansion(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_PASS", "s3cret")
    yaml_file = tmp_path / "test.yaml"
    yaml_file.write_text('job1:\n  server: "imap.example.com"\n  password: "${TEST_PASS}"\n')
    config = conf.load(yaml_file)
    assert config.jobs[0].password == "s3cret"


def test_load_yaml_with_password_cmd(tmp_path):
    yaml_file = tmp_path / "test.yaml"
    yaml_file.write_text('job1:\n  server: "imap.example.com"\n  password_cmd: "echo s3cret"\n')
    config = conf.load(yaml_file, allow_exec=True)
    assert config.jobs[0].password == "s3cret"


def test_load_yaml_password_cmd_does_not_overwrite_explicit(tmp_path):
    """If both password and password_cmd exist, password_cmd wins (it resolves later)."""
    yaml_file = tmp_path / "test.yaml"
    yaml_file.write_text('job1:\n  password: "old"\n  password_cmd: "echo new"\n')
    config = conf.load(yaml_file, allow_exec=True)
    assert config.jobs[0].password == "new"


def test_load_yaml_with_failing_cmd(tmp_path):
    yaml_file = tmp_path / "test.yaml"
    yaml_file.write_text('job1:\n  password_cmd: "false"\n')
    config = conf.load(yaml_file, allow_exec=True)
    # Command fails, password stays at default (empty string)
    assert config.jobs[0].password == ""


def test_load_yaml_password_cmd_ignored_without_allow_exec(tmp_path):
    yaml_file = tmp_path / "test.yaml"
    yaml_file.write_text('job1:\n  password_cmd: "echo s3cret"\n')
    config = conf.load(yaml_file)
    assert config.jobs[0].password == ""


def test_non_string_values_unchanged(tmp_path):
    yaml_file = tmp_path / "test.yaml"
    yaml_file.write_text("job1:\n  port: 993\n  tls: true\n")
    config = conf.load(yaml_file)
    assert config.jobs[0].port == 993
    assert config.jobs[0].tls is True


def test_jobconfig_from_dict():
    job = conf.JobConfig.from_dict("test", {
        "server": "imap.example.com",
        "port": 143,
        "username": "user",
        "password": "pass",
        "tls": False,
        "folders": ["INBOX", "Sent"],
        "ignore_folder_flags": ["Junk", "Trash"],
    })
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
            '[global]\n'
            'compress = true\n'
            '\n'
            '[[job]]\n'
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
            '[[job]]\n'
            'name = "gmail"\n'
            'server = "imap.gmail.com"\n'
            '\n'
            '[[job]]\n'
            'name = "work"\n'
            'server = "imap.work.com"\n'
        )
        config = conf.load(toml_file)
        assert len(config.jobs) == 2
        assert config.jobs[0].name == "gmail"
        assert config.jobs[1].name == "work"

    def test_load_toml_defaults(self, tmp_path):
        toml_file = tmp_path / "test.toml"
        toml_file.write_text(
            '[[job]]\n'
            'name = "test"\n'
        )
        config = conf.load(toml_file)
        assert config.compress is False
        job = config.jobs[0]
        assert job.server == "localhost"
        assert job.port == 993
        assert job.tls is True

    def test_load_toml_no_global(self, tmp_path):
        toml_file = tmp_path / "test.toml"
        toml_file.write_text(
            '[[job]]\n'
            'name = "test"\n'
            'server = "imap.example.com"\n'
        )
        config = conf.load(toml_file)
        assert config.compress is False
        assert len(config.jobs) == 1

    def test_load_toml_unknown_global_fields(self, tmp_path):
        toml_file = tmp_path / "test.toml"
        toml_file.write_text(
            '[global]\n'
            'unknown_thing = 42\n'
            '\n'
            '[[job]]\n'
            'name = "test"\n'
        )
        config = conf.load(toml_file)
        assert not hasattr(config, "unknown_thing")

    def test_load_toml_job_fields(self, tmp_path):
        toml_file = tmp_path / "test.toml"
        toml_file.write_text(
            '[[job]]\n'
            'name = "full"\n'
            'server = "imap.example.com"\n'
            'port = 143\n'
            'username = "user"\n'
            'password = "pass"\n'
            'tls = false\n'
            'folders = ["INBOX", "Sent"]\n'
            'ignore_folder_flags = ["Junk"]\n'
            'with_db = false\n'
            'incremental = false\n'
        )
        config = conf.load(toml_file)
        job = config.jobs[0]
        assert job.server == "imap.example.com"
        assert job.port == 143
        assert job.tls is False
        assert job.folders == ["INBOX", "Sent"]
        assert job.ignore_folder_flags == ["Junk"]
        assert job.with_db is False
        assert job.incremental is False

    def test_load_toml_env_expansion(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TEST_PASS", "s3cret")
        toml_file = tmp_path / "test.toml"
        toml_file.write_text(
            '[[job]]\n'
            'name = "test"\n'
            'password = "${TEST_PASS}"\n'
        )
        config = conf.load(toml_file)
        assert config.jobs[0].password == "s3cret"

    def test_load_toml_password_cmd(self, tmp_path):
        toml_file = tmp_path / "test.toml"
        toml_file.write_text(
            '[[job]]\n'
            'name = "test"\n'
            'password_cmd = "echo s3cret"\n'
        )
        config = conf.load(toml_file, allow_exec=True)
        assert config.jobs[0].password == "s3cret"

    def test_load_toml_copy_roles(self, tmp_path):
        toml_file = tmp_path / "test.toml"
        toml_file.write_text(
            '[[job]]\n'
            'name = "src"\n'
            'role = "source"\n'
            'server = "imap.src.com"\n'
            '\n'
            '[[job]]\n'
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
        toml_file.write_text(
            '[global]\n'
            'compress = true\n'
        )
        config = conf.load(toml_file)
        assert config.compress is True
        assert config.jobs == []
