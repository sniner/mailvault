"""Tests for mailvault.jobs with mocked Mailbox and CAS."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from mailvault import conf, jobs, mailutils
from mailvault.backend import base
from mailvault.jobs import migration
from mailvault.jobs.db import DEFAULT_QUERY_DB_NAME, refresh_db
from mailvault.jobs.reconcile import archived_message_counts, places_from_log
from mailvault.legacy import state_json as state
from mailvault.legacy import store_db
from mailvault.store import cas, heads, index_db, marker, metalog
from tests.legacy_store_db import legacy_store_db

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


# A resume point as a backend would hand one back. Its shape is the backend's
# business and the job runner never looks inside, so these tests only ever check
# that the very same object comes back out again. The date in it is a label, not
# a mechanism -- it is what makes the assertions readable.
def _token(date: datetime) -> dict:
    return {"kind": "test-backend", "at": date.isoformat()}


ARCHIVED_TOKEN = _token(ARCHIVED_AT)


def _head(root, mailbox: str = "test-job", folder: str = "INBOX") -> heads.Head:
    """The head of one place in an archive, which a test only asks for when it is there."""
    head = heads.read(root / heads.DEFAULT_HEADS_DIR, mailbox, folder)
    assert head is not None, f"no head for {mailbox}::{folder}"
    return head


def _resume_date(root, mailbox: str = "test-job", folder: str = "INBOX") -> datetime | None:
    """Read the resume date out of a head, or None when there is no head at all.

    Not through `_head`: a folder that failed has no head, and that is one of the
    things this is asked about.
    """
    head = heads.read(root / heads.DEFAULT_HEADS_DIR, mailbox, folder)
    token = None if head is None else head.resume
    return None if token is None else datetime.fromisoformat(token["at"])


def _seed_resume(
    root,
    date: datetime,
    mailbox: str = "test-job",
    folder: str = "INBOX",
) -> None:
    """Put a folder's resume point into an archive, as a run would have."""
    heads.write(
        root / heads.DEFAULT_HEADS_DIR,
        heads.Head(
            job=mailbox,
            folder=folder,
            last_run=date.isoformat(),
            resume=_token(date),
        ),
    )


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


def _places_of(db, message_id: int) -> list[tuple[str | None, str | None]]:
    """The (mailbox, folder) pairs a projection records, straight from the tables.

    Read with SQL rather than through an accessor: the projection has no reader
    for this in production -- the log answers it during a run -- and a method
    that exists only so a test can call it is the kind of thing that outlives its
    reason. `mailvault.legacy.store_db` has one because the migration needs it.
    """
    rows = [
        (row[0], row[1])
        for row in db.execute(
            "SELECT mb.name, f.name FROM message_location loc "
            "LEFT JOIN mailbox mb USING (mailbox_id) "
            "LEFT JOIN folder f USING (folder_id) "
            "WHERE loc.message_id=?",
            (message_id,),
        ).fetchall()
    ]
    return sorted(rows, key=lambda place: (place[0] or "", place[1] or ""))


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
            base.BackupResult(total=1, stored=1, resume=ARCHIVED_TOKEN),
        ]

        with patch("mailvault.backend.session.open_mailbox") as mock_mb_cls:
            mock_mb_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
            mock_mb_cls.return_value.__exit__ = MagicMock(return_value=False)

            jobs.backup(job, tmp_path)

        assert mock_client.folder_backup.call_count == 2
        assert _resume_date(tmp_path, folder="Broken") is None
        assert _resume_date(tmp_path) is not None

    def test_backup_incremental_uses_snapshot(self, tmp_path):
        job = _make_job(folders=["INBOX"])
        mock_client = _make_mock_client()
        mock_client.folder_backup.return_value = base.BackupResult()

        snapshot_date = datetime(2026, 2, 1, tzinfo=UTC)
        _seed_resume(tmp_path, snapshot_date)

        with patch("mailvault.backend.session.open_mailbox") as mock_mb_cls:
            mock_mb_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
            mock_mb_cls.return_value.__exit__ = MagicMock(return_value=False)

            jobs.backup(job, tmp_path, incremental=True)

        assert mock_client.folder_backup.call_args.kwargs.get("resume") == _token(snapshot_date)

    def test_backup_non_incremental_ignores_snapshot(self, tmp_path):
        job = _make_job(folders=["INBOX"])
        mock_client = _make_mock_client()
        mock_client.folder_backup.return_value = base.BackupResult()

        with patch("mailvault.backend.session.open_mailbox") as mock_mb_cls:
            mock_mb_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
            mock_mb_cls.return_value.__exit__ = MagicMock(return_value=False)

            jobs.backup(job, tmp_path, incremental=False)

        assert mock_client.folder_backup.call_args.kwargs.get("resume") is None

    def test_backup_db_stores_metadata(self, tmp_path):
        job = _make_job(folders=["INBOX"])
        mock_client = _make_mock_client()

        def fake_folder_backup(folder_name, store, resume=None, callback=None):
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
            resume=ARCHIVED_TOKEN,
        )

        with patch("mailvault.backend.session.open_mailbox") as mock_mb_cls:
            mock_mb_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
            mock_mb_cls.return_value.__exit__ = MagicMock(return_value=False)

            jobs.backup(job, tmp_path)

        assert _resume_date(tmp_path) is not None

    def test_snapshot_frozen_on_failed_downloads(self, tmp_path):
        """A failed download must not be hidden behind an advanced snapshot."""
        job = _make_job(folders=["INBOX"])
        mock_client = _make_mock_client()
        mock_client.folder_backup.return_value = base.BackupResult(total=3, stored=2, failed=1)
        old_snapshot = datetime(2026, 2, 1, tzinfo=UTC)
        _seed_resume(tmp_path, old_snapshot, folder="INBOX")

        with patch("mailvault.backend.session.open_mailbox") as mock_mb_cls:
            mock_mb_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
            mock_mb_cls.return_value.__exit__ = MagicMock(return_value=False)

            jobs.backup(job, tmp_path)

        assert _resume_date(tmp_path) == old_snapshot


# ---------------------------------------------------------------------------
# where the next run resumes
# ---------------------------------------------------------------------------


