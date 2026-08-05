"""Tests for mailvault.jobs with mocked Mailbox and CAS."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from mailvault import conf, jobs, mailutils
from mailvault.backend import base
from mailvault.jobs.storedb import DEFAULT_QUERY_DB_NAME, refresh_db
from mailvault.jobs.verification import _archived_message_ids, _places_from_log
from mailvault.store import cas, metadb, metalog, state

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DUMMY_EML = b"""From: sender@example.com
To: recipient@example.com
Subject: Test
Message-ID: <test@example.com>
Date: Wed, 20 Feb 2026 12:00:00 +0100

Body.
"""


# Stands in for the timestamp a backend reports for the newest message it
# stored. A pass that archived something always has one -- it is what the
# snapshot is then taken from -- so a BackupResult without it means "nothing was
# archived", not "a message with no date".
ARCHIVED_AT = datetime(2026, 2, 20, 11, 0, tzinfo=UTC)


def _make_job(**overrides: Any) -> conf.JobConfig:
    defaults: dict[str, Any] = dict(
        name="test-job",
        server="imap.example.com",
        username="user",
        password="pass",
    )
    defaults.update(overrides)
    return conf.JobConfig(**defaults)


def _make_mock_client():
    """Create a mock MailboxClient."""
    client = MagicMock()
    client.job_name = "test-job"
    client.folders.return_value = iter(["INBOX"])
    return client


# ---------------------------------------------------------------------------
# backup
# ---------------------------------------------------------------------------


class TestBackup:
    def test_backup_records_to_the_log(self, tmp_path):
        job = _make_job(folders=["INBOX"])
        mock_client = _make_mock_client()
        mock_client.folder_backup.return_value = base.BackupResult(total=5, stored=5)

        with patch("mailvault.backend.session.open_mailbox") as mock_mb_cls:
            mock_mb_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
            mock_mb_cls.return_value.__exit__ = MagicMock(return_value=False)

            jobs.backup(job, tmp_path)

        mock_client.folder_backup.assert_called_once()
        call_kwargs = mock_client.folder_backup.call_args
        assert (
            call_kwargs.kwargs.get("callback") is not None
            or call_kwargs[1].get("callback") is not None
        )

        # A backup writes no database at all any more.
        assert not (tmp_path / "store.db").exists()

    def test_backup_writes_no_database(self, tmp_path):
        job = _make_job(folders=["INBOX", "Sent"])
        mock_client = _make_mock_client()
        mock_client.folder_backup.return_value = base.BackupResult()

        with patch("mailvault.backend.session.open_mailbox") as mock_mb_cls:
            mock_mb_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
            mock_mb_cls.return_value.__exit__ = MagicMock(return_value=False)

            jobs.backup(job, tmp_path)

        assert mock_client.folder_backup.call_count == 2
        assert not (tmp_path / "store.db").exists()

    def test_without_folders_every_folder_of_the_mailbox_is_backed_up(self, tmp_path):
        job = _make_job()  # folders=None
        mock_client = _make_mock_client()
        mock_client.folders.return_value = iter(["INBOX", "Sent", "Archive"])
        mock_client.folder_backup.return_value = base.BackupResult()

        with patch("mailvault.backend.session.open_mailbox") as mock_mb_cls:
            mock_mb_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
            mock_mb_cls.return_value.__exit__ = MagicMock(return_value=False)

            jobs.backup(job, tmp_path)

        assert mock_client.folder_backup.call_count == 3

    def test_one_broken_folder_does_not_stop_the_backup(self, tmp_path):
        """A folder that cannot be read costs that folder, not the run."""
        job = _make_job(folders=["Broken", "INBOX"])
        mock_client = _make_mock_client()
        mock_client.folder_backup.side_effect = [
            OSError("connection reset"),
            base.BackupResult(total=1, stored=1, newest=ARCHIVED_AT),
        ]

        with patch("mailvault.backend.session.open_mailbox") as mock_mb_cls:
            mock_mb_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
            mock_mb_cls.return_value.__exit__ = MagicMock(return_value=False)

            jobs.backup(job, tmp_path)

        assert mock_client.folder_backup.call_count == 2
        s = state.SnapshotState.load(tmp_path / "state.json")
        assert s.get_date("test-job", "Broken") is None
        assert s.get_date("test-job", "INBOX") is not None

    def test_backup_incremental_uses_snapshot(self, tmp_path):
        job = _make_job(folders=["INBOX"])
        mock_client = _make_mock_client()
        mock_client.folder_backup.return_value = base.BackupResult()

        # Pre-populate a snapshot
        with metadb.MetaDatabase(tmp_path / "store.db") as db:
            mb_id = db.add_mailbox("test-job")
            label_id = db.add_label("INBOX")
            snapshot_date = datetime(2026, 2, 1, tzinfo=UTC)
            db.set_snapshot(mb_id, label_id, date=snapshot_date)

        with patch("mailvault.backend.session.open_mailbox") as mock_mb_cls:
            mock_mb_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
            mock_mb_cls.return_value.__exit__ = MagicMock(return_value=False)

            jobs.backup(job, tmp_path, incremental=True)

        call_kwargs = mock_client.folder_backup.call_args
        since = call_kwargs.kwargs.get("since") or call_kwargs[1].get("since")
        assert since == snapshot_date

    def test_backup_non_incremental_ignores_snapshot(self, tmp_path):
        job = _make_job(folders=["INBOX"])
        mock_client = _make_mock_client()
        mock_client.folder_backup.return_value = base.BackupResult()

        with patch("mailvault.backend.session.open_mailbox") as mock_mb_cls:
            mock_mb_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
            mock_mb_cls.return_value.__exit__ = MagicMock(return_value=False)

            jobs.backup(job, tmp_path, incremental=False)

        call_kwargs = mock_client.folder_backup.call_args
        since = call_kwargs.kwargs.get("since") or call_kwargs[1].get("since")
        assert since is None

    def test_backup_db_stores_metadata(self, tmp_path):
        job = _make_job(folders=["INBOX"])
        mock_client = _make_mock_client()

        def fake_folder_backup(folder_name, store, since=None, callback=None):
            if callback:
                callback(
                    mailutils.MessageMetadata(
                        mailbox="test-job",
                        store_id="abc123",
                        folders=["INBOX"],
                    )
                )
            return base.BackupResult(total=1, stored=1)

        mock_client.folder_backup.side_effect = fake_folder_backup

        with patch("mailvault.backend.session.open_mailbox") as mock_mb_cls:
            mock_mb_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
            mock_mb_cls.return_value.__exit__ = MagicMock(return_value=False)

            jobs.backup(job, tmp_path)

        logs = list(metalog.read_all(tmp_path / "meta"))
        assert [(f.mailbox, f.folder) for f in logs] == [("test-job", "INBOX")]
        assert logs[0].store_ids == ["abc123"]

    def test_snapshot_advances_on_clean_run(self, tmp_path):
        job = _make_job(folders=["INBOX"])
        mock_client = _make_mock_client()
        mock_client.folder_backup.return_value = base.BackupResult(
            total=3,
            stored=3,
            newest=ARCHIVED_AT,
        )

        with patch("mailvault.backend.session.open_mailbox") as mock_mb_cls:
            mock_mb_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
            mock_mb_cls.return_value.__exit__ = MagicMock(return_value=False)

            jobs.backup(job, tmp_path)

        s = state.SnapshotState.load(tmp_path / "state.json")
        assert s.get_date("test-job", "INBOX") is not None

    def test_snapshot_frozen_on_failed_downloads(self, tmp_path):
        """A failed download must not be hidden behind an advanced snapshot."""
        job = _make_job(folders=["INBOX"])
        mock_client = _make_mock_client()
        mock_client.folder_backup.return_value = base.BackupResult(total=3, stored=2, failed=1)
        old_snapshot = datetime(2026, 2, 1, tzinfo=UTC)
        s = state.SnapshotState(tmp_path / "state.json")
        s.set_date("test-job", "INBOX", old_snapshot)
        s.save()

        with patch("mailvault.backend.session.open_mailbox") as mock_mb_cls:
            mock_mb_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
            mock_mb_cls.return_value.__exit__ = MagicMock(return_value=False)

            jobs.backup(job, tmp_path)

        assert (
            state.SnapshotState.load(tmp_path / "state.json").get_date("test-job", "INBOX")
            == old_snapshot
        )


# ---------------------------------------------------------------------------
# where the next run resumes
# ---------------------------------------------------------------------------


class TestResumePoint:
    """The snapshot follows the mail that was archived, never the wall clock.

    A source can report an empty folder without reporting an error -- Proton
    Bridge does exactly that between starting up and finishing its first sync,
    and an IMAP proxy with a cold cache behaves the same way. Taking the clock
    as the snapshot would answer that silence with "everything up to now is
    archived", and every message already in the mailbox would then sit before
    the next date filter: never fetched, never reported missing.
    """

    @staticmethod
    def _run(job, mock_client, tmp_path) -> None:
        with patch("mailvault.backend.session.open_mailbox") as mock_mb_cls:
            mock_mb_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
            mock_mb_cls.return_value.__exit__ = MagicMock(return_value=False)
            jobs.backup(job, tmp_path)

    def test_a_source_with_nothing_to_offer_does_not_start_a_snapshot(self, tmp_path):
        """The Proton Bridge case: no error, no mail, and no claim of coverage."""
        job = _make_job(folders=["INBOX"])
        mock_client = _make_mock_client()
        mock_client.folder_backup.return_value = base.BackupResult(total=0, stored=0)

        self._run(job, mock_client, tmp_path)

        s = state.SnapshotState.load(tmp_path / "state.json")
        assert s.get_date("test-job", "INBOX") is None

    def test_the_next_run_then_reads_the_folder_in_full(self, tmp_path):
        """What the held-back snapshot is for: the mail is still reachable."""
        job = _make_job(folders=["INBOX"])
        mock_client = _make_mock_client()
        mock_client.folder_backup.return_value = base.BackupResult(total=0, stored=0)
        self._run(job, mock_client, tmp_path)

        # The bridge has finished syncing by now and the mailbox is there.
        mock_client.folder_backup.reset_mock()
        mock_client.folder_backup.return_value = base.BackupResult(
            total=2,
            stored=2,
            newest=ARCHIVED_AT,
        )
        self._run(job, mock_client, tmp_path)

        assert mock_client.folder_backup.call_args.kwargs.get("since") is None

    def test_the_snapshot_is_the_newest_message_not_the_clock(self, tmp_path):
        """A source lagging behind must not be credited with the time it lags."""
        job = _make_job(folders=["INBOX"])
        mock_client = _make_mock_client()
        mock_client.folder_backup.return_value = base.BackupResult(
            total=1,
            stored=1,
            newest=ARCHIVED_AT,
        )

        self._run(job, mock_client, tmp_path)

        s = state.SnapshotState.load(tmp_path / "state.json")
        assert s.get_date("test-job", "INBOX") == ARCHIVED_AT

    def test_a_message_dated_in_the_future_cannot_carry_the_snapshot_past_now(
        self,
        tmp_path,
    ):
        """Someone else's broken clock must not skip our next window."""
        job = _make_job(folders=["INBOX"])
        mock_client = _make_mock_client()
        far_future = datetime(2099, 1, 1, tzinfo=UTC)
        mock_client.folder_backup.return_value = base.BackupResult(
            total=1,
            stored=1,
            newest=far_future,
        )

        self._run(job, mock_client, tmp_path)

        recorded = state.SnapshotState.load(tmp_path / "state.json").get_date(
            "test-job",
            "INBOX",
        )
        assert recorded is not None
        assert recorded < far_future

    def test_the_snapshot_never_moves_backwards(self, tmp_path):
        """The search window reaches back a day, so an older find is normal."""
        job = _make_job(folders=["INBOX"])
        mock_client = _make_mock_client()
        current = datetime(2026, 3, 1, tzinfo=UTC)
        s = state.SnapshotState(tmp_path / "state.json")
        s.set_date("test-job", "INBOX", current)
        s.save()
        mock_client.folder_backup.return_value = base.BackupResult(
            total=1,
            stored=1,
            newest=datetime(2026, 2, 28, tzinfo=UTC),
        )

        self._run(job, mock_client, tmp_path)

        assert (
            state.SnapshotState.load(tmp_path / "state.json").get_date("test-job", "INBOX")
            == current
        )

    def test_a_full_pass_may_move_the_snapshot_backwards(self, tmp_path):
        """Read without a date filter, the mail found *is* the coverage.

        This is what repairs a snapshot that was set too far ahead: the pass saw
        everything the source was willing to show, so the point is set to
        exactly that. Trusting a source that holds less than it should errs the
        harmless way here -- the next window comes out too wide.
        """
        job = _make_job(folders=["INBOX"])
        mock_client = _make_mock_client()
        ahead = datetime(2026, 7, 1, tzinfo=UTC)
        s = state.SnapshotState(tmp_path / "state.json")
        s.set_date("test-job", "INBOX", ahead)
        s.save()
        mock_client.folder_backup.return_value = base.BackupResult(
            total=1,
            stored=1,
            newest=ARCHIVED_AT,
        )

        with patch("mailvault.backend.session.open_mailbox") as mock_mb_cls:
            mock_mb_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
            mock_mb_cls.return_value.__exit__ = MagicMock(return_value=False)
            jobs.backup(job, tmp_path, incremental=False)

        assert (
            state.SnapshotState.load(tmp_path / "state.json").get_date("test-job", "INBOX")
            == ARCHIVED_AT
        )

    def test_a_full_pass_that_finds_nothing_clears_nothing(self, tmp_path):
        """There is no value to set it to, so the existing one stands."""
        job = _make_job(folders=["INBOX"])
        mock_client = _make_mock_client()
        current = datetime(2026, 3, 1, tzinfo=UTC)
        s = state.SnapshotState(tmp_path / "state.json")
        s.set_date("test-job", "INBOX", current)
        s.save()
        mock_client.folder_backup.return_value = base.BackupResult(total=0, stored=0)

        with patch("mailvault.backend.session.open_mailbox") as mock_mb_cls:
            mock_mb_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
            mock_mb_cls.return_value.__exit__ = MagicMock(return_value=False)
            jobs.backup(job, tmp_path, incremental=False)

        assert (
            state.SnapshotState.load(tmp_path / "state.json").get_date("test-job", "INBOX")
            == current
        )

    def test_a_quiet_folder_keeps_its_snapshot(self, tmp_path):
        """Nothing new is not the same as nothing there: the snapshot stands."""
        job = _make_job(folders=["INBOX"])
        mock_client = _make_mock_client()
        current = datetime(2026, 3, 1, tzinfo=UTC)
        s = state.SnapshotState(tmp_path / "state.json")
        s.set_date("test-job", "INBOX", current)
        s.save()
        mock_client.folder_backup.return_value = base.BackupResult(total=0, stored=0)

        self._run(job, mock_client, tmp_path)

        assert (
            state.SnapshotState.load(tmp_path / "state.json").get_date("test-job", "INBOX")
            == current
        )


