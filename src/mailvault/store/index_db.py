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
that *was* the truth in archives written before 0.8.0. They share the plumbing in
`mailvault.store.sqlite` and nothing else.
"""

from __future__ import annotations

import collections.abc
import dataclasses
import pathlib
import sqlite3
import textwrap
import types
from datetime import datetime

from mailvault.store.sqlite import DatabaseConnection, connect


class SchemaError(Exception):
    """A database was asked for something its shape does not allow."""


# The shape this version writes, kept in SQLite's own `user_version`. It is what
# lets a projection built by another version be *recognised* rather than silently
# used, and it is the whole recognition: a file stamped with anything else is not
# read and not written, it is built again.
#
# There is no upgrade path and there must not be one. Everything here can be
# rebuilt from the archive, so the answer to a mismatch is to build it again.
# Raise this whenever `SCHEMA` changes in a way a reader would notice -- an added
# index counts, because what a reader gets without it is the right answer at a
# cost nothing would explain to them.
SCHEMA_VERSION = 2

# Page size of a database this version creates. SQLite reads a page at a time,
# and over a network share every page is a round trip -- a query that has to look
# through the message table touches the whole file, which at the 4 KiB default is
# tens of thousands of them. Against an archive of 131,504 messages on an SMB
# share, the same scan took 25 s at 4 KiB, 8 s at 16 KiB and 2.8 s at 64 KiB.
#
# It can only be set on a file that has nothing in it yet, which is why it is
# applied where the database is created and nowhere else.
PAGE_SIZE = 65536

# How much page cache a connection may use, in KiB. SQLite's default is two
# megabytes, which at the page size above is thirty-two pages -- and a B-tree
# keeps coming back to the same interior nodes, so a cache that small evicts
# exactly what is about to be read again. Over a network share every one of
# those is a round trip.
#
# It matters in both directions. Filling a database: 30,000 messages into an
# 18.4 MiB file wrote 166.8 MiB with the default cache and 18.7 MiB with this
# one. Reading one: a search over 131,504 messages that answered with 3,084 of
# them took 25.0 s against 0.5 s, because every hit follows a handful of
# scattered index entries.
#
# Allocated on demand, so a small database never takes it.
CACHE_KIB = 65536


# --- the shape of a projection -------------------------------------------------


@dataclasses.dataclass(frozen=True)
class SchemaObject:
    """One table, index or view of a projection, and the statement that makes it.

    Written down once and read twice: to create a database, and to recognise one
    that is already there. A check driven by a second list would go on passing a
    file that is missing whatever the list forgot.
    """

    kind: str
    name: str
    sql: str

    def __str__(self) -> str:
        return f"{self.kind} {self.name}"


def _body(text: str, indent: str = "") -> str:
    """A statement as it will be stored: dedented, without the blank edges."""
    return textwrap.indent(textwrap.dedent(text).strip(), indent)


def _table(name: str, columns: str) -> SchemaObject:
    return SchemaObject("table", name, f"CREATE TABLE {name} (\n{_body(columns, '    ')}\n)")


def _index(name: str, on: str, unique: bool = False) -> SchemaObject:
    unique_sql = "UNIQUE " if unique else ""
    return SchemaObject("index", name, f"CREATE {unique_sql}INDEX {name} ON {on}")


def _view(name: str, select: str) -> SchemaObject:
    return SchemaObject("view", name, f"CREATE VIEW {name} AS\n{_body(select)}")


# Every object a current projection holds, in the order they are made. Nothing
# here is conditional: these statements run against a file that has nothing in
# it, and a file that holds something is asked what it holds instead.
#
# What is *not* here is an index that repeats one SQLite makes for itself. A
# UNIQUE column gets `sqlite_autoindex_<table>_<n>`, and a second index over the
# same column is another B-tree to maintain on every insert that no query ever
# reads: measured against a projection of 4,000 messages, six such indexes cost
# 23% of the file (3,670,016 bytes against 2,818,048) and not one query plan
# differs without them -- the planner was taking the autoindex either way, as
# `EXPLAIN QUERY PLAN` says in as many words: `SEARCH s USING COVERING INDEX
# sqlite_autoindex_message_sender_1`. That is paid on the write path over the
# network share this shape exists to make cheaper.
#
# It applies to a leading column too: `message_sender(message_id)` is covered by
# `sqlite_autoindex_message_sender_1(message_id, address_id)`.
SCHEMA: tuple[SchemaObject, ...] = (
    _table(
        "mailbox",
        """
        mailbox_id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        UNIQUE(name) ON CONFLICT IGNORE
        """,
    ),
    _table(
        "address",
        """
        address_id INTEGER PRIMARY KEY,
        address TEXT NOT NULL,
        UNIQUE(address) ON CONFLICT IGNORE
        """,
    ),
    # Not "label". Gmail has labels where IMAP has folders, and the difference is
    # how many of them a message may carry, not what they are -- so one word
    # covers both, and it is the one everybody uses.
    _table(
        "folder",
        """
        folder_id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        UNIQUE(name) ON CONFLICT IGNORE
        """,
    ),
    _table(
        "subject",
        """
        subject_id INTEGER PRIMARY KEY,
        text TEXT NOT NULL,
        UNIQUE(text) ON CONFLICT IGNORE
        """,
    ),
    _table(
        "message",
        """
        message_id INTEGER PRIMARY KEY,
        store_id TEXT NOT NULL,
        email_id TEXT,
        date TEXT,
        subject_id INTEGER,
        FOREIGN KEY(subject_id) REFERENCES subject(subject_id),
        UNIQUE(store_id) ON CONFLICT IGNORE
        """,
    ),
    # A date filter is a range over this index, so `--since` reads the days it
    # names instead of the whole table. It only works while the filter compares
    # the column itself: `substr(date, 1, 10) >= ?` cannot use it and scans.
    _index("idx_message_2", "message(date)"),
    # One row is one place: "this message was seen in that folder of that
    # mailbox". Both halves may be NULL, and both cases are real. A mailbox with
    # no folder is an archive whose history did not record one. A folder with no
    # mailbox is what an import writes -- the place is named, and the name is
    # deliberately not in the mailbox field, because that field is read as a job
    # name by the guard, by `verify` and by the catch-up. What must never happen
    # is a pairing invented to satisfy a NOT NULL.
    _table(
        "message_location",
        """
        message_id INTEGER NOT NULL,
        mailbox_id INTEGER,
        folder_id INTEGER,
        FOREIGN KEY(message_id) REFERENCES message(message_id),
        FOREIGN KEY(mailbox_id) REFERENCES mailbox(mailbox_id),
        FOREIGN KEY(folder_id) REFERENCES folder(folder_id)
        """,
    ),
    # The uniqueness has to be spelt out over IFNULL, not as a plain
    # UNIQUE(message_id, mailbox_id, folder_id): SQLite holds every NULL to be
    # distinct from every other NULL, so `INSERT OR IGNORE` would not recognise a
    # repeat of a place whose folder or mailbox is unknown -- three inserts of the
    # same folderless location give three rows. Replaying the log is meant to be
    # idempotent, and the log is replayed on every refresh.
    _index(
        "idx_message_location_1",
        "message_location(message_id, IFNULL(mailbox_id, -1), IFNULL(folder_id, -1))",
        unique=True,
    ),
    _index("idx_message_location_2", "message_location(mailbox_id)"),
    _index("idx_message_location_3", "message_location(folder_id)"),
    _table(
        "message_sender",
        """
        message_id INTEGER NOT NULL,
        address_id INTEGER NOT NULL,
        FOREIGN KEY(message_id) REFERENCES message(message_id),
        FOREIGN KEY(address_id) REFERENCES address(address_id),
        UNIQUE(message_id, address_id) ON CONFLICT IGNORE
        """,
    ),
    _index("idx_message_sender_2", "message_sender(address_id)"),
    _table(
        "message_recipient",
        """
        message_id INTEGER NOT NULL,
        address_id INTEGER NOT NULL,
        FOREIGN KEY(message_id) REFERENCES message(message_id),
        FOREIGN KEY(address_id) REFERENCES address(address_id),
        UNIQUE(message_id, address_id) ON CONFLICT IGNORE
        """,
    ),
    _index("idx_message_recipient_2", "message_recipient(address_id)"),
    # Which log files have already been folded in, by content hash (their name),
    # and which chain head of each place that added up to. No query reads either,
    # and both are part of the file's shape all the same: without them a refresh
    # cannot say what it has seen, and a reader cannot be told that the archive
    # has moved on since.
    #
    # The heads are held in plain text and not as ids. This is a statement about
    # the archive, not about the mail: a place the projection has never seen a
    # message from still has a head, and interning it would put a row in
    # `mailbox` for a mailbox no query can find anything in.
    _table(
        "applied_log",
        """
        hash TEXT PRIMARY KEY
        """,
    ),
    _table(
        "folded_head",
        """
        mailbox TEXT,
        folder TEXT,
        log TEXT
        """,
    ),
    # Same reason as in `message_location`: a place may name no mailbox.
    _index(
        "idx_folded_head_1",
        "folded_head(IFNULL(mailbox, ''), IFNULL(folder, ''))",
        unique=True,
    ),
    # Every join to the left. An inner join would drop a message the view cannot
    # complete, and a message with no readable recipient is not a rarity in an
    # archive that goes back to the nineties -- it is the group address, the
    # malformed header, the `Undisclosed recipients:;`. A view that silently
    # holds fewer messages than the archive is the worst kind of wrong here:
    # nothing about it looks like an error, and `SELECT count(*)` lies.
    _view(
        "v_messages",
        """
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
        """,
    ),
    _view(
        "v_duplicates",
        """
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
        """,
    ),
)


class IndexDatabase:
    """Open the projection as a context manager.

    `with IndexDatabase(path) as db:` yields an `IndexDatabaseConnection` and
    closes the connection on exit.

    **Opening writes nothing.** A database that is already there is opened and
    asked what it holds -- `outdated`, `missing()` and `usable` answer that, and
    the caller decides what to say about it. Making an object that is not there,
    or lifting one that is of an older shape, would put DDL on the path of every
    query that only meant to read: over a network share, rebuilding an index the
    way a database is opened costs seconds each time.

    `create=True` says the file may be a new one and its schema is to be written
    -- `db create` and nothing else. An existing database is left alone even
    then, whatever shape it turns out to be in.
    """

    def __init__(self, path: pathlib.Path | str, create: bool = False):
        self.dbconn: sqlite3.Connection | None = None
        self.client: IndexDatabaseConnection | None = None
        self.path = path
        self.create = create

    def __enter__(self) -> IndexDatabaseConnection:
        self.dbconn = connect(self.path)
        self.dbconn.execute(f"PRAGMA cache_size = -{CACHE_KIB}")
        self.client = IndexDatabaseConnection(self.dbconn)
        if self.create and self.client.is_new:
            # Before the first byte of content: a page size is fixed when a
            # database gets its first page and is ignored afterwards.
            self.dbconn.execute(f"PRAGMA page_size = {PAGE_SIZE:d}")
            self.client.create()
        return self.client

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: types.TracebackType | None,
    ) -> None:
        if self.dbconn:
            self.dbconn.close()
            self.dbconn = None
            self.client = None


class IndexDatabaseConnection(DatabaseConnection):
    """The projection's schema and the operations that fill and query it.

    `create()` writes the schema, `missing()` and `usable` say whether a file
    already there still holds it, and the rest insert messages, addresses,
    subjects and locations, interning the lookup-table values through
    per-instance id caches.
    """

    def __init__(self, dbconn: sqlite3.Connection):
        super().__init__(dbconn)
        # Which shape this file is in: what was found when it was opened, and
        # what `create()` wrote once it has. 0 for a file nobody has stamped -- a
        # fresh one, or one from before the shape was recorded at all, which is
        # what `is_new` tells apart.
        self.shape = self.schema_version()
        self.is_new = (
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

    def _rolled_back(self) -> None:
        """Forget the interned ids: the rows they name went with the rollback.

        The id is cached as soon as its row *was* inserted, and the insert may
        well be inside a larger block that is undone afterwards. What is left
        then is an id for a row that is not there, handed out as a foreign key
        to everything written next -- and nothing about the database would look
        wrong until something followed one.
        """
        for cache in (
            self._mailbox_ids,
            self._folder_ids,
            self._address_ids,
            self._subject_ids,
        ):
            cache.clear()

    def create(self) -> None:
        """Write the schema into a database that has nothing in it, and stamp it.

        The one place that writes DDL. Everything in `SCHEMA` is created outright
        -- a file that already holds objects is refused here, because making the
        missing ones would leave a database that is half of two shapes.
        """
        if not self.is_new:
            raise SchemaError("a database that already holds a projection is not created into")
        with self.transaction():
            for obj in SCHEMA:
                self.execute(obj.sql)
            self.execute(f"PRAGMA user_version = {SCHEMA_VERSION:d}")
        self.is_new = False
        self.shape = SCHEMA_VERSION

    def missing(self) -> list[SchemaObject]:
        """Which objects of `SCHEMA` this file does not have, in creation order.

        One query against `sqlite_master`, which the connection reads when it
        opens anyway. Objects beyond these are not looked at: SQLite makes its own
        indexes for a UNIQUE column, and what matters is that everything a query
        needs is there.

        An object whose name is taken by something *else* counts as missing, and
        that is the point of holding the statement against `sqlite_master.sql` and
        not only the name. `SchemaObject` keeps the statement so this list can be
        read twice -- "a check driven by a second list would go on passing a file
        that is missing whatever the list forgot" -- and comparing names alone
        threw the other half away: a `v_messages` built with the old INNER JOINs,
        or an `idx_message_location_1` without the `IFNULL` spelling, both of
        which earlier releases really did write, answered every query wrongly and
        passed as complete. SQLite stores the text as it was given, so the
        comparison is exact for anything this version wrote.

        A difference means what absence means: build it again.
        """
        present = {
            row[0]: (row[1], row[2])
            for row in self.execute("SELECT name, type, sql FROM sqlite_master")
        }
        return [obj for obj in SCHEMA if present.get(obj.name) != (obj.kind, obj.sql)]

    @property
    def usable(self) -> bool:
        """Whether a query can be run against this file and its answer believed."""
        return not self.outdated and not self.missing()

    def migrate(self) -> None:
        """Lift a projection of an older shape to the current one, in place.

        Deprecated, and empty. A projection is rebuildable from the archive, so
        the answer to a file of the wrong shape is `db create --force`; this is
        the slot for the one case where rebuilding is too expensive to ask for,
        and it holds the statements of a single lift for a single release.

        Nothing calls it, and nothing should: `SCHEMA_VERSION` moves whenever the
        shape does, so a file that would need lifting is refused as unreadable
        before anything gets this far.
        """

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
        return not self.is_new and self.shape != SCHEMA_VERSION

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