class TestResumePoint:
    """What the job runner decides about the resume point, which is not much.

    The point itself is the backend's to make and to read; nothing here looks
    inside it. What the runner owns is *whether* a new one is taken at all --
    and the answer is no whenever the pass fell short of its own claim.
    """

    @staticmethod
    def _run(job, mock_client, tmp_path, incremental: bool = True) -> None:
        with patch("mailvault.backend.session.open_mailbox") as mock_mb_cls:
            mock_mb_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
            mock_mb_cls.return_value.__exit__ = MagicMock(return_value=False)
            jobs.backup(job, tmp_path, incremental=incremental)

    def test_a_source_with_nothing_to_offer_starts_no_resume_point(self, tmp_path):
        """The Proton Bridge case: no error, no mail, and no claim of coverage."""
        job = _make_job(folders=["INBOX"])
        mock_client = _make_mock_client()
        mock_client.folder_backup.return_value = base.BackupResult(total=0, stored=0)

        self._run(job, mock_client, tmp_path)

        head = _head(tmp_path)
        assert head.resume is None
        # The visit is still on record -- it just earned nothing.
        assert head.last_run_at() is not None

    def test_the_next_run_then_reads_the_folder_in_full(self, tmp_path):
        """What the withheld point is for: the mail is still reachable."""
        job = _make_job(folders=["INBOX"])
        mock_client = _make_mock_client()
        mock_client.folder_backup.return_value = base.BackupResult(total=0, stored=0)
        self._run(job, mock_client, tmp_path)

        mock_client.folder_backup.reset_mock()
        self._run(job, mock_client, tmp_path)

        assert mock_client.folder_backup.call_args.kwargs.get("resume") is None

    def test_the_backend_point_is_stored_and_handed_back_verbatim(self, tmp_path):
        """The runner is a courier, not a reader."""
        job = _make_job(folders=["INBOX"])
        mock_client = _make_mock_client()
        token = {"kind": "imap-uid", "uidvalidity": 1239278212, "uid": 48127}
        mock_client.folder_backup.return_value = base.BackupResult(
            total=1,
            stored=1,
            resume=token,
        )

        self._run(job, mock_client, tmp_path)
        assert _head(tmp_path).resume == token

        mock_client.folder_backup.reset_mock()
        self._run(job, mock_client, tmp_path)
        assert mock_client.folder_backup.call_args.kwargs.get("resume") == token

    def test_full_withholds_the_stored_point(self, tmp_path):
        """`--full` is what makes a pass authoritative: the backend gets nothing."""
        job = _make_job(folders=["INBOX"])
        mock_client = _make_mock_client()
        _seed_resume(tmp_path, datetime(2026, 7, 1, tzinfo=UTC))
        mock_client.folder_backup.return_value = base.BackupResult(
            total=1,
            stored=1,
            resume=ARCHIVED_TOKEN,
        )

        self._run(job, mock_client, tmp_path, incremental=False)

        assert mock_client.folder_backup.call_args.kwargs.get("resume") is None
        assert _resume_date(tmp_path) == ARCHIVED_AT

    def test_a_pass_that_earned_nothing_clears_nothing(self, tmp_path):
        """Nothing new is not the same as nothing there: the old point stands."""
        job = _make_job(folders=["INBOX"])
        mock_client = _make_mock_client()
        current = datetime(2026, 3, 1, tzinfo=UTC)
        _seed_resume(tmp_path, current)
        mock_client.folder_backup.return_value = base.BackupResult(total=0, stored=0)

        self._run(job, mock_client, tmp_path)

        assert _resume_date(tmp_path) == current

    def test_a_failed_pass_discards_the_point_the_backend_offered(self, tmp_path):
        """Taking it would push the failed messages out of every future pass."""
        job = _make_job(folders=["INBOX"])
        mock_client = _make_mock_client()
        current = datetime(2026, 3, 1, tzinfo=UTC)
        _seed_resume(tmp_path, current)
        mock_client.folder_backup.return_value = base.BackupResult(
            total=3,
            stored=2,
            failed=1,
            resume=ARCHIVED_TOKEN,
        )

        self._run(job, mock_client, tmp_path)

        assert _resume_date(tmp_path) == current