# ---------------------------------------------------------------------------
# snapshot state file (state.json)
# ---------------------------------------------------------------------------


class TestSnapshotStateFile:
    """The snapshot state has to survive a metadata database that does not."""

    @staticmethod
    def _run(job, mock_client, tmp_path) -> None:
        with patch("mailvault.backend.session.open_mailbox") as mock_mb_cls:
            mock_mb_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
            mock_mb_cls.return_value.__exit__ = MagicMock(return_value=False)
            jobs.backup(job, tmp_path)

    @staticmethod
    def _since(mock_client):
        call_kwargs = mock_client.folder_backup.call_args
        return call_kwargs.kwargs.get("since") or call_kwargs[1].get("since")

    def test_state_file_written_on_clean_run(self, tmp_path):
        job = _make_job(folders=["INBOX"])
        mock_client = _make_mock_client()
        mock_client.folder_backup.return_value = base.BackupResult(
            total=3,
            stored=3,
            newest=ARCHIVED_AT,
        )

        self._run(job, mock_client, tmp_path)

        s = state.SnapshotState.load(tmp_path / "state.json")
        assert s.get_date("test-job", "INBOX") is not None

    def test_state_file_frozen_on_failed_downloads(self, tmp_path):
        job = _make_job(folders=["INBOX"])
        mock_client = _make_mock_client()
        mock_client.folder_backup.return_value = base.BackupResult(total=3, stored=2, failed=1)

        self._run(job, mock_client, tmp_path)

        s = state.SnapshotState.load(tmp_path / "state.json")
        assert s.get_date("test-job", "INBOX") is None

    def test_state_file_takes_precedence_over_database(self, tmp_path):
        """The state file is the durable copy, so it decides where a run resumes."""
        job = _make_job(folders=["INBOX"])
        mock_client = _make_mock_client()
        mock_client.folder_backup.return_value = base.BackupResult()

        stale = datetime(2026, 1, 1, tzinfo=UTC)
        current = datetime(2026, 6, 1, tzinfo=UTC)
        with metadb.MetaDatabase(tmp_path / "store.db") as db:
            db.set_snapshot(db.add_mailbox("test-job"), db.add_label("INBOX"), date=stale)
        s = state.SnapshotState(tmp_path / "state.json")
        s.set_date("test-job", "INBOX", current)
        s.save()

        self._run(job, mock_client, tmp_path)

        assert self._since(mock_client) == current

    def test_database_snapshot_is_adopted_when_state_file_is_absent(self, tmp_path):
        """Upgrading an existing archive must not trigger a full re-fetch."""
        job = _make_job(folders=["INBOX"])
        mock_client = _make_mock_client()
        mock_client.folder_backup.return_value = base.BackupResult()

        existing = datetime(2026, 2, 1, tzinfo=UTC)
        with metadb.MetaDatabase(tmp_path / "store.db") as db:
            db.set_snapshot(db.add_mailbox("test-job"), db.add_label("INBOX"), date=existing)

        self._run(job, mock_client, tmp_path)

        assert self._since(mock_client) == existing
        adopted = state.SnapshotState.load(tmp_path / "state.json")
        assert adopted.get_date("test-job", "INBOX") is not None

    def test_all_database_snapshots_are_adopted_at_once(self, tmp_path):
        """One run must carry over every folder, not just the ones it visits."""
        job = _make_job(folders=["INBOX"])
        mock_client = _make_mock_client()
        mock_client.folder_backup.return_value = base.BackupResult(
            total=1,
            stored=1,
            newest=ARCHIVED_AT,
        )

        untouched = datetime(2026, 2, 1, tzinfo=UTC)
        with metadb.MetaDatabase(tmp_path / "store.db") as db:
            mb_id = db.add_mailbox("test-job")
            for folder in ("INBOX", "Sent", "Archiv/2016"):
                db.set_snapshot(mb_id, db.add_label(folder), date=untouched)

        self._run(job, mock_client, tmp_path)

        s = state.SnapshotState.load(tmp_path / "state.json")
        assert s.get_date("test-job", "Sent") == untouched
        assert s.get_date("test-job", "Archiv/2016") == untouched
        # The visited folder advanced, the others kept the adopted timestamp.
        inbox_date = s.get_date("test-job", "INBOX")
        assert inbox_date is not None
        assert inbox_date > untouched

    def test_adoption_never_overwrites_an_existing_state_file(self, tmp_path):
        job = _make_job(folders=["INBOX"])
        mock_client = _make_mock_client()
        mock_client.folder_backup.return_value = base.BackupResult()

        current = datetime(2026, 6, 1, tzinfo=UTC)
        stale = datetime(2026, 1, 1, tzinfo=UTC)
        with metadb.MetaDatabase(tmp_path / "store.db") as db:
            db.set_snapshot(db.add_mailbox("test-job"), db.add_label("Sent"), date=stale)
        s = state.SnapshotState(tmp_path / "state.json")
        s.set_date("test-job", "Sent", current)
        s.save()

        self._run(job, mock_client, tmp_path)

        assert (
            state.SnapshotState.load(tmp_path / "state.json").get_date("test-job", "Sent")
            == current
        )

    def test_unwritable_state_file_does_not_abort_the_run(self, tmp_path, caplog):
        """Losing the state file costs bandwidth later, never the run in progress."""
        job = _make_job(folders=["INBOX"])
        mock_client = _make_mock_client()
        mock_client.folder_backup.return_value = base.BackupResult(
            total=1,
            stored=1,
            newest=ARCHIVED_AT,
        )

        with patch.object(state.SnapshotState, "save", side_effect=OSError("read-only")):
            self._run(job, mock_client, tmp_path)

        assert "resume state not written" in caplog.text


