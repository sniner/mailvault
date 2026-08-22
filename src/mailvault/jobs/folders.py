"""List the folders of a mailbox -- the `folders` command."""

from __future__ import annotations

from mailvault import conf
from mailvault.backend import session


def folder_list(job: conf.JobConfig) -> list[str]:
    """The folders this job's mailbox holds, in the order the server names them.

    The names come back rather than going out, because how a folder is written
    for a reader -- `job::folder`, and whether that is the answer or a line of a
    report -- is not a decision a job has any way of making. The command that
    was asked for them puts them into words.
    """
    with session.open_mailbox(job) as mb:
        return list(mb.folders())
