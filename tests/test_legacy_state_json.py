"""Tests for `mailvault.legacy.state_json`, the reader the migration uses.

Nothing writes this format any more. What has to keep working is reading both
versions out of an archive that predates `heads/`, once, and degrading into
"nothing known" -- which costs a full pass and never mail -- for anything that
cannot be trusted.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

import pytest

from mailvault.legacy import state_json

LAST_RUN = datetime(2026, 2, 1, 12, 30, tzinfo=UTC)
UID_TOKEN = {"kind": "imap-uid", "uidvalidity": 1239278212, "uid": 48127}


def _write(path, payload) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _v2(snapshots) -> dict:
    return {"version": state_json.STATE_VERSION, "snapshots": snapshots}


def _v1(snapshots) -> dict:
    return {"version": state_json.LEGACY_STATE_VERSION, "snapshots": snapshots}


def _places(path) -> dict[tuple[str, str], state_json.FolderState]:
    return {(mb, f): entry for mb, f, entry in state_json.SnapshotState.load(path).entries()}


class TestVersion2:
    """`last_run` and an opaque `resume`, both carried over."""

    def test_both_fields_come_through(self, tmp_path):
        path = tmp_path / "state.json"
        _write(
            path,
            _v2({"job": {"INBOX": {"last_run": LAST_RUN.isoformat(), "resume": UID_TOKEN}}}),
        )

        entry = _places(path)[("job", "INBOX")]

        assert entry.last_run == LAST_RUN.isoformat()
        assert entry.resume == UID_TOKEN

    def test_a_backend_specific_payload_survives_untouched(self, tmp_path):
        """This module must not need changing when a backend learns something."""
        token = {"kind": "graph-delta", "delta_link": "https://…/delta?$skip=X", "extra": 7}
        path = tmp_path / "state.json"
        _write(path, _v2({"job": {"INBOX": {"resume": token}}}))

        assert _places(path)[("job", "INBOX")].resume == token

    def test_a_token_without_a_kind_is_refused(self, tmp_path, caplog):
        path = tmp_path / "state.json"
        _write(path, _v2({"job": {"INBOX": {"last_run": LAST_RUN.isoformat(), "resume": {}}}}))

        entry = _places(path)[("job", "INBOX")]

        assert entry.resume is None, "read the folder in full, which is the safe outcome"
        assert entry.last_run == LAST_RUN.isoformat(), "the record of the run survives it"
        assert "unusable resume point" in caplog.text

    def test_a_token_that_is_not_an_object_is_refused(self, tmp_path, caplog):
        path = tmp_path / "state.json"
        _write(
            path,
            _v2({"job": {"INBOX": {"last_run": LAST_RUN.isoformat(), "resume": "uid:48127"}}}),
        )

        assert _places(path)[("job", "INBOX")].resume is None
        assert "unusable resume point" in caplog.text

    def test_a_folder_with_neither_is_not_an_entry(self, tmp_path):
        path = tmp_path / "state.json"
        _write(path, _v2({"job": {"INBOX": {}}}))

        assert _places(path) == {}


class TestVersion1:
    """A bare timestamp. It was used as a resume point, and it must not be again."""

    def test_the_timestamp_becomes_a_last_run(self, tmp_path):
        path = tmp_path / "state.json"
        _write(path, _v1({"job": {"INBOX": LAST_RUN.isoformat()}}))

        assert _places(path)[("job", "INBOX")].last_run == LAST_RUN.isoformat()

    def test_and_never_a_resume_point(self, tmp_path, caplog):
        """A date is a statement about the run, not about coverage.

        Adopting it would inherit exactly the gap version 2 exists to close: a
        message copied into a folder keeps its old date and lands behind it.
        """
        path = tmp_path / "state.json"
        _write(path, _v1({"job": {"INBOX": LAST_RUN.isoformat()}}))

        with caplog.at_level(logging.INFO):
            assert _places(path)[("job", "INBOX")].resume is None

        assert "not resume points" in caplog.text

    def test_a_naive_timestamp_is_carried_over_as_written(self, tmp_path):
        """Parsing is the reader's business at the other end, not this one's."""
        naive = "2026-02-01T12:30:00"
        path = tmp_path / "state.json"
        _write(path, _v1({"job": {"INBOX": naive}}))

        assert _places(path)[("job", "INBOX")].last_run == naive


class TestWhatCannotBeTrusted:
    """Everything here degrades to "nothing known", which costs a pass, not mail."""

    def test_a_missing_file(self, tmp_path):
        assert list(state_json.SnapshotState.load(tmp_path / "state.json").entries()) == []

    def test_broken_json(self, tmp_path, caplog):
        path = tmp_path / "state.json"
        path.write_text('{"version": 2, "snapshots": {', encoding="utf-8")

        assert _places(path) == {}
        assert "not valid JSON" in caplog.text

    def test_a_version_nobody_knows(self, tmp_path, caplog):
        path = tmp_path / "state.json"
        _write(path, {"version": 99, "snapshots": {"job": {"INBOX": {"resume": UID_TOKEN}}}})

        assert _places(path) == {}
        assert "unknown state version" in caplog.text

    def test_a_payload_that_is_not_an_object(self, tmp_path, caplog):
        path = tmp_path / "state.json"
        _write(path, ["not", "an", "object"])

        assert _places(path) == {}
        assert "expected a JSON object" in caplog.text

    def test_snapshots_that_are_not_an_object(self, tmp_path, caplog):
        path = tmp_path / "state.json"
        _write(path, {"version": state_json.STATE_VERSION, "snapshots": []})

        assert _places(path) == {}
        assert "'snapshots' is not an object" in caplog.text

    def test_one_broken_entry_does_not_cost_the_others(self, tmp_path, caplog):
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

        places = _places(path)

        assert places[("good-job", "INBOX")].resume == UID_TOKEN
        assert ("good-job", "Sent") not in places
        assert ("broken-job", "INBOX") not in places
        assert "skipping malformed entry" in caplog.text


def test_entries_come_out_sorted(tmp_path):
    path = tmp_path / "state.json"
    _write(
        path,
        _v2(
            {
                "zeta": {"Sent": {"resume": UID_TOKEN}},
                "alpha": {"Sent": {"resume": UID_TOKEN}, "INBOX": {"resume": UID_TOKEN}},
            }
        ),
    )

    assert [(mb, f) for mb, f, _ in state_json.SnapshotState.load(path).entries()] == [
        ("alpha", "INBOX"),
        ("alpha", "Sent"),
        ("zeta", "Sent"),
    ]


@pytest.mark.parametrize("payload", [{}, {"version": state_json.STATE_VERSION}, 42])
def test_anything_unusable_yields_no_places(tmp_path, payload):
    path = tmp_path / "state.json"
    _write(path, payload)

    assert _places(path) == {}