# ---------------------------------------------------------------------------
# metadata log (meta/*.jsonl)
# ---------------------------------------------------------------------------


def _fake_folder_backup(*store_ids: str, failed: int = 0):
    """Build a folder_backup stand-in that reports the given messages."""

    def run(folder_name, store, since=None, callback=None):
        for store_id in store_ids:
            if callback:
                callback(
                    mailutils.MessageMetadata(
                        mailbox="test-job",
                        store_id=store_id,
                        folders=[folder_name],
                    )
                )
        return base.BackupResult(
            total=len(store_ids) + failed, stored=len(store_ids), failed=failed
        )

    return run


class TestStoreMessage:
    """A message whose location was not recorded must not be reported as archived:
    a non-None return lets the caller delete it from the server, and the location
    is the one fact the archive cannot reconstruct."""

    @staticmethod
    def _store(tmp_path):
        return cas.ContentAddressedStorage(tmp_path, suffix=".eml")

    def test_metadata_failure_holds_and_is_not_deletable(self, tmp_path):
        result = base.BackupResult()
        recorded: list = []

        def failing_metadata(_store_id):
            raise RuntimeError("label fetch failed")

        store_id = base.store_message(
            self._store(tmp_path),
            DUMMY_EML,
            result=result,
            log_ctx="test-job::INBOX[1]",
            callback=recorded.append,
            metadata_fn=failing_metadata,
        )

        # Stored on disk, but its location was never handed to the callback, so
        # it must count as failed (snapshot holds) and never become deletable.
        assert store_id is None
        assert result.failed == 1
        assert result.stored == 0
        assert recorded == []

    def test_recording_failure_holds(self, tmp_path):
        result = base.BackupResult()

        def failing_callback(_metadata):
            raise RuntimeError("disk full")

        store_id = base.store_message(
            self._store(tmp_path),
            DUMMY_EML,
            result=result,
            log_ctx="test-job::INBOX[1]",
            callback=failing_callback,
            metadata_fn=lambda sid: mailutils.metadata("test-job", "INBOX", sid),
        )

        assert store_id is None
        assert result.failed == 1
        assert result.stored == 0

    def test_success_records_and_returns_the_store_id(self, tmp_path):
        result = base.BackupResult()
        recorded: list = []

        store_id = base.store_message(
            self._store(tmp_path),
            DUMMY_EML,
            result=result,
            log_ctx="test-job::INBOX[1]",
            callback=recorded.append,
            metadata_fn=lambda sid: mailutils.metadata("test-job", "INBOX", sid),
        )

        assert store_id is not None
        assert result.stored == 1
        assert result.failed == 0
        assert [m.store_id for m in recorded] == [store_id]


