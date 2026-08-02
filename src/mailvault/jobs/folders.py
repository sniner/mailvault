"""List the folders of a mailbox -- the `folders` command."""

from __future__ import annotations

from mailvault import conf
from mailvault.backend import session


def folder_list(job: conf.JobConfig) -> None:
    with session.open_mailbox(job) as mb:
        for folder in mb.folders():
            print(f"{job.name}::{folder}")
