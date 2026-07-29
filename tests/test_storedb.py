from datetime import datetime, timezone

import pytest

from imapbackup import storedb


def test_store_db_setup(tmp_path):
    db_path = tmp_path / "test.db"
    with storedb.StoreDatabase(db_path) as db:
        res = db.dbconn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        tables = [r[0] for r in res]
        assert "mailbox" in tables
        assert "message" in tables
        assert "label" in tables
        assert "address" in tables
        assert "subject" in tables
        assert "snapshot" in tables
        assert "message_mailbox" in tables
        assert "message_label" in tables
        assert "message_sender" in tables
        assert "message_recipient" in tables


def test_store_db_setup_views(tmp_path):
    db_path = tmp_path / "test.db"
    with storedb.StoreDatabase(db_path) as db:
        res = db.dbconn.execute("SELECT name FROM sqlite_master WHERE type='view'").fetchall()
        views = [r[0] for r in res]
        assert "v_messages" in views
        assert "v_duplicates" in views


def test_store_db_setup_indexes(tmp_path):
    """Verify that message_recipient indexes are on the correct table (B1 fix)."""
    db_path = tmp_path / "test.db"
    with storedb.StoreDatabase(db_path) as db:
        indexes = db.dbconn.execute(
            "SELECT name, tbl_name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_message_recipient%'"
        ).fetchall()
        for name, tbl_name in indexes:
            assert tbl_name == "message_recipient", (
                f"Index {name} is on table {tbl_name}, expected message_recipient"
            )


def test_store_db_add_mailbox(tmp_path):
    db_path = tmp_path / "test.db"
    with storedb.StoreDatabase(db_path) as db:
        mb_id = db.add_mailbox("INBOX")
        assert mb_id > 0
        # Adding same mailbox should return same id
        mb_id_2 = db.add_mailbox("INBOX")
        assert mb_id == mb_id_2


def test_store_db_add_message_and_labels(tmp_path):
    db_path = tmp_path / "test.db"
    with storedb.StoreDatabase(db_path) as db:
        msg_id = db.add_message(
            store_id="hash123",
            email_id="<message-id@example.com>",
            date=datetime.now(timezone.utc),
            subject="Test Subject",
        )
        assert msg_id > 0

        db.add_message_labels(msg_id, "Label1", "Label2")

        labels = db.get_message_labels(msg_id)
        assert set(labels) == {"Label1", "Label2"}


def test_store_db_update_message_labels(tmp_path):
    db_path = tmp_path / "test.db"
    with storedb.StoreDatabase(db_path) as db:
        msg_id = db.add_message(
            store_id="hash_upd",
            email_id="<upd@example.com>",
            date=datetime.now(timezone.utc),
            subject="Update Labels",
        )
        db.add_message_labels(msg_id, "Old1", "Old2", "Keep")
        assert set(db.get_message_labels(msg_id)) == {"Old1", "Old2", "Keep"}

        # Update: remove Old1, Old2; add New1; keep Keep
        db.update_message_labels(msg_id, "Keep", "New1")
        assert set(db.get_message_labels(msg_id)) == {"Keep", "New1"}


def test_store_db_add_message_with_mailbox(tmp_path):
    db_path = tmp_path / "test.db"
    with storedb.StoreDatabase(db_path) as db:
        mb_id = db.add_mailbox("TestMailbox")
        msg_id = db.add_message(
            store_id="hash_mb",
            email_id="<mb@example.com>",
            date=datetime.now(timezone.utc),
            subject="With Mailbox",
            mailbox_id=mb_id,
        )
        row = db.execute(
            "SELECT mailbox_id FROM message_mailbox WHERE message_id=?", (msg_id,)
        ).fetchone()
        assert row is not None
        assert row[0] == mb_id