class TestDeleteAfterExport:
    """Deletion is gated on a durable log: purge runs only after a good seal."""

    @staticmethod
    def _backup_with_deletable(*store_ids: str, deletable: list):
        """A folder_backup stand-in that records the messages and reports which
        server ids may be deleted once the log is sealed."""

        def run(folder_name, store, since=None, callback=None):
            for sid in store_ids:
                if callback:
                    callback(
                        mailutils.MessageMetadata(
                            mailbox="test-job",
                            store_id=sid,
                            folders=[folder_name],
                        )
                    )
            return base.BackupResult(
                total=len(store_ids), stored=len(store_ids), deletable=list(deletable)
            )

        return run

    @staticmethod
    def _run(job, mock_client, tmp_path) -> None:
        with patch("mailvault.backend.session.open_mailbox") as mock_mb_cls:
            mock_mb_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
            mock_mb_cls.return_value.__exit__ = MagicMock(return_value=False)
            jobs.backup(job, tmp_path)

    def test_purges_after_a_successful_seal(self, tmp_path):
        job = _make_job(folders=["INBOX"], delete_after_export=True)
        mock_client = _make_mock_client()
        mock_client.folder_backup.side_effect = self._backup_with_deletable(
            "aaa", deletable=[1, 2]
        )

        # Capture, at the moment purge runs, whether the log is already durable.
        # This is the ordering the fix is about: the location reaches disk before
        # the message is removed from the server.
        log_present_at_purge = []

        def record_purge(folder, ids):
            log_present_at_purge.append(bool(list(metalog.read_all(tmp_path / "meta"))))

        mock_client.purge.side_effect = record_purge

        self._run(job, mock_client, tmp_path)

        mock_client.purge.assert_called_once_with("INBOX", [1, 2])
        assert log_present_at_purge == [True]

    def test_does_not_purge_when_the_seal_fails(self, tmp_path, monkeypatch):
        job = _make_job(folders=["INBOX"], delete_after_export=True)
        mock_client = _make_mock_client()
        mock_client.folder_backup.side_effect = self._backup_with_deletable(
            "aaa", deletable=[1, 2]
        )

        def boom(self, date):
            raise OSError("no space left on device")

        monkeypatch.setattr(metalog.LogWriter, "seal", boom)

        self._run(job, mock_client, tmp_path)

        # A message must never leave the server when its location was not written.
        mock_client.purge.assert_not_called()

    def test_does_not_purge_without_delete_after_export(self, tmp_path):
        # The caller-side gate: a populated deletable list is ignored when the
        # job does not delete after export.
        job = _make_job(folders=["INBOX"])
        mock_client = _make_mock_client()
        mock_client.folder_backup.side_effect = self._backup_with_deletable(
            "aaa", deletable=[1, 2]
        )

        self._run(job, mock_client, tmp_path)

        mock_client.purge.assert_not_called()


