import sys

import pytest

from mailvault import conf

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def test_load_all_job_defaults(tmp_path):
    toml_file = tmp_path / "test.toml"
    toml_file.write_text('[[job]]\nname = "job1"\nusername = "user"\npassword = "pass"\n')
    config = conf.load(toml_file)
    assert config.compress is False
    assert config.incremental is True
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
# JobConfig.validate
# ---------------------------------------------------------------------------


def _imap_job(**overrides) -> conf.JobConfig:
    return conf.JobConfig(name="j", server="imap.example.com", username="u", **overrides)


def _graph_job(**overrides) -> conf.JobConfig:
    return conf.JobConfig(
        name="j",
        backend="msgraph",
        username="u",
        tenant_id="t",
        client_id="c",
        client_secret="s",
        **overrides,
    )


class TestJobValidate:
    def test_an_unknown_backend_is_refused(self):
        with pytest.raises(conf.ConfigError, match="unknown backend"):
            _imap_job(backend="pigeon").validate()

    def test_a_missing_graph_credential_is_named(self):
        job = conf.JobConfig(name="j", backend="msgraph", username="u")
        with pytest.raises(conf.ConfigError, match="tenant_id"):
            job.validate()

    def test_a_trash_folder_without_deleting_stops_the_job(self):
        """Emptying a trash nobody asked to fill is not a default worth having.

        `trash_folder` removes everything in that folder, including mail the
        owner put there. A job that never set `delete_after_export` did not ask
        for any deletion at all.
        """
        with pytest.raises(conf.ConfigError, match="delete_after_export"):
            _imap_job(trash_folder="[Gmail]/Trash").validate()

    def test_a_trash_folder_is_fine_when_deleting(self):
        _imap_job(trash_folder="[Gmail]/Trash", delete_after_export=True).validate()

    def test_a_trash_folder_on_graph_is_refused(self):
        """An option deciding the fate of mail must not look effective while inert."""
        with pytest.raises(conf.ConfigError, match="use 'permanent_delete' instead"):
            _graph_job(trash_folder="Deleted Items", delete_after_export=True).validate()

    def test_permanent_delete_needs_the_graph_backend(self):
        with pytest.raises(conf.ConfigError, match="use 'trash_folder' instead"):
            _imap_job(permanent_delete=True, delete_after_export=True).validate()

    def test_permanent_delete_without_deleting_stops_the_job(self):
        with pytest.raises(conf.ConfigError, match="delete_after_export"):
            _graph_job(permanent_delete=True).validate()

    def test_permanent_delete_is_fine_when_deleting(self):
        _graph_job(permanent_delete=True, delete_after_export=True).validate()

    def test_an_error_folder_without_journal_only_warns(self, caplog):
        # Inert, not harmful: nothing is moved when nothing fails to unwrap.
        _imap_job(error_folder="Errors").validate()
        assert "only applies to 'exchange_journal' jobs" in caplog.text

    def test_an_error_folder_with_journal_is_quiet(self, caplog):
        _imap_job(error_folder="Errors", exchange_journal=True).validate()
        assert "error_folder" not in caplog.text


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
        assert config.incremental is True
        job = config.jobs[0]
        assert job.server == "localhost"
        assert job.port == 993
        assert job.tls is True

    def test_load_toml_global_incremental(self, tmp_path):
        toml_file = tmp_path / "test.toml"
        toml_file.write_text('[global]\nincremental = false\n\n[[job]]\nname = "test"\n')
        config = conf.load(toml_file)
        assert config.incremental is False

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
        )
        config = conf.load(toml_file)
        job = config.jobs[0]
        assert job.server == "imap.example.com"
        assert job.port == 143
        assert job.tls is False
        assert job.folders == ["INBOX", "Sent"]
        assert job.ignore_folder_flags == ["Junk"]

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

    def test_load_toml_empty_jobs(self, tmp_path):
        toml_file = tmp_path / "test.toml"
        toml_file.write_text("[global]\ncompress = true\n")
        config = conf.load(toml_file)
        assert config.compress is True
        assert config.jobs == []


