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

# The shape this version writes, kept in SQLite's own `user_version`. It exists
# so that a projection built by an earlier version can be *recognised* rather
# than silently used: the tables are created with IF NOT EXISTS, so an old file
# would quietly gain the new ones, keep the old ones, and go on being read
# through a view that no longer says what it says here -- while `applied_log`
# reports every log file as folded in, so nothing would ever fill the new tables.
#
# There is no upgrade path and there must not be one. Everything here can be
# rebuilt from the archive, so the answer to a mismatch is to build it again.
# Raise this whenever the schema changes in a way a reader would notice.
SCHEMA_VERSION = 1

# How much page cache a connection that fills a database in one go may use, in
# KiB. SQLite's default is two megabytes, and a build overruns that within the
# first few thousand messages: from then on it evicts pages it is about to touch
# again, because a B-tree being filled keeps coming back to the same interior
# nodes. What that costs is not memory but *writes*, and it grows with the file.
#
# Measured on 30,000 messages, an 18.4 MiB database: 166.8 MiB written with the
# default cache, 18.7 MiB with this one -- nine times the traffic against once.
# On a local disk it makes no difference in time, because the page cache of the
# operating system absorbs it; over a network share it is the difference between
# writing the database once and writing it nine times.
#
# Allocated on demand, so a build that stays small never takes it.
BULK_CACHE_KIB = 65536


