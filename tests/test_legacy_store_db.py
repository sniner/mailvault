"""Tests for `mailvault.legacy.store_db`, the reader of a pre-0.8.0 `store.db`.

Every specimen here is built by `tests.legacy_store_db`, from a frozen copy of
the schema as it was. That is the point of the split: these tests describe an
archive nobody writes any more, so nothing they rest on may move when the
projection's schema does.
"""

from __future__ import annotations

from datetime import datetime

from mailvault.legacy import store_db
from tests.legacy_store_db import legacy_store_db


def test_an_empty_database_answers_everything_with_nothing(tmp_path):
    path = tmp_path / "store.db"
    with legacy_store_db(path):
        pass

    with store_db.StoreDatabase(path) as db:
        assert list(db.iter_messages()) == []
        assert db.all_snapshots() == []
        assert db.message_mailboxes() == {}
        assert db.message_labels() == {}


def test_the_messages_come_back_with_their_store_ids(tmp_path):
    path = tmp_path / "store.db"
    with legacy_store_db(path) as old:
        old.add_message("aaa", subject="One")
        old.add_message("bbb", subject="Two")

    with store_db.StoreDatabase(path) as db:
        assert sorted(store_id for _, store_id in db.iter_messages()) == ["aaa", "bbb"]


def test_where_a_message_was_seen_is_what_the_migration_comes_for(tmp_path):
    """The one thing the old database held that exists nowhere else."""
    path = tmp_path / "store.db"
    with legacy_store_db(path) as old:
        mailbox = old.add_mailbox("example.com")
        message = old.add_message("aaa", subject="One", mailbox_id=mailbox)
        old.add_message_labels(message, "INBOX", "Archiv/2016")

    with store_db.StoreDatabase(path) as db:
        assert db.message_mailboxes() == {message: ["example.com"]}
        assert sorted(db.message_labels()[message]) == ["Archiv/2016", "INBOX"]


def test_the_snapshots_come_back_as_mailbox_folder_and_time(tmp_path):
    path = tmp_path / "store.db"
    with legacy_store_db(path) as old:
        mailbox = old.add_mailbox("SnapMB")
        old.set_snapshot(mailbox, old.add_label("INBOX"), date=datetime(2026, 1, 1))
        old.set_snapshot(mailbox, old.add_label("Archiv/2016"), date=datetime(2026, 1, 2))

    with store_db.StoreDatabase(path) as db:
        assert sorted(db.all_snapshots()) == [
            ("SnapMB", "Archiv/2016", "2026-01-02T00:00:00"),
            ("SnapMB", "INBOX", "2026-01-01T00:00:00"),
        ]


def test_a_database_without_a_snapshot_table_does_not_raise(tmp_path):
    """These files were written by versions nobody has a copy of any more.

    A missing table has to mean "nothing recorded" -- a traceback in the middle
    of a migration would stop an archive being lifted over a record of when a
    folder was last read, which is the least of what it holds.
    """
    path = tmp_path / "store.db"
    with legacy_store_db(path) as old:
        old.dbconn.execute("DROP TABLE snapshot")

    with store_db.StoreDatabase(path) as db:
        assert db.all_snapshots() == []


def test_the_reader_cannot_create_what_it_did_not_find(tmp_path):
    """Read-only by construction: no schema, so nothing to write into.

    Opening a database that is not there yields an empty file and no tables. A
    reader that quietly furnished one would turn "this archive has no `store.db`"
    into "this archive has an empty one", and the migration reads that as an
    archive with nothing in it rather than as an archive it must not touch.
    """
    path = tmp_path / "store.db"

    with store_db.StoreDatabase(path) as db:
        assert not hasattr(db, "create")
        tables = [
            row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        ]

    assert tables == []
