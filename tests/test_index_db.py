import logging
from datetime import UTC, datetime

import pytest

from mailvault.store import index_db, sqlite


def _places(db, message_id) -> list[tuple[str | None, str | None]]:
    """The (mailbox, folder) pairs recorded for a message, straight from the tables."""
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
    # Sorted on a key that survives the NULLs, which are the interesting rows.
    return sorted(rows, key=lambda place: (place[0] or "", place[1] or ""))


def test_index_db_setup(tmp_path):
    db_path = tmp_path / "test.db"
    with index_db.IndexDatabase(db_path, create=True) as db:
        res = db.dbconn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        tables = [r[0] for r in res]
        assert "mailbox" in tables
        assert "message" in tables
        assert "folder" in tables
        assert "address" in tables
        assert "subject" in tables
        assert "message_location" in tables
        assert "message_sender" in tables
        assert "message_recipient" in tables
        # What a projection is not: the resume timestamps of an archive that
        # kept its truth in SQLite. Nothing has written this since 0.8.0, and
        # creating it in every rebuild made a dead table look like a live one.
        assert "snapshot" not in tables
        # And the two relations a place used to be split across.
        assert "message_mailbox" not in tables
        assert "message_label" not in tables
        assert "label" not in tables


def test_index_db_setup_views(tmp_path):
    db_path = tmp_path / "test.db"
    with index_db.IndexDatabase(db_path, create=True) as db:
        res = db.dbconn.execute("SELECT name FROM sqlite_master WHERE type='view'").fetchall()
        views = [r[0] for r in res]
        assert "v_messages" in views
        assert "v_duplicates" in views


def test_index_db_setup_indexes(tmp_path):
    """Verify that message_recipient indexes are on the correct table (B1 fix)."""
    db_path = tmp_path / "test.db"
    with index_db.IndexDatabase(db_path, create=True) as db:
        indexes = db.dbconn.execute(
            "SELECT name, tbl_name FROM sqlite_master "
            "WHERE type='index' AND name LIKE 'idx_message_recipient%'"
        ).fetchall()
        for name, tbl_name in indexes:
            assert tbl_name == "message_recipient", (
                f"Index {name} is on table {tbl_name}, expected message_recipient"
            )


def test_index_db_add_mailbox(tmp_path):
    db_path = tmp_path / "test.db"
    with index_db.IndexDatabase(db_path, create=True) as db:
        mb_id = db.add_mailbox("INBOX")
        assert mb_id > 0
        # Adding same mailbox should return same id
        mb_id_2 = db.add_mailbox("INBOX")
        assert mb_id == mb_id_2


def test_index_db_a_place_goes_in_as_one_row(tmp_path):
    db_path = tmp_path / "test.db"
    with index_db.IndexDatabase(db_path, create=True) as db:
        msg_id = db.add_message(
            store_id="hash123",
            email_id="<message-id@example.com>",
            date=datetime.now(UTC),
            subject="Test Subject",
        )
        assert msg_id > 0

        db.add_message_location(msg_id, "gmail.com", "INBOX")
        db.add_message_location(msg_id, "gmail.com", "\\Sent")

        assert _places(db, msg_id) == [("gmail.com", "INBOX"), ("gmail.com", "\\Sent")]


def test_index_db_add_message_with_a_place(tmp_path):
    db_path = tmp_path / "test.db"
    with index_db.IndexDatabase(db_path, create=True) as db:
        msg_id = db.add_message(
            store_id="hash_mb",
            email_id="<mb@example.com>",
            date=datetime.now(UTC),
            subject="With Mailbox",
            mailbox="TestMailbox",
            folder="INBOX",
        )

        assert _places(db, msg_id) == [("TestMailbox", "INBOX")]


def test_index_db_the_same_place_twice_is_one_row(tmp_path):
    db_path = tmp_path / "test.db"
    with index_db.IndexDatabase(db_path, create=True) as db:
        msg_id = db.add_message(
            store_id="hash_assign",
            email_id="<assign@example.com>",
            date=datetime.now(UTC),
            subject="Assign",
        )
        db.add_message_location(msg_id, "Box1", "INBOX")
        db.add_message_location(msg_id, "Box1", "INBOX")

        assert _places(db, msg_id) == [("Box1", "INBOX")]