class TestCatchUp:
    """A folder with archived mail but no resume point is listed, not downloaded.

    The case an upgrade leaves behind: version 1 of the state file held dates,
    which are not resume points, so the first run after it has an archive full of
    mail and nothing saying how far it reaches. Downloading the mailbox again
    would be correct and absurd.
    """

    @staticmethod
    def _archive_with(tmp_path, *store_ids: str) -> None:
        """Put messages in the store and record them in the log, as a backup would."""
        store = cas.mail_store(tmp_path)
        writer = metalog.LogWriter(tmp_path / "meta", tmp_path / "heads")
        for store_id in store_ids:
            body = DUMMY_EML.replace(b"<test@example.com>", f"<{store_id}@x>".encode())
            _status, sid, _path = store.add(body)
            writer.add("test-job", ["INBOX"], sid)
        writer.seal(datetime(2026, 2, 1, tzinfo=UTC))

    @staticmethod
    def _client(on_server: list[str]):
        client = _make_mock_client()
        client.resume_point.return_value = {"kind": "test-backend", "at": "now"}
        client.message_index.return_value = iter(
            [base.MessageRef(msg_id=i, message_id=f"<{m}@x>") for i, m in enumerate(on_server)]
        )
        client.fetch_message.return_value = DUMMY_EML
        return client

    @staticmethod
    def _run(job, client, tmp_path) -> None:
        with patch("mailvault.backend.session.open_mailbox") as mock_mb_cls:
            mock_mb_cls.return_value.__enter__ = MagicMock(return_value=client)
            mock_mb_cls.return_value.__exit__ = MagicMock(return_value=False)
            jobs.backup(job, tmp_path)

    def test_an_archived_folder_without_a_point_is_reconciled(self, tmp_path, caplog):
        self._archive_with(tmp_path, "a", "b")
        client = self._client(["a", "b"])

        with caplog.at_level(logging.INFO):
            self._run(_make_job(folders=["INBOX"]), client, tmp_path)

        client.folder_backup.assert_not_called()
        client.message_index.assert_called_once()
        assert "reconciling against the archive by Message-ID" in caplog.text

    def test_only_what_is_missing_is_fetched(self, tmp_path):
        self._archive_with(tmp_path, "a")
        client = self._client(["a", "b"])

        self._run(_make_job(folders=["INBOX"]), client, tmp_path)

        assert client.fetch_message.call_count == 1

    def test_the_point_is_taken_before_the_comparison(self, tmp_path):
        """Anything arriving during it is then either found or left above the point."""
        self._archive_with(tmp_path, "a")
        client = self._client(["a"])
        order: list[str] = []
        client.resume_point.side_effect = lambda _f: order.append("point") or {"kind": "t"}
        client.message_index.side_effect = lambda _f: (order.append("list"), iter([]))[1]

        self._run(_make_job(folders=["INBOX"]), client, tmp_path)

        assert order == ["point", "list"]

    def test_a_log_that_did_not_reach_disk_holds_the_point_back(self, tmp_path, caplog):
        """Downloads clean, locations not written: advancing would lose them.

        The point claims everything below it is archived. Past messages whose
        place was never recorded, no later run asks for them again -- they lie in
        `mail/` for good with nothing saying which folder they came from. The
        ordinary pass has guarded this since 0.8.0; the catch-up threw the answer
        away.
        """
        self._archive_with(tmp_path, "a")
        client = self._client(["a", "b"])

        with patch("mailvault.jobs.reconcile._seal_log", return_value=False):
            with caplog.at_level(logging.WARNING):
                self._run(_make_job(folders=["INBOX"]), client, tmp_path)

        assert "metadata log not sealed" in caplog.text
        assert _head(tmp_path).resume is None

    def test_a_sealed_log_starts_the_point(self, tmp_path):
        """The other side of it: a clean catch-up does earn a resume point."""
        self._archive_with(tmp_path, "a")
        client = self._client(["a"])

        self._run(_make_job(folders=["INBOX"]), client, tmp_path)

        assert _head(tmp_path).resume == {
            "kind": "test-backend",
            "at": "now",
        }

    def test_an_empty_archive_is_downloaded_as_before(self, tmp_path):
        """Nothing to compare against, so listing first would only add a round trip."""
        client = self._client([])
        client.folder_backup.return_value = base.BackupResult()

        self._run(_make_job(folders=["INBOX"]), client, tmp_path)

        client.folder_backup.assert_called_once()
        client.message_index.assert_not_called()

    def test_a_job_that_deletes_after_export_is_downloaded(self, tmp_path):
        """A skipped message is never deleted, and would then never be seen again."""
        self._archive_with(tmp_path, "a")
        client = self._client(["a"])
        client.folder_backup.return_value = base.BackupResult()

        self._run(_make_job(folders=["INBOX"], delete_after_export=True), client, tmp_path)

        client.folder_backup.assert_called_once()
        client.message_index.assert_not_called()

    def test_a_journal_job_is_downloaded(self, tmp_path):
        """The archive holds the unwrapped message, whose Message-ID differs."""
        self._archive_with(tmp_path, "a")
        client = self._client(["a"])
        client.folder_backup.return_value = base.BackupResult()

        self._run(_make_job(folders=["INBOX"], exchange_journal=True), client, tmp_path)

        client.folder_backup.assert_called_once()
        client.message_index.assert_not_called()

    def test_a_version_1_state_file_leads_here(self, tmp_path, caplog):
        """The upgrade path end to end, which every existing archive walks once.

        Version 1 held a bare timestamp. It is kept as a record of the run but is
        not a resume point, so the folder has archived mail and nowhere to carry
        on from -- and that is exactly what this path is for.
        """
        self._archive_with(tmp_path, "a", "b")
        (tmp_path / "state.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "snapshots": {"test-job": {"INBOX": "2026-02-01T12:00:00+00:00"}},
                }
            ),
            encoding="utf-8",
        )
        client = self._client(["a", "b", "c"])

        with caplog.at_level(logging.INFO):
            self._run(_make_job(folders=["INBOX"]), client, tmp_path)

        client.folder_backup.assert_not_called()
        # Only the one the archive lacks, not the two it already has.
        assert client.fetch_message.call_count == 1

        assert _head(tmp_path).resume == {"kind": "test-backend", "at": "now"}

    def test_a_void_point_is_caught_up_instead_of_downloaded(self, tmp_path, caplog):
        """The backend reports; the runner picks listing over downloading again."""
        self._archive_with(tmp_path, "a")
        client = self._client(["a", "b"])
        _seed_resume(tmp_path, datetime(2026, 2, 1, tzinfo=UTC))
        client.folder_backup.return_value = base.BackupResult(resume_lost=True)

        with caplog.at_level(logging.INFO):
            self._run(_make_job(folders=["INBOX"]), client, tmp_path)

        assert "the resume point is void" in caplog.text
        client.message_index.assert_called_once()
        # Only the one the archive lacks, not the whole folder.
        assert client.fetch_message.call_count == 1

    def test_a_void_point_falls_back_to_a_full_read_when_it_must(self, tmp_path, caplog):
        """A job that deletes after export cannot be caught up, so it says so."""
        self._archive_with(tmp_path, "a")
        client = self._client(["a"])
        _seed_resume(tmp_path, datetime(2026, 2, 1, tzinfo=UTC))
        client.folder_backup.side_effect = [
            base.BackupResult(resume_lost=True),
            base.BackupResult(total=1, stored=1, resume=ARCHIVED_TOKEN),
        ]

        with caplog.at_level(logging.INFO):
            self._run(_make_job(folders=["INBOX"], delete_after_export=True), client, tmp_path)

        assert "reading the folder in full" in caplog.text
        client.message_index.assert_not_called()
        # The second call asks for the folder without a point at all.
        assert client.folder_backup.call_args.kwargs.get("resume") is None

    def test_a_failed_fetch_holds_the_point_back(self, tmp_path):
        self._archive_with(tmp_path, "a")
        client = self._client(["a", "b"])
        client.fetch_message.side_effect = OSError("connection reset")

        self._run(_make_job(folders=["INBOX"]), client, tmp_path)

        head = _head(tmp_path)
        assert head.resume is None
        assert head.last_run_at() is not None


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
    def _resume(mock_client):
        return mock_client.folder_backup.call_args.kwargs.get("resume")

    def test_state_file_written_on_clean_run(self, tmp_path):
        job = _make_job(folders=["INBOX"])
        mock_client = _make_mock_client()
        mock_client.folder_backup.return_value = base.BackupResult(
            total=3,
            stored=3,
            resume=ARCHIVED_TOKEN,
        )

        self._run(job, mock_client, tmp_path)

        assert _resume_date(tmp_path) is not None

    def test_state_file_frozen_on_failed_downloads(self, tmp_path):
        job = _make_job(folders=["INBOX"])
        mock_client = _make_mock_client()
        mock_client.folder_backup.return_value = base.BackupResult(total=3, stored=2, failed=1)

        self._run(job, mock_client, tmp_path)

        assert _resume_date(tmp_path) is None

    def test_state_file_takes_precedence_over_database(self, tmp_path):
        """The state file is the durable copy, so it decides where a run resumes."""
        job = _make_job(folders=["INBOX"])
        mock_client = _make_mock_client()
        mock_client.folder_backup.return_value = base.BackupResult()

        stale = datetime(2026, 1, 1, tzinfo=UTC)
        current = datetime(2026, 6, 1, tzinfo=UTC)
        with legacy_store_db(tmp_path / "store.db") as db:
            db.set_snapshot(db.add_mailbox("test-job"), db.add_label("INBOX"), date=stale)
        _seed_resume(tmp_path, current, folder="INBOX")

        self._run(job, mock_client, tmp_path)

        assert self._resume(mock_client) == _token(current)

    def test_a_database_snapshot_is_kept_as_a_record_not_as_a_resume_point(self, tmp_path):
        """A legacy timestamp came from the wall clock, so it says when, not how far.

        Adopting it as a resume point would inherit exactly the gap it could
        hide, so the folder is read in full once instead.
        """
        job = _make_job(folders=["INBOX"])
        mock_client = _make_mock_client()
        mock_client.folder_backup.return_value = base.BackupResult()

        existing = datetime(2026, 2, 1, tzinfo=UTC)
        with legacy_store_db(tmp_path / "store.db") as db:
            db.set_snapshot(db.add_mailbox("test-job"), db.add_label("INBOX"), date=existing)

        self._run(job, mock_client, tmp_path)

        assert self._resume(mock_client) is None
        adopted = _head(tmp_path)
        assert adopted.resume is None
        # The run has read the folder by now, so its own timestamp is the one
        # standing there -- that the adopted value survives is the business of
        # the folders a run does not visit.
        last_run = adopted.last_run_at()
        assert last_run is not None and last_run > existing

    def test_all_database_snapshots_are_adopted_at_once(self, tmp_path):
        """One run must carry over every folder, not just the ones it visits."""
        job = _make_job(folders=["INBOX"])
        mock_client = _make_mock_client()
        mock_client.folder_backup.return_value = base.BackupResult(
            total=1,
            stored=1,
            resume=ARCHIVED_TOKEN,
        )

        untouched = datetime(2026, 2, 1, tzinfo=UTC)
        with legacy_store_db(tmp_path / "store.db") as db:
            mb_id = db.add_mailbox("test-job")
            for folder in ("INBOX", "Sent", "Archiv/2016"):
                db.set_snapshot(mb_id, db.add_label(folder), date=untouched)

        self._run(job, mock_client, tmp_path)

        sent = _head(tmp_path, folder="Sent")
        archiv = _head(tmp_path, folder="Archiv/2016")
        assert sent.last_run_at() == untouched
        assert archiv.last_run_at() == untouched
        # None of them is a resume point, so none shortens the next pass.
        assert sent.resume is None
        assert archiv.resume is None
        # The visited folder was read and earned one.
        assert _resume_date(tmp_path) == ARCHIVED_AT

    def test_adoption_never_overwrites_existing_heads(self, tmp_path):
        job = _make_job(folders=["INBOX"])
        mock_client = _make_mock_client()
        mock_client.folder_backup.return_value = base.BackupResult()

        current = datetime(2026, 6, 1, tzinfo=UTC)
        stale = datetime(2026, 1, 1, tzinfo=UTC)
        with legacy_store_db(tmp_path / "store.db") as db:
            db.set_snapshot(db.add_mailbox("test-job"), db.add_label("Sent"), date=stale)
        _seed_resume(tmp_path, current, folder="Sent")

        self._run(job, mock_client, tmp_path)

        assert _resume_date(tmp_path, folder="Sent") == current

    def test_an_unwritable_head_does_not_abort_the_run(self, tmp_path, caplog):
        """Losing a resume point costs bandwidth later, never the run in progress."""
        job = _make_job(folders=["INBOX"])
        mock_client = _make_mock_client()
        mock_client.folder_backup.return_value = base.BackupResult(
            total=1,
            stored=1,
            resume=ARCHIVED_TOKEN,
        )

        with patch.object(heads, "write", side_effect=OSError("read-only")):
            self._run(job, mock_client, tmp_path)

        assert "resume point not written" in caplog.text