class TestAValueMustBeWhatTheFieldHolds:
    """`validate` asks which options are there, never what they are."""

    @staticmethod
    def _load(tmp_path, body: str):
        toml_file = tmp_path / "test.toml"
        toml_file.write_text(body)
        return conf.load(toml_file)

    def test_a_single_folder_without_brackets_is_named(self, tmp_path):
        # The worst of them: the string is iterated, and mailvault goes looking
        # for the folders I, N, B, O and X -- which reads like a server problem.
        with pytest.raises(conf.ConfigError) as exc:
            self._load(tmp_path, '[[job]]\nname = "j"\nfolders = "INBOX"\n')
        assert 'folders = ["INBOX"]' in str(exc.value)

    def test_one_wrong_entry_in_a_list_is_named(self, tmp_path):
        with pytest.raises(conf.ConfigError, match="one of them is a number: 3"):
            self._load(tmp_path, '[[job]]\nname = "j"\nfolders = ["INBOX", 3]\n')

    def test_a_quoted_port_is_refused_here_and_not_in_imapclient(self, tmp_path):
        with pytest.raises(conf.ConfigError, match="'port' must be a number, not a string"):
            self._load(tmp_path, '[[job]]\nname = "j"\nport = "993"\n')

    def test_a_quoted_boolean_is_refused_rather_than_right_by_accident(self, tmp_path):
        # A non-empty string is true, so `tls = "yes"` worked -- and so did
        # `tls = "no"`, which is the same value with the opposite meaning.
        with pytest.raises(conf.ConfigError, match="'tls' must be a boolean"):
            self._load(tmp_path, '[[job]]\nname = "j"\ntls = "no"\n')

    def test_a_boolean_is_not_a_number(self, tmp_path):
        # In Python a bool is an int, so `port = true` would pass unnoticed.
        with pytest.raises(conf.ConfigError, match="'port' must be a number, not a boolean"):
            self._load(tmp_path, '[[job]]\nname = "j"\nport = true\n')

    def test_a_global_option_is_checked_too(self, tmp_path):
        with pytest.raises(conf.ConfigError, match=r"\[global\]: 'compress' must be a boolean"):
            self._load(tmp_path, '[global]\ncompress = "yes"\n')

    def test_a_job_name_that_is_not_a_string_is_named(self, tmp_path):
        with pytest.raises(conf.ConfigError, match="'name' must be a string, not a number"):
            self._load(tmp_path, "[[job]]\nname = 3\n")

    def test_an_optional_field_left_out_is_still_fine(self, tmp_path):
        config = self._load(tmp_path, '[[job]]\nname = "j"\nserver = "s"\n')
        assert config.jobs[0].folders is None
        assert config.jobs[0].trash_folder is None

    def test_the_types_a_configuration_actually_uses_pass(self, tmp_path):
        config = self._load(
            tmp_path,
            "[global]\n"
            "compress = true\n"
            "\n"
            "[[job]]\n"
            'name = "j"\n'
            'server = "imap.example.com"\n'
            "port = 993\n"
            "tls = true\n"
            'folders = ["INBOX", "Sent"]\n'
            'ignore_folder_flags = ["noselect"]\n'
            "max_retries = 3\n",
        )
        assert config.jobs[0].folders == ["INBOX", "Sent"]
        assert config.jobs[0].max_retries == 3


class TestTheJobSectionIsAList:
    """`[job]` for `[[job]]` is the commonest TOML mistake, and it parses."""

    def test_single_brackets_name_the_bracket(self, tmp_path):
        toml_file = tmp_path / "test.toml"
        toml_file.write_text('[job]\nname = "gmail"\nserver = "imap.gmail.com"\n')
        with pytest.raises(conf.ConfigError, match=r"\[\[job\]\]"):
            conf.load(toml_file)

    def test_a_job_that_is_not_a_table_is_named_too(self, tmp_path):
        toml_file = tmp_path / "test.toml"
        toml_file.write_text('job = ["gmail"]\n')
        with pytest.raises(conf.ConfigError, match="a string"):
            conf.load(toml_file)

    def test_a_job_key_that_is_a_value_is_named_too(self, tmp_path):
        toml_file = tmp_path / "test.toml"
        toml_file.write_text("job = 3\n")
        with pytest.raises(conf.ConfigError, match="a number"):
            conf.load(toml_file)


