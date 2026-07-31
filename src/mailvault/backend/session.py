"""Backend-agnostic mailbox session.

`open_mailbox()` is the single entry point the job runner uses to talk to a
mailbox, regardless of whether it is IMAP or MS Graph. It picks the backend from
`job.backend`, opens the connection, and closes it again on exit -- so neither
`jobs.py` nor the concrete backends need to know about each other.
"""

from __future__ import annotations

import collections.abc
import contextlib

from mailvault import conf
from mailvault.backend.base import MailboxClient
from mailvault.backend.graph import MSGraphClient
from mailvault.backend.imap import ImapClient


def create_client(job: conf.JobConfig) -> MailboxClient:
    """Build (and connect) the mailbox client for `job`'s backend."""
    job.validate()
    if job.backend == "msgraph":
        return MSGraphClient(job)
    return ImapClient.connect(job)


@contextlib.contextmanager
def open_mailbox(job: conf.JobConfig) -> collections.abc.Iterator[MailboxClient]:
    """Yield a connected mailbox client for `job`, closing it on exit."""
    client = create_client(job)
    try:
        yield client
    finally:
        client.close()
