# Changelog

## Unreleased

### Changed

- **Compression** uses the standard-library `zstd` module on Python 3.14+ (PEP 784); the
  `zstandard` package is now only required on older interpreters

### Fixed

- **`delete_after_export`** removed a message from the server before its location was written to
  the log. A crash or a failed log write in that window left the archived `.eml` with no record of
  where it was seen -- and, the server copy being gone, no way to recover it. A message is now
  deleted only after the folder's metadata log has been sealed to disk; a seal that fails holds the
  deletion back entirely, and the messages are re-fetched (and deduplicated) next run

- **Incremental snapshots** no longer advance when the location log could not be written. A folder
  whose downloads were clean but whose log did not reach disk is re-fetched next run and recorded
  again, rather than being skipped for good with its locations lost

## 0.8.0 (2026-08-02)

### Breaking changes

- **An archive no longer contains a database.** `store.db` held the only record of which
  mailbox and folder each message was seen in, and it is a SQLite file rewritten in place --
  over SMB or NFS a torn write can take the whole thing with it. That record now lives in an
  append-only log inside the archive, and a backup writes nothing that is modified in place.

  Existing archives are migrated automatically at the start of the next backup, or explicitly
  with `mailvault archive migrate <archive>`. Nothing is deleted: the database is renamed to
  `store.db.migrated` and no longer used, so it remains as a fallback until you remove it. The
  migration is idempotent and can be repeated.

- **`archive rebuild-db` is now `archive create-db <archive> <database>`**, and the destination
  is an argument rather than a fixed place inside the archive. It is not a rebuild -- the
  archive has no database to restore -- it builds one wherever you want it, out of the archived
  messages and the log. What it produces is a snapshot: accurate when built, stale from the
  next backup onwards. Build it again when that matters.

  An existing file is refused unless `--force` is given, and `--force` replaces rather than
  adds to it. The database is built through a temporary file beside the target, so an
  interrupted run leaves the previous one intact.

- **The job option `with_db` is gone.** It existed because of the SQLite database:
  106 MB rewritten in place on every run was worth switching off. What remains in
  its place is a few kilobytes of immutable files, while turning it off would
  disable incremental backups and `verify` -- which is not what anyone wants from
  an option about metadata. A configuration that still sets it says so on load,
  rather than having the field quietly dropped as if it were a typo

### Added

- **`meta/`, an append-only record of where each message was seen.** One file is one place:
  its header names a mailbox and a folder, its lines name the messages seen there. A message
  belonging to several places appears in several files and is never ambiguous. Like the mail
  itself it is content-addressed, so every file carries its own integrity check -- `sha384sum`
  against the filename settles it, with no knowledge of the format required

- **`state.json`, where the next incremental run picks up.** The per-folder timestamps that
  used to live in the database, replaced only atomically, so an interrupted or torn write
  cannot destroy them

- **`archive migrate <archive>`** moves an older archive off its database, as described above

### Changed

- **`verify` no longer needs a database.** It reads which messages are archived for a folder
  from the log and their Message-IDs from the messages themselves

- **Gmail folders are taken from `X-GM-LABELS` alone.** The IMAP folder name is a localised
  view of what Gmail already reports canonically -- `[Google Mail]/Gesendet` is `\Sent` -- so
  recording both stored one place twice, in a spelling that differed per account. A message
  carrying no label of its own is now recorded as being in `\All`; previously it was archived
  with no location at all

- **Reading a message's headers no longer reads the message.** Anything that only needs headers
  stops at the blank line, which on a real archive is one to five per cent of the bytes

### Fixed

- **A message whose headers could not be parsed was dropped from the metadata entirely.** An
  exception while *reading* a field was treated as a failure to *store* the message, though it
  was already archived -- so it got no record at all, stayed invisible to `verify`, and froze
  the folder's snapshot in a way no retry could clear. Reading a field can no longer fail the
  message, and the extraction of dates, subjects, Message-IDs and addresses reports and yields
  nothing instead of raising

- **Group addresses were recorded as an empty recipient.** `To: Undisclosed recipients:;` is
  legal RFC 5322, and the classic address parser reports it as an empty address, which reached
  the database as if it were one. Addresses are now read through the policy that understands
  groups

- **Dates that could not be parsed are recovered where that is unambiguous:** a header encoded
  whole as an RFC 2047 word, a timezone glued to the time, a UTC offset beyond 24 hours. What
  still cannot be read is stored as unknown rather than guessed at

- **Labels added to a message were lost when nothing else was written afterwards**, because the
  rows were left uncommitted in the expectation that a later call would commit them

- **A resume timestamp without a timezone could make a Microsoft 365 job skip mail.** Older
  versions wrote local time without saying so; the Graph filter stamped it `Z`, so it was read
  as UTC — one or two hours later than meant, and the mail that arrived in between was passed
  over once and never looked at again. Such a timestamp is now read as the local time it is,
  and the filter converts to UTC before labelling it as UTC. IMAP was never affected, since it
  compares whole days with a day to spare

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
