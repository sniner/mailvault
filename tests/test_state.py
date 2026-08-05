"""Tests for mailvault.store.state (the durable resume state file)."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

import pytest

from mailvault.store import state

LAST_RUN = datetime(2026, 2, 1, 12, 30, tzinfo=UTC)
UID_TOKEN = {"kind": "imap-uid", "uidvalidity": 1239278212, "uid": 48127}


def _write(path, payload) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _v2(snapshots) -> dict:
    return {"version": state.STATE_VERSION, "snapshots": snapshots}


def _v1(snapshots) -> dict:
    return {"version": state.LEGACY_STATE_VERSION, "snapshots": snapshots}


class TestLoad:
    def test_missing_file_yields_empty_state(self, tmp_path):
        s = state.SnapshotState.load(tmp_path / "state.json")
        assert s.resume("job", "INBOX") is None
        assert s.last_run("job", "INBOX") is None
        assert list(s.entries()) == []

    def test_unknown_mailbox_or_folder_is_none(self, tmp_path):
        path = tmp_path / "state.json"
        s = state.SnapshotState(path)
        s.record("job", "INBOX", last_run=LAST_RUN, resume=UID_TOKEN)
        s.save()

        s = state.SnapshotState.load(path)
        assert s.resume("other-job", "INBOX") is None
        assert s.resume("job", "Sent") is None

    def test_broken_json_yields_empty_state(self, tmp_path, caplog):
        path = tmp_path / "state.json"
        path.write_text('{"version": 2, "snapshots": {', encoding="utf-8")

        s = state.SnapshotState.load(path)

        assert list(s.entries()) == []
        assert "not valid JSON" in caplog.text

    def test_unknown_version_is_rejected(self, tmp_path, caplog):
        path = tmp_path / "state.json"
        _write(path, {"version": 99, "snapshots": {"job": {"INBOX": {"resume": UID_TOKEN}}}})

        s = state.SnapshotState.load(path)

        assert s.resume("job", "INBOX") is None
        assert "unknown state version" in caplog.text

    def test_non_object_payload_is_rejected(self, tmp_path, caplog):
        path = tmp_path / "state.json"
        _write(path, ["not", "an", "object"])

        assert list(state.SnapshotState.load(path).entries()) == []
        assert "expected a JSON object" in caplog.text

    def test_malformed_entries_are_dropped_but_valid_ones_kept(self, tmp_path, caplog):
        path = tmp_path / "state.json"
        _write(
            path,
            _v2(
                {
                    "good-job": {"INBOX": {"resume": UID_TOKEN}, "Sent": 12345},
                    "broken-job": "not-an-object",
                }
            ),
        )

        s = state.SnapshotState.load(path)

        assert s.resume("good-job", "INBOX") == UID_TOKEN
        assert s.resume("good-job", "Sent") is None
        assert s.resume("broken-job", "INBOX") is None
        assert "skipping malformed entry" in caplog.text


class TestResumePoint:
    """The resume point is opaque: kept whole, or not kept at all."""

    def test_a_backend_specific_payload_survives_untouched(self, tmp_path):
        """This module must not need changing when a backend learns something."""
        path = tmp_path / "state.json"
        token = {"kind": "graph-delta", "delta_link": "https://example.invalid/x?$deltatoken=y"}
        s = state.SnapshotState(path)
        s.record("job", "INBOX", last_run=LAST_RUN, resume=token)
        s.save()

        assert state.SnapshotState.load(path).resume("job", "INBOX") == token

    def test_a_token_without_a_kind_is_refused(self, tmp_path, caplog):
        path = tmp_path / "state.json"
        _write(path, _v2({"job": {"INBOX": {"resume": {"uid": 5}}}}))

        s = state.SnapshotState.load(path)

        assert s.resume("job", "INBOX") is None
        assert "unusable resume point" in caplog.text

    def test_a_token_that_is_not_an_object_is_refused(self, tmp_path, caplog):
        path = tmp_path / "state.json"
        _write(path, _v2({"job": {"INBOX": {"resume": "imap-uid:48127"}}}))

        assert state.SnapshotState.load(path).resume("job", "INBOX") is None
        assert "unusable resume point" in caplog.text

    def test_a_refused_token_keeps_the_last_run(self, tmp_path):
        """Losing the resume point costs a full read, not the record of the run."""
        path = tmp_path / "state.json"
        _write(
            path,
            _v2({"job": {"INBOX": {"last_run": LAST_RUN.isoformat(), "resume": {"uid": 5}}}}),
        )

        s = state.SnapshotState.load(path)

        assert s.resume("job", "INBOX") is None
        assert s.last_run("job", "INBOX") == LAST_RUN

    def test_recording_without_one_leaves_the_previous_standing(self, tmp_path):
        """A pass that archived nothing has nothing to offer, and forgets nothing."""
        path = tmp_path / "state.json"
        later = datetime(2026, 3, 1, tzinfo=UTC)
        s = state.SnapshotState(path)
        s.record("job", "INBOX", last_run=LAST_RUN, resume=UID_TOKEN)

        s.record("job", "INBOX", last_run=later, resume=None)

        assert s.resume("job", "INBOX") == UID_TOKEN
        assert s.last_run("job", "INBOX") == later


class TestLegacyState:
    """Version 1 held a bare timestamp and resumed from it. Version 2 does not."""

    def test_a_version_1_timestamp_becomes_a_last_run(self, tmp_path):
        path = tmp_path / "state.json"
        _write(path, _v1({"job": {"INBOX": LAST_RUN.isoformat()}}))

        s = state.SnapshotState.load(path)

        assert s.last_run("job", "INBOX") == LAST_RUN

    def test_a_version_1_timestamp_is_not_a_resume_point(self, tmp_path, caplog):
        """The whole reason for version 2: a date says when, not how far."""
        path = tmp_path / "state.json"
        _write(path, _v1({"job": {"INBOX": LAST_RUN.isoformat()}}))

        with caplog.at_level(logging.INFO):
            s = state.SnapshotState.load(path)

        assert s.resume("job", "INBOX") is None
        assert "read in full" in caplog.text

    def test_a_version_1_file_is_rewritten_as_version_2(self, tmp_path):
        path = tmp_path / "state.json"
        _write(path, _v1({"job": {"INBOX": LAST_RUN.isoformat()}}))

        state.SnapshotState.load(path).save()

        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["version"] == state.STATE_VERSION
        assert payload["snapshots"] == {"job": {"INBOX": {"last_run": LAST_RUN.isoformat()}}}


class TestSave:
    def test_roundtrip_preserves_both_fields(self, tmp_path):
        path = tmp_path / "state.json"
        s = state.SnapshotState(path)
        s.record("job", "INBOX", last_run=LAST_RUN, resume=UID_TOKEN)
        s.save()

        loaded = state.SnapshotState.load(path)
        assert loaded.last_run("job", "INBOX") == LAST_RUN
        assert loaded.resume("job", "INBOX") == UID_TOKEN

    def test_folder_names_with_separators_survive(self, tmp_path):
        """Folder names are dictionary keys, never path components."""
        path = tmp_path / "state.json"
        s = state.SnapshotState(path)
        s.record("job", "Archiv/2016", last_run=LAST_RUN, resume=UID_TOKEN)
        s.record("job", "\\Sent", last_run=LAST_RUN, resume=UID_TOKEN)
        s.save()

        s = state.SnapshotState.load(path)
        assert s.resume("job", "Archiv/2016") == UID_TOKEN
        assert s.resume("job", "\\Sent") == UID_TOKEN
        assert list(tmp_path.iterdir()) == [path]

    def test_leaves_no_temporary_file_behind(self, tmp_path):
        path = tmp_path / "state.json"
        s = state.SnapshotState(path)
        s.record("job", "INBOX", last_run=LAST_RUN, resume=UID_TOKEN)
        s.save()

        assert [p.name for p in tmp_path.iterdir()] == ["state.json"]

    def test_replaces_previous_content(self, tmp_path):
        path = tmp_path / "state.json"
        later = datetime(2026, 3, 1, tzinfo=UTC)

        s = state.SnapshotState(path)
        s.record("job", "INBOX", last_run=LAST_RUN, resume=UID_TOKEN)
        s.save()
        s.record("job", "INBOX", last_run=later, resume={"kind": "imap-uid", "uid": 99})
        s.save()

        loaded = state.SnapshotState.load(path)
        assert loaded.last_run("job", "INBOX") == later
        assert loaded.resume("job", "INBOX") == {"kind": "imap-uid", "uid": 99}

    def test_creates_missing_parent_directory(self, tmp_path):
        path = tmp_path / "fresh-archive" / "state.json"
        s = state.SnapshotState(path)
        s.record("job", "INBOX", last_run=LAST_RUN, resume=UID_TOKEN)
        s.save()

        assert path.exists()

    def test_written_file_is_readable_json(self, tmp_path):
        """The file is meant to be inspectable without mailvault."""
        path = tmp_path / "state.json"
        s = state.SnapshotState(path)
        s.record("job", "INBOX", last_run=LAST_RUN, resume=UID_TOKEN)
        s.save()

        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload == {
            "version": state.STATE_VERSION,
            "snapshots": {
                "job": {"INBOX": {"last_run": LAST_RUN.isoformat(), "resume": UID_TOKEN}}
            },
        }


class TestEntries:
    def test_entries_are_sorted(self, tmp_path):
        s = state.SnapshotState(tmp_path / "state.json")
        s.record("b-job", "INBOX", last_run=LAST_RUN, resume=None)
        s.record("a-job", "Sent", last_run=LAST_RUN, resume=None)
        s.record("a-job", "INBOX", last_run=LAST_RUN, resume=None)

        assert [(m, f) for m, f, _ in s.entries()] == [
            ("a-job", "INBOX"),
            ("a-job", "Sent"),
            ("b-job", "INBOX"),
        ]


class TestTimezones:
    def test_a_naive_timestamp_is_read_as_local_time(self, tmp_path):
        """Older versions wrote datetime.now(), which means local time."""
        path = tmp_path / "state.json"
        _write(path, _v1({"job": {"INBOX": "2025-10-16T19:16:59.494153"}}))

        parsed = state.SnapshotState.load(path).last_run("job", "INBOX")

        assert parsed is not None
        assert parsed.tzinfo is not None
        assert parsed.replace(tzinfo=None) == datetime(2025, 10, 16, 19, 16, 59, 494153)

    def test_an_unparsable_timestamp_counts_as_unknown(self, tmp_path, caplog):
        path = tmp_path / "state.json"
        _write(path, _v2({"job": {"INBOX": {"last_run": "soon", "resume": UID_TOKEN}}}))

        s = state.SnapshotState.load(path)

        assert s.last_run("job", "INBOX") is None
        assert "unparsable timestamp" in caplog.text
        # The resume point is a separate field and survives the bad neighbour.
        assert s.resume("job", "INBOX") == UID_TOKEN

    def test_an_aware_timestamp_is_left_alone(self, tmp_path):
        path = tmp_path / "state.json"
        s = state.SnapshotState(path)
        s.record("job", "INBOX", last_run=LAST_RUN, resume=UID_TOKEN)
        s.save()

        assert state.SnapshotState.load(path).last_run("job", "INBOX") == LAST_RUN


class TestMailboxes:
    """Who has written into an archive, without reading the file as a run does."""

    def test_a_missing_file_names_nobody(self, tmp_path):
        assert state.mailboxes(tmp_path / "state.json") == set()

    def test_the_recorded_mailboxes(self, tmp_path):
        path = tmp_path / "state.json"
        _write(path, _v2({"gmail.com": {"INBOX": {}}, "posteo.de": {"Sent": {}}}))

        assert state.mailboxes(path) == {"gmail.com", "posteo.de"}

    def test_a_version_1_file_answers_as_well(self, tmp_path):
        path = tmp_path / "state.json"
        _write(path, _v1({"gmail.com": {"INBOX": "2026-02-01T12:30:00+00:00"}}))

        assert state.mailboxes(path) == {"gmail.com"}

    def test_it_stays_quiet_about_what_the_run_would_be_told(self, tmp_path, caplog):
        """The version 1 notice belongs to the run that resumes, not to this."""
        path = tmp_path / "state.json"
        _write(path, _v1({"gmail.com": {"INBOX": "2026-02-01T12:30:00+00:00"}}))

        with caplog.at_level(logging.INFO):
            state.mailboxes(path)

        assert caplog.records == []

    def test_a_mailbox_without_folders_is_not_one(self, tmp_path):
        path = tmp_path / "state.json"
        _write(path, _v2({"gmail.com": {"INBOX": {}}, "empty.example": {}}))

        assert state.mailboxes(path) == {"gmail.com"}

    @pytest.mark.parametrize(
        "payload",
        [
            {"version": 99, "snapshots": {"gmail.com": {"INBOX": {}}}},
            {"version": state.STATE_VERSION, "snapshots": []},
            ["not", "an", "object"],
        ],
    )
    def test_anything_unusable_names_nobody(self, tmp_path, payload):
        """The caller falls back to the metadata log, which is the safe answer."""
        path = tmp_path / "state.json"
        _write(path, payload)

        assert state.mailboxes(path) == set()

    def test_broken_json_names_nobody(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text("{ not json", encoding="utf-8")

        assert state.mailboxes(path) == set()
