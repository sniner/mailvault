"""Tests for mailvault.jobs with mocked Mailbox and CAS."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from mailvault import conf, jobs, mailutils
from mailvault.backend import base
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
    def test_backup_with_db(self, tmp_path):
        job = _make_job(with_db=True, folders=["INBOX"])
        mock_client = _make_mock_client()
        mock_client.folder_backup.return_value = base.BackupResult(total=5, stored=5)

        with patch("mailvault.jobs.session.open_mailbox") as mock_mb_cls:
            mock_mb_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
            mock_mb_cls.return_value.__exit__ = MagicMock(return_value=False)

            jobs.backup(job, tmp_path)

        mock_client.folder_backup.assert_called_once()
        call_kwargs = mock_client.folder_backup.call_args
        assert (
            call_kwargs.kwargs.get("callback") is not None
            or call_kwargs[1].get("callback") is not None
        )

        # DB should exist
        assert (tmp_path / "store.db").exists()

    def test_backup_without_db(self, tmp_path):
        job = _make_job(with_db=False, folders=["INBOX", "Sent"])
        mock_client = _make_mock_client()
        mock_client.folder_backup.return_value = base.BackupResult()

        with patch("mailvault.jobs.session.open_mailbox") as mock_mb_cls:
            mock_mb_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
            mock_mb_cls.return_value.__exit__ = MagicMock(return_value=False)

            jobs.backup(job, tmp_path)

        assert mock_client.folder_backup.call_count == 2
        assert not (tmp_path / "store.db").exists()

    def test_backup_without_db_no_folders(self, tmp_path):
        job = _make_job(with_db=False)  # folders=None -> full_backup
        mock_client = _make_mock_client()
        mock_client.full_backup.return_value = None

        with patch("mailvault.jobs.session.open_mailbox") as mock_mb_cls:
            mock_mb_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
            mock_mb_cls.return_value.__exit__ = MagicMock(return_value=False)

            jobs.backup(job, tmp_path)

        mock_client.full_backup.assert_called_once()

    def test_backup_incremental_uses_snapshot(self, tmp_path):
        job = _make_job(with_db=True, folders=["INBOX"], incremental=True)
        mock_client = _make_mock_client()
        mock_client.folder_backup.return_value = base.BackupResult()

        # Pre-populate a snapshot
        with metadb.MetaDatabase(tmp_path / "store.db") as db:
            mb_id = db.add_mailbox("test-job")
            label_id = db.add_label("INBOX")
            snapshot_date = datetime(2026, 2, 1, tzinfo=UTC)
            db.set_snapshot(mb_id, label_id, date=snapshot_date)

        with patch("mailvault.jobs.session.open_mailbox") as mock_mb_cls:
            mock_mb_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
            mock_mb_cls.return_value.__exit__ = MagicMock(return_value=False)

            jobs.backup(job, tmp_path)

        call_kwargs = mock_client.folder_backup.call_args
        since = call_kwargs.kwargs.get("since") or call_kwargs[1].get("since")
        assert since == snapshot_date

    def test_backup_non_incremental_ignores_snapshot(self, tmp_path):
        job = _make_job(with_db=True, folders=["INBOX"], incremental=False)
        mock_client = _make_mock_client()
        mock_client.folder_backup.return_value = base.BackupResult()

        with patch("mailvault.jobs.session.open_mailbox") as mock_mb_cls:
            mock_mb_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
            mock_mb_cls.return_value.__exit__ = MagicMock(return_value=False)

            jobs.backup(job, tmp_path)

        call_kwargs = mock_client.folder_backup.call_args
        since = call_kwargs.kwargs.get("since") or call_kwargs[1].get("since")
        assert since is None

    def test_backup_db_stores_metadata(self, tmp_path):
        job = _make_job(with_db=True, folders=["INBOX"])
        mock_client = _make_mock_client()

        def fake_folder_backup(folder_name, store, since=None, callback=None):
            if callback:
                callback(
                    mailutils.MessageMetadata(
                        mailbox="test-job",
                        folder="INBOX",
                        store_id="abc123",
                        email_id="<test@example.com>",
                        date=datetime(2026, 2, 20, tzinfo=UTC),
                        subject="Test",
                        folders=["INBOX"],
                        sender={"sender@example.com"},
                        recipients={"recipient@example.com"},
                    )
                )
            return base.BackupResult(total=1, stored=1)

        mock_client.folder_backup.side_effect = fake_folder_backup

        with patch("mailvault.jobs.session.open_mailbox") as mock_mb_cls:
            mock_mb_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
            mock_mb_cls.return_value.__exit__ = MagicMock(return_value=False)

            jobs.backup(job, tmp_path)

        # Verify metadata was stored
        with metadb.MetaDatabase(tmp_path / "store.db") as db:
            row = db.execute("SELECT * FROM message WHERE store_id='abc123'").fetchone()
            assert row is not None

    def test_snapshot_advances_on_clean_run(self, tmp_path):
        job = _make_job(with_db=True, folders=["INBOX"])
        mock_client = _make_mock_client()
        mock_client.folder_backup.return_value = base.BackupResult(total=3, stored=3)

        with patch("mailvault.jobs.session.open_mailbox") as mock_mb_cls:
            mock_mb_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
            mock_mb_cls.return_value.__exit__ = MagicMock(return_value=False)

            jobs.backup(job, tmp_path)

        with metadb.MetaDatabase(tmp_path / "store.db") as db:
            mb_id = db.add_mailbox("test-job")
            label_id = db.add_label("INBOX")
            assert db.get_snapshot_date(mb_id, label_id) is not None

    def test_snapshot_frozen_on_failed_downloads(self, tmp_path):
        """A failed download must not be hidden behind an advanced snapshot."""
        job = _make_job(with_db=True, folders=["INBOX"])
        mock_client = _make_mock_client()
        mock_client.folder_backup.return_value = base.BackupResult(total=3, stored=2, failed=1)
        old_snapshot = datetime(2026, 2, 1, tzinfo=UTC)

        with metadb.MetaDatabase(tmp_path / "store.db") as db:
            db.set_snapshot(
                db.add_mailbox("test-job"), db.add_label("INBOX"), date=old_snapshot
            )

        with patch("mailvault.jobs.session.open_mailbox") as mock_mb_cls:
            mock_mb_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
            mock_mb_cls.return_value.__exit__ = MagicMock(return_value=False)

            jobs.backup(job, tmp_path)

        with metadb.MetaDatabase(tmp_path / "store.db") as db:
            mb_id = db.add_mailbox("test-job")
            label_id = db.add_label("INBOX")
            assert db.get_snapshot_date(mb_id, label_id) == old_snapshot


# ---------------------------------------------------------------------------
# snapshot state file (store.json)
# ---------------------------------------------------------------------------


class TestSnapshotStateFile:
    """The snapshot state has to survive a metadata database that does not."""

    @staticmethod
    def _run(job, mock_client, tmp_path) -> None:
        with patch("mailvault.jobs.session.open_mailbox") as mock_mb_cls:
            mock_mb_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
            mock_mb_cls.return_value.__exit__ = MagicMock(return_value=False)
            jobs.backup(job, tmp_path)

    @staticmethod
    def _since(mock_client):
        call_kwargs = mock_client.folder_backup.call_args
        return call_kwargs.kwargs.get("since") or call_kwargs[1].get("since")

    def test_state_file_written_on_clean_run(self, tmp_path):
        job = _make_job(with_db=True, folders=["INBOX"])
        mock_client = _make_mock_client()
        mock_client.folder_backup.return_value = base.BackupResult(total=3, stored=3)

        self._run(job, mock_client, tmp_path)

        s = state.SnapshotState.load(tmp_path / "store.json")
        assert s.get_date("test-job", "INBOX") is not None

    def test_state_file_frozen_on_failed_downloads(self, tmp_path):
        job = _make_job(with_db=True, folders=["INBOX"])
        mock_client = _make_mock_client()
        mock_client.folder_backup.return_value = base.BackupResult(total=3, stored=2, failed=1)

        self._run(job, mock_client, tmp_path)

        s = state.SnapshotState.load(tmp_path / "store.json")
        assert s.get_date("test-job", "INBOX") is None

    def test_state_file_takes_precedence_over_database(self, tmp_path):
        """The state file is the durable copy, so it decides where a run resumes."""
        job = _make_job(with_db=True, folders=["INBOX"], incremental=True)
        mock_client = _make_mock_client()
        mock_client.folder_backup.return_value = base.BackupResult()

        stale = datetime(2026, 1, 1, tzinfo=UTC)
        current = datetime(2026, 6, 1, tzinfo=UTC)
        with metadb.MetaDatabase(tmp_path / "store.db") as db:
            db.set_snapshot(db.add_mailbox("test-job"), db.add_label("INBOX"), date=stale)
        s = state.SnapshotState(tmp_path / "store.json")
        s.set_date("test-job", "INBOX", current)
        s.save()

        self._run(job, mock_client, tmp_path)

        assert self._since(mock_client) == current

    def test_database_snapshot_is_adopted_when_state_file_is_absent(self, tmp_path):
        """Upgrading an existing archive must not trigger a full re-fetch."""
        job = _make_job(with_db=True, folders=["INBOX"], incremental=True)
        mock_client = _make_mock_client()
        mock_client.folder_backup.return_value = base.BackupResult()

        existing = datetime(2026, 2, 1, tzinfo=UTC)
        with metadb.MetaDatabase(tmp_path / "store.db") as db:
            db.set_snapshot(db.add_mailbox("test-job"), db.add_label("INBOX"), date=existing)

        self._run(job, mock_client, tmp_path)

        assert self._since(mock_client) == existing
        adopted = state.SnapshotState.load(tmp_path / "store.json")
        assert adopted.get_date("test-job", "INBOX") is not None

    def test_all_database_snapshots_are_adopted_at_once(self, tmp_path):
        """One run must carry over every folder, not just the ones it visits."""
        job = _make_job(with_db=True, folders=["INBOX"], incremental=True)
        mock_client = _make_mock_client()
        mock_client.folder_backup.return_value = base.BackupResult()

        untouched = datetime(2026, 2, 1, tzinfo=UTC)
        with metadb.MetaDatabase(tmp_path / "store.db") as db:
            mb_id = db.add_mailbox("test-job")
            for folder in ("INBOX", "Sent", "Archiv/2016"):
                db.set_snapshot(mb_id, db.add_label(folder), date=untouched)

        self._run(job, mock_client, tmp_path)

        s = state.SnapshotState.load(tmp_path / "store.json")
        assert s.get_date("test-job", "Sent") == untouched
        assert s.get_date("test-job", "Archiv/2016") == untouched
        # The visited folder advanced, the others kept the adopted timestamp.
        assert s.get_date("test-job", "INBOX") > untouched

    def test_adoption_never_overwrites_an_existing_state_file(self, tmp_path):
        job = _make_job(with_db=True, folders=["INBOX"], incremental=True)
        mock_client = _make_mock_client()
        mock_client.folder_backup.return_value = base.BackupResult()

        current = datetime(2026, 6, 1, tzinfo=UTC)
        stale = datetime(2026, 1, 1, tzinfo=UTC)
        with metadb.MetaDatabase(tmp_path / "store.db") as db:
            db.set_snapshot(db.add_mailbox("test-job"), db.add_label("Sent"), date=stale)
        s = state.SnapshotState(tmp_path / "store.json")
        s.set_date("test-job", "Sent", current)
        s.save()

        self._run(job, mock_client, tmp_path)

        assert (
            state.SnapshotState.load(tmp_path / "store.json").get_date("test-job", "Sent")
            == current
        )

    def test_unwritable_state_file_does_not_abort_the_run(self, tmp_path, caplog):
        """Losing the state file costs bandwidth later, never the run in progress."""
        job = _make_job(with_db=True, folders=["INBOX"])
        mock_client = _make_mock_client()
        mock_client.folder_backup.return_value = base.BackupResult(total=1, stored=1)

        with patch.object(state.SnapshotState, "save", side_effect=OSError("read-only")):
            self._run(job, mock_client, tmp_path)

        assert "snapshot state not written" in caplog.text
        with metadb.MetaDatabase(tmp_path / "store.db") as db:
            mb_id = db.add_mailbox("test-job")
            assert db.get_snapshot_date(mb_id, db.add_label("INBOX")) is not None


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
                        folder=folder_name,
                        store_id=store_id,
                        email_id=f"<{store_id}@example.com>",
                        date=datetime(2026, 2, 20, tzinfo=UTC),
                        subject="Test",
                        folders=[folder_name],
                        sender={"sender@example.com"},
                        recipients={"recipient@example.com"},
                    )
                )
        return base.BackupResult(
            total=len(store_ids) + failed, stored=len(store_ids), failed=failed
        )

    return run


class TestMetadataLog:
    """The attribution the .eml files cannot carry has to reach the log."""

    @staticmethod
    def _run(job, mock_client, tmp_path) -> None:
        with patch("mailvault.jobs.session.open_mailbox") as mock_mb_cls:
            mock_mb_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
            mock_mb_cls.return_value.__exit__ = MagicMock(return_value=False)
            jobs.backup(job, tmp_path)

    def test_backup_records_mailbox_and_folder(self, tmp_path):
        job = _make_job(with_db=True, folders=["INBOX"])
        mock_client = _make_mock_client()
        mock_client.folder_backup.side_effect = _fake_folder_backup("aaa", "bbb")

        self._run(job, mock_client, tmp_path)

        logs = list(metalog.read_all(tmp_path / "meta"))
        assert len(logs) == 1
        assert logs[0].mailbox == "test-job"
        assert logs[0].folder == "INBOX"
        assert logs[0].store_ids == ["aaa", "bbb"]

    def test_unchanged_folder_writes_no_log(self, tmp_path):
        job = _make_job(with_db=True, folders=["INBOX"])
        mock_client = _make_mock_client()
        mock_client.folder_backup.return_value = base.BackupResult()

        self._run(job, mock_client, tmp_path)

        assert not metalog.has_logs(tmp_path / "meta")

    def test_failed_folder_is_logged_but_snapshot_is_not_advanced(self, tmp_path):
        """Stored messages keep their attribution; only progress is withheld."""
        job = _make_job(with_db=True, folders=["INBOX"])
        mock_client = _make_mock_client()
        mock_client.folder_backup.side_effect = _fake_folder_backup("aaa", failed=1)

        self._run(job, mock_client, tmp_path)

        logs = list(metalog.read_all(tmp_path / "meta"))
        assert len(logs) == 1
        assert logs[0].complete is False
        assert logs[0].store_ids == ["aaa"]
        assert state.SnapshotState.load(tmp_path / "store.json").is_empty()

    def test_existing_archive_is_bootstrapped_on_first_run(self, tmp_path):
        """An archive filled by an earlier version is protected straight away."""
        with metadb.MetaDatabase(tmp_path / "store.db") as db:
            mb_id = db.add_mailbox("old-job")
            msg_id = db.add_message("old", "<old@example.com>", None, "Subject")
            db.assign_message_to_mailbox(msg_id, mb_id)
            db.add_message_labels(msg_id, "Archiv/2016")

        job = _make_job(with_db=True, folders=["INBOX"])
        mock_client = _make_mock_client()
        mock_client.folder_backup.return_value = base.BackupResult()

        self._run(job, mock_client, tmp_path)

        logs = list(metalog.read_all(tmp_path / "meta"))
        assert len(logs) == 1
        assert logs[0].mailbox == "old-job"
        assert logs[0].folder == "Archiv/2016"
        assert logs[0].store_ids == ["old"]

    def test_existing_log_is_not_bootstrapped_again(self, tmp_path):
        job = _make_job(with_db=True, folders=["INBOX"])
        mock_client = _make_mock_client()
        mock_client.folder_backup.side_effect = _fake_folder_backup("aaa")
        self._run(job, mock_client, tmp_path)
        before = metalog.log_files(tmp_path / "meta")

        mock_client.folder_backup.side_effect = _fake_folder_backup()
        self._run(job, mock_client, tmp_path)

        assert metalog.log_files(tmp_path / "meta") == before


class TestBootstrapMetalog:
    def test_exports_the_attribution_of_an_existing_database(self, tmp_path):
        with metadb.MetaDatabase(tmp_path / "store.db") as db:
            msg_id = db.add_message("aaa", "<a@example.com>", None, "Subject")
            for mailbox in ("job-a", "job-b"):
                db.assign_message_to_mailbox(msg_id, db.add_mailbox(mailbox))
            db.add_message_labels(msg_id, "INBOX", "\\Sent")

        result = jobs.bootstrap_metalog(tmp_path)

        assert result.messages == 1
        assert result.written is True
        places = {(f.mailbox, f.folder) for f in metalog.read_all(tmp_path / "meta")}
        assert places == {("job-a", None), ("job-b", None)}

    def test_skips_an_archive_that_was_already_exported(self, tmp_path):
        with metadb.MetaDatabase(tmp_path / "store.db") as db:
            msg_id = db.add_message("aaa", "<a@example.com>", None, "Subject")
            db.assign_message_to_mailbox(msg_id, db.add_mailbox("job-a"))
        jobs.bootstrap_metalog(tmp_path)

        result = jobs.bootstrap_metalog(tmp_path)

        assert result.skipped is True
        assert len(metalog.log_files(tmp_path / "meta")) == 1

    def test_partial_export_is_retried_because_the_marker_is_missing(self, tmp_path):
        """Log files alone must not be read as "already done"."""
        with metadb.MetaDatabase(tmp_path / "store.db") as db:
            msg_id = db.add_message("aaa", "<a@example.com>", None, "Subject")
            db.assign_message_to_mailbox(msg_id, db.add_mailbox("job-a"))
        jobs.bootstrap_metalog(tmp_path)
        (tmp_path / "meta" / metalog.BOOTSTRAP_MARKER).unlink()

        result = jobs.bootstrap_metalog(tmp_path)

        assert result.skipped is False
        assert result.messages == 1

    def test_force_exports_again(self, tmp_path):
        with metadb.MetaDatabase(tmp_path / "store.db") as db:
            msg_id = db.add_message("aaa", "<a@example.com>", None, "Subject")
            db.assign_message_to_mailbox(msg_id, db.add_mailbox("job-a"))
        jobs.bootstrap_metalog(tmp_path)

        result = jobs.bootstrap_metalog(tmp_path, force=True)

        assert result.skipped is False
        assert len(metalog.log_files(tmp_path / "meta")) == 2

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

        result = jobs.bootstrap_metalog(tmp_path)

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

        result = jobs.bootstrap_metalog(tmp_path)

        assert result.undecidable == 1
        places = {(f.mailbox, f.folder) for f in metalog.read_all(tmp_path / "meta")}
        assert places == {("a.example.org", None), ("b.example.org", None)}

    def test_empty_database_exports_nothing(self, tmp_path):
        result = jobs.bootstrap_metalog(tmp_path)

        assert result.messages == 0
        assert result.written is False
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

        result = jobs.rebuild_metadb(tmp_path)

        # Two places -- one file each -- so the message is applied twice.
        assert result.messages == 1
        assert result.replay.files == 2
        assert result.replay.applied == 2
        assert result.replay.unknown == 0
        with metadb.MetaDatabase(tmp_path / "store.db") as db:
            msg_id = db.store_id_map()[store_id]
            assert db.message_mailboxes()[msg_id] == ["mail.example.org"]
            assert sorted(db.get_message_labels(msg_id)) == ["INBOX", "\\Sent"]

    def test_replay_restores_a_message_held_in_several_mailboxes(self, tmp_path):
        store_id = self._archive_with_log(
            tmp_path, [("mail.example.org", ["INBOX"]), ("other.example.org", ["INBOX"])]
        )

        jobs.rebuild_metadb(tmp_path)

        with metadb.MetaDatabase(tmp_path / "store.db") as db:
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

        result = jobs.rebuild_metadb(tmp_path)

        assert result.replay.unknown == 1
        assert result.replay.applied == 0
        with metadb.MetaDatabase(tmp_path / "store.db") as db:
            assert db.store_id_map() == {}

    def test_rebuild_without_a_log_reports_no_files(self, tmp_path):
        store = cas.ContentAddressedStorage(tmp_path, suffix=".eml")
        store.add(DUMMY_EML)

        result = jobs.rebuild_metadb(tmp_path)

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
    with metadb.MetaDatabase(store_path / "store.db") as db:
        mb_id = db.add_mailbox(job_name)
        writer = jobs._metadata_writer(db, mb_id)
        writer(mailutils.metadata(msg, mailbox=job_name, folder=folder, store_id=store_id))


def _verify_client(index: list[base.MessageRef], bodies: dict[str, bytes]):
    client = _make_mock_client()
    client.message_index.return_value = iter(index)
    client.fetch_message.side_effect = lambda msg_id, folder: bodies[msg_id]
    return client


class TestVerify:
    def test_reports_missing_messages(self, tmp_path):
        job = _make_job(with_db=True, folders=["INBOX"])
        archived = _eml("<a@example.com>", "Archived")
        _archive_message(tmp_path, "test-job", "INBOX", archived)

        client = _verify_client(
            [
                base.MessageRef(msg_id="id-a", message_id="<a@example.com>"),
                base.MessageRef(msg_id="id-b", message_id="<b@example.com>"),
            ],
            {},
        )
        with patch("mailvault.jobs.session.open_mailbox") as mock_mb_cls:
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
        job = _make_job(with_db=True, folders=["INBOX"])
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
        with patch("mailvault.jobs.session.open_mailbox") as mock_mb_cls:
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
        with metadb.MetaDatabase(tmp_path / "store.db") as db:
            mb_id = db.add_mailbox("test-job")
            known = db.get_known_message_ids(mb_id, db.add_label("INBOX"))
            assert known == {"a@example.com", "b@example.com"}

    def test_repair_is_idempotent(self, tmp_path):
        """A second verify run right after a repair must find nothing."""
        job = _make_job(with_db=True, folders=["INBOX"])
        lost = _eml("<b@example.com>", "Lost")
        index = [base.MessageRef(msg_id="id-b", message_id="<b@example.com>")]

        with patch("mailvault.jobs.session.open_mailbox") as mock_mb_cls:
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
        job = _make_job(with_db=True, folders=["INBOX"])
        _archive_message(tmp_path, "test-job", "INBOX", _eml("<Mixed@Example.COM>"))

        client = _verify_client(
            [base.MessageRef(msg_id="id-a", message_id="mixed@example.com")], {}
        )
        with patch("mailvault.jobs.session.open_mailbox") as mock_mb_cls:
            mock_mb_cls.return_value.__enter__ = MagicMock(return_value=client)
            mock_mb_cls.return_value.__exit__ = MagicMock(return_value=False)

            results = jobs.verify(job, tmp_path)

        assert results[0].missing == 0

    def test_download_failure_is_counted(self, tmp_path):
        job = _make_job(with_db=True, folders=["INBOX"])
        client = _make_mock_client()
        client.message_index.return_value = iter(
            [base.MessageRef(msg_id="id-b", message_id="<b@example.com>")]
        )
        client.fetch_message.side_effect = OSError("connection reset")

        with patch("mailvault.jobs.session.open_mailbox") as mock_mb_cls:
            mock_mb_cls.return_value.__enter__ = MagicMock(return_value=client)
            mock_mb_cls.return_value.__exit__ = MagicMock(return_value=False)

            results = jobs.verify(job, tmp_path, repair=True)

        assert results[0].missing == 1
        assert results[0].restored == 0
        assert results[0].failed == 1

    def test_verify_leaves_snapshot_untouched(self, tmp_path):
        job = _make_job(with_db=True, folders=["INBOX"])
        old_snapshot = datetime(2026, 2, 1, tzinfo=UTC)
        with metadb.MetaDatabase(tmp_path / "store.db") as db:
            db.set_snapshot(
                db.add_mailbox("test-job"), db.add_label("INBOX"), date=old_snapshot
            )

        client = _verify_client([], {})
        with patch("mailvault.jobs.session.open_mailbox") as mock_mb_cls:
            mock_mb_cls.return_value.__enter__ = MagicMock(return_value=client)
            mock_mb_cls.return_value.__exit__ = MagicMock(return_value=False)

            jobs.verify(job, tmp_path)

        with metadb.MetaDatabase(tmp_path / "store.db") as db:
            mb_id = db.add_mailbox("test-job")
            assert db.get_snapshot_date(mb_id, db.add_label("INBOX")) == old_snapshot

    def test_requires_db(self, tmp_path):
        job = _make_job(with_db=False, folders=["INBOX"])
        with pytest.raises(jobs.JobError, match="with_db"):
            jobs.verify(job, tmp_path)

    def test_rejects_exchange_journal(self, tmp_path):
        job = _make_job(with_db=True, exchange_journal=True, folders=["INBOX"])
        with pytest.raises(jobs.JobError, match="exchange_journal"):
            jobs.verify(job, tmp_path)

    def test_one_broken_folder_does_not_stop_the_rest(self, tmp_path):
        job = _make_job(with_db=True, folders=["Broken", "INBOX"])
        client = _make_mock_client()

        def index(folder, since=None):
            if folder == "Broken":
                raise OSError("folder vanished")
            return iter([base.MessageRef(msg_id="id-a", message_id="<a@example.com>")])

        client.message_index.side_effect = index

        with patch("mailvault.jobs.session.open_mailbox") as mock_mb_cls:
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

        with patch("mailvault.jobs.session.open_mailbox") as mock_mb_cls:
            mock_mb_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
            mock_mb_cls.return_value.__exit__ = MagicMock(return_value=False)

            jobs.folder_list(job)

        output = capsys.readouterr().out
        assert "test-job::INBOX" in output
        assert "test-job::Sent" in output
        assert "test-job::Archive" in output


# ---------------------------------------------------------------------------
# copy
# ---------------------------------------------------------------------------


class TestCopy:
    def test_copy_basic(self):
        source = _make_job(name="src", role="source", folders=["INBOX"])
        dest = _make_job(name="dst", role="destination")

        mock_src_client = _make_mock_client()
        mock_src_client.job_name = "src"
        mock_dst_client = _make_mock_client()
        mock_dst_client.job_name = "dst"

        # get_messages returns (msg_id, msg_date, msg)
        msg_date = datetime(2026, 2, 20, tzinfo=UTC)
        mock_src_client.get_messages.return_value = iter([(1, msg_date, DUMMY_EML)])

        with patch("mailvault.jobs.session.open_mailbox") as mock_mb_cls:
            clients = iter([mock_src_client, mock_dst_client])
            mock_mb_cls.return_value.__enter__ = MagicMock(side_effect=lambda: next(clients))
            mock_mb_cls.return_value.__exit__ = MagicMock(return_value=False)

            jobs.copy(source, dest)

        mock_dst_client.save_message.assert_called_once_with(DUMMY_EML, "INBOX", date=msg_date)

    def test_copy_with_archive(self):
        source = _make_job(
            name="src",
            role="source",
            folders=["INBOX"],
            move_to_archive=True,
            archive_folder="Archive/%Y/%m",
        )
        dest = _make_job(name="dst", role="destination")

        mock_src_client = _make_mock_client()
        mock_dst_client = _make_mock_client()

        msg_date = datetime(2026, 2, 20, tzinfo=UTC)
        mock_src_client.get_messages.return_value = iter([(1, msg_date, DUMMY_EML)])

        with patch("mailvault.jobs.session.open_mailbox") as mock_mb_cls:
            clients = iter([mock_src_client, mock_dst_client])
            mock_mb_cls.return_value.__enter__ = MagicMock(side_effect=lambda: next(clients))
            mock_mb_cls.return_value.__exit__ = MagicMock(return_value=False)

            jobs.copy(source, dest)

        # Should have attempted to move to archive
        mock_src_client.move_message.assert_called_once()

    def test_copy_missing_archive_folder_raises(self):
        source = _make_job(
            name="src",
            role="source",
            move_to_archive=True,
            archive_folder=None,
        )
        dest = _make_job(name="dst", role="destination")

        with pytest.raises(jobs.JobError, match="archive_folder"):
            jobs.copy(source, dest)

    def test_idle_rejects_non_imap_source(self):
        source = _make_job(name="src", role="source", backend="msgraph")
        dest = _make_job(name="dst", role="destination")

        with pytest.raises(jobs.JobError, match="idle"):
            jobs.copy(source, dest, idle=True)

    def test_copy_default_inbox(self):
        source = _make_job(name="src", role="source")  # folders=None -> default INBOX
        dest = _make_job(name="dst", role="destination")

        mock_src_client = _make_mock_client()
        mock_dst_client = _make_mock_client()
        mock_src_client.get_messages.return_value = iter([])

        with patch("mailvault.jobs.session.open_mailbox") as mock_mb_cls:
            clients = iter([mock_src_client, mock_dst_client])
            mock_mb_cls.return_value.__enter__ = MagicMock(side_effect=lambda: next(clients))
            mock_mb_cls.return_value.__exit__ = MagicMock(return_value=False)

            jobs.copy(source, dest)

        mock_src_client.get_messages.assert_called_once_with("INBOX")


# ---------------------------------------------------------------------------
# rebuild_metadb
# ---------------------------------------------------------------------------


class TestUpdateDbFromArchive:
    def test_rebuilds_db(self, tmp_path):
        # Create a CAS with a message
        store = cas.ContentAddressedStorage(tmp_path, suffix=".eml")
        store.add(DUMMY_EML)

        jobs.rebuild_metadb(tmp_path, mailbox="test")

        with metadb.MetaDatabase(tmp_path / "store.db") as db:
            rows = db.execute("SELECT * FROM message").fetchall()
            assert len(rows) == 1

    def test_rebuilds_db_without_mailbox(self, tmp_path):
        store = cas.ContentAddressedStorage(tmp_path, suffix=".eml")
        store.add(DUMMY_EML)

        jobs.rebuild_metadb(tmp_path)

        with metadb.MetaDatabase(tmp_path / "store.db") as db:
            rows = db.execute("SELECT * FROM message").fetchall()
            assert len(rows) == 1
            # No mailbox assignment
            mm_rows = db.execute("SELECT * FROM message_mailbox").fetchall()
            assert len(mm_rows) == 0


# ---------------------------------------------------------------------------
# _format_archive_folder
# ---------------------------------------------------------------------------


class TestFormatArchiveFolder:
    def test_strftime_expansion(self):
        result = jobs._format_archive_folder("Archive/%Y")
        year = datetime.now().strftime("%Y")
        assert result == f"Archive/{year}"

    def test_plain_string(self):
        result = jobs._format_archive_folder("Archive/Fixed")
        assert result == "Archive/Fixed"
