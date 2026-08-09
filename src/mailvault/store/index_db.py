"""The queryable projection of an archive: `index.db`.

The archive holds no database. Its metadata lives in the append-only log under
`meta/`, and this turns that log plus the messages into something SQL can be run
against -- sender, recipients, subject, date, and which mailbox and folder each
message was seen in.

What this is, and the whole reason it may be shaped freely: **a projection, not
a source of truth.** Everything in it can be rebuilt from the archive, so a
change to the schema costs a rebuild rather than a migration path. That is the
right answer for a projection and must never be the answer for the archive.

Not to be confused with `mailvault.legacy.store_db`, which reads the `store.db`
that *was* the truth in archives written before 0.8.0. The two were one class
until they were told apart: one `setup()` created the union of both schemas,
which is how a table nothing reads ended up in every projection built since.
They share the plumbing in `mailvault.store.sqlite` and nothing else.
"""

from __future__ import annotations

import collections.abc
import pathlib
import sqlite3
from datetime import datetime
from typing import Any

from mailvault.store.sqlite import DatabaseConnection, connect


class IndexDatabase:
    """Open the projection as a context manager, creating its schema on entry.

    `with IndexDatabase(path) as db:` yields an `IndexDatabaseConnection` and
    closes the connection on exit. The schema is always created -- a projection
    that is not there yet is one to be built, which is the ordinary case.
    """

    def __init__(self, path: pathlib.Path | str):
        self.dbconn: sqlite3.Connection | None = None
        self.client: IndexDatabaseConnection | None = None
        self.path = path

    def __enter__(self) -> IndexDatabaseConnection:
        self.dbconn = connect(self.path)
        self.client = IndexDatabaseConnection(self.dbconn)
        self.client.setup()
        return self.client

    def __exit__(
        self,
        exc_type: type | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        if self.dbconn:
            self.dbconn.close()
            self.dbconn = None
            self.client = None


class IndexDatabaseConnection(DatabaseConnection):
    """The projection's schema and the operations that fill and query it.

    `setup()` creates the tables and the `v_messages` / `v_duplicates` views; the
    rest insert messages, addresses, subjects and locations, interning the
    lookup-table values through per-instance id caches.
    """

    def __init__(self, dbconn: sqlite3.Connection):
        super().__init__(dbconn)
        # Per-instance id caches for the lookup tables. These map a value (folder
        # name, address, ...) to its primary key so a repeated value is not
        # re-queried. Kept on the instance -- not via functools.lru_cache on the
        # method -- so the cache (and the connection it references) is released
        # with the connection instead of living on the class until process exit.
        self._mailbox_ids: dict[str, int] = {}
        self._label_ids: dict[str, int] = {}
        self._address_ids: dict[str, int] = {}
        self._subject_ids: dict[str, int] = {}

    def _intern(
        self,
        cache: dict[str, int],
        table: str,
        id_column: str,
        key_column: str,
        value: str,
    ) -> int:
        """Return the id for `value` in a lookup table, inserting it if new.

        `table`, `id_column` and `key_column` are internal constants, never user
        input, so interpolating them into the statement is safe.
        """
        cached = cache.get(value)
        if cached is not None:
            return cached
        with self.transaction():
            self.execute(f"INSERT OR IGNORE INTO {table}({key_column}) VALUES (?)", (value,))
            row_id = self.execute(
                f"SELECT {id_column} FROM {table} WHERE {key_column}=?",
                (value,),
            ).fetchone()[0]
        cache[value] = row_id
        return row_id

    def setup(self) -> None:
        with self.transaction():
            self.execute("""
                CREATE TABLE IF NOT EXISTS mailbox (
                mailbox_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                UNIQUE(name) ON CONFLICT IGNORE)
            """)

            self.execute("""
                CREATE TABLE IF NOT EXISTS address (
                address_id INTEGER PRIMARY KEY,
                address TEXT NOT NULL,
                UNIQUE(address) ON CONFLICT IGNORE)
            """)
            self.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_address_1 ON address(address)")

            self.execute("""
                CREATE TABLE IF NOT EXISTS label (
                label_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                UNIQUE(name) ON CONFLICT IGNORE)
            """)
            self.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_label_1 ON label(name)")
            self.execute("""
                INSERT OR IGNORE INTO label(name) VALUES ("INBOX")
            """)

            self.execute("""
                CREATE TABLE IF NOT EXISTS subject (
                subject_id INTEGER PRIMARY KEY,
                text TEXT NOT NULL,
                UNIQUE(text) ON CONFLICT IGNORE)
            """)
            self.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_subject_1 ON subject(text)")

            self.execute("""
                CREATE TABLE IF NOT EXISTS message (
                message_id INTEGER PRIMARY KEY,
                store_id TEXT NOT NULL,
                email_id TEXT,
                date TEXT,
                subject_id INTEGER,
                FOREIGN KEY(subject_id) REFERENCES subject(subject_id),
                UNIQUE(store_id) ON CONFLICT IGNORE)
            """)
            self.execute("CREATE INDEX IF NOT EXISTS idx_message_1 ON message(store_id)")

            self.execute("""
                CREATE TABLE IF NOT EXISTS message_mailbox (
                message_id INTEGER,
                mailbox_id INTEGER,
                FOREIGN KEY(message_id) REFERENCES message(message_id),
                FOREIGN KEY(mailbox_id) REFERENCES mailbox(mailbox_id),
                UNIQUE(message_id, mailbox_id) ON CONFLICT IGNORE)
            """)
            self.execute(
                "CREATE INDEX IF NOT EXISTS idx_message_mailbox_1 "
                "ON message_mailbox(message_id)"
            )
            self.execute(
                "CREATE INDEX IF NOT EXISTS idx_message_mailbox_2 "
                "ON message_mailbox(mailbox_id)"
            )

            self.execute("""
                CREATE TABLE IF NOT EXISTS message_label (
                message_id INTEGER NOT NULL,
                label_id INTEGER NOT NULL,
                FOREIGN KEY(message_id) REFERENCES message(message_id),
                FOREIGN KEY(label_id) REFERENCES label(label_id),
                UNIQUE(message_id, label_id) ON CONFLICT IGNORE);
            """)
            self.execute(
                "CREATE INDEX IF NOT EXISTS idx_message_label_1 ON message_label(message_id)"
            )
            self.execute(
                "CREATE INDEX IF NOT EXISTS idx_message_label_2 ON message_label(label_id)"
            )

            self.execute("""
                CREATE TABLE IF NOT EXISTS message_sender (
                message_id INTEGER NOT NULL,
                address_id INTEGER NOT NULL,
                FOREIGN KEY(message_id) REFERENCES message(message_id),
                FOREIGN KEY(address_id) REFERENCES address(address_id),
                UNIQUE(message_id, address_id) ON CONFLICT IGNORE)
            """)
            self.execute(
                "CREATE INDEX IF NOT EXISTS idx_message_sender_1 ON message_sender(message_id)"
            )
            self.execute(
                "CREATE INDEX IF NOT EXISTS idx_message_sender_2 ON message_sender(address_id)"
            )

            self.execute("""
                CREATE TABLE IF NOT EXISTS message_recipient (
                message_id INTEGER NOT NULL,
                address_id INTEGER NOT NULL,
                FOREIGN KEY(message_id) REFERENCES message(message_id),
                FOREIGN KEY(address_id) REFERENCES address(address_id),
                UNIQUE(message_id, address_id) ON CONFLICT IGNORE)
            """)
            # Migration: earlier versions created these indexes with a different
            # definition; drop the old ones before (re)creating them below.
            self.execute("DROP INDEX IF EXISTS idx_message_recipient_1")
            self.execute("DROP INDEX IF EXISTS idx_message_recipient_2")
            self.execute(
                "CREATE INDEX IF NOT EXISTS idx_message_recipient_1 "
                "ON message_recipient(message_id)"
            )
            self.execute(
                "CREATE INDEX IF NOT EXISTS idx_message_recipient_2 "
                "ON message_recipient(address_id)"
            )

            self.execute("""
                CREATE TABLE IF NOT EXISTS snapshot (
                snapshot_id INTEGER PRIMARY KEY,
                mailbox_id INTEGER NOT NULL,
                label_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                FOREIGN KEY(mailbox_id) REFERENCES mailbox(mailbox_id),
                FOREIGN KEY(label_id) REFERENCES label(label_id),
                UNIQUE(mailbox_id, label_id) ON CONFLICT REPLACE)
            """)
            self.execute("CREATE INDEX IF NOT EXISTS idx_snapshot_1 ON snapshot(mailbox_id)")

            self.execute("""
                CREATE VIEW IF NOT EXISTS v_messages AS
                SELECT
                msg.message_id,
                msg.email_id,
                msg.store_id,
                msg.date,
                mb.name "mailbox",
                addr_send.address "sender",
                addr_rcpt.address "recipient",
                subject.text "subject"
                FROM message msg
                JOIN message_sender send USING (message_id)
                JOIN message_recipient rcpt USING (message_id)
                JOIN subject USING (subject_id)
                JOIN address addr_send ON addr_send.address_id=send.address_id
                JOIN address addr_rcpt ON addr_rcpt.address_id=rcpt.address_id
                LEFT OUTER JOIN message_mailbox mm USING (message_id)
                LEFT OUTER JOIN mailbox mb ON mb.mailbox_id=mm.mailbox_id
            """)

            self.execute("""
                CREATE VIEW IF NOT EXISTS v_duplicates AS
                SELECT DISTINCT
                msg.message_id,
                msg.email_id,
                msg.store_id,
                msg.date
                FROM message msg
                INNER JOIN message dup
                ON msg.email_id=dup.email_id
                  AND msg.date=dup.date
                  AND msg.store_id<>dup.store_id
                ORDER BY msg.date, msg.email_id, msg.message_id
            """)

    def add_mailbox(self, mailbox_name: str) -> int:
        return self._intern(self._mailbox_ids, "mailbox", "mailbox_id", "name", mailbox_name)

    def add_label(self, label_name: str) -> int:
        return self._intern(self._label_ids, "label", "label_id", "name", label_name)

    def add_address(self, address: str) -> int:
        return self._intern(self._address_ids, "address", "address_id", "address", address)

    def add_subject(self, subject: str) -> int:
        return self._intern(self._subject_ids, "subject", "subject_id", "text", subject)

    def add_message(
        self,
        store_id: str,
        email_id: str,
        date: datetime | None,
        subject: str,
        mailbox_id: int | None = None,
    ) -> int:
        with self.transaction():
            subject_id = self.add_subject(subject)
            self.execute(
                "INSERT OR IGNORE INTO message(store_id, email_id, date, subject_id) "
                "VALUES (?, ?, ?, ?)",
                (store_id, email_id, date.isoformat() if date else None, subject_id),
            )
            msg_id = self.execute(
                "SELECT message_id FROM message WHERE store_id=?",
                (store_id,),
            ).fetchone()[0]
            if mailbox_id is not None:
                self.assign_message_to_mailbox(msg_id, mailbox_id)
            return msg_id

    def assign_message_to_mailbox(self, message_id: int, mailbox_id: int) -> None:
        with self.transaction():
            self.execute(
                "INSERT OR IGNORE INTO message_mailbox(message_id, mailbox_id) VALUES (?, ?)",
                (message_id, mailbox_id),
            )

    def iter_messages(self) -> collections.abc.Iterator[tuple[int, str]]:
        """Yield (message_id, store_id) for every archived message."""
        for row in self.execute("SELECT message_id, store_id FROM message"):
            yield row[0], row[1]

    def store_id_map(self) -> dict[str, int]:
        """Map every store_id to its message_id.

        Built in one query because the alternative -- one lookup per log entry
        while replaying -- costs a round trip per message over the whole archive.
        """
        return {store_id: message_id for message_id, store_id in self.iter_messages()}

    def add_message_labels(self, message_id: int, *label_names: str) -> None:
        # The transaction is what commits these rows. Without it the inserts sat
        # in the connection until some later call happened to commit them, and
        # the labels added last -- with nothing following -- were lost when the
        # connection closed. Every sibling method here is wrapped the same way.
        with self.transaction():
            for label in label_names:
                label_id = self.add_label(label)
                self.execute(
                    "INSERT OR IGNORE INTO message_label(message_id, label_id) VALUES (?, ?)",
                    (message_id, label_id),
                )

    def add_message_sender(self, message_id: int, *sender: str) -> None:
        with self.transaction():
            for addr in sender:
                addr_id = self.add_address(addr)
                self.execute(
                    "INSERT OR IGNORE INTO message_sender(message_id, address_id) "
                    "VALUES (?, ?)",
                    (message_id, addr_id),
                )

    def add_message_recipients(self, message_id: int, *recipients: str) -> None:
        with self.transaction():
            for addr in recipients:
                addr_id = self.add_address(addr)
                self.execute(
                    "INSERT OR IGNORE INTO message_recipient(message_id, address_id) "
                    "VALUES (?, ?)",
                    (message_id, addr_id),
                )
