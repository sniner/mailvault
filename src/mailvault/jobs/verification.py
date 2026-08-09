"""Check the archive for messages the server still has but the archive lacks.

A last resort, not part of the routine: a folder whose downloads partly failed
does not advance its resume point, so the next backup fetches it again. What is
left for verify are archives from older versions and mail moved into a folder
with an internal date older than the point -- the latter is what the move to
UID and delta resume points removes.

The comparison itself is `reconcile`, which the backup runner also uses when it
has to read a folder in full.
"""

from __future__ import annotations

import logging
import pathlib

from mailvault import conf
from mailvault.backend import session
from mailvault.jobs.common import JobError
from mailvault.jobs.reconcile import ArchivedPlaces, ReconcileResult, reconcile_folder
from mailvault.store import cas, heads, metalog

log = logging.getLogger(__name__)

# `verify` reports what `reconcile` found; the name is what the CLI has always
# printed and what the public API exposes.
VerifyResult = ReconcileResult


def verify(
    job: conf.JobConfig,
    store_path: pathlib.Path,
    repair: bool = False,
    compress: bool = False,
    places: ArchivedPlaces | None = None,
) -> list[VerifyResult]:
    """Check the archive for messages the server still has but the archive lacks.

    Gaps are rare by design: a folder whose downloads partly failed does not
    advance its snapshot, so the next run fetches it again, and a message that
    was never stored is never deleted from the server either. What is left are
    archives from older versions and mail moved into a folder with an internal
    date older than the snapshot. This is a last resort, not part of the routine
    -- which is why it can afford to read the archive itself rather than keep an
    index alongside it.
    """
    if job.exchange_journal:
        raise JobError(
            f"{job.name}: verify does not support 'exchange_journal' jobs, because the"
            " archive holds the unwrapped message whose Message-ID differs from the"
            " journal envelope reported by the server"
        )

    results: list[VerifyResult] = []
    log_root = store_path / metalog.DEFAULT_LOG_DIR
    heads_root = store_path / heads.DEFAULT_HEADS_DIR
    # The first thing that happens and the first thing that takes a while: the
    # whole log is read before a connection is even opened, and it announces
    # itself for that reason. `places` comes from the caller where there is a
    # run to share it across -- one archive has one log, and every job was
    # reading all of it to keep the sixth of it that is theirs.
    places = places if places is not None else ArchivedPlaces(log_root)
    with session.open_mailbox(job) as mb:
        store = cas.mail_store(store_path, compress=compress)
        folders = job.folders if job.folders else list(mb.folders())
        for folder in folders:
            try:
                results.append(
                    reconcile_folder(
                        mb,
                        store,
                        log_root,
                        heads_root,
                        places.of(job.name, folder),
                        job.name,
                        folder,
                        repair=repair,
                    )
                )
            except Exception as exc:
                log.error("%s::%s: verify failed: %s", job.name, folder, exc)
    return results
