# Changelog

## 0.4.0 (2026-07-29)

### Added

- **`ib-mailbox verify`:** compare a mailbox against its local archive and report messages that
  the server still holds but the archive is missing. Matching is done by Message-ID and only
  needs the folder listing, so checking a large mailbox takes minutes instead of the hours a
  full re-download would. With `--repair` the missing messages are fetched and added to the
  archive. Requires a job with `with_db = true`; not available for `exchange_journal` jobs,
  where the archived message and the server's journal envelope carry different Message-IDs.
  Malformed Message-IDs and the length cap Exchange applies to the ones it reports are
  accounted for, so a repaired archive verifies as complete instead of reporting the same
  messages again on every run

- **`max_retries` job option** (default 5): how often a failed request to the MS Graph API is
  retried

### Fixed

- **MS Graph backend:** transient HTTP failures (`429`, `500`, `502`, `503`, `504`, `408`) and
  connection/timeout errors are now retried with exponential backoff, honouring the
  `Retry-After` header. Previously any such failure made the affected message be skipped
  silently, which regularly cost single messages during long export runs

- **Incremental backups no longer hide failed downloads.** The snapshot date is only advanced
  when every message of a folder was archived. Previously it advanced regardless, so messages
  lost to a failed download fell outside the date filter of every later run and were never
  picked up again. Existing archives with such gaps can be repaired with `ib-mailbox verify
  --repair`

- **`ib-mailbox`:** a job that fails no longer prevents the remaining jobs of a config file
  from running

## 0.3.0 (2026-03-28)

This is a major release with several **breaking changes**. If you are upgrading
from 0.2.x, please review the sections below carefully before updating.

### Breaking changes

- **TOML is now the preferred configuration format.** YAML is still supported
  for `ib-mailbox` and `ib-archive`, but `ib-copy` now requires TOML. The TOML
  format uses a `[[job]]` array of tables instead of top-level keys per job, and
  supports a `[global]` section for shared options like `compress`. See
  `README.md` for examples.

- **Renamed CLI flag:** `--job` (job file path) is now `--config`.
  The new `--job` flag selects individual jobs by name within a config file.

- **Renamed subcommand:** `list` is now `folders` in both `ib-mailbox` and
  `ib-copy`.

- **`ib-mailbox backup` destination is now required.** It was previously
  optional with a default value.

### New features

- **MS Graph backend** (`backend = "msgraph"`): access Microsoft 365 mailboxes
  via the Graph API using OAuth2 client credentials, as an alternative to IMAP.
  Install with `uv tool install imapbackup[graph]`.

- **zstd compression:** emails can be stored compressed with zstandard.
  Use `--compress` on `ib-mailbox backup` or `ib-archive import`, or set
  `compress = true` in the `[global]` config section.

- **`ib-archive compress` / `decompress`:** retroactively compress or
  decompress all files in an existing archive.

- **`--job` filter:** run only specific named jobs from a multi-job config
  file with `ib-mailbox --job NAME backup ...` (repeatable).

- **Environment variable expansion** in config values: `${VAR}` and
  `${VAR:-default}` syntax.

- **Command substitution** for any config field via `*_cmd` variants
  (e.g. `password_cmd = "pass show email/example"`). Requires `--allow-exec`
  on the command line to prevent unintended command execution.

- **Incremental backup** with `with_db` and `incremental` options: only
  download messages added since the last run (enabled by default).

- **Human-readable sizes** in `ib-archive stats` output.

- **Progress logging** every 100 messages during folder backup.

### Bug fixes

- Fix `message_recipient` database indexes pointing to the wrong table.
- Fix `folders()` yielding raw tuples instead of folder name strings.
- Fix variable shadowing of the imported `jobs` module in CLI.
- Fix snapshot timestamps using local time instead of UTC.
- Remove broken `get_message()` SQL query from the database layer.

### Improvements

- Introduced `MailboxClient` protocol to allow multiple backends (IMAP,
  MS Graph) behind a common interface.
- Extracted shared folder iteration logic into `_iter_folder`.
- Narrowed exception handling in IMAP operations and file I/O.
- Added exponential backoff to `ib-copy --idle` reconnect loop.
- TLS warnings logged when hostname check or certificate verification
  is disabled.
- Centralized `setup_logger` into `cli/__init__.py`, including suppression
  of verbose third-party loggers (httpx, msal, imapclient).
- Added type annotations throughout the codebase.
- Comprehensive test suite for CAS, database, config loading, and mail
  utilities.