class TestMetadataLog:
    """The attribution the .eml files cannot carry has to reach the log."""

    @staticmethod
    def _run(job, mock_client, tmp_path) -> None:
        with patch("mailvault.backend.session.open_mailbox") as mock_mb_cls:
            mock_mb_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
            mock_mb_cls.return_value.__exit__ = MagicMock(return_value=False)
            jobs.backup(job, tmp_path)

    def test_backup_records_mailbox_and_folder(self, tmp_path):
        job = _make_job(folders=["INBOX"])
        mock_client = _make_mock_client()
        mock_client.folder_backup.side_effect = _fake_folder_backup("aaa", "bbb")

        self._run(job, mock_client, tmp_path)

        logs = list(metalog.read_all(tmp_path / "meta"))
        assert len(logs) == 1
        assert logs[0].mailbox == "test-job"
        assert logs[0].folder == "INBOX"
        assert logs[0].store_ids == ["aaa", "bbb"]

    def test_unchanged_folder_writes_no_log(self, tmp_path):
        job = _make_job(folders=["INBOX"])
        mock_client = _make_mock_client()
        mock_client.folder_backup.return_value = base.BackupResult()

        self._run(job, mock_client, tmp_path)

        assert not metalog.has_logs(tmp_path / "meta")

    def test_failed_folder_is_logged_but_snapshot_is_not_advanced(self, tmp_path):
        """Stored messages keep their attribution; only progress is withheld."""
        job = _make_job(folders=["INBOX"])
        mock_client = _make_mock_client()
        mock_client.folder_backup.side_effect = _fake_folder_backup("aaa", failed=1)

        self._run(job, mock_client, tmp_path)

        logs = list(metalog.read_all(tmp_path / "meta"))
        assert len(logs) == 1
        assert logs[0].store_ids == ["aaa"]
        assert state.SnapshotState.load(tmp_path / "state.json").is_empty()

    def test_existing_archive_is_bootstrapped_on_first_run(self, tmp_path):
        """An archive filled by an earlier version is protected straight away."""
        with metadb.MetaDatabase(tmp_path / "store.db") as db:
            mb_id = db.add_mailbox("old-job")
            msg_id = db.add_message("old", "<old@example.com>", None, "Subject")
            db.assign_message_to_mailbox(msg_id, mb_id)
            db.add_message_labels(msg_id, "Archiv/2016")

        job = _make_job(folders=["INBOX"])
        mock_client = _make_mock_client()
        mock_client.folder_backup.return_value = base.BackupResult()

        self._run(job, mock_client, tmp_path)

        logs = list(metalog.read_all(tmp_path / "meta"))
        assert len(logs) == 1
        assert logs[0].mailbox == "old-job"
        assert logs[0].folder == "Archiv/2016"
        assert logs[0].store_ids == ["old"]

    def test_existing_log_is_not_bootstrapped_again(self, tmp_path):
        job = _make_job(folders=["INBOX"])
        mock_client = _make_mock_client()
        mock_client.folder_backup.side_effect = _fake_folder_backup("aaa")
        self._run(job, mock_client, tmp_path)
        before = metalog.log_files(tmp_path / "meta")

        mock_client.folder_backup.side_effect = _fake_folder_backup()
        self._run(job, mock_client, tmp_path)

        assert metalog.log_files(tmp_path / "meta") == before


class TestMigration:
    def test_moves_locations_out_of_an_existing_database(self, tmp_path):
        with metadb.MetaDatabase(tmp_path / "store.db") as db:
            msg_id = db.add_message("aaa", "<a@example.com>", None, "Subject")
            for mailbox in ("job-a", "job-b"):
                db.assign_message_to_mailbox(msg_id, db.add_mailbox(mailbox))
            db.add_message_labels(msg_id, "INBOX", "\\Sent")

        result = jobs.migrate_archive(tmp_path)

        assert result.needed is True
        assert result.verified is True
        assert result.messages == 1
        places = {(f.mailbox, f.folder) for f in metalog.read_all(tmp_path / "meta")}
        assert places == {("job-a", None), ("job-b", None)}
        # Renamed, never deleted -- the name says which artefact counts.
        assert not (tmp_path / "store.db").exists()
        assert (tmp_path / "store.db.migrated").exists()

    def test_the_legacy_database_is_not_written_to(self, tmp_path):
        """Migration only reads store.db, so the renamed file is byte-identical."""
        with metadb.MetaDatabase(tmp_path / "store.db") as db:
            msg_id = db.add_message("aaa", "<a@example.com>", None, "Subject")
            db.assign_message_to_mailbox(msg_id, db.add_mailbox("job-a"))
            db.add_message_labels(msg_id, "INBOX")
        before = (tmp_path / "store.db").read_bytes()

        result = jobs.migrate_archive(tmp_path)

        assert result.needed is True
        # setup() is skipped for the read, so not one byte of it changed.
        assert (tmp_path / "store.db.migrated").read_bytes() == before

    def test_a_second_run_has_nothing_to_do(self, tmp_path):
        """The absence of store.db is what says "already migrated"."""
        with metadb.MetaDatabase(tmp_path / "store.db") as db:
            msg_id = db.add_message("aaa", "<a@example.com>", None, "Subject")
            db.assign_message_to_mailbox(msg_id, db.add_mailbox("job-a"))
        jobs.migrate_archive(tmp_path)

        result = jobs.migrate_archive(tmp_path)

        assert result.needed is False
        assert len(metalog.log_files(tmp_path / "meta")) == 1

    def test_it_announces_the_migration_before_doing_it(self, tmp_path, caplog):
        """The slow work must not look like a hang: say it is happening first."""
        with metadb.MetaDatabase(tmp_path / "store.db") as db:
            msg_id = db.add_message("aaa", "<a@example.com>", None, "Subject")
            db.assign_message_to_mailbox(msg_id, db.add_mailbox("job-a"))

        with caplog.at_level(logging.INFO):
            jobs.migrate_archive(tmp_path)

        assert "may take a moment" in caplog.text

    def test_nothing_to_migrate_says_nothing(self, tmp_path, caplog):
        """No legacy database, no announcement -- the common case stays quiet."""
        with caplog.at_level(logging.INFO):
            result = jobs.migrate_archive(tmp_path)

        assert result.needed is False
        assert "may take a moment" not in caplog.text

    def test_an_interrupted_migration_is_simply_repeated(self, tmp_path):
        """store.db still there means not done -- exporting twice is harmless."""
        with metadb.MetaDatabase(tmp_path / "store.db") as db:
            msg_id = db.add_message("aaa", "<a@example.com>", None, "Subject")
            db.assign_message_to_mailbox(msg_id, db.add_mailbox("job-a"))
        jobs.migrate_archive(tmp_path)
        # Put it back, as if the rename had never happened.
        (tmp_path / "store.db.migrated").rename(tmp_path / "store.db")

        result = jobs.migrate_archive(tmp_path)

        assert result.needed is True
        assert result.verified is True
        # A second file, because its header carries a later date -- and harmless,
        # because it says the same thing and replaying it is idempotent.
        assert len(metalog.log_files(tmp_path / "meta")) == 2
        places = {(f.mailbox, f.folder) for f in metalog.read_all(tmp_path / "meta")}
        assert places == {("job-a", None)}

    def test_folder_is_placed_by_elimination_when_nothing_witnesses_it(self, tmp_path):
        """A folder only ever seen on messages in two mailboxes has no witness.

        It becomes decidable anyway: one message names a folder that belongs to
        the other mailbox, which leaves exactly one mailbox unexplained, and the
        pairing learnt there settles every remaining message.
        """
        with metadb.MetaDatabase(tmp_path / "store.db") as db:
            gmail = db.add_mailbox("mail.example.org")
            imapbox = db.add_mailbox("other.example.org")
            db.set_snapshot(imapbox, db.add_label("Archiv/Chat"), date=datetime.now(UTC))
            for n, extra in enumerate(([], ["\\Important"])):
                msg = db.add_message(f"m{n}", f"<m{n}@example.com>", None, "Subject")
                db.assign_message_to_mailbox(msg, gmail)
                db.assign_message_to_mailbox(msg, imapbox)
                db.add_message_labels(msg, "Archiv/Chat", "Chat", *extra)
            # Witness that '\Important' can only be the Gmail-style mailbox's.
            solo = db.add_message("solo", "<solo@example.com>", None, "Subject")
            db.assign_message_to_mailbox(solo, gmail)
            db.add_message_labels(solo, "\\Important")

        result = jobs.migrate_archive(tmp_path)

        assert result.undecidable == 0
        places = {
            (f.mailbox, f.folder): set(f.store_ids) for f in metalog.read_all(tmp_path / "meta")
        }
        assert places[("mail.example.org", "Chat")] == {"m0", "m1"}
        assert places[("other.example.org", "Archiv/Chat")] == {"m0", "m1"}

    def test_undecidable_folder_is_left_out_rather_than_guessed(self, tmp_path):
        """Two mailboxes with the same folder name and no way to tell them apart."""
        with metadb.MetaDatabase(tmp_path / "store.db") as db:
            first = db.add_mailbox("a.example.org")
            second = db.add_mailbox("b.example.org")
            inbox = db.add_label("INBOX")
            db.set_snapshot(first, inbox, date=datetime.now(UTC))
            db.set_snapshot(second, inbox, date=datetime.now(UTC))
            msg = db.add_message("aaa", "<a@example.com>", None, "Subject")
            db.assign_message_to_mailbox(msg, first)
            db.assign_message_to_mailbox(msg, second)
            db.add_message_labels(msg, "INBOX")

        result = jobs.migrate_archive(tmp_path)

        assert result.undecidable == 1
        places = {(f.mailbox, f.folder) for f in metalog.read_all(tmp_path / "meta")}
        assert places == {("a.example.org", None), ("b.example.org", None)}

    def test_archive_without_a_database_needs_nothing(self, tmp_path):
        result = jobs.migrate_archive(tmp_path)

        assert result.needed is False
        assert not metalog.has_logs(tmp_path / "meta")