def test_store_db_assign_message_to_mailbox(tmp_path):
    db_path = tmp_path / "test.db"
    with storedb.StoreDatabase(db_path) as db:
        mb_id = db.add_mailbox("Box1")
        msg_id = db.add_message(
            store_id="hash_assign",
            email_id="<assign@example.com>",
            date=datetime.now(timezone.utc),
            subject="Assign",
        )
        db.assign_message_to_mailbox(msg_id, mb_id)

        # Duplicate assignment should not fail (ON CONFLICT IGNORE)
        db.assign_message_to_mailbox(msg_id, mb_id)

        rows = db.execute(
            "SELECT mailbox_id FROM message_mailbox WHERE message_id=?", (msg_id,)
        ).fetchall()
        assert len(rows) == 1


def test_store_db_sender_and_recipients(tmp_path):
    db_path = tmp_path / "test.db"
    with storedb.StoreDatabase(db_path) as db:
        msg_id = db.add_message(
            store_id="hash_addr",
            email_id="<addr@example.com>",
            date=datetime.now(timezone.utc),
            subject="Addresses",
        )
        db.add_message_sender(msg_id, "alice@example.com", "bob@example.com")
        db.add_message_recipients(msg_id, "carol@example.com", "dave@example.com")

        senders = db.execute(
            "SELECT a.address FROM message_sender ms JOIN address a USING (address_id) WHERE ms.message_id=?",
            (msg_id,),
        ).fetchall()
        assert set(r[0] for r in senders) == {"alice@example.com", "bob@example.com"}

        recipients = db.execute(
            "SELECT a.address FROM message_recipient mr JOIN address a USING (address_id) WHERE mr.message_id=?",
            (msg_id,),
        ).fetchall()
        assert set(r[0] for r in recipients) == {"carol@example.com", "dave@example.com"}


def test_store_db_snapshot_lifecycle(tmp_path):
    db_path = tmp_path / "test.db"
    with storedb.StoreDatabase(db_path) as db:
        mb_id = db.add_mailbox("SnapMB")
        label_id = db.add_label("INBOX")

        # No snapshot yet
        assert db.get_snapshot(mb_id, label_id) is None
        assert db.get_snapshot_date(mb_id, label_id) is None
        assert db.get_snapshot_date(mb_id, label_id, default=datetime(2020, 1, 1)) == datetime(
            2020, 1, 1
        )

        # Set snapshot
        snap_date = datetime(2026, 3, 27, 12, 0, 0)
        db.set_snapshot(mb_id, label_id, date=snap_date)

        s = db.get_snapshot(mb_id, label_id)
        assert s is not None
        assert db.get_snapshot_date(mb_id, label_id) == snap_date

        # Update snapshot (ON CONFLICT REPLACE)
        new_date = datetime(2026, 3, 28, 12, 0, 0)
        db.set_snapshot(mb_id, label_id, date=new_date)
        assert db.get_snapshot_date(mb_id, label_id) == new_date

        # Delete snapshot for specific label
        db.delete_snapshot(mb_id, label_id)
        assert db.get_snapshot(mb_id, label_id) is None


def test_store_db_delete_snapshot_all_labels(tmp_path):
    db_path = tmp_path / "test.db"
    with storedb.StoreDatabase(db_path) as db:
        mb_id = db.add_mailbox("SnapAll")
        l1 = db.add_label("Folder1")
        l2 = db.add_label("Folder2")

        db.set_snapshot(mb_id, l1, date=datetime(2026, 1, 1))
        db.set_snapshot(mb_id, l2, date=datetime(2026, 1, 2))

        # Delete all snapshots for mailbox
        db.delete_snapshot(mb_id)
        assert db.get_snapshot(mb_id, l1) is None
        assert db.get_snapshot(mb_id, l2) is None


def test_store_db_transaction_rollback(tmp_path):
    db_path = tmp_path / "test.db"
    with storedb.StoreDatabase(db_path) as db:
        db.add_mailbox("BeforeRollback")

        with pytest.raises(storedb.RollbackException):
            with db.transaction():
                db.execute("INSERT OR IGNORE INTO mailbox(name) VALUES (?)", ("RolledBack",))
                db.rollback()

        # RolledBack should not exist
        row = db.execute("SELECT name FROM mailbox WHERE name='RolledBack'").fetchone()
        assert row is None

        # BeforeRollback should still exist
        row = db.execute("SELECT name FROM mailbox WHERE name='BeforeRollback'").fetchone()
        assert row is not None