def test_a_retired_option_is_reported_rather_than_ignored(tmp_path, caplog):
    """A dropped field is otherwise indistinguishable from a typo."""
    path = tmp_path / "old.toml"
    path.write_text(
        '[[job]]\nname = "j"\nserver = "s"\nusername = "u"\npassword = "p"\nwith_db = false\n',
        encoding="utf-8",
    )

    config = conf.load(path)

    assert len(config.jobs) == 1
    assert "'with_db' no longer exists" in caplog.text
    assert "metadata is always recorded" in caplog.text


def test_the_later_name_is_reported_too(tmp_path, caplog):
    path = tmp_path / "old.toml"
    path.write_text(
        '[[job]]\nname = "j"\nserver = "s"\nusername = "u"\npassword = "p"\n'
        "with_metadata = false\n",
        encoding="utf-8",
    )

    conf.load(path)

    assert "'with_metadata' no longer exists" in caplog.text


def test_per_job_incremental_is_reported_as_global_now(tmp_path, caplog):
    """It moved to [global]; a per-job setting is dropped, not silently obeyed."""
    path = tmp_path / "old.toml"
    path.write_text(
        '[[job]]\nname = "j"\nserver = "s"\nusername = "u"\npassword = "p"\n'
        "incremental = false\n",
        encoding="utf-8",
    )

    config = conf.load(path)

    assert "'incremental' no longer exists" in caplog.text
    assert "global option now" in caplog.text
    # Dropped from the job, and the global default is unaffected by the stray line.
    assert not hasattr(config.jobs[0], "incremental")
    assert config.incremental is True


def test_a_pre_0_9_copy_job_is_reported_option_by_option(tmp_path, caplog):
    """The per-job copy options are gone with the command, and each says so.

    A config written for `copy` must not load as if it still meant something --
    the backup jobs in it are still valid, but the copying will not happen.
    """
    path = tmp_path / "old.toml"
    path.write_text(
        '[[job]]\nname = "j"\nserver = "s"\nusername = "u"\npassword = "p"\n'
        'role = "source"\nmove_to_archive = true\narchive_folder = "Archive/%Y"\n',
        encoding="utf-8",
    )

    config = conf.load(path)

    for field in ("role", "move_to_archive", "archive_folder"):
        assert f"'{field}' no longer exists" in caplog.text
        assert not hasattr(config.jobs[0], field)
    assert "removed in 0.9.0" in caplog.text
    # The job itself still loads -- only the copy options were dropped.
    assert config.jobs[0].server == "s"


def test_a_leftover_copy_section_is_reported(tmp_path, caplog):
    """A whole section that stopped meaning anything must not pass unmentioned."""
    path = tmp_path / "old.toml"
    path.write_text(
        '[copy]\nsource = "a"\ndestination = "b"\n\n[[job]]\nname = "a"\nserver = "s"\n',
        encoding="utf-8",
    )

    config = conf.load(path)

    assert "[copy] no longer does anything" in caplog.text
    assert "removed in 0.9.0" in caplog.text
    assert not hasattr(config, "copy")
    assert config.jobs[0].name == "a"


# ---------------------------------------------------------------------------
# The archive a configuration does not belong to
# ---------------------------------------------------------------------------


class TestTheConfigNamesNoArchive:
    """A configuration does not say which archive it belongs to. It lies in it.

    A path in the file cannot carry across machines: the NAS is mounted at a
    different place on each of them, so no single path is right on both. A
    configuration inside the archive has that distance by construction.
    """

    @staticmethod
    def _load(tmp_path, line: str = "", name: str = "mailvault.toml"):
        path = tmp_path / name
        path.write_text(f'[global]\n{line}\n[[job]]\nname = "j"\nserver = "s"\n')
        return conf.load(path)

    def test_there_is_no_such_option(self):
        assert not hasattr(conf.Config(), "destination")

    def test_one_left_over_in_a_file_is_reported_rather_than_obeyed(self, tmp_path, caplog):
        """It never shipped, so it needs no retirement notice -- but not silence either."""
        config = self._load(tmp_path, 'destination = "/archive/private"')

        assert "destination" in caplog.text
        assert config.jobs[0].name == "j", "and the rest of the file is still read"
