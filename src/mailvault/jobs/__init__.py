"""Job commands, one module each: backup, verify, migrate, create-db, folders.

The package presents them as a flat API, so callers keep using ``jobs.backup(...)``
and friends. Pieces shared between commands live in ``common``.
"""

from __future__ import annotations

from mailvault.jobs.backup import backup
from mailvault.jobs.check import CheckResult, check
from mailvault.jobs.common import JobError
from mailvault.jobs.db import (
    DEFAULT_QUERY_DB_NAME,
    Freshness,
    RebuildResult,
    RefreshResult,
    ReplayResult,
    SearchHit,
    SearchQuery,
    create_db,
    drop_db,
    freshness,
    refresh_db,
    search,
)
from mailvault.jobs.folders import folder_list
from mailvault.jobs.init import InitResult, init_archive
from mailvault.jobs.migration import MigrationResult, migrate_archive
from mailvault.jobs.reconcile import ArchivedPlaces
from mailvault.jobs.verification import VerifyResult, verify

__all__ = [
    "DEFAULT_QUERY_DB_NAME",
    "ArchivedPlaces",
    "CheckResult",
    "Freshness",
    "InitResult",
    "JobError",
    "MigrationResult",
    "RebuildResult",
    "RefreshResult",
    "ReplayResult",
    "SearchHit",
    "SearchQuery",
    "VerifyResult",
    "backup",
    "check",
    "create_db",
    "drop_db",
    "folder_list",
    "freshness",
    "init_archive",
    "migrate_archive",
    "refresh_db",
    "search",
    "verify",
]
