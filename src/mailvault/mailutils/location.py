"""Where a message was seen, as the backup hands it on.

The one record here that is not read out of a message: it says which mailbox and
which folders a message turned up in, which is precisely what the message itself
cannot say.
"""

from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True)
class MessageMetadata:
    """Where an archived message was seen: its store id and the folders it is in.

    Produced by :func:`metadata` and handed to the backup's callback, which
    records it in the metadata log. That location is the only thing a backup needs
    to write about a message -- subject, sender and date are in the message itself.
    ``folders`` is the set of places it is in: one for IMAP, possibly several for
    Gmail, which reports every label a message carries no matter which folder it
    was fetched from. It may hold ``bytes`` as well as ``str`` (Gmail reports
    label names as raw bytes), so it is deliberately not annotated ``list[str]``.
    """

    mailbox: str
    store_id: str
    folders: list


def metadata(
    mailbox: str,
    folder: str,
    store_id: str,
    folders: list | None = None,
) -> MessageMetadata:
    """Build the location record a backend hands to the backup callback.

    `folders`, when given, is the exact set of places the message is in (Gmail
    labels); otherwise it defaults to the single `folder` it was fetched from.
    Nothing is read out of the message -- the log records only the location.
    """
    return MessageMetadata(
        mailbox=mailbox,
        store_id=store_id,
        folders=folders if folders is not None else [folder],
    )
