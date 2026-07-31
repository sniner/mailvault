"""Backend-agnostic mailbox interface.

The shared types every mailbox backend (IMAP, MS Graph, ...) speaks in. Kept in
a module of its own so that both the backends and the job runner can depend on
them without the backends having to import each other.
"""

from __future__ import annotations

import collections.abc
import dataclasses
from datetime import datetime
from typing import Any, Protocol

from mailvault.store import cas


@dataclasses.dataclass
class BackupResult:
    """Outcome of a folder backup.

    `failed` counts messages that were seen on the server but could not be
    stored locally. A run with failures is incomplete, so the caller must not
    advance the incremental snapshot — otherwise those messages would fall
    outside the date filter of every future run and stay lost for good.
    """

    total: int = 0
    stored: int = 0
    failed: int = 0

    @property
    def complete(self) -> bool:
        """True if every message seen on the server was accounted for."""
        return self.failed == 0


@dataclasses.dataclass(frozen=True)
class MessageRef:
    """Reference to a message on the server, without its content.

    `msg_id` is backend-specific (IMAP UID, Graph message id) and only valid
    together with the folder it was listed from.
    """

    msg_id: Any
    message_id: str
    date: datetime | None = None


class MailboxClient(Protocol):
    """Protocol defining the interface for mailbox backends.

    Each backend (IMAP, MS Graph, ...) must implement these methods so that
    the job runner in ``jobs.py`` can treat them interchangeably.
    """

    job_name: str

    def folders(self) -> collections.abc.Generator[str, None, None]: ...

    def folder_backup(
        self,
        folder_name: str,
        store: cas.ContentAddressedStorage,
        since: datetime | None = ...,
        callback: collections.abc.Callable[[dict], None] | None = ...,
    ) -> BackupResult: ...

    def message_index(
        self,
        folder_name: str,
        since: datetime | None = ...,
    ) -> collections.abc.Generator[MessageRef, None, None]: ...

    def fetch_message(self, msg_id: Any, folder_name: str) -> bytes: ...

    def full_backup(
        self,
        store: cas.ContentAddressedStorage,
        since: datetime | None = ...,
        callback: collections.abc.Callable[[dict], None] | None = ...,
    ) -> None: ...

    def get_messages(
        self,
        folder_name: str,
        since: datetime | None = ...,
    ) -> collections.abc.Generator[tuple[Any, datetime | None, bytes], None, None]: ...

    def save_message(
        self,
        msg: bytes,
        folder_name: str,
        date: datetime | None = ...,
    ) -> None: ...

    def move_message(self, msg_id: Any, folder_name: str) -> None: ...

    def delete_message(self, msg_id: Any, expunge: bool = ...) -> None: ...