def test_index_db_an_unknown_half_of_a_place_is_null_not_invented(tmp_path):
    """Both halves may be missing, and neither may be filled in.

    A mailbox with no folder is an archive whose history did not record one; a
    folder with no mailbox is what an import writes. Guessing either is the one
    thing this must not do.
    """
    db_path = tmp_path / "test.db"
    with index_db.IndexDatabase(db_path, create=True) as db:
        no_folder = db.add_message("h1", "<a@example.com>", None, "A")
        db.add_message_location(no_folder, "example.com", None)
        no_mailbox = db.add_message("h2", "<b@example.com>", None, "B")
        db.add_message_location(no_mailbox, None, "docuware-2019")

        assert _places(db, no_folder) == [("example.com", None)]
        assert _places(db, no_mailbox) == [(None, "docuware-2019")]


def test_index_db_a_repeated_place_with_an_unknown_half_is_still_one_row(tmp_path):
    """SQLite holds every NULL distinct, so plain UNIQUE would not catch this.

    The log is replayed on every refresh. Without the IFNULL index that is one
    new row per run for every message whose folder was never recorded -- which
    on an archive migrated from an old format is most of them.
    """
    db_path = tmp_path / "test.db"
    with index_db.IndexDatabase(db_path, create=True) as db:
        msg_id = db.add_message("h1", "<a@example.com>", None, "A")
        for _ in range(3):
            db.add_message_location(msg_id, "example.com", None)
            db.add_message_location(msg_id, None, "docuware-2019")

        assert _places(db, msg_id) == [(None, "docuware-2019"), ("example.com", None)]


def test_index_db_sender_and_recipients(tmp_path):
    db_path = tmp_path / "test.db"
    with index_db.IndexDatabase(db_path, create=True) as db:
        msg_id = db.add_message(
            store_id="hash_addr",
            email_id="<addr@example.com>",
            date=datetime.now(UTC),
            subject="Addresses",
        )
        db.add_message_sender(msg_id, "alice@example.com", "bob@example.com")
        db.add_message_recipients(msg_id, "carol@example.com", "dave@example.com")

        senders = db.execute(
            "SELECT a.address FROM message_sender ms JOIN address a USING (address_id) "
            "WHERE ms.message_id=?",
            (msg_id,),
        ).fetchall()
        assert set(r[0] for r in senders) == {"alice@example.com", "bob@example.com"}

        recipients = db.execute(
            "SELECT a.address FROM message_recipient mr JOIN address a USING (address_id) "
            "WHERE mr.message_id=?",
            (msg_id,),
        ).fetchall()
        assert set(r[0] for r in recipients) == {"carol@example.com", "dave@example.com"}


def test_index_db_labels_are_committed_without_a_following_write(tmp_path):
    """Places added last must survive the connection closing."""
    db_path = tmp_path / "test.db"
    with index_db.IndexDatabase(db_path, create=True) as db:
        msg_id = db.add_message("aaa", "<a@example.com>", None, "Subject")
        db.add_message_location(msg_id, "gmail.com", "INBOX")
        db.add_message_location(msg_id, "gmail.com", "Archiv/2016")

    with index_db.IndexDatabase(db_path) as db:
        msg_id = db.store_id_map()["aaa"]
        assert _places(db, msg_id) == [
            ("gmail.com", "Archiv/2016"),
            ("gmail.com", "INBOX"),
        ]


def test_index_db_transaction_rollback(tmp_path):
    db_path = tmp_path / "test.db"
    with index_db.IndexDatabase(db_path, create=True) as db:
        db.add_mailbox("BeforeRollback")

        with pytest.raises(sqlite.RollbackException):
            with db.transaction():
                db.execute("INSERT OR IGNORE INTO mailbox(name) VALUES (?)", ("RolledBack",))
                db.rollback()

        # RolledBack should not exist
        row = db.execute("SELECT name FROM mailbox WHERE name='RolledBack'").fetchone()
        assert row is None

        # BeforeRollback should still exist
        row = db.execute("SELECT name FROM mailbox WHERE name='BeforeRollback'").fetchone()
        assert row is not None


def test_a_deliberate_rollback_reports_nothing(tmp_path, caplog):
    # It used to log ERROR once per nesting level, each line reading
    # "Transaction failed:" and then nothing -- the exception carries no
    # message, and the operation is not a failure to begin with.
    db_path = tmp_path / "test.db"
    with index_db.IndexDatabase(db_path, create=True) as db, caplog.at_level(logging.DEBUG):
        with pytest.raises(sqlite.RollbackException):
            with db.transaction(), db.transaction(), db.transaction():
                db.execute("INSERT OR IGNORE INTO mailbox(name) VALUES (?)", ("RolledBack",))
                db.rollback()

        assert caplog.text == ""
        assert db.execute("SELECT name FROM mailbox WHERE name='RolledBack'").fetchone() is None


