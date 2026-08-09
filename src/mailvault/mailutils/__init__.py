"""Reading fields out of a raw email: headers, dates, addresses, Message-ID.

Everything the rest of mailvault needs to know about a message is parsed here,
from bytes -- subject, sender and recipients, date, Message-ID -- along with the
`MessageMetadata` record a backup carries and the unwrapping of Exchange journal
envelopes. The archive stores the message itself, so these are read back out of
it on demand rather than kept in a database.

The package is that job cut into the pieces it consists of, and callers see none
of the seam: `mailutils.date(msg)` is where it has always been.

- `reading` -- bytes in, a parsed message out. The way in
- `headers` -- one field at a time, tolerating what three decades of mail
  programs have put in them
- `dates` -- the Date header, which arrives broken often enough to need a
  strategy of its own
- `location` -- where a message was seen, the one record here not read out of a
  message
- `journal` -- Exchange journal envelopes, and the bounce that wraps them again
"""

from __future__ import annotations

from mailvault.mailutils.dates import date
from mailvault.mailutils.headers import (
    addresses,
    message_id,
    normalize_message_id,
    subject,
)
from mailvault.mailutils.journal import unwrap_exchange_journal_item
from mailvault.mailutils.location import MessageMetadata, metadata
from mailvault.mailutils.reading import decode_email, decode_email_header

__all__ = [
    "MessageMetadata",
    "addresses",
    "date",
    "decode_email",
    "decode_email_header",
    "message_id",
    "metadata",
    "normalize_message_id",
    "subject",
    "unwrap_exchange_journal_item",
]
