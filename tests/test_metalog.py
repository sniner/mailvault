"""Tests for mailvault.store.metalog (the append-only attribution log)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from mailvault.store import metalog

WHEN = datetime(2026, 8, 1, 18, 2, 21, tzinfo=UTC)
STORE_ID = "df3823f1cd1638d0f374745bb0e200e3"


def _write_log(root, mailbox="job", folder="INBOX", entries=((STORE_ID, ["INBOX"]),)):
    writer = metalog.LogWriter(root, mailbox=mailbox, folder=folder)
    for store_id, labels in entries:
        writer.add(store_id, labels)
    return writer.seal(WHEN)


class TestWriting:
    def test_nothing_observed_writes_no_file(self, tmp_path):
        """An unchanged folder must not litter the log with empty files."""
        writer = metalog.LogWriter(tmp_path / "meta", mailbox="job", folder="INBOX")

        assert writer.seal(WHEN) is None
        assert not (tmp_path / "meta").exists()

    def test_filename_is_sortable_and_free_of_path_characters(self, tmp_path):
        path = _write_log(tmp_path / "meta")

        assert path is not None
        assert path.name == "2026-08-01T18-02-21Z_001.jsonl"

    def test_second_file_in_the_same_second_gets_its_own_name(self, tmp_path):
        root = tmp_path / "meta"
        first = _write_log(root, folder="INBOX")
        second = _write_log(root, folder="Sent")

        assert first is not None and second is not None
        assert first != second
        assert second.name.endswith("_002.jsonl")

    def test_header_carries_the_folder_not_the_filename(self, tmp_path):
        """Names like 'Archiv/2016' must never become path components."""
        path = _write_log(tmp_path / "meta", folder="Archiv/2016")

        assert path is not None
        assert "/" not in path.name
        assert metalog.read_log(path).folder == "Archiv/2016"

    def test_backslash_folder_survives(self, tmp_path):
        path = _write_log(tmp_path / "meta", folder="\\Sent")

        assert path is not None
        assert metalog.read_log(path).folder == "\\Sent"

    def test_byte_labels_are_decoded(self, tmp_path):
        """Gmail reports its labels as raw bytes, which JSON cannot hold."""
        path = _write_log(tmp_path / "meta", entries=[(STORE_ID, [b"\\Sent", "INBOX"])])

        assert path is not None
        assert metalog.read_log(path).entries[0].labels == ["\\Sent", "INBOX"]

    def test_leaves_no_temporary_file_behind(self, tmp_path):
        root = tmp_path / "meta"
        _write_log(root)

        assert [p.suffix for p in root.iterdir()] == [".jsonl"]

    def test_incomplete_folder_is_still_recorded(self, tmp_path):
        """Stored messages need their attribution even when the folder failed."""
        writer = metalog.LogWriter(tmp_path / "meta", mailbox="job", folder="INBOX")
        writer.add(STORE_ID, ["INBOX"])
        path = writer.seal(WHEN, complete=False)

        assert path is not None
        logfile = metalog.read_log(path)
        assert logfile.complete is False
        assert len(logfile.entries) == 1

    def test_writer_is_reusable_after_sealing(self, tmp_path):
        root = tmp_path / "meta"
        writer = metalog.LogWriter(root, mailbox="job", folder="INBOX")
        writer.add(STORE_ID, ["INBOX"])
        writer.seal(WHEN)

        assert len(writer) == 0
        assert writer.seal(WHEN) is None


class TestReading:
    def test_roundtrip(self, tmp_path):
        path = _write_log(
            tmp_path / "meta",
            mailbox="mail.example.org",
            folder="INBOX",
            entries=[("aaa", ["INBOX"]), ("bbb", ["INBOX", "\\Sent"])],
        )

        logfile = metalog.read_log(path)

        assert logfile.mailbox == "mail.example.org"
        assert logfile.folder == "INBOX"
        assert logfile.complete is True
        assert [e.store_id for e in logfile.entries] == ["aaa", "bbb"]
        assert logfile.entries[1].labels == ["INBOX", "\\Sent"]

    def test_entries_inherit_the_header_mailbox(self, tmp_path):
        path = _write_log(tmp_path / "meta", mailbox="mail.example.net")

        assert metalog.read_log(path).entries[0].mailboxes == ["mail.example.net"]

    def test_line_may_name_its_own_mailboxes(self, tmp_path):
        """How the bootstrap export records a message held in several mailboxes."""
        writer = metalog.LogWriter(tmp_path / "meta")
        writer.add(STORE_ID, ["INBOX"], mailboxes=["a.tld", "b.tld"])
        path = writer.seal(WHEN)

        entry = metalog.read_log(path).entries[0]
        assert entry.mailboxes == ["a.tld", "b.tld"]

    def test_torn_final_line_is_skipped_and_the_rest_survives(self, tmp_path):
        """The expected shape of an interrupted write."""
        path = _write_log(tmp_path / "meta", entries=[("aaa", ["INBOX"]), ("bbb", ["INBOX"])])
        body = path.read_text(encoding="utf-8")
        path.write_text(body[: body.rindex("\n") - 12], encoding="utf-8")

        logfile = metalog.read_log(path)

        assert [e.store_id for e in logfile.entries] == ["aaa"]

    def test_unknown_version_is_rejected(self, tmp_path, caplog):
        path = tmp_path / "log.jsonl"
        path.write_text(json.dumps({"version": 99, "mailbox": "j"}) + "\n", encoding="utf-8")

        assert metalog.read_log(path) is None
        assert "unknown log version" in caplog.text

    def test_unreadable_header_discards_the_file(self, tmp_path, caplog):
        path = tmp_path / "log.jsonl"
        path.write_text('{"version": 1, "mail\n{"store_id":"x"}\n', encoding="utf-8")

        assert metalog.read_log(path) is None
        assert "unreadable header" in caplog.text

    def test_empty_file_is_rejected(self, tmp_path):
        path = tmp_path / "log.jsonl"
        path.write_text("", encoding="utf-8")

        assert metalog.read_log(path) is None

    def test_entry_without_store_id_is_skipped(self, tmp_path, caplog):
        path = tmp_path / "log.jsonl"
        path.write_text(
            json.dumps({"version": 1, "mailbox": "j", "folder": "INBOX"})
            + "\n"
            + json.dumps({"labels": ["INBOX"]})
            + "\n"
            + json.dumps({"store_id": "ok", "labels": []})
            + "\n",
            encoding="utf-8",
        )

        logfile = metalog.read_log(path)

        assert [e.store_id for e in logfile.entries] == ["ok"]
        assert "no usable store_id" in caplog.text


class TestDiscovery:
    def test_log_files_are_in_chronological_order(self, tmp_path):
        root = tmp_path / "meta"
        root.mkdir()
        for name in ("2026-08-02T00-00-00Z_001", "2026-08-01T18-02-21Z_001"):
            (root / f"{name}.jsonl").write_text("{}", encoding="utf-8")

        assert [p.stem for p in metalog.log_files(root)] == [
            "2026-08-01T18-02-21Z_001",
            "2026-08-02T00-00-00Z_001",
        ]

    def test_transient_files_are_ignored(self, tmp_path):
        root = tmp_path / "meta"
        root.mkdir()
        (root / "log.jsonl").write_text("{}", encoding="utf-8")
        (root / "log.jsonl._tmp_").write_text("half", encoding="utf-8")

        assert [p.name for p in metalog.log_files(root)] == ["log.jsonl"]

    def test_missing_directory_is_not_an_error(self, tmp_path):
        assert metalog.log_files(tmp_path / "nope") == []
        assert metalog.has_logs(tmp_path / "nope") is False

    def test_has_logs(self, tmp_path):
        root = tmp_path / "meta"
        assert metalog.has_logs(root) is False
        _write_log(root)
        assert metalog.has_logs(root) is True

    def test_read_all_skips_unusable_files(self, tmp_path):
        root = tmp_path / "meta"
        _write_log(root)
        (root / "2027-01-01T00-00-00Z_001.jsonl").write_text("broken", encoding="utf-8")

        assert len(list(metalog.read_all(root))) == 1