def test_a_rollback_takes_the_interned_ids_with_it(tmp_path):
    # The id is cached as soon as the row was inserted, and the insert can sit
    # inside a larger block that is undone afterwards. Keeping it would hand out
    # a foreign key to a row that is not there.
    db_path = tmp_path / "test.db"
    with index_db.IndexDatabase(db_path, create=True) as db:
        with pytest.raises(sqlite.RollbackException):
            with db.transaction():
                db.add_mailbox("RolledBack")
                db.rollback()

        assert db.execute("SELECT name FROM mailbox WHERE name='RolledBack'").fetchone() is None
        # Asked again: the answer has to come from the table, not from what the
        # cache remembers about a row that was undone.
        again = db.add_mailbox("RolledBack")
        row = db.execute("SELECT mailbox_id FROM mailbox WHERE name='RolledBack'").fetchone()
        assert row is not None and row[0] == again


def test_a_real_failure_is_reported_once_and_the_stack_waits_for_verbose(tmp_path, caplog):
    db_path = tmp_path / "test.db"
    with index_db.IndexDatabase(db_path, create=True) as db, caplog.at_level(logging.DEBUG):
        with pytest.raises(ValueError):
            with db.transaction(), db.transaction():
                raise ValueError("something nobody diagnosed")

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "something nobody diagnosed" in errors[0].getMessage()
    assert errors[0].exc_info is None
    debug = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert any(r.exc_info for r in debug), "the stack is there for -v"


def test_index_db_v_messages_view(tmp_path):
    db_path = tmp_path / "test.db"
    with index_db.IndexDatabase(db_path, create=True) as db:
        msg_id = db.add_message(
            store_id="hash_view",
            email_id="<view@example.com>",
            date=datetime(2026, 3, 27, tzinfo=UTC),
            subject="View Subject",
            mailbox="ViewTest",
            folder="INBOX",
        )
        db.add_message_sender(msg_id, "sender@example.com")
        db.add_message_recipients(msg_id, "rcpt@example.com")

        rows = db.execute("SELECT * FROM v_messages WHERE store_id='hash_view'").fetchall()
        assert len(rows) >= 1
        row = dict(rows[0])
        assert row["sender"] == "sender@example.com"
        assert row["recipient"] == "rcpt@example.com"
        assert row["mailbox"] == "ViewTest"
        assert row["folder"] == "INBOX"
        assert row["subject"] == "View Subject"


def test_index_db_v_messages_holds_every_message_the_archive_holds(tmp_path):
    """The view used to inner-join sender, recipient and subject.

    A message the view could not complete simply was not in it -- and a message
    with no readable recipient is not a rarity in an archive going back to the
    nineties. `SELECT count(*) FROM v_messages` then answered a question nobody
    asked, and looked like an answer to the one they did.
    """
    db_path = tmp_path / "test.db"
    with index_db.IndexDatabase(db_path, create=True) as db:
        complete = db.add_message("h1", "<a@example.com>", None, "A")
        db.add_message_sender(complete, "from@example.com")
        db.add_message_recipients(complete, "to@example.com")
        db.add_message_location(complete, "gmail.com", "INBOX")
        # `To: Undisclosed recipients:;` -- legal RFC 5322, and no address.
        db.add_message("h2", "<b@example.com>", None, "B")
        # Nothing at all: no sender, no recipient, no subject, no place.
        db.add_message("h3", "", None, "")

        stored = db.execute("SELECT count(*) FROM message").fetchone()[0]
        in_view = db.execute("SELECT count(DISTINCT store_id) FROM v_messages").fetchone()[0]

        assert stored == 3
        assert in_view == stored


def test_index_db_v_duplicates_view(tmp_path):
    db_path = tmp_path / "test.db"
    with index_db.IndexDatabase(db_path, create=True) as db:
        date = datetime(2026, 3, 27, tzinfo=UTC)
        # Two messages with same email_id and date but different store_id = duplicates
        db.add_message(
            store_id="hash_dup_1",
            email_id="<dup@example.com>",
            date=date,
            subject="Duplicate",
        )
        db.add_message(
            store_id="hash_dup_2",
            email_id="<dup@example.com>",
            date=date,
            subject="Duplicate",
        )

        rows = db.execute("SELECT * FROM v_duplicates").fetchall()
        store_ids = {dict(r)["store_id"] for r in rows}
        assert "hash_dup_1" in store_ids
        assert "hash_dup_2" in store_ids