class TestRebuildWithLog:
    """A rebuild has to restore what the .eml files cannot supply."""

    @staticmethod
    def _archive_with_log(tmp_path, places):
        store = cas.ContentAddressedStorage(tmp_path, suffix=".eml")
        _status, store_id, _path = store.add(DUMMY_EML)
        writer = metalog.LogWriter(tmp_path / "meta")
        for mailbox, folders in places:
            writer.add(mailbox, folders, store_id)
        writer.seal(datetime(2026, 8, 1, tzinfo=UTC))
        return store_id

    def test_replay_restores_mailbox_and_labels(self, tmp_path):
        store_id = self._archive_with_log(tmp_path, [("mail.example.org", ["INBOX", "\\Sent"])])

        result = jobs.create_db(tmp_path, tmp_path / "out.db")

        # Two places -- one file each -- so the message is applied twice.
        assert result.messages == 1
        assert result.replay.files == 2
        assert result.replay.applied == 2
        assert result.replay.unknown == 0
        with metadb.MetaDatabase(tmp_path / "out.db") as db:
            msg_id = db.store_id_map()[store_id]
            assert db.message_mailboxes()[msg_id] == ["mail.example.org"]
            labels = [
                row[0]
                for row in db.execute(
                    "SELECT l.name FROM message_label ml JOIN label l USING (label_id) "
                    "WHERE message_id=?",
                    (msg_id,),
                ).fetchall()
            ]
            assert sorted(labels) == ["INBOX", "\\Sent"]

    def test_replay_restores_a_message_held_in_several_mailboxes(self, tmp_path):
        store_id = self._archive_with_log(
            tmp_path, [("mail.example.org", ["INBOX"]), ("other.example.org", ["INBOX"])]
        )

        jobs.create_db(tmp_path, tmp_path / "out.db")

        with metadb.MetaDatabase(tmp_path / "out.db") as db:
            msg_id = db.store_id_map()[store_id]
            assert sorted(db.message_mailboxes()[msg_id]) == [
                "mail.example.org",
                "other.example.org",
            ]

    def test_log_entries_for_absent_messages_are_counted_not_invented(self, tmp_path):
        """A blob removed from the archive must not reappear as a database row."""
        writer = metalog.LogWriter(tmp_path / "meta")
        writer.add("job", ["INBOX"], "deadbeef")
        writer.seal(datetime(2026, 8, 1, tzinfo=UTC))

        result = jobs.create_db(tmp_path, tmp_path / "out.db")

        assert result.replay.unknown == 1
        assert result.replay.applied == 0
        with metadb.MetaDatabase(tmp_path / "out.db") as db:
            assert db.store_id_map() == {}

    def test_an_existing_database_is_refused(self, tmp_path):
        """ "create" creates; filling an existing file would make it an accumulation."""
        store = cas.ContentAddressedStorage(tmp_path, suffix=".eml")
        store.add(DUMMY_EML)
        target = tmp_path / "out.db"
        jobs.create_db(tmp_path, target)

        with pytest.raises(jobs.JobError, match="already exists"):
            jobs.create_db(tmp_path, target)

    def test_force_replaces_rather_than_adds(self, tmp_path):
        store = cas.ContentAddressedStorage(tmp_path, suffix=".eml")
        store.add(DUMMY_EML)
        target = tmp_path / "out.db"
        jobs.create_db(tmp_path, target)
        with metadb.MetaDatabase(target) as db:
            db.add_message("stale", "<stale@example.com>", None, "Gone from the archive")

        jobs.create_db(tmp_path, target, force=True)

        with metadb.MetaDatabase(target) as db:
            assert "stale" not in db.store_id_map()

    def test_an_interrupted_build_leaves_the_previous_database_alone(self, tmp_path):
        store = cas.ContentAddressedStorage(tmp_path, suffix=".eml")
        store.add(DUMMY_EML)
        target = tmp_path / "out.db"
        jobs.create_db(tmp_path, target)
        before = target.read_bytes()

        with patch("mailvault.jobs.storedb._replay_metalog", side_effect=KeyboardInterrupt):
            with pytest.raises(KeyboardInterrupt):
                jobs.create_db(tmp_path, target, force=True)

        assert target.read_bytes() == before
        assert not (tmp_path / "out.db._tmp_").exists()

    def test_rebuild_without_a_log_reports_no_files(self, tmp_path):
        store = cas.ContentAddressedStorage(tmp_path, suffix=".eml")
        store.add(DUMMY_EML)

        result = jobs.create_db(tmp_path, tmp_path / "out.db")

        assert result.messages == 1
        assert result.replay.files == 0


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


