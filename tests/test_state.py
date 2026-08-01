"""Tests for mailvault.store.state (the durable snapshot state file)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from mailvault.store import state

SNAPSHOT = datetime(2026, 2, 1, 12, 30, tzinfo=UTC)


def _write(path, payload) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


class TestLoad:
    def test_missing_file_yields_empty_state(self, tmp_path):
        s = state.SnapshotState.load(tmp_path / "store.json")
        assert s.get_date("job", "INBOX") is None
        assert list(s.entries()) == []

    def test_unknown_mailbox_or_folder_is_none(self, tmp_path):
        path = tmp_path / "store.json"
        s = state.SnapshotState(path)
        s.set_date("job", "INBOX", SNAPSHOT)
        s.save()

        s = state.SnapshotState.load(path)
        assert s.get_date("other-job", "INBOX") is None
        assert s.get_date("job", "Sent") is None

    def test_broken_json_yields_empty_state(self, tmp_path, caplog):
        path = tmp_path / "store.json"
        path.write_text('{"version": 1, "snapshots": {', encoding="utf-8")

        s = state.SnapshotState.load(path)

        assert list(s.entries()) == []
        assert "not valid JSON" in caplog.text

    def test_unknown_version_is_rejected(self, tmp_path, caplog):
        path = tmp_path / "store.json"
        _write(path, {"version": 99, "snapshots": {"job": {"INBOX": SNAPSHOT.isoformat()}}})

        s = state.SnapshotState.load(path)

        assert s.get_date("job", "INBOX") is None
        assert "unknown state version" in caplog.text

    def test_non_object_payload_is_rejected(self, tmp_path, caplog):
        path = tmp_path / "store.json"
        _write(path, ["not", "an", "object"])

        assert list(state.SnapshotState.load(path).entries()) == []
        assert "expected a JSON object" in caplog.text

    def test_malformed_entries_are_dropped_but_valid_ones_kept(self, tmp_path, caplog):
        path = tmp_path / "store.json"
        _write(
            path,
            {
                "version": state.STATE_VERSION,
                "snapshots": {
                    "good-job": {"INBOX": SNAPSHOT.isoformat(), "Sent": 12345},
                    "broken-job": "not-an-object",
                },
            },
        )

        s = state.SnapshotState.load(path)

        assert s.get_date("good-job", "INBOX") == SNAPSHOT
        assert s.get_date("good-job", "Sent") is None
        assert s.get_date("broken-job", "INBOX") is None
        assert "skipping malformed entry" in caplog.text

    def test_unparsable_timestamp_counts_as_unknown(self, tmp_path, caplog):
        path = tmp_path / "store.json"
        _write(path, {"version": state.STATE_VERSION, "snapshots": {"job": {"INBOX": "soon"}}})

        assert state.SnapshotState.load(path).get_date("job", "INBOX") is None
        assert "unparsable timestamp" in caplog.text


class TestSave:
    def test_roundtrip_preserves_timestamp(self, tmp_path):
        path = tmp_path / "store.json"
        s = state.SnapshotState(path)
        s.set_date("job", "INBOX", SNAPSHOT)
        s.save()

        assert state.SnapshotState.load(path).get_date("job", "INBOX") == SNAPSHOT

    def test_folder_names_with_separators_survive(self, tmp_path):
        """Folder names are dictionary keys, never path components."""
        path = tmp_path / "store.json"
        s = state.SnapshotState(path)
        s.set_date("job", "Archiv/2016", SNAPSHOT)
        s.set_date("job", "\\Sent", SNAPSHOT)
        s.save()

        s = state.SnapshotState.load(path)
        assert s.get_date("job", "Archiv/2016") == SNAPSHOT
        assert s.get_date("job", "\\Sent") == SNAPSHOT
        assert list(tmp_path.iterdir()) == [path]

    def test_leaves_no_temporary_file_behind(self, tmp_path):
        path = tmp_path / "store.json"
        s = state.SnapshotState(path)
        s.set_date("job", "INBOX", SNAPSHOT)
        s.save()

        assert [p.name for p in tmp_path.iterdir()] == ["store.json"]

    def test_replaces_previous_content(self, tmp_path):
        path = tmp_path / "store.json"
        later = datetime(2026, 3, 1, tzinfo=UTC)

        s = state.SnapshotState(path)
        s.set_date("job", "INBOX", SNAPSHOT)
        s.save()
        s.set_date("job", "INBOX", later)
        s.save()

        assert state.SnapshotState.load(path).get_date("job", "INBOX") == later

    def test_creates_missing_parent_directory(self, tmp_path):
        path = tmp_path / "fresh-archive" / "store.json"
        s = state.SnapshotState(path)
        s.set_date("job", "INBOX", SNAPSHOT)
        s.save()

        assert path.exists()

    def test_written_file_is_readable_json(self, tmp_path):
        """The file is meant to be inspectable without mailvault."""
        path = tmp_path / "store.json"
        s = state.SnapshotState(path)
        s.set_date("job", "INBOX", SNAPSHOT)
        s.save()

        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload == {
            "version": state.STATE_VERSION,
            "snapshots": {"job": {"INBOX": SNAPSHOT.isoformat()}},
        }


class TestEntries:
    def test_entries_are_sorted(self, tmp_path):
        s = state.SnapshotState(tmp_path / "store.json")
        s.set_date("b-job", "INBOX", SNAPSHOT)
        s.set_date("a-job", "Sent", SNAPSHOT)
        s.set_date("a-job", "INBOX", SNAPSHOT)

        assert [(m, f) for m, f, _ in s.entries()] == [
            ("a-job", "INBOX"),
            ("a-job", "Sent"),
            ("b-job", "INBOX"),
        ]