# ---------------------------------------------------------------------------
# metadata log (meta/*.jsonl)
# ---------------------------------------------------------------------------


def _fake_folder_backup(*store_ids: str, failed: int = 0):
    """Build a folder_backup stand-in that reports the given messages."""

    def run(folder_name, store, resume=None, callback=None):
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
            total=len(store_ids) + failed,
            stored=len(store_ids),
            failed=failed,
        )

    return run


class TestStoreMessage:
    """A message whose location was not recorded must not be reported as archived:
    a non-None return lets the caller delete it from the server, and the location
    is the one fact the archive cannot reconstruct."""

    @staticmethod
    def _store(tmp_path):
        return cas.mail_store(tmp_path)

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

        def run(folder_name, store, resume=None, callback=None):
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
                total=len(store_ids),
                stored=len(store_ids),
                deletable=list(deletable),
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
            "aaa",
            deletable=[1, 2],
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
            "aaa",
            deletable=[1, 2],
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
            "aaa",
            deletable=[1, 2],
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
        # The folder was read, so that is recorded -- but nothing was earned.
        head = _head(tmp_path)
        assert head.last_run_at() is not None
        assert head.resume is None

    def test_existing_archive_is_bootstrapped_on_first_run(self, tmp_path):
        """An archive filled by an earlier version is protected straight away."""
        with legacy_store_db(tmp_path / "store.db") as db:
            mb_id = db.add_mailbox("old-job")
            # Store ids are hashes wherever they are real, and the log skips a
            # line whose store id is not one -- so the stand-ins here are hex.
            msg_id = db.add_message("decade", "<old@example.com>", None, "Subject")
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
        assert logs[0].store_ids == ["decade"]

    def test_existing_log_is_not_bootstrapped_again(self, tmp_path):
        job = _make_job(folders=["INBOX"])
        mock_client = _make_mock_client()
        mock_client.folder_backup.side_effect = _fake_folder_backup("aaa")
        self._run(job, mock_client, tmp_path)
        before = metalog.log_files(tmp_path / "meta")

        mock_client.folder_backup.side_effect = _fake_folder_backup()
        self._run(job, mock_client, tmp_path)

        assert metalog.log_files(tmp_path / "meta") == before