def test_index_db_add_message_idempotent(tmp_path):
    """Adding same store_id twice should not create duplicate (ON CONFLICT IGNORE)."""
    db_path = tmp_path / "test.db"
    with index_db.IndexDatabase(db_path, create=True) as db:
        date = datetime(2026, 1, 1, tzinfo=UTC)
        id1 = db.add_message(store_id="same_hash", email_id="<a@b>", date=date, subject="First")
        id2 = db.add_message(store_id="same_hash", email_id="<a@b>", date=date, subject="First")
        assert id1 == id2


def test_a_projection_that_lost_an_object_is_recognised(tmp_path):
    """`missing` names it, and `usable` is what a reader is turned away by."""
    db_path = tmp_path / "test.db"
    with index_db.IndexDatabase(db_path, create=True) as db:
        assert db.usable
        assert db.missing() == []

    with index_db.IndexDatabase(db_path) as db:
        db.execute("DROP INDEX idx_message_2")

    with index_db.IndexDatabase(db_path) as db:
        assert [str(obj) for obj in db.missing()] == ["index idx_message_2"]
        assert not db.usable


def test_an_object_of_the_right_name_and_the_wrong_shape_is_missing(tmp_path):
    """The name says an object is there; only the statement says it is the one.

    Earlier releases really did write a `v_messages` with INNER JOINs and an
    `idx_message_location_1` without the `IFNULL` spelling. Either answers a query
    differently while satisfying a check that compares names, so the file passes
    as complete and lies to whoever reads it.
    """
    db_path = tmp_path / "test.db"
    with index_db.IndexDatabase(db_path, create=True) as db:
        assert db.missing() == []

    with index_db.IndexDatabase(db_path) as db:
        db.execute("DROP INDEX idx_message_2")
        db.execute("CREATE INDEX idx_message_2 ON message(store_id)")

    with index_db.IndexDatabase(db_path) as db:
        assert [str(obj) for obj in db.missing()] == ["index idx_message_2"]
        assert not db.usable, "the name is taken, the object is not the one"


def test_the_indexes_sqlite_makes_for_itself_are_not_declared_again(tmp_path):
    """A UNIQUE column already has a B-tree; a second one over the same columns is
    maintained on every insert and read by nobody -- the planner takes the
    autoindex. Measured on a projection of 4,000 messages, six such indexes cost
    23% of the file (3,670,016 bytes against 2,818,048) and changed no query plan.

    Asked of SQLite rather than of the statements, because what covers what is its
    answer to give: a declared index is redundant when its columns are the leading
    columns of an autoindex on the same table.
    """
    db_path = tmp_path / "test.db"
    with index_db.IndexDatabase(db_path, create=True) as db:

        def columns(index: str) -> list[str | None]:
            return [row[2] for row in db.execute(f"PRAGMA index_info({index})")]

        tables = [
            row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        ]
        for table in tables:
            listed = [row[1] for row in db.execute(f"PRAGMA index_list({table})")]
            auto = [columns(name) for name in listed if name.startswith("sqlite_autoindex_")]
            for name in listed:
                if name.startswith("sqlite_autoindex_"):
                    continue
                mine = columns(name)
                if None in mine:  # an index over expressions covers nothing else
                    continue
                for theirs in auto:
                    assert mine != theirs[: len(mine)], (
                        f"{name} on {table} repeats what SQLite already indexes"
                    )


def test_an_object_that_is_missing_is_not_made_on_the_way_past(tmp_path):
    """The file comes back exactly as it was, however it was opened."""
    db_path = tmp_path / "test.db"
    with index_db.IndexDatabase(db_path, create=True) as db:
        db.add_message("a" * 96, "<a@example.com>", None, "Subject")
    with index_db.IndexDatabase(db_path) as db:
        db.execute("DROP INDEX idx_message_2")
    before = db_path.read_bytes()

    for _ in range(2):
        with index_db.IndexDatabase(db_path, create=True) as db:
            assert not db.usable

    assert db_path.read_bytes() == before


def test_a_file_that_already_holds_a_projection_is_not_created_into(tmp_path):
    """Half of one shape and half of another is worse than either."""
    db_path = tmp_path / "test.db"
    with index_db.IndexDatabase(db_path, create=True) as db:
        with pytest.raises(index_db.SchemaError):
            db.create()


def test_a_new_database_carries_the_page_size_this_version_writes(tmp_path):
    """It can only be set before the first page, so nothing later can put it right."""
    db_path = tmp_path / "test.db"
    with index_db.IndexDatabase(db_path, create=True) as db:
        assert db.execute("PRAGMA page_size").fetchone()[0] == index_db.PAGE_SIZE
        assert db.schema_version() == index_db.SCHEMA_VERSION
