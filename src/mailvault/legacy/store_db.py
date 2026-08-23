"""Reading the `store.db` of an archive written before 0.8.0.

Back then the archive kept its metadata in SQLite, and that database *was* the
truth: which mailbox and folder a message was seen in existed nowhere else. It
is read exactly once per archive, by `mailvault.jobs.migration`, which writes
what it finds into the metadata log and then renames the file to
`store.db.migrated`.

Only what the migration needs, and only reading. Nothing here creates a schema
and nothing inserts: the format is never written again, a database that is about to be set
aside should not be written to, and asking for write access to read it would be
worse than pointless. A specimen for the tests is built by the test suite, from
a schema frozen there -- see `tests/legacy_store_db.py`.

Everything here tolerates a database older than the tables it asks about. These
files were written by versions nobody has a copy of any more, and a missing
table has to mean "nothing recorded" rather than a traceback in the middle of a
migration.
"""

from __future__ import annotations

import collections.abc
import logging
import pathlib
import sqlite3
import types

from mailvault.store.sqlite import DatabaseConnection, connect

log = logging.getLogger(__name__)

# The legacy database filename. Archives no longer keep a database inside them;
# this is the name the migration looks for and renames to `store.db.migrated`.
DEFAULT_DB_NAME = "store.db"


class StoreDatabase:
    """Open a legacy `store.db` for reading, as a context manager.

    `with StoreDatabase(path) as db:` yields a `StoreDatabaseConnection` and
    closes the connection on exit. Nothing is created and nothing is written.
    """

    def __init__(self, path: pathlib.Path | str):
        self.dbconn: sqlite3.Connection | None = None
        self.client: StoreDatabaseConnection | None = None
        self.path = path

    def __enter__(self) -> StoreDatabaseConnection:
        self.dbconn = connect(self.path)
        self.client = StoreDatabaseConnection(self.dbconn)
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


class StoreDatabaseConnection(DatabaseConnection):
    """The four questions the migration asks a legacy database."""

    def iter_messages(self) -> collections.abc.Iterator[tuple[int, str]]:
        """Yield (message_id, store_id) for every message the database knows."""
        for row in self.execute("SELECT message_id, store_id FROM message"):
            yield row[0], row[1]

    def message_mailboxes(self) -> dict[int, list[str]]:
        """Map message_id to the mailboxes it was seen in."""
        return self._attribution(
            "SELECT mm.message_id, mb.name FROM message_mailbox mm "
            "JOIN mailbox mb USING (mailbox_id)"
        )

    def message_labels(self) -> dict[int, list[str]]:
        """Map message_id to the labels it carries."""
        return self._attribution(
            "SELECT ml.message_id, l.name FROM message_label ml JOIN label l USING (label_id)"
        )

    def _attribution(self, statement: str) -> dict[int, list[str]]:
        """Collect a (message_id, name) query into a mapping of lists."""
        result: dict[int, list[str]] = {}
        for message_id, name in self.execute(statement):
            result.setdefault(message_id, []).append(name)
        return result

    def all_snapshots(self) -> list[tuple[str, str, str]]:
        """Return (mailbox, folder, timestamp) for every snapshot in the database.

        Read once per archive, to carry the resume timestamps of a legacy archive
        over as a record of when a folder was last read -- never as a point to
        carry on from, which is the whole reason the format moved. A database old
        enough to predate the table answers with nothing rather than raising.
        """
        try:
            rows = self.execute(
                "SELECT mb.name, l.name, s.date FROM snapshot s "
                "JOIN mailbox mb USING (mailbox_id) JOIN label l USING (label_id)"
            ).fetchall()
        except sqlite3.OperationalError as exc:
            log.debug("No snapshot table to read: %s", exc)
            return []
        return [(row[0], row[1], row[2]) for row in rows]