class TestImportStateFile:
    """The one-shot move of `state.json` into `heads/`, which every older archive walks."""

    @staticmethod
    def _write_state(tmp_path, payload):
        path = tmp_path / state.DEFAULT_STATE_NAME
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_a_version_2_file_brings_its_resume_points_along(self, tmp_path):
        """Opaque tokens, so moving them costs nothing and saves a full pass."""
        token = {"kind": "imap-uid", "uidvalidity": 1239278212, "uid": 48127}
        path = self._write_state(
            tmp_path,
            {
                "version": 2,
                "snapshots": {
                    "test-job": {
                        "INBOX": {"last_run": "2026-02-01T12:00:00+00:00", "resume": token}
                    }
                },
            },
        )

        assert migration.import_state_file(tmp_path) == 1

        head = _head(tmp_path)
        assert head.resume == token
        assert head.last_run_at() == datetime(2026, 2, 1, 12, tzinfo=UTC)
        assert not path.exists(), "the question must not come up a second time"

    def test_a_version_1_file_brings_only_the_record_of_the_run(self, tmp_path):
        """Its timestamps came from the wall clock; as resume points they would cost mail."""
        self._write_state(
            tmp_path,
            {"version": 1, "snapshots": {"test-job": {"INBOX": "2026-02-01T12:00:00+00:00"}}},
        )

        assert migration.import_state_file(tmp_path) == 1

        head = _head(tmp_path)
        assert head.resume is None, "so the folder is read in full once"
        assert head.last_run_at() == datetime(2026, 2, 1, 12, tzinfo=UTC)

    def test_every_place_comes_over_not_just_one(self, tmp_path):
        self._write_state(
            tmp_path,
            {
                "version": 2,
                "snapshots": {
                    "a": {"INBOX": {"last_run": "2026-02-01T12:00:00+00:00"}},
                    "b": {"INBOX": {"last_run": "2026-02-01T12:00:00+00:00"}},
                    "c": {"Sent": {"last_run": "2026-02-01T12:00:00+00:00"}},
                },
            },
        )

        assert migration.import_state_file(tmp_path) == 3
        assert heads.mailboxes(tmp_path / heads.DEFAULT_HEADS_DIR) == {"a", "b", "c"}

    def test_an_archive_without_one_is_left_alone(self, tmp_path):
        assert migration.import_state_file(tmp_path) == 0
        assert not (tmp_path / heads.DEFAULT_HEADS_DIR).exists()

    def test_running_it_twice_is_harmless(self, tmp_path):
        self._write_state(
            tmp_path,
            {
                "version": 2,
                "snapshots": {"test-job": {"INBOX": {"resume": _token(ARCHIVED_AT)}}},
            },
        )
        migration.import_state_file(tmp_path)

        assert migration.import_state_file(tmp_path) == 0
        assert _resume_date(tmp_path) == ARCHIVED_AT

    def test_existing_heads_are_the_newer_truth_and_survive(self, tmp_path, caplog):
        """And the file still goes, so nothing is left to ask about again."""
        current = datetime(2026, 6, 1, tzinfo=UTC)
        _seed_resume(tmp_path, current)
        path = self._write_state(
            tmp_path,
            {
                "version": 2,
                "snapshots": {"test-job": {"INBOX": {"resume": _token(ARCHIVED_AT)}}},
            },
        )

        with caplog.at_level(logging.INFO):
            assert migration.import_state_file(tmp_path) == 0

        assert _resume_date(tmp_path) == current
        assert not path.exists()
        assert "heads are already there" in caplog.text

    def test_a_file_nobody_can_read_costs_the_resume_points_and_nothing_else(self, tmp_path):
        """Everything in it is recoverable by reading the folders in full."""
        path = self._write_state(tmp_path, {"version": 99, "snapshots": {"j": {"f": {}}}})

        assert migration.import_state_file(tmp_path) == 0
        assert not path.exists()


class TestLiftingAnOldArchive:
    """The whole migration: every older shape, in one command, mark written last."""

    @staticmethod
    def _generation_zero(tmp_path, messages=("a", "b", "c")):
        """An archive as an earlier version left it: shards in the root."""
        store = cas.ContentAddressedStorage(tmp_path, suffix=".eml")
        ids = [store.add(f"Message-Id: <{m}>\r\n\r\n{m}\r\n".encode())[1] for m in messages]
        (tmp_path / state.DEFAULT_STATE_NAME).write_text(
            json.dumps(
                {
                    "version": 2,
                    "snapshots": {"test-job": {"INBOX": {"resume": _token(ARCHIVED_AT)}}},
                }
            ),
            encoding="utf-8",
        )
        return ids

    def test_the_messages_move_under_mail(self, tmp_path):
        ids = self._generation_zero(tmp_path)

        result = jobs.migrate_archive(tmp_path)

        assert result.shards_moved == 3
        store = cas.mail_store(tmp_path)
        for store_id in ids:
            assert store.locate(store_id, exists=True) is not None
        assert not [p for p in tmp_path.iterdir() if p.is_dir() and len(p.name) == 2]

    def test_a_shard_rename_moves_no_data(self, tmp_path):
        """O(shards), not O(messages) -- at most 256 renames whatever the size."""
        store = cas.ContentAddressedStorage(tmp_path, suffix=".eml")
        for n in range(40):
            store.add(f"Message-Id: <{n}>\r\n\r\nbody {n}\r\n".encode())
        shards = len([p for p in tmp_path.iterdir() if p.is_dir()])

        assert jobs.migrate_archive(tmp_path).shards_moved == shards

    def test_a_store_split_by_an_unmigrated_run_is_merged(self, tmp_path):
        """What Stefan's accepted risk actually looks like on disk.

        A version that already writes to `mail/` ran before the migration, so
        the store is in two places. The names are content hashes, so a file in
        both is the same file.
        """
        old = cas.ContentAddressedStorage(tmp_path, suffix=".eml")
        _s, both, _p = old.add(b"Message-Id: <shared>\r\n\r\nin both\r\n")
        _s, only_old, _p = old.add(b"Message-Id: <old>\r\n\r\nonly in the root\r\n")
        new = cas.mail_store(tmp_path)
        new.add(b"Message-Id: <shared>\r\n\r\nin both\r\n")
        _s, only_new, _p = new.add(b"Message-Id: <new>\r\n\r\nonly in mail\r\n")

        jobs.migrate_archive(tmp_path)

        store = cas.mail_store(tmp_path)
        for store_id in (both, only_old, only_new):
            assert store.locate(store_id, exists=True) is not None
        assert not [p for p in tmp_path.iterdir() if p.is_dir() and len(p.name) == 2]

    def test_the_resume_points_come_along(self, tmp_path):
        self._generation_zero(tmp_path)

        result = jobs.migrate_archive(tmp_path)

        assert result.resume_points == 1
        assert _resume_date(tmp_path) == ARCHIVED_AT
        assert not (tmp_path / state.DEFAULT_STATE_NAME).exists()

    def test_the_mark_is_written_last(self, tmp_path):
        self._generation_zero(tmp_path)

        jobs.migrate_archive(tmp_path)

        assert marker.read(tmp_path) == marker.CURRENT_FORMAT

    def test_a_marked_archive_is_not_migrated_again(self, tmp_path):
        """After the one migration the cost is reading one small file."""
        self._generation_zero(tmp_path)
        jobs.migrate_archive(tmp_path)
        (tmp_path / state.DEFAULT_STATE_NAME).write_text("{}", encoding="utf-8")

        result = jobs.migrate_archive(tmp_path)

        assert result.generation == marker.CURRENT_FORMAT
        assert result.shards_moved == 0
        assert (tmp_path / state.DEFAULT_STATE_NAME).exists(), "nothing was touched"

    def test_an_archive_from_the_future_is_refused(self, tmp_path):
        """Reading it with this version would misread it, and misreading is silent."""
        marker.write(tmp_path, marker.CURRENT_FORMAT + 1)

        with pytest.raises(marker.FormatError, match="Upgrade mailvault"):
            jobs.migrate_archive(tmp_path)

    def test_the_log_is_consolidated_so_every_chain_has_a_root(self, tmp_path):
        """An archive from before the chain has no `prev` anywhere. The
        consolidation produces the first file that has one."""
        ids = self._generation_zero(tmp_path)
        writer = metalog.LogWriter(tmp_path / "meta", tmp_path / "heads")
        for store_id in ids:
            writer.add("test-job", ["INBOX"], store_id)
        writer.seal(ARCHIVED_AT)

        jobs.migrate_archive(tmp_path)

        (logfile,) = metalog.log_files(tmp_path / "meta")
        entry = metalog.read_log(logfile)
        assert entry is not None
        assert entry.prev is None
        head = _head(tmp_path)
        assert head.log == entry.hashval

    def test_an_empty_directory_becomes_a_current_archive(self, tmp_path):
        result = jobs.migrate_archive(tmp_path)

        assert result.shards_moved == 0
        assert result.resume_points == 0
        assert marker.read(tmp_path) == marker.CURRENT_FORMAT


