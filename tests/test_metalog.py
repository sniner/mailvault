"""Tests for mailvault.store.metalog (the append-only location log)."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from mailvault.store import metalog

WHEN = datetime(2026, 8, 1, 18, 2, 21, tzinfo=UTC)
STORE_ID = "df3823f1cd1638d0f374745bb0e200e3"


def _write(root, mailbox="job", folder="INBOX", store_ids=(STORE_ID,)):
    writer = metalog.LogWriter(root)
    for store_id in store_ids:
        writer.add(mailbox, [folder] if folder is not None else [], store_id)
    return writer.seal(WHEN)


class TestWriting:
    def test_nothing_observed_writes_no_file(self, tmp_path):
        """An unchanged folder must not litter the log with empty files."""
        writer = metalog.LogWriter(tmp_path / "meta")

        assert writer.seal(WHEN) == []
        assert not (tmp_path / "meta").exists()

    def test_name_is_the_hash_of_the_content(self, tmp_path):
        (path,) = _write(tmp_path / "meta")

        assert path.name == hashlib.sha384(path.read_bytes()).hexdigest() + ".jsonl"
        assert path.parent.name == path.name[:2]

    def test_corrupted_content_is_reported_but_still_read(self, tmp_path, caplog):
        """What syntax alone can never catch: a flipped bit in a valid line.

        The file is not discarded: a log never claims to be exhaustive, so what
        still parses is a subset of the truth, which is what it always was.
        """
        (path,) = _write(tmp_path / "meta", store_ids=["aaaa", "bbbb"])
        body = path.read_text(encoding="utf-8")
        path.write_text(body.replace("bbbb", "cccc"), encoding="utf-8")

        logfile = metalog.read_log(path)

        assert "damaged" in caplog.text
        assert logfile.store_ids == ["aaaa", "cccc"]

    def test_each_place_becomes_its_own_file(self, tmp_path):
        """One file is one (mailbox, folder) -- that is what makes it unambiguous."""
        writer = metalog.LogWriter(tmp_path / "meta")
        writer.add("job", ["INBOX", "\\Sent"], STORE_ID)
        writer.add("other", ["INBOX"], STORE_ID)

        paths = writer.seal(WHEN)

        assert len(paths) == 3
        places = {(f.mailbox, f.folder) for f in metalog.read_all(tmp_path / "meta")}
        assert places == {("job", "INBOX"), ("job", "\\Sent"), ("other", "INBOX")}

    def test_files_written_together_get_distinct_names(self, tmp_path):
        writer = metalog.LogWriter(tmp_path / "meta")
        for folder in ("a", "b", "c", "d", "e"):
            writer.add("job", [folder], STORE_ID)

        paths = writer.seal(WHEN)

        assert len({p.name for p in paths}) == 5

    def test_identical_content_is_stored_once(self, tmp_path):
        """Content addressing, applied to the log: the same seal is one file."""
        root = tmp_path / "meta"
        first = _write(root)
        second = _write(root)

        assert first == second
        assert len(metalog.log_files(root)) == 1

    def test_folder_with_separators_stays_out_of_the_filename(self, tmp_path):
        """Names like 'Archiv/2016' must never become path components."""
        (path,) = _write(tmp_path / "meta", folder="Archiv/2016")

        assert "/" not in path.name
        assert metalog.read_log(path).folder == "Archiv/2016"

    def test_backslash_folder_survives(self, tmp_path):
        (path,) = _write(tmp_path / "meta", folder="\\Sent")

        assert metalog.read_log(path).folder == "\\Sent"

    def test_byte_folder_names_are_decoded(self, tmp_path):
        """Gmail reports its folder names as raw bytes, which JSON cannot hold."""
        writer = metalog.LogWriter(tmp_path / "meta")
        writer.add("job", [b"\\Sent"], STORE_ID)

        (path,) = writer.seal(WHEN)

        assert metalog.read_log(path).folder == "\\Sent"

    def test_message_without_a_folder_is_recorded_against_the_mailbox(self, tmp_path):
        """Knowing less is not the same as knowing nothing."""
        writer = metalog.LogWriter(tmp_path / "meta")
        writer.add("job", [], STORE_ID)

        (path,) = writer.seal(WHEN)

        logfile = metalog.read_log(path)
        assert logfile.mailbox == "job"
        assert logfile.folder is None
        assert logfile.store_ids == [STORE_ID]

    def test_leaves_no_temporary_file_behind(self, tmp_path):
        root = tmp_path / "meta"
        _write(root)

        assert [p.suffix for p in root.glob("*/*")] == [".jsonl"]

    def test_declared_count_matches_what_is_written(self, tmp_path):
        (path,) = _write(tmp_path / "meta", store_ids=["aaa", "bbb"])

        header = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        assert header["messages"] == 2

    def test_writer_is_reusable_after_sealing(self, tmp_path):
        writer = metalog.LogWriter(tmp_path / "meta")
        writer.add("job", ["INBOX"], STORE_ID)
        writer.seal(WHEN)

        assert len(writer) == 0
        assert writer.places == 0
        assert writer.seal(WHEN) == []


class TestReading:
    def test_roundtrip(self, tmp_path):
        (path,) = _write(
            tmp_path / "meta",
            mailbox="mail.example.org",
            folder="INBOX",
            store_ids=["aaa", "bbb"],
        )

        logfile = metalog.read_log(path)

        assert logfile.mailbox == "mail.example.org"
        assert logfile.folder == "INBOX"
        assert logfile.store_ids == ["aaa", "bbb"]

    def test_torn_final_line_is_skipped_and_the_rest_survives(self, tmp_path, caplog):
        """The expected shape of an interrupted write."""
        (path,) = _write(tmp_path / "meta", store_ids=["aaa", "bbb"])
        body = path.read_text(encoding="utf-8")
        path.write_text(body[: body.rindex("\n") - 12], encoding="utf-8")

        assert metalog.read_log(path).store_ids == ["aaa"]

    def test_truncation_on_a_line_boundary_is_reported(self, tmp_path, caplog):
        """A file cut at a newline parses cleanly and is still short."""
        (path,) = _write(tmp_path / "meta", store_ids=["aaa", "bbb", "ccc"])
        lines = path.read_text(encoding="utf-8").splitlines()
        path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")

        logfile = metalog.read_log(path)

        assert logfile.store_ids == ["aaa", "bbb"]
        assert "header declares 3 message(s) but 2 were readable" in caplog.text

    def test_unknown_version_is_rejected(self, tmp_path, caplog):
        path = tmp_path / "log.jsonl"
        path.write_text(json.dumps({"version": 99, "mailbox": "j"}) + "\n", encoding="utf-8")

        assert metalog.read_log(path) is None
        assert "is not 1" in caplog.text

    def test_unreadable_header_discards_the_file(self, tmp_path, caplog):
        path = tmp_path / "log.jsonl"
        path.write_text('{"version": 1, "mail\n{"store_id":"x"}\n', encoding="utf-8")

        assert metalog.read_log(path) is None
        assert "unreadable header" in caplog.text

    def test_empty_file_is_rejected(self, tmp_path):
        path = tmp_path / "log.jsonl"
        path.write_text("", encoding="utf-8")

        assert metalog.read_log(path) is None

    def test_line_without_store_id_is_skipped(self, tmp_path, caplog):
        path = tmp_path / "log.jsonl"
        path.write_text(
            json.dumps({"version": 1, "mailbox": "j", "folder": "INBOX"})
            + "\n"
            + json.dumps({"nothing": "useful"})
            + "\n"
            + json.dumps({"store_id": "ok"})
            + "\n",
            encoding="utf-8",
        )

        assert metalog.read_log(path).store_ids == ["ok"]
        assert "no usable store_id" in caplog.text


class TestDiscovery:
    def test_files_are_found_across_shards(self, tmp_path):
        root = tmp_path / "meta"
        _write(root, folder="one")
        _write(root, folder="two")

        found = metalog.log_files(root)
        assert len(found) == 2
        assert all(p.parent.parent == root for p in found)

    def test_transient_and_non_log_files_are_ignored(self, tmp_path):
        root = tmp_path / "meta"
        _write(root)
        (root / "aa").mkdir(exist_ok=True)
        (root / "aa" / "half._tmp_").write_text("half", encoding="utf-8")
        (root / ".hidden").write_text("x", encoding="utf-8")

        assert len(metalog.log_files(root)) == 1

    def test_missing_directory_is_not_an_error(self, tmp_path):
        assert metalog.log_files(tmp_path / "nope") == []
        assert metalog.has_logs(tmp_path / "nope") is False

    def test_read_all_skips_unusable_files(self, tmp_path):
        root = tmp_path / "meta"
        _write(root)
        (root / "ff").mkdir(exist_ok=True)
        (root / "ff" / "ff00.jsonl").write_text("broken", encoding="utf-8")

        assert len(list(metalog.read_all(root))) == 1
