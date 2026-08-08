"""Job commands, one module each: backup, verify, migrate, create-db, folders.

The package presents them as a flat API, so callers keep using ``jobs.backup(...)``
and friends. Pieces shared between commands live in ``common``.
"""

from __future__ import annotations

from mailvault.jobs.backup import backup
from mailvault.jobs.check import CheckResult, check
from mailvault.jobs.common import JobError
from mailvault.jobs.folders import folder_list
from mailvault.jobs.init import InitResult, init_archive
from mailvault.jobs.migration import MigrationResult, migrate_archive
from mailvault.jobs.storedb import RebuildResult, ReplayResult, create_db
from mailvault.jobs.verification import VerifyResult, verify

__all__ = [
    "CheckResult",
    "InitResult",
    "JobError",
    "MigrationResult",
    "RebuildResult",
    "ReplayResult",
    "VerifyResult",
    "backup",
    "check",
    "create_db",
    "folder_list",
    "init_archive",
    "migrate_archive",
    "verify",
]