class TestMigration:
    def test_moves_locations_out_of_an_existing_database(self, tmp_path):
        with legacy_store_db(tmp_path / "store.db") as db:
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
        with legacy_store_db(tmp_path / "store.db") as db:
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
        with legacy_store_db(tmp_path / "store.db") as db:
            msg_id = db.add_message("aaa", "<a@example.com>", None, "Subject")
            db.assign_message_to_mailbox(msg_id, db.add_mailbox("job-a"))
        jobs.migrate_archive(tmp_path)

        result = jobs.migrate_archive(tmp_path)

        assert result.needed is False
        assert len(metalog.log_files(tmp_path / "meta")) == 1

    def test_it_announces_the_migration_before_doing_it(self, tmp_path, caplog):
        """The slow work must not look like a hang: say it is happening first."""
        with legacy_store_db(tmp_path / "store.db") as db:
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
        """An interrupt leaves no mark, and no mark means the work is picked up.

        The mark is written last for exactly this: everything before it is
        idempotent, so repeating costs nothing, and a mark written first would
        claim a layout that only half exists.
        """
        with legacy_store_db(tmp_path / "store.db") as db:
            msg_id = db.add_message("aaa", "<a@example.com>", None, "Subject")
            db.assign_message_to_mailbox(msg_id, db.add_mailbox("job-a"))
        jobs.migrate_archive(tmp_path)
        # What an interrupt before the last step leaves behind.
        (tmp_path / marker.FORMAT_NAME).unlink()
        (tmp_path / "store.db.migrated").rename(tmp_path / "store.db")

        result = jobs.migrate_archive(tmp_path)

        assert result.needed is True
        assert result.verified is True
        # The repeated export writes a second file -- its header carries a later
        # date -- and the consolidation at the end of the migration folds it back
        # into one. Nothing is duplicated and nothing is lost.
        assert len(metalog.log_files(tmp_path / "meta")) == 1
        places = {(f.mailbox, f.folder) for f in metalog.read_all(tmp_path / "meta")}
        assert places == {("job-a", None)}
        assert marker.read(tmp_path) == marker.CURRENT_FORMAT

    def test_folder_is_placed_by_elimination_when_nothing_witnesses_it(self, tmp_path):
        """A folder only ever seen on messages in two mailboxes has no witness.

        It becomes decidable anyway: one message names a folder that belongs to
        the other mailbox, which leaves exactly one mailbox unexplained, and the
        pairing learnt there settles every remaining message.
        """
        with legacy_store_db(tmp_path / "store.db") as db:
            gmail = db.add_mailbox("mail.example.org")
            imapbox = db.add_mailbox("other.example.org")
            db.set_snapshot(imapbox, db.add_label("Archiv/Chat"), date=datetime.now(UTC))
            for n, extra in enumerate(([], ["\\Important"])):
                msg = db.add_message(f"beef{n}", f"<m{n}@example.com>", None, "Subject")
                db.assign_message_to_mailbox(msg, gmail)
                db.assign_message_to_mailbox(msg, imapbox)
                db.add_message_labels(msg, "Archiv/Chat", "Chat", *extra)
            # Witness that '\Important' can only be the Gmail-style mailbox's.
            solo = db.add_message("aced", "<solo@example.com>", None, "Subject")
            db.assign_message_to_mailbox(solo, gmail)
            db.add_message_labels(solo, "\\Important")

        result = jobs.migrate_archive(tmp_path)

        assert result.undecidable == 0
        places = {
            (f.mailbox, f.folder): set(f.store_ids) for f in metalog.read_all(tmp_path / "meta")
        }
        assert places[("mail.example.org", "Chat")] == {"beef0", "beef1"}
        assert places[("other.example.org", "Archiv/Chat")] == {"beef0", "beef1"}

    def test_undecidable_folder_is_left_out_rather_than_guessed(self, tmp_path):
        """Two mailboxes with the same folder name and no way to tell them apart."""
        with legacy_store_db(tmp_path / "store.db") as db:
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
        store = cas.mail_store(tmp_path)
        _status, store_id, _path = store.add(DUMMY_EML)
        writer = metalog.LogWriter(tmp_path / "meta", tmp_path / "heads")
        for mailbox, folders in places:
            writer.add(mailbox, folders, store_id)
        writer.seal(datetime(2026, 8, 1, tzinfo=UTC))
        return store_id

    def test_replay_restores_the_places_a_message_was_seen_in(self, tmp_path):
        store_id = self._archive_with_log(tmp_path, [("mail.example.org", ["INBOX", "\\Sent"])])

        result = jobs.create_db(tmp_path, tmp_path / "out.db")

        # Two places -- one file each -- so the message is applied twice.
        assert result.messages == 1
        assert result.replay.files == 2
        assert result.replay.applied == 2
        assert result.replay.unknown == 0
        with index_db.IndexDatabase(tmp_path / "out.db") as db:
            msg_id = db.store_id_map()[store_id]
            assert _places_of(db, msg_id) == [
                ("mail.example.org", "INBOX"),
                ("mail.example.org", "\\Sent"),
            ]

    def test_replay_keeps_which_folder_of_which_mailbox(self, tmp_path):
        """The pairing the log holds and the database used to take apart again.

        Two mailboxes, one folder name each, and the same message in both. Split
        across `message_mailbox` and `message_label` this came out as two
        mailboxes and one folder, from which no query can tell whether the
        message was in `INBOX` of one, of the other, or of both.
        """
        store_id = self._archive_with_log(
            tmp_path,
            [("mail.example.org", ["INBOX"]), ("other.example.org", ["Archiv"])],
        )

        jobs.create_db(tmp_path, tmp_path / "out.db")

        with index_db.IndexDatabase(tmp_path / "out.db") as db:
            msg_id = db.store_id_map()[store_id]
            assert _places_of(db, msg_id) == [
                ("mail.example.org", "INBOX"),
                ("other.example.org", "Archiv"),
            ]

    def test_log_entries_for_absent_messages_are_counted_not_invented(self, tmp_path):
        """A blob removed from the archive must not reappear as a database row."""
        writer = metalog.LogWriter(tmp_path / "meta", tmp_path / "heads")
        writer.add("job", ["INBOX"], "deadbeef")
        writer.seal(datetime(2026, 8, 1, tzinfo=UTC))

        result = jobs.create_db(tmp_path, tmp_path / "out.db")

        assert result.replay.unknown == 1
        assert result.replay.applied == 0
        with index_db.IndexDatabase(tmp_path / "out.db") as db:
            assert db.store_id_map() == {}

    def test_an_existing_database_is_refused(self, tmp_path):
        """ "create" creates; filling an existing file would make it an accumulation."""
        store = cas.mail_store(tmp_path)
        store.add(DUMMY_EML)
        target = tmp_path / "out.db"
        jobs.create_db(tmp_path, target)

        with pytest.raises(jobs.JobError, match="already exists"):
            jobs.create_db(tmp_path, target)

    def test_force_replaces_rather_than_adds(self, tmp_path):
        store = cas.mail_store(tmp_path)
        store.add(DUMMY_EML)
        target = tmp_path / "out.db"
        jobs.create_db(tmp_path, target)
        with index_db.IndexDatabase(target) as db:
            db.add_message("stale", "<stale@example.com>", None, "Gone from the archive")

        jobs.create_db(tmp_path, target, force=True)

        with index_db.IndexDatabase(target) as db:
            assert "stale" not in db.store_id_map()

    def test_an_interrupted_build_leaves_the_previous_database_alone(self, tmp_path):
        store = cas.mail_store(tmp_path)
        store.add(DUMMY_EML)
        target = tmp_path / "out.db"
        jobs.create_db(tmp_path, target)
        before = target.read_bytes()

        with patch("mailvault.jobs.db._replay_metalog", side_effect=KeyboardInterrupt):
            with pytest.raises(KeyboardInterrupt):
                jobs.create_db(tmp_path, target, force=True)

        assert target.read_bytes() == before
        assert not (tmp_path / "out.db._tmp_").exists()

    def test_rebuild_without_a_log_reports_no_files(self, tmp_path):
        store = cas.mail_store(tmp_path)
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
    store = cas.mail_store(store_path)
    _status, store_id, _path = store.add(msg)
    writer = metalog.LogWriter(
        store_path / metalog.DEFAULT_LOG_DIR, store_path / heads.DEFAULT_HEADS_DIR
    )
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

        store = cas.mail_store(tmp_path)
        assert len(list(store.walk())) == 2
        # The restored message reached the log too, not just the archive.
        places = places_from_log(tmp_path / metalog.DEFAULT_LOG_DIR)
        known = archived_message_counts(store, places[("test-job", "INBOX")])
        assert known == {"a@example.com": 1, "b@example.com": 1}

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

    def test_a_byte_identical_duplicate_is_not_a_missing_message(self, tmp_path):
        """The finding from `archive-ruhl`: 1,729 reported missing, none missing.

        A server folder may hold the same message twice, byte for byte. The
        archive is addressed by content and holds it once -- which is its job,
        not a gap -- so the second copy has nothing left to claim. Counted as
        missing it stood in the report of a complete archive after every run,
        for good, and `--repair` fetched it every time to no effect.
        """
        job = _make_job(folders=["INBOX"])
        _archive_message(tmp_path, "test-job", "INBOX", _eml("<a@example.com>", "Twice"))

        client = _verify_client(
            [
                base.MessageRef(msg_id="id-a1", message_id="<a@example.com>"),
                base.MessageRef(msg_id="id-a2", message_id="<a@example.com>"),
            ],
            {},
        )
        with patch("mailvault.backend.session.open_mailbox") as mock_mb_cls:
            mock_mb_cls.return_value.__enter__ = MagicMock(return_value=client)
            mock_mb_cls.return_value.__exit__ = MagicMock(return_value=False)

            results = jobs.verify(job, tmp_path)

        assert results[0].on_server == 2
        assert results[0].missing == 0
        assert results[0].extra_copies == 1

    def test_an_extra_copy_is_still_fetched_and_reported_for_what_it_was(self, tmp_path):
        """Fetched because only its bytes can say which of the two kinds it is.

        A second copy is usually a duplicate the store cannot hold twice, and
        occasionally the byte-different version that really is absent. Nothing
        short of downloading it tells them apart, so it is downloaded -- and
        counted under what it turned out to be, never under "missing".
        """
        job = _make_job(folders=["INBOX"])
        _archive_message(tmp_path, "test-job", "INBOX", _eml("<a@example.com>", "First"))
        other_version = _eml("<a@example.com>", "Second, and not the same bytes")

        client = _verify_client(
            [
                base.MessageRef(msg_id="id-a1", message_id="<a@example.com>"),
                base.MessageRef(msg_id="id-a2", message_id="<a@example.com>"),
            ],
            {"id-a2": other_version},
        )
        with patch("mailvault.backend.session.open_mailbox") as mock_mb_cls:
            mock_mb_cls.return_value.__enter__ = MagicMock(return_value=client)
            mock_mb_cls.return_value.__exit__ = MagicMock(return_value=False)

            results = jobs.verify(job, tmp_path, repair=True)

        assert results[0].missing == 0
        assert results[0].extra_copies == 1
        # Not "restored": nothing was missing. It differed, so it was kept.
        assert results[0].restored == 0
        assert results[0].recovered_copies == 1
        assert len(list(cas.mail_store(tmp_path).walk())) == 2

    def test_a_duplicate_that_really_is_one_leaves_the_archive_alone(self, tmp_path):
        """The other outcome of the same fetch, and the common one."""
        job = _make_job(folders=["INBOX"])
        same = _eml("<a@example.com>", "Twice")
        _archive_message(tmp_path, "test-job", "INBOX", same)

        client = _verify_client(
            [
                base.MessageRef(msg_id="id-a1", message_id="<a@example.com>"),
                base.MessageRef(msg_id="id-a2", message_id="<a@example.com>"),
            ],
            {"id-a2": same},
        )
        with patch("mailvault.backend.session.open_mailbox") as mock_mb_cls:
            mock_mb_cls.return_value.__enter__ = MagicMock(return_value=client)
            mock_mb_cls.return_value.__exit__ = MagicMock(return_value=False)

            results = jobs.verify(job, tmp_path, repair=True)

        assert results[0].extra_copies == 1
        assert results[0].recovered_copies == 0
        assert results[0].restored == 0
        assert len(list(cas.mail_store(tmp_path).walk())) == 1

    def test_message_id_matching_ignores_brackets_and_case(self, tmp_path):
        job = _make_job(folders=["INBOX"])
        _archive_message(tmp_path, "test-job", "INBOX", _eml("<Mixed@Example.COM>"))

        client = _verify_client(
            [base.MessageRef(msg_id="id-a", message_id="mixed@example.com")],
            {},
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
        with legacy_store_db(tmp_path / "store.db") as db:
            db.set_snapshot(
                db.add_mailbox("test-job"),
                db.add_label("INBOX"),
                date=old_snapshot,
            )

        client = _verify_client([], {})
        with patch("mailvault.backend.session.open_mailbox") as mock_mb_cls:
            mock_mb_cls.return_value.__enter__ = MagicMock(return_value=client)
            mock_mb_cls.return_value.__exit__ = MagicMock(return_value=False)

            jobs.verify(job, tmp_path)

        with store_db.StoreDatabase(tmp_path / "store.db") as db:
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
        store = cas.mail_store(tmp_path)
        store.add(DUMMY_EML)

        jobs.create_db(tmp_path, tmp_path / "out.db", mailbox="test")

        with index_db.IndexDatabase(tmp_path / "out.db") as db:
            rows = db.execute("SELECT * FROM message").fetchall()
            assert len(rows) == 1

    def test_rebuilds_db_without_mailbox(self, tmp_path):
        store = cas.mail_store(tmp_path)
        store.add(DUMMY_EML)

        jobs.create_db(tmp_path, tmp_path / "out.db")

        with index_db.IndexDatabase(tmp_path / "out.db") as db:
            rows = db.execute("SELECT * FROM message").fetchall()
            assert len(rows) == 1
            # The message is there and no place is claimed for it -- which is
            # the truth, and is what an archive built by `archive import` looks
            # like all the way through.
            places = db.execute("SELECT * FROM message_location").fetchall()
            assert len(places) == 0


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

    def test_a_projection_from_an_earlier_version_is_built_again(self, tmp_path):
        """The trap the shape marker exists to close.

        The tables are created with IF NOT EXISTS, so an older projection would
        quietly gain the new ones and keep the old -- and `applied_log`, which
        survives untouched, reports every log file as already folded in. The new
        tables would then stay empty for good, on a database that answers every
        query without complaint.
        """
        _archive_message(tmp_path, "job", "INBOX", _eml("<a@example.com>"))
        db_path = tmp_path / DEFAULT_QUERY_DB_NAME
        refresh_db(tmp_path, db_path)
        with index_db.IndexDatabase(db_path) as db:
            db.execute("PRAGMA user_version = 0")
            db.commit()
            db.execute("DELETE FROM message_location")
            db.commit()

        result = refresh_db(tmp_path, db_path)

        assert result.rebuilt is True
        with index_db.IndexDatabase(db_path) as db:
            assert db.schema_version() == index_db.SCHEMA_VERSION
            (msg_id,) = db.store_id_map().values()
            assert _places_of(db, msg_id) == [("job", "INBOX")]

    def test_only_new_logs_are_applied_on_a_refresh(self, tmp_path):
        _archive_message(tmp_path, "job", "INBOX", _eml("<a@example.com>"))
        db_path = tmp_path / DEFAULT_QUERY_DB_NAME
        refresh_db(tmp_path, db_path)  # initial full build

        _archive_message(tmp_path, "job", "Archive", _eml("<b@example.com>", "Second"))
        result = refresh_db(tmp_path, db_path)

        assert result.rebuilt is False
        assert result.files == 1  # only the new log file was read
        assert result.messages == 1  # only the new message was inserted
        with index_db.IndexDatabase(db_path) as db:
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
        with index_db.IndexDatabase(db_path) as db:
            assert len(db.store_id_map()) == 1

    def test_a_full_build_says_why_before_it_starts(self, tmp_path, caplog):
        """Reading every message is worth minutes; nobody should have to guess why."""
        _archive_message(tmp_path, "job", "INBOX", _eml("<a@example.com>"))
        db_path = tmp_path / DEFAULT_QUERY_DB_NAME

        with caplog.at_level(logging.INFO):
            refresh_db(tmp_path, db_path)

        assert "no query database yet" in caplog.text
        assert "building the query database" in caplog.text
        # Named as it reads inside the archive, not by the whole path.
        assert str(tmp_path) not in caplog.text


def _storing_backup(eml: bytes):
    """A folder_backup stand-in that stores an eml and logs it, like a real run."""

    def run(folder_name, store, resume=None, callback=None):
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
        with index_db.IndexDatabase(db_path) as db:
            assert len(db.store_id_map()) == 1

    def test_backup_writes_no_projection_by_default(self, tmp_path):
        mock_client = _make_mock_client()
        mock_client.folder_backup.side_effect = _storing_backup(_eml("<a@example.com>"))

        self._run(mock_client, tmp_path, index_db=False)

        assert not (tmp_path / DEFAULT_QUERY_DB_NAME).exists()