def _eml(message_id: str, subject: str = "Subject") -> bytes:
    return (
        b"From: sender@example.com\r\n"
        b"To: recipient@example.com\r\n"
        + f"Subject: {subject}\r\n".encode()
        + f"Message-ID: {message_id}\r\n".encode()
        + b"Date: Wed, 20 Feb 2026 12:00:00 +0100\r\n"
        b"\r\n" + f"Body of {subject}.\r\n".encode()
    )


def _archive_message(store_path, job_name: str, folder: str, msg: bytes) -> None:
    """Put a message into the archive the way a successful backup would."""
    store = cas.ContentAddressedStorage(store_path, suffix=".eml")
    _status, store_id, _path = store.add(msg)
    writer = metalog.LogWriter(store_path / metalog.DEFAULT_LOG_DIR)
    writer.add(job_name, [folder], store_id)
    writer.seal(datetime.now(UTC))


def _verify_client(index: list[base.MessageRef], bodies: dict[str, bytes]):
    client = _make_mock_client()
    client.message_index.return_value = iter(index)
    client.fetch_message.side_effect = lambda msg_id, folder: bodies[msg_id]
    return client


class TestVerify:
    def test_reports_missing_messages(self, tmp_path):
        job = _make_job(folders=["INBOX"])
        archived = _eml("<a@example.com>", "Archived")
        _archive_message(tmp_path, "test-job", "INBOX", archived)

        client = _verify_client(
            [
                base.MessageRef(msg_id="id-a", message_id="<a@example.com>"),
                base.MessageRef(msg_id="id-b", message_id="<b@example.com>"),
            ],
            {},
        )
        with patch("mailvault.backend.session.open_mailbox") as mock_mb_cls:
            mock_mb_cls.return_value.__enter__ = MagicMock(return_value=client)
            mock_mb_cls.return_value.__exit__ = MagicMock(return_value=False)

            results = jobs.verify(job, tmp_path)

        assert len(results) == 1
        assert results[0].on_server == 2
        assert results[0].missing == 1
        assert results[0].restored == 0
        # Without --repair nothing is downloaded.
        client.fetch_message.assert_not_called()

    def test_repair_fetches_and_stores(self, tmp_path):
        job = _make_job(folders=["INBOX"])
        archived = _eml("<a@example.com>", "Archived")
        lost = _eml("<b@example.com>", "Lost")
        _archive_message(tmp_path, "test-job", "INBOX", archived)

        client = _verify_client(
            [
                base.MessageRef(msg_id="id-a", message_id="<a@example.com>"),
                base.MessageRef(msg_id="id-b", message_id="<b@example.com>"),
            ],
            {"id-b": lost},
        )
        with patch("mailvault.backend.session.open_mailbox") as mock_mb_cls:
            mock_mb_cls.return_value.__enter__ = MagicMock(return_value=client)
            mock_mb_cls.return_value.__exit__ = MagicMock(return_value=False)

            results = jobs.verify(job, tmp_path, repair=True)

        assert results[0].missing == 1
        assert results[0].restored == 1
        assert results[0].failed == 0
        # Only the missing message is downloaded.
        client.fetch_message.assert_called_once_with("id-b", "INBOX")

        store = cas.ContentAddressedStorage(tmp_path, suffix=".eml")
        assert len(list(store.walk())) == 2
        # The restored message reached the log too, not just the archive.
        places = _places_from_log(tmp_path / metalog.DEFAULT_LOG_DIR)
        known = _archived_message_ids(store, places[("test-job", "INBOX")])
        assert known == {"a@example.com", "b@example.com"}

    def test_repair_is_idempotent(self, tmp_path):
        """A second verify run right after a repair must find nothing."""
        job = _make_job(folders=["INBOX"])
        lost = _eml("<b@example.com>", "Lost")
        index = [base.MessageRef(msg_id="id-b", message_id="<b@example.com>")]

        with patch("mailvault.backend.session.open_mailbox") as mock_mb_cls:
            mock_mb_cls.return_value.__enter__ = MagicMock(
                return_value=_verify_client(list(index), {"id-b": lost})
            )
            mock_mb_cls.return_value.__exit__ = MagicMock(return_value=False)
            jobs.verify(job, tmp_path, repair=True)

            client = _verify_client(list(index), {"id-b": lost})
            mock_mb_cls.return_value.__enter__ = MagicMock(return_value=client)
            results = jobs.verify(job, tmp_path, repair=True)

        assert results[0].missing == 0
        client.fetch_message.assert_not_called()

    def test_message_id_matching_ignores_brackets_and_case(self, tmp_path):
        job = _make_job(folders=["INBOX"])
        _archive_message(tmp_path, "test-job", "INBOX", _eml("<Mixed@Example.COM>"))

        client = _verify_client(
            [base.MessageRef(msg_id="id-a", message_id="mixed@example.com")], {}
        )
        with patch("mailvault.backend.session.open_mailbox") as mock_mb_cls:
            mock_mb_cls.return_value.__enter__ = MagicMock(return_value=client)
            mock_mb_cls.return_value.__exit__ = MagicMock(return_value=False)

            results = jobs.verify(job, tmp_path)

        assert results[0].missing == 0

    def test_download_failure_is_counted(self, tmp_path):
        job = _make_job(folders=["INBOX"])
        client = _make_mock_client()
        client.message_index.return_value = iter(
            [base.MessageRef(msg_id="id-b", message_id="<b@example.com>")]
        )
        client.fetch_message.side_effect = OSError("connection reset")

        with patch("mailvault.backend.session.open_mailbox") as mock_mb_cls:
            mock_mb_cls.return_value.__enter__ = MagicMock(return_value=client)
            mock_mb_cls.return_value.__exit__ = MagicMock(return_value=False)

            results = jobs.verify(job, tmp_path, repair=True)

        assert results[0].missing == 1
        assert results[0].restored == 0
        assert results[0].failed == 1

    def test_verify_leaves_snapshot_untouched(self, tmp_path):
        job = _make_job(folders=["INBOX"])
        old_snapshot = datetime(2026, 2, 1, tzinfo=UTC)
        with metadb.MetaDatabase(tmp_path / "store.db") as db:
            db.set_snapshot(
                db.add_mailbox("test-job"), db.add_label("INBOX"), date=old_snapshot
            )

        client = _verify_client([], {})
        with patch("mailvault.backend.session.open_mailbox") as mock_mb_cls:
            mock_mb_cls.return_value.__enter__ = MagicMock(return_value=client)
            mock_mb_cls.return_value.__exit__ = MagicMock(return_value=False)

            jobs.verify(job, tmp_path)

        with metadb.MetaDatabase(tmp_path / "store.db") as db:
            assert db.all_snapshots() == [("test-job", "INBOX", old_snapshot.isoformat())]

    def test_rejects_exchange_journal(self, tmp_path):
        job = _make_job(exchange_journal=True, folders=["INBOX"])
        with pytest.raises(jobs.JobError, match="exchange_journal"):
            jobs.verify(job, tmp_path)

    def test_one_broken_folder_does_not_stop_the_rest(self, tmp_path):
        job = _make_job(folders=["Broken", "INBOX"])
        client = _make_mock_client()

        def index(folder, since=None):
            if folder == "Broken":
                raise OSError("folder vanished")
            return iter([base.MessageRef(msg_id="id-a", message_id="<a@example.com>")])

        client.message_index.side_effect = index

        with patch("mailvault.backend.session.open_mailbox") as mock_mb_cls:
            mock_mb_cls.return_value.__enter__ = MagicMock(return_value=client)
            mock_mb_cls.return_value.__exit__ = MagicMock(return_value=False)

            results = jobs.verify(job, tmp_path)

        assert [r.folder for r in results] == ["INBOX"]
        assert results[0].missing == 1