def test_store_db_v_messages_view(tmp_path):
    db_path = tmp_path / "test.db"
    with storedb.StoreDatabase(db_path) as db:
        mb_id = db.add_mailbox("ViewTest")
        msg_id = db.add_message(
            store_id="hash_view",
            email_id="<view@example.com>",
            date=datetime(2026, 3, 27, tzinfo=timezone.utc),
            subject="View Subject",
            mailbox_id=mb_id,
        )
        db.add_message_sender(msg_id, "sender@example.com")
        db.add_message_recipients(msg_id, "rcpt@example.com")

        rows = db.execute("SELECT * FROM v_messages WHERE store_id='hash_view'").fetchall()
        assert len(rows) >= 1
        row = dict(rows[0])
        assert row["sender"] == "sender@example.com"
        assert row["recipient"] == "rcpt@example.com"
        assert row["mailbox"] == "ViewTest"
        assert row["subject"] == "View Subject"


def test_store_db_v_duplicates_view(tmp_path):
    db_path = tmp_path / "test.db"
    with storedb.StoreDatabase(db_path) as db:
        date = datetime(2026, 3, 27, tzinfo=timezone.utc)
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


def test_store_db_add_message_idempotent(tmp_path):
    """Adding same store_id twice should not create duplicate (ON CONFLICT IGNORE)."""
    db_path = tmp_path / "test.db"
    with storedb.StoreDatabase(db_path) as db:
        date = datetime(2026, 1, 1, tzinfo=timezone.utc)
        id1 = db.add_message(store_id="same_hash", email_id="<a@b>", date=date, subject="First")
        id2 = db.add_message(store_id="same_hash", email_id="<a@b>", date=date, subject="First")
        assert id1 == id2


# ---------------------------------------------------------------------------
# get_known_message_ids
# ---------------------------------------------------------------------------

def test_get_known_message_ids(tmp_path):
    with storedb.StoreDatabase(tmp_path / "test.db") as db:
        date = datetime(2026, 1, 1, tzinfo=timezone.utc)
        mb_id = db.add_mailbox("mbox")
        other_mb = db.add_mailbox("other-mbox")
        inbox = db.add_label("INBOX")
        sent = db.add_label("Sent")

        msg = db.add_message("hash_a", "<A@example.com>", date, "A", mailbox_id=mb_id)
        db.add_message_labels(msg, "INBOX")
        other_folder = db.add_message("hash_b", "<b@example.com>", date, "B", mailbox_id=mb_id)
        db.add_message_labels(other_folder, "Sent")
        other_mailbox = db.add_message(
            "hash_c", "<c@example.com>", date, "C", mailbox_id=other_mb
        )
        db.add_message_labels(other_mailbox, "INBOX")

        known = db.get_known_message_ids(mb_id, inbox)
        # Normalised: no angle brackets, case-folded.
        assert known == {"a@example.com"}
        assert db.get_known_message_ids(mb_id, sent) == {"b@example.com"}


def test_get_known_message_ids_skips_unusable(tmp_path):
    """Messages without a Message-ID cannot be matched and must not be listed."""
    with storedb.StoreDatabase(tmp_path / "test.db") as db:
        date = datetime(2026, 1, 1, tzinfo=timezone.utc)
        mb_id = db.add_mailbox("mbox")
        inbox = db.add_label("INBOX")
        for store_id, email_id in [("h1", ""), ("h2", "<>"), ("h3", "<ok@example.com>")]:
            msg = db.add_message(store_id, email_id, date, "S", mailbox_id=mb_id)
            db.add_message_labels(msg, "INBOX")

        assert db.get_known_message_ids(mb_id, inbox) == {"ok@example.com"}
