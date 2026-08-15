"""The SQLite plumbing both databases in this program sit on.

Connecting, and a reentrant `transaction()` block. Nothing here knows a table
name: this is what is left over once the projection (`mailvault.store.index_db`)
and the reader of the legacy `store.db` (`mailvault.legacy.store_db`) are told
apart, and it is deliberately all they have in common. Anything that names a
column belongs on one side or the other.
"""

from __future__ import annotations

import collections.abc
import logging
import pathlib
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any

log = logging.getLogger(__name__)


class RollbackException(Exception):
    pass


def connect(path: pathlib.Path | str) -> sqlite3.Connection:
    """Open a database file, with rows that can be read by column name."""
    dbconn = sqlite3.connect(path, check_same_thread=False)
    dbconn.row_factory = sqlite3.Row
    return dbconn


class DatabaseConnection:
    """A SQLite connection with a reentrant `transaction()` block.

    Nested `transaction()` calls share one outermost commit, so each write helper
    can wrap itself in a transaction and still batch when a caller wraps a whole
    run in one. `rollback()` aborts the enclosing block by raising.
    """

    def __init__(self, dbconn: sqlite3.Connection):
        self.dbconn = dbconn
        self.lock = threading.RLock()
        self._transaction = 0

    @contextmanager
    def transaction(self) -> collections.abc.Generator[DatabaseConnection, None, None]:
        with self.lock:
            outer = self._transaction == 0
            self._transaction += 1
            try:
                yield self
                if outer:
                    self.dbconn.commit()
            except RollbackException:
                # Not a failure and not reported as one: `rollback()` is how a
                # caller asks for the block to be undone, and raising is how it
                # asks. What stood here was three ERROR lines for a deliberate
                # abort -- one per nesting level -- each of them saying
                # "Transaction failed:" and then nothing, because the exception
                # carries no message.
                if outer:
                    self.dbconn.rollback()
                raise
            except Exception:
                # Once, from the outermost block, and with the stack: an inner
                # block that re-raises would otherwise report the same failure
                # again at every level on the way out, and for an exception
                # nobody has diagnosed the traceback is the only clue there is.
                if outer:
                    log.exception("Transaction failed")
                    self.dbconn.rollback()
                raise
            finally:
                self._transaction -= 1

    def execute(self, statement: str, *args: Any) -> sqlite3.Cursor:
        with self.lock:
            return self.dbconn.execute(statement, *args)

    def commit(self) -> None:
        with self.lock:
            if self._transaction == 0:
                self.dbconn.commit()

    def rollback(self) -> None:
        """Abort the enclosing ``transaction()`` block.

        This does not roll back directly; it raises ``RollbackException``, which
        propagates out of the ``with transaction()`` block and makes it issue the
        actual ``dbconn.rollback()``. Only meaningful inside such a block --
        called on its own it simply raises.
        """
        raise RollbackException()