class IndexDatabase:
    """Open the projection as a context manager, creating its schema on entry.

    `with IndexDatabase(path) as db:` yields an `IndexDatabaseConnection` and
    closes the connection on exit. The schema is created for a database that is
    not there yet, which is the ordinary case.

    **A database whose shape this version does not know is not touched at all.**
    Not created into, not stamped, not written -- `outdated` says so and the
    caller decides. Running `setup()` on one would add this version's tables
    beside the old ones and stamp the file as current, which turns a database
    that can be recognised as old into one that cannot: the complaint would be
    made once and never again, over a projection that is still wrong.
    """

    def __init__(self, path: pathlib.Path | str, bulk: bool = False):
        self.dbconn: sqlite3.Connection | None = None
        self.client: IndexDatabaseConnection | None = None
        self.path = path
        self.bulk = bulk

    def __enter__(self) -> IndexDatabaseConnection:
        self.dbconn = connect(self.path)
        if self.bulk:
            self.dbconn.execute(f"PRAGMA cache_size = -{BULK_CACHE_KIB}")
        self.client = IndexDatabaseConnection(self.dbconn)
        if not self.client.outdated:
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
        # What shape the file was in when it was opened, read here because this
        # is the last moment it can be: `setup()` stamps the current version, so
        # anyone asking afterwards is told what this version writes rather than
        # what it found. 0 for a file nobody has stamped -- a fresh one, or one
        # from before the shape was recorded at all, which is what the emptiness
        # below tells apart.
        self.shape_on_open = self.schema_version()
        self._new_file = (
            self.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
            == 0
        )
        # Per-instance id caches for the lookup tables. These map a value (folder
        # name, address, ...) to its primary key so a repeated value is not
        # re-queried. Kept on the instance -- not via functools.lru_cache on the
        # method -- so the cache (and the connection it references) is released
        # with the connection instead of living on the class until process exit.
        self._mailbox_ids: dict[str, int] = {}
        self._folder_ids: dict[str, int] = {}
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

            # Not "label". Gmail has labels where IMAP has folders, and the
            # difference is how many of them a message may carry, not what they
            # are -- so one word covers both, and it is the one everybody uses.
            # Carrying "label" as a separate idea is what let the two halves of
            # a place drift into two relations in the first place.
            self.execute("""
                CREATE TABLE IF NOT EXISTS folder (
                folder_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                UNIQUE(name) ON CONFLICT IGNORE)
            """)
            self.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_folder_1 ON folder(name)")

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

            # One row is one place: "this message was seen in that folder of that
            # mailbox". It used to be two independent relations, `message_mailbox`
            # and `message_label`, which is a normalisation mistake rather than a
            # bug in any function -- one fact split across two tables loses the
            # pairing at write time. On the reference archive that was 61.6 % of
            # 130,887 messages: everything in more than one mailbox, where "which
            # folder of which mailbox" could no longer be answered.
            #
            # Both halves may be NULL, and both cases are real. A mailbox with no
            # folder is an archive whose history did not record one. A folder with
            # no mailbox is what an import writes -- the place is named, and the
            # name is deliberately not in the mailbox field, because that field is
            # read as a job name by the guard, by `verify` and by the catch-up.
            # What must never happen is a pairing invented to satisfy a NOT NULL.
            self.execute("""
                CREATE TABLE IF NOT EXISTS message_location (
                message_id INTEGER NOT NULL,
                mailbox_id INTEGER,
                folder_id INTEGER,
                FOREIGN KEY(message_id) REFERENCES message(message_id),
                FOREIGN KEY(mailbox_id) REFERENCES mailbox(mailbox_id),
                FOREIGN KEY(folder_id) REFERENCES folder(folder_id))
            """)
            # The uniqueness has to be spelt out over IFNULL, not as a plain
            # UNIQUE(message_id, mailbox_id, folder_id): SQLite holds every NULL
            # to be distinct from every other NULL, so `INSERT OR IGNORE` would
            # not recognise a repeat of a place whose folder or mailbox is
            # unknown. Measured -- three inserts of the same folderless location
            # gave three rows. Replaying the log is meant to be idempotent, and
            # the log is replayed on every refresh, so that is a row per run for
            # every message an old archive has no folder for.
            self.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_message_location_1 "
                "ON message_location(message_id, IFNULL(mailbox_id, -1), IFNULL(folder_id, -1))"
            )
            self.execute(
                "CREATE INDEX IF NOT EXISTS idx_message_location_2 "
                "ON message_location(mailbox_id)"
            )
            self.execute(
                "CREATE INDEX IF NOT EXISTS idx_message_location_3 "
                "ON message_location(folder_id)"
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

            # `snapshot` is gone. It held the resume timestamps of an archive
            # that kept its truth in SQLite, and nothing has written it since
            # 0.8.0 -- resume points live in `heads/`. A current projection
            # created the table and left it empty, on every rebuild. The reader
            # it still had is where it belongs: `mailvault.legacy.store_db`,
            # which opens the old database that really does hold them.

            # Every join to the left, and that is the whole change. It used to
            # inner-join sender, recipient and subject, so a message the view
            # could not complete simply was not in it -- and a message with no
            # readable recipient is not a rarity in an archive that goes back to
            # the nineties, it is the group address, the malformed header, the
            # `Undisclosed recipients:;`. A view that silently holds fewer
            # messages than the archive is the worst kind of wrong here: nothing
            # about it looks like an error, and `SELECT count(*)` lies.
            self.execute("""
                CREATE VIEW IF NOT EXISTS v_messages AS
                SELECT
                msg.message_id,
                msg.email_id,
                msg.store_id,
                msg.date,
                mb.name "mailbox",
                f.name "folder",
                addr_send.address "sender",
                addr_rcpt.address "recipient",
                subject.text "subject"
                FROM message msg
                LEFT JOIN subject USING (subject_id)
                LEFT JOIN message_sender send USING (message_id)
                LEFT JOIN address addr_send ON addr_send.address_id=send.address_id
                LEFT JOIN message_recipient rcpt USING (message_id)
                LEFT JOIN address addr_rcpt ON addr_rcpt.address_id=rcpt.address_id
                LEFT JOIN message_location loc USING (message_id)
                LEFT JOIN mailbox mb ON mb.mailbox_id=loc.mailbox_id
                LEFT JOIN folder f ON f.folder_id=loc.folder_id
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

            # Stamped last, and only on a file that was empty when it was
            # opened. Stamping one that already held a projection would be a
            # claim nobody checked -- the tables above are created IF NOT
            # EXISTS, so they say nothing about what else is in there.
            if self._new_file:
                self.execute(f"PRAGMA user_version = {SCHEMA_VERSION:d}")

    def schema_version(self) -> int:
        """Which shape this file was written in; 0 for anything before stamping."""
        return self.execute("PRAGMA user_version").fetchone()[0]

    @property
    def outdated(self) -> bool:
        """Whether this file holds a projection this version does not read.

        An empty file is not outdated, it is unwritten -- which is why the two
        are told apart by whether there were any tables, and not by the version
        alone: both answer 0.
        """
        return not self._new_file and self.shape_on_open != SCHEMA_VERSION

    def add_mailbox(self, mailbox_name: str) -> int:
        return self._intern(self._mailbox_ids, "mailbox", "mailbox_id", "name", mailbox_name)

    def add_folder(self, folder_name: str) -> int:
        return self._intern(self._folder_ids, "folder", "folder_id", "name", folder_name)

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
        mailbox: str | None = None,
        folder: str | None = None,
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
            if mailbox is not None or folder is not None:
                self.add_message_location(msg_id, mailbox, folder)
            return msg_id

    def add_message_location(
        self,
        message_id: int,
        mailbox: str | None,
        folder: str | None,
    ) -> None:
        """Record that a message was seen in one folder of one mailbox.

        One call, because it is one fact. Either half may be unknown -- an old
        archive that never recorded a folder, an import that names a place and
        no mailbox -- and an unknown half is written as NULL rather than filled
        in. The pairing is the whole point: it cannot be recovered afterwards
        from two separate lists, which is what the schema used to keep.
        """
        with self.transaction():
            mailbox_id = self.add_mailbox(mailbox) if mailbox is not None else None
            folder_id = self.add_folder(folder) if folder is not None else None
            self.execute(
                "INSERT OR IGNORE INTO message_location(message_id, mailbox_id, folder_id) "
                "VALUES (?, ?, ?)",
                (message_id, mailbox_id, folder_id),
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

    def add_message_sender(self, message_id: int, *sender: str) -> None:
        # The transaction is what commits these rows. Without it the inserts sat
        # in the connection until some later call happened to commit them, and
        # the rows added last -- with nothing following -- were lost when the
        # connection closed. Every sibling method here is wrapped the same way.
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
