"""Build a `store.db` the way an archive written before 0.8.0 held one.

The specimen the migration tests need. It lives here and not in
`mailvault.legacy` for two reasons, and the second is the one that matters:

Nothing in mailvault ever writes this format. The reader in
`mailvault.legacy.store_db` opens a database somebody else left behind, and
giving it a `setup()` that only a test calls would be production code with no
production caller.

And the schema here is **frozen**. Until now these fixtures were built with the
projection's own `setup()`, because one class served both -- so the day the
projection's schema moves, every migration test would quietly start building a
database no version of mailvault ever wrote, and go on passing. What it tests
has to be what existed, which means keeping a copy of what existed.

Only the tables the migration reads: where a message was seen, and the resume
timestamps. The projection's addresses, subjects and views were in the same file
but are no part of what a migration lifts out of it.
"""

from __future__ import annotations

import contextlib
import pathlib
import sqlite3
from datetime import UTC, datetime

# The schema as of 0.7.x, copied out of what was then `metadb.setup()`. Do not
# "keep this in step" with the projection: its whole purpose is to stop being in
# step. If a real archive is found with a shape this does not cover, add that
# shape, from the archive.
LEGACY_DDL = """
CREATE TABLE IF NOT EXISTS mailbox (
    mailbox_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    UNIQUE(name) ON CONFLICT IGNORE);

CREATE TABLE IF NOT EXISTS label (
    label_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    UNIQUE(name) ON CONFLICT IGNORE);

CREATE TABLE IF NOT EXISTS subject (
    subject_id INTEGER PRIMARY KEY,
    text TEXT NOT NULL,
    UNIQUE(text) ON CONFLICT IGNORE);

CREATE TABLE IF NOT EXISTS message (
    message_id INTEGER PRIMARY KEY,
    store_id TEXT NOT NULL,
    email_id TEXT,
    date TEXT,
    subject_id INTEGER,
    FOREIGN KEY(subject_id) REFERENCES subject(subject_id),
    UNIQUE(store_id) ON CONFLICT IGNORE);

CREATE TABLE IF NOT EXISTS message_mailbox (
    message_id INTEGER,
    mailbox_id INTEGER,
    FOREIGN KEY(message_id) REFERENCES message(message_id),
    FOREIGN KEY(mailbox_id) REFERENCES mailbox(mailbox_id),
    UNIQUE(message_id, mailbox_id) ON CONFLICT IGNORE);

CREATE TABLE IF NOT EXISTS message_label (
    message_id INTEGER NOT NULL,
    label_id INTEGER NOT NULL,
    FOREIGN KEY(message_id) REFERENCES message(message_id),
    FOREIGN KEY(label_id) REFERENCES label(label_id),
    UNIQUE(message_id, label_id) ON CONFLICT IGNORE);

CREATE TABLE IF NOT EXISTS snapshot (
    snapshot_id INTEGER PRIMARY KEY,
    mailbox_id INTEGER NOT NULL,
    label_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    FOREIGN KEY(mailbox_id) REFERENCES mailbox(mailbox_id),
    FOREIGN KEY(label_id) REFERENCES label(label_id),
    UNIQUE(mailbox_id, label_id) ON CONFLICT REPLACE);
"""


class LegacyStoreDatabase:
    """Fill a legacy `store.db`, with the few writes the fixtures need.

    Deliberately not a mirror of what the projection can do -- these are the
    operations a test needs to describe an old archive, no more.
    """

    def __init__(self, dbconn: sqlite3.Connection):
        self.dbconn = dbconn

    def _intern(self, table: str, id_column: str, key_column: str, value: str) -> int:
        self.dbconn.execute(f"INSERT OR IGNORE INTO {table}({key_column}) VALUES (?)", (value,))
        return self.dbconn.execute(
            f"SELECT {id_column} FROM {table} WHERE {key_column}=?", (value,)
        ).fetchone()[0]

    def add_mailbox(self, name: str) -> int:
        return self._intern("mailbox", "mailbox_id", "name", name)

    def add_label(self, name: str) -> int:
        return self._intern("label", "label_id", "name", name)

    def add_subject(self, text: str) -> int:
        return self._intern("subject", "subject_id", "text", text)

    def add_message(
        self,
        store_id: str,
        email_id: str = "",
        date: datetime | None = None,
        subject: str = "",
        mailbox_id: int | None = None,
    ) -> int:
        subject_id = self.add_subject(subject)
        self.dbconn.execute(
            "INSERT OR IGNORE INTO message(store_id, email_id, date, subject_id) "
            "VALUES (?, ?, ?, ?)",
            (store_id, email_id, date.isoformat() if date else None, subject_id),
        )
        message_id = self.dbconn.execute(
            "SELECT message_id FROM message WHERE store_id=?", (store_id,)
        ).fetchone()[0]
        if mailbox_id is not None:
            self.assign_message_to_mailbox(message_id, mailbox_id)
        return message_id

    def assign_message_to_mailbox(self, message_id: int, mailbox_id: int) -> None:
        self.dbconn.execute(
            "INSERT OR IGNORE INTO message_mailbox(message_id, mailbox_id) VALUES (?, ?)",
            (message_id, mailbox_id),
        )

    def add_message_labels(self, message_id: int, *label_names: str) -> None:
        for label in label_names:
            label_id = self.add_label(label)
            self.dbconn.execute(
                "INSERT OR IGNORE INTO message_label(message_id, label_id) VALUES (?, ?)",
                (message_id, label_id),
            )

    def set_snapshot(
        self,
        mailbox_id: int,
        label_id: int,
        date: datetime | None = None,
    ) -> None:
        if date is None:
            date = datetime.now(UTC)
        # Works because of ON CONFLICT REPLACE, as it did then.
        self.dbconn.execute(
            "INSERT INTO snapshot(mailbox_id, label_id, date) VALUES (?, ?, ?)",
            (mailbox_id, label_id, date.isoformat()),
        )


@contextlib.contextmanager
def legacy_store_db(path: pathlib.Path | str):
    """Open (creating it if need be) a legacy `store.db` and yield a writer."""
    dbconn = sqlite3.connect(path, check_same_thread=False)
    dbconn.row_factory = sqlite3.Row
    try:
        dbconn.executescript(LEGACY_DDL)
        yield LegacyStoreDatabase(dbconn)
        dbconn.commit()
    finally:
        dbconn.close()
