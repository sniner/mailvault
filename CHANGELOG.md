# Changelog

## Unreleased

### Added

- **The archive now carries an append-only metadata log under `meta/`.** It records which
  mailbox and which folder each message was seen in -- the only part of the metadata database
  that cannot be recovered from the archived `.eml` files, since subject, sender, recipients
  and date are all in the message itself. One file per folder per run, written once and never
  modified, so a damaged database no longer means losing that attribution permanently

- **`mailvault archive bootstrap-log <archive>`** exports the mailbox and folder attribution
  of an existing metadata database into the log, so archives created by earlier versions are
  protected. This also runs automatically at the start of the next backup when an archive has
  no log yet. Use `--force` to export again when a log already exists

- **The snapshot state of an archive is now also kept in `store.json`**, next to `store.db`.
  It holds the per-folder timestamps that decide where the next incremental run resumes, and
  is only ever replaced atomically, so an interrupted or torn write cannot destroy it. The
  metadata database is a SQLite file rewritten in place, which is a real hazard on SMB and NFS
  shares; the state file is what makes an incremental backup survive a damaged database

### Changed

- **An incremental run reads its start date from `store.json` when that file knows the folder**,
  and falls back to the metadata database otherwise. Existing archives therefore need no
  migration: the first run after upgrading adopts *every* timestamp already in the database --
  not only the folders that run happens to visit -- so the state file is complete straight
  away and nothing is re-fetched. A state file that already holds something is never
  overwritten from the database

- **A snapshot state file that cannot be written no longer aborts the run.** The problem is
  logged and the backup continues, because the database still holds the timestamp and the
  archived mail is unaffected

- **`archive rebuild-db` now applies the metadata log** and reports what it restored. Without
  a log it says so plainly, because the rebuilt database then lacks the mailbox and folder
  attribution that `verify` compares against

### Fixed

- **Labels added to a message were lost when nothing else was written afterwards.**
  `add_message_labels` left its rows uncommitted and relied on a later call to commit them, so
  the labels of the last message written on a connection could be dropped silently

## 0.7.0 (2026-08-01)

### Breaking changes

- **YAML configuration files are no longer supported.** TOML is the only configuration format;
  `pyyaml` is gone from the dependencies. A YAML config now fails with a clear parse error
  instead of being loaded. Migration: convert the flat `job-name: {...}` mapping into one
  `[[job]]` table per job, with the former key as `name`

### Changed

- **A broken or missing configuration file now reports a single error line** naming the file
  and the problem, instead of dumping a traceback. Errors from the mailbox connection itself
  are still logged in full

- **The configuration file name no longer matters:** its content is always parsed as TOML, so a
  config may be called `backup.job` or anything else. `copy` previously rejected every file not
  named `*.toml` -- that restriction is gone, and `copy` accepts the same files as every other
  command

- **`mailvault` is now on PyPI:** install it with `uv tool install mailvault` (or `pipx install`
  / `pip install`) instead of pulling from the Git repository. The old name `imapbackup` was
  already taken on PyPI; `mailvault` was free, which is part of why the project was renamed.
  The package now carries the metadata a PyPI release needs (README as description,
  GPL-3.0-or-later license, project URLs) and is built with the `uv_build` backend

## 0.6.0 (2026-08-01)

### Breaking changes

- **The project was renamed from `imapbackup` to `mailvault`.** Same tool, new name -- the
  PyPI name `imapbackup` was taken and the project now covers more than IMAP. The import
  package is renamed `imapbackup` -> `mailvault`. To keep the old `ib-*` commands, pin to the
  last pre-rename release, v0.5.0

- **A single `mailvault` command replaces the three `ib-mailbox` / `ib-archive` / `ib-copy`
  tools.** The subcommands map directly:
  - `ib-mailbox folders|backup|verify` -> `mailvault folders|backup|verify`
  - `ib-copy copy [--idle]` -> `mailvault copy [--idle]`; `ib-copy folders` -> `mailvault copy --list-folders`
  - `ib-archive <sub>` -> `mailvault archive <sub>`, with `db-from-archive` renamed to `rebuild-db`

  Global options are unified across all commands (`-v/--verbose`, `-q/--quiet`, `--log-file`,
  `--config`, `--allow-exec`, `--job`) and must precede the command. The Windows build now
  ships a single `mailvault.exe` instead of three `ib-*.exe`

### Changed

- **The Microsoft Graph backend is now a core dependency** (`msal`, `httpx`), no longer an
  optional `graph` extra. Microsoft 365 is a first-class mailbox source, so `mailvault` always
  ships with Graph support. The `mailvault[graph]` install variant no longer exists -- install
  `mailvault` plainly

- **Job configuration is validated up front:** an unknown `backend` value, or a `msgraph` job
  missing `tenant_id` / `client_id` / `client_secret`, now fails immediately with a clear error
  naming the job. Previously an unknown backend silently fell back to IMAP and missing Graph
  credentials only surfaced deep in the backend

- **`copy --idle`** now reports a clear error when the source mailbox is not an IMAP backend,
  instead of failing later with an internal error. IDLE is an IMAP-only feature

### Fixed

- **Message-ID matching:** a malformed Message-ID such as `<>` no longer crashes a run on
  Python 3.11/3.12, where CPython's email header parser raises `IndexError` on such values.
  The value is now treated as unusable (empty key), consistent with the behaviour on 3.13+

## 0.5.0 (2026-07-31)

### Breaking changes

- **Minimum Python is now 3.11** (was 3.10). TOML configuration relies on the standard-library
  `tomllib`, which only exists from 3.11 on; on 3.10 TOML configs never worked and only YAML was
  usable. Dropping 3.10 removes that split. Migration: run `imapbackup` under Python 3.11 or newer

### Fixed

- **`ib-archive`:** `stats`, `addresses` and `import` now see zstd-compressed archives. Previously
  they only matched plain `.eml` files, so an archive written with `--compress` (files ending in
  `.eml.zst`) was reported as empty and could not be used as an import source
- **`ib-copy`:** a configuration in which a job omits `role` no longer crashes the role lookup
- **`ib-mailbox` (exchange_journal + delete_after_export):** a message that is not a journal item
  is no longer deleted from the server while being skipped. Without an `error_folder` such a
  message is now kept on the server instead of being removed unarchived

### Changed

- **CLI:** `ib-mailbox`, `ib-archive` and `ib-copy` now exit with a non-zero status when a job or
  operation fails, so cron jobs and scripts can detect failures
- **CLI:** all three tools gained a `--version` flag
- **`ib-archive`:** the default log level is now `INFO`, consistent with `ib-mailbox` and
  `ib-copy` (was `WARNING`)

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