# ---------------------------------------------------------------------------
# folder_list
# ---------------------------------------------------------------------------


class TestFolderList:
    def test_prints_folders(self, capsys):
        job = _make_job()
        mock_client = _make_mock_client()
        mock_client.folders.return_value = iter(["INBOX", "Sent", "Archive"])

        with patch("mailvault.backend.session.open_mailbox") as mock_mb_cls:
            mock_mb_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
            mock_mb_cls.return_value.__exit__ = MagicMock(return_value=False)

            jobs.folder_list(job)

        output = capsys.readouterr().out
        assert "test-job::INBOX" in output
        assert "test-job::Sent" in output
        assert "test-job::Archive" in output


# ---------------------------------------------------------------------------
# rebuild_metadb
# ---------------------------------------------------------------------------


class TestUpdateDbFromArchive:
    def test_rebuilds_db(self, tmp_path):
        # Create a CAS with a message
        store = cas.ContentAddressedStorage(tmp_path, suffix=".eml")
        store.add(DUMMY_EML)

        jobs.create_db(tmp_path, tmp_path / "out.db", mailbox="test")

        with metadb.MetaDatabase(tmp_path / "out.db") as db:
            rows = db.execute("SELECT * FROM message").fetchall()
            assert len(rows) == 1

    def test_rebuilds_db_without_mailbox(self, tmp_path):
        store = cas.ContentAddressedStorage(tmp_path, suffix=".eml")
        store.add(DUMMY_EML)

        jobs.create_db(tmp_path, tmp_path / "out.db")

        with metadb.MetaDatabase(tmp_path / "out.db") as db:
            rows = db.execute("SELECT * FROM message").fetchall()
            assert len(rows) == 1
            # No mailbox assignment
            mm_rows = db.execute("SELECT * FROM message_mailbox").fetchall()
            assert len(mm_rows) == 0


# ---------------------------------------------------------------------------
# refresh_db (the kept-fresh projection)
# ---------------------------------------------------------------------------


class TestRefreshDb:
    """Incremental, self-healing, rebuildable -- never a source of truth."""

    def test_absent_database_is_built_from_scratch(self, tmp_path):
        _archive_message(tmp_path, "job", "INBOX", _eml("<a@example.com>"))
        db_path = tmp_path / DEFAULT_QUERY_DB_NAME

        result = refresh_db(tmp_path, db_path)

        assert result.rebuilt is True
        assert result.messages == 1
        assert db_path.exists()

    def test_only_new_logs_are_applied_on_a_refresh(self, tmp_path):
        _archive_message(tmp_path, "job", "INBOX", _eml("<a@example.com>"))
        db_path = tmp_path / DEFAULT_QUERY_DB_NAME
        refresh_db(tmp_path, db_path)  # initial full build

        _archive_message(tmp_path, "job", "Archive", _eml("<b@example.com>", "Second"))
        result = refresh_db(tmp_path, db_path)

        assert result.rebuilt is False
        assert result.files == 1  # only the new log file was read
        assert result.messages == 1  # only the new message was inserted
        with metadb.MetaDatabase(db_path) as db:
            assert len(db.store_id_map()) == 2

    def test_an_up_to_date_database_is_left_untouched(self, tmp_path):
        _archive_message(tmp_path, "job", "INBOX", _eml("<a@example.com>"))
        db_path = tmp_path / DEFAULT_QUERY_DB_NAME
        refresh_db(tmp_path, db_path)

        result = refresh_db(tmp_path, db_path)

        assert result.rebuilt is False
        assert result.files == 0
        assert result.messages == 0

    def test_an_unreadable_database_is_rebuilt(self, tmp_path):
        _archive_message(tmp_path, "job", "INBOX", _eml("<a@example.com>"))
        db_path = tmp_path / DEFAULT_QUERY_DB_NAME
        db_path.write_bytes(b"this is not a sqlite database at all")

        result = refresh_db(tmp_path, db_path)

        assert result.rebuilt is True
        assert result.messages == 1
        with metadb.MetaDatabase(db_path) as db:
            assert len(db.store_id_map()) == 1


def _storing_backup(eml: bytes):
    """A folder_backup stand-in that stores an eml and logs it, like a real run."""

    def run(folder_name, store, since=None, callback=None):
        _status, store_id, _path = store.add(eml)
        if callback:
            callback(
                mailutils.MessageMetadata(
                    mailbox="test-job",
                    store_id=store_id,
                    folders=[folder_name],
                )
            )
        return base.BackupResult(total=1, stored=1)

    return run


class TestBackupIndexDb:
    """--index-db refreshes a queryable projection beside the archive, opt-in."""

    @staticmethod
    def _run(mock_client, tmp_path, index_db):
        job = _make_job(folders=["INBOX"])
        with patch("mailvault.backend.session.open_mailbox") as mock_mb_cls:
            mock_mb_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
            mock_mb_cls.return_value.__exit__ = MagicMock(return_value=False)
            jobs.backup(job, tmp_path, index_db=index_db)

    def test_index_db_builds_the_projection(self, tmp_path):
        mock_client = _make_mock_client()
        mock_client.folder_backup.side_effect = _storing_backup(_eml("<a@example.com>"))

        self._run(mock_client, tmp_path, index_db=True)

        db_path = tmp_path / DEFAULT_QUERY_DB_NAME
        assert db_path.exists()
        with metadb.MetaDatabase(db_path) as db:
            assert len(db.store_id_map()) == 1

    def test_backup_writes_no_projection_by_default(self, tmp_path):
        mock_client = _make_mock_client()
        mock_client.folder_backup.side_effect = _storing_backup(_eml("<a@example.com>"))

        self._run(mock_client, tmp_path, index_db=False)

        assert not (tmp_path / DEFAULT_QUERY_DB_NAME).exists()
