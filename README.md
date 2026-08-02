# mailvault -- Back up and archive email from IMAP and Microsoft 365 mailboxes

> [!IMPORTANT]
> **0.8.0 changes how an archive stores its metadata.** The SQLite database that
> used to live inside the archive is gone; what it knew is now kept in files that
> are only ever written once or replaced atomically.
>
> **Your existing archive keeps working and needs nothing from you.** The next
> backup migrates it automatically, and nothing is deleted — the old database is
> renamed to `store.db.migrated` and left where it is, so you can go back to it
> until you are satisfied and remove it yourself.
>
> **If you queried that database with SQL, read this.** There is no longer a
> database inside the archive to point your tools at. You build one from the
> archive whenever you need it, wherever you want it — or set `--index-db` (or
> `index_db = true` in the config) to maintain a fresh `index.db` beside the
> archive after every backup. Either way it is a snapshot of the archive, not
> part of it — see [Querying an archive with SQL](#querying-an-archive-with-sql).
>
> See the [CHANGELOG](CHANGELOG.md) for the full list.


## Why an archive holds no database

The archive is often the only copy of your mail — that is the point of the tool:
it takes mail *out* of a mailbox. So the interesting question is not what happens
in the good case, but what happens when a write goes wrong.

Message files are safe by construction. They are named after the hash of their
content, never modified, and never written twice; a half-written one is
recognizable and can be thrown away without touching anything else.

An SQLite database is the opposite. It rewrites pages **in place** and relies on
`fsync` behaving. Over SMB or NFS `fsync` is not reliably honoured, so a torn
write does not cost you the last change — it can cost you the file. And that file
held the one thing the messages themselves cannot tell you: which mailbox and
which folder each message was seen in. Subject, sender, recipients and date are
all in the message; that single fact was not, and losing it meant losing it for
good.

So a backup no longer writes anything that is modified in place. Everything it
records is either immutable or replaced atomically, and the database became what
it should have been all along: a tool you build when you want to run a query, not
part of the archive.


## Installation

After years of Python packaging being an adventure in its own right --
virtualenvs, pip, pipx, setup.py, setuptools, poetry, and whatever else came
and went -- [uv](https://docs.astral.sh/uv/) has finally brought sanity to the
table. The recommended way to install `mailvault` is from
[PyPI](https://pypi.org/project/mailvault/):

```console
$ uv tool install mailvault
```

This installs the `mailvault` command into an isolated environment and makes it
available on your `PATH` -- no manual virtualenv juggling required. Support for
Microsoft 365 mailboxes via MS Graph is built in; no extra is needed. If you
prefer other tooling, `pipx install mailvault` or `pip install mailvault` work
just as well.

To pin a specific release, or to install the current development state straight
from the repository:

```console
$ uv tool install mailvault==0.8.0
$ uv tool install git+https://github.com/sniner/mailvault
```

Wheels and pre-compiled Windows executables are also available on the
[GitHub Releases](https://github.com/sniner/mailvault/releases) page.


## Overview

`mailvault` is a single command with several subcommands:

* `mailvault folders | backup | verify` -- back up IMAP or Microsoft 365
  mailboxes to a local archive and verify the result
* `mailvault archive ...` -- manage the local email archive (import, compress,
  statistics, build a database for querying, etc.)
* `mailvault copy` -- copy emails between IMAP mailboxes (experimental)

Global options (`--config`, `-v/--verbose`, `-q/--quiet`, `--log-file`,
`--allow-exec`, `--job`) are given **before** the command.


## Backing up mailboxes

`mailvault backup` downloads emails from one or more mailboxes and stores them
in a local content-addressed archive. The backup can be repeated at regular
intervals without creating duplicates, as long as you always export from the
same mailbox.

A configuration file defines the accounts and options for the backup job
(see [Configuration file](#configuration-file) below).

First, you may want to get an overview of all available folders:

```console
$ mailvault --config example.toml folders
example.org::Trash
example.org::Archive
example.org::Archive/2022
example.org::Archive/2021
example.org::Archive/2020
example.org::INBOX
```

Then run the backup:

```console
$ mailvault --config example.toml backup ./backup
2024-08-15 10:05:52,275 INFO -- START
2024-08-15 10:05:52,276 INFO -- Processing mailbox: example.org
2024-08-15 10:05:52,527 INFO -- example.org::INBOX: found 3 messages
2024-08-15 10:05:52,799 INFO -- example.org::INBOX[1]: NEW: id=25652e390168...a234
2024-08-15 10:05:52,799 INFO -- example.org::INBOX[2]: NEW: id=fa1f63a13f91...c9ee
2024-08-15 10:05:52,799 INFO -- example.org::INBOX[3]: NEW: id=800be881dc38...7fa8
```

On subsequent runs, already archived messages are recognized and skipped:

```console
$ mailvault --config example.toml backup ./backup
2024-08-15 10:09:28,248 INFO -- START
2024-08-15 10:09:28,250 INFO -- Processing mailbox: example.org
2024-08-15 10:09:28,531 INFO -- example.org::INBOX: found 3 messages
2024-08-15 10:09:28,820 INFO -- example.org::INBOX[1]: EXISTS: id=25652e390168...a234
2024-08-15 10:09:28,820 INFO -- example.org::INBOX[2]: EXISTS: id=fa1f63a13f91...c9ee
2024-08-15 10:09:28,820 INFO -- example.org::INBOX[3]: EXISTS: id=800be881dc38...7fa8
```

Use `--compress` to store emails compressed with zstd. Use `--job NAME` to run
only specific jobs from the configuration file.

### Verify and repair

A backup run can lose individual messages: the server may answer a single
download with a `504 Gateway Timeout` or drop the connection, and that message
is skipped. With `incremental = true` the gap is invisible to every later run,
because the message is older than the snapshot date and thus filtered out.

`verify` compares the mailbox against the archive and reports what is missing:

```console
$ mailvault --config example.toml verify ./backup
example.org::INBOX: 77,592 on server, 43 not archived
example.org: 43 message(s) missing, run again with --repair
```

The comparison only lists the folder's message headers, which costs a handful
of requests instead of one download per message — checking a large mailbox
takes minutes, not hours. With `--repair` the missing messages are downloaded
and added to the archive:

```console
$ mailvault --config example.toml verify --repair ./backup
example.org::INBOX: 77,592 on server, 43 not archived, 43 restored
example.org: 43 of 43 message(s) restored
```

Messages are matched by their `Message-ID`. A message whose `Message-ID` is
missing or ambiguous is treated as not archived and fetched again, which is
harmless: the content-addressed storage recognizes the duplicate and discards
it. `verify` does not support `exchange_journal` jobs, because there the archived
message and the server's journal envelope carry different `Message-ID`s.

`verify` is a last resort, not routine. A folder whose downloads partly failed
does not advance its snapshot, so the next ordinary backup fetches it again by
itself. What is left for `verify` are archives from older versions and mail moved
into a folder with an internal date older than the snapshot.


## Managing the archive

`mailvault archive` provides several subcommands for working with the local
archive.

### Import emails

Import existing `.eml` files into the archive. For example, to consolidate
emails from `./my_mails` into `./backup`:

```console
$ mailvault --verbose archive import ./my_mails ./backup
```

Use `--move` to remove source files after import, `--compress` to store them
compressed, and `--docuware` if the source is a Docuware email archive.

### Statistics

Show the number of emails and total size of an archive:

```console
$ mailvault archive stats ./backup
./backup: 1,234 emails, 567.8 MiB total
```

### Compress / Decompress

Retroactively compress all uncompressed files in an archive with zstd, or
revert compressed files back to plain `.eml`:

```console
$ mailvault archive compress ./backup
./backup: 1,234 files compressed, 0 already compressed

$ mailvault archive decompress ./backup
./backup: 1,234 files decompressed, 0 already plain
```

### Email addresses

List all sender and recipient addresses found in the archive:

```console
$ mailvault archive addresses ./backup
```

### Querying an archive with SQL

The archive itself holds no database. To run SQL against it, build one:

```console
$ mailvault archive create-db ./backup ./backup.db
./backup: 130,997 message(s) read from the archive
./backup: metadata log: 60 file(s), 219,690 of 219,690 location(s) applied
./backup.db: written -- a snapshot, stale from the next backup onwards
```

It is assembled from the messages -- which carry their own subject, sender,
recipients and date -- and from the archive's record of where each message was
seen. It goes wherever you say and is not part of the archive. **It is a
snapshot:** correct for the moment it was built, out of date from the next backup
onwards. Build it again when that matters -- or have one maintained for you.

**Maintaining an `index.db`.** Pass `--index-db` to `backup` (or set
`index_db = true` in the `[global]` config) and mailvault keeps a queryable
`index.db` beside the archive, refreshed after every backup. The refresh is
incremental -- only the log files added since the last one are folded in -- and it
stays a projection, never a source of truth: a database that is missing or
unreadable is rebuilt from scratch, so you can delete it at any time. Mail added
by `archive import` writes no log and is not picked up this way; rebuild with
`create-db` when that matters.

An existing file is refused unless you pass `--force`, which replaces it rather
than adding to it -- merging two runs into one file would give you neither
snapshot:

```console
$ mailvault archive create-db ./backup ./backup.db
./backup.db: already exists, use --force to replace it
```

Two views are there for convenience, `v_messages` and `v_duplicates`:

```console
$ sqlite3 ./backup.db "SELECT date, sender, subject FROM v_messages LIMIT 5"
```

Use `--mailbox NAME` for archives that predate the location record, where nothing
says which job a message came from; every message is then attributed to that one
name.

### Migrating an older archive

Archives written before 0.8.0 keep their metadata in `store.db` inside the
archive. The next backup moves it out by itself, but you can also do it
deliberately:

```console
$ mailvault archive migrate ./backup
./backup: 130,887 message(s) moved into 59 mailbox/folder place(s)
./backup: 46 resume timestamp(s) moved into state.json
./backup: the database is now store.db.migrated and is no longer used
./backup: delete it once you are satisfied with the archive
```

Nothing is deleted. The database is renamed and left alone, so you keep a way
back until you remove it yourself. Running the command again on a migrated
archive does nothing, and an interrupted migration is simply repeated.


## Copying between mailboxes

> [!WARNING]
> **Experimental / Proof of Concept**
> This subcommand is in an early experimental stage and may have hardcoded
> limitations (e.g., `--idle` mode only watches the `INBOX`). Use with
> caution and test with non-critical data first.

`mailvault copy` transfers emails from one IMAP mailbox to another. It requires
a TOML configuration file with two accounts, one with `role = "source"` and the
other with `role = "destination"`:

```toml
[[job]]
name = "source_account"
server = "imap.source.com"
username = "john@source.com"
password = "secret"
role = "source"
folders = ["INBOX"]
move_to_archive = true
archive_folder = "Archive/%Y"

[[job]]
name = "destination_account"
server = "imap.destination.com"
username = "john@destination.com"
password = "secret"
role = "destination"
```

Copy all matching emails:

```console
$ mailvault --config copy.toml copy
```

Use `--idle` to keep the connection open and continuously transfer new incoming
emails. If `move_to_archive` is enabled on the source, copied emails are moved
into the `archive_folder` instead of remaining in the inbox. Use
`--list-folders` to list the source mailbox folders instead of copying.


## Migrating from ib-*

The three former commands are now subcommands of a single `mailvault` command.
Global options are given **before** the command.

| Previously | Now |
|------------|-----|
| `ib-mailbox --config c.toml folders` | `mailvault --config c.toml folders` |
| `ib-mailbox --config c.toml backup <dest>` | `mailvault --config c.toml backup <dest>` |
| `ib-mailbox --config c.toml verify [--repair] <dest>` | `mailvault --config c.toml verify [--repair] <dest>` |
| `ib-archive stats\|import\|addresses\|compress\|decompress <dir>` | `mailvault archive stats\|import\|addresses\|compress\|decompress <dir>` |
| `ib-archive db-from-archive --mailbox NAME <dir>` | `mailvault archive create-db <dir> <database>` |
| `ib-copy --config c.toml copy [--idle]` | `mailvault --config c.toml copy [--idle]` |
| `ib-copy --config c.toml folders` | `mailvault --config c.toml copy --list-folders` |

To keep using the old `ib-*` commands, pin to
[v0.5.0](https://github.com/sniner/mailvault/releases/tag/v0.5.0), the last
release before the rename.


## Mail archive structure

Emails are stored as RFC 822 `.eml` files in a content-addressed directory
structure:

```
./archive
├── 00
│   ├── 00
│   │   └── 00003c6ec5464cca9...7af8.eml
│   ├── 0f
│   │   └── 000ffe5b49390d9b2...26eb.eml
│   ├── 11
│   │   └── 001124d77ce778289...4fd8.eml
│   ├── 30
│   │   └── 0030f33161416b03e...97aa.eml
```

The filename is the SHA-384 hash of the file content and serves as the key to
the archive. This makes it easy to verify file integrity by comparing the hash
with the filename.

Two more things live beside the messages:

```
./archive
├── 00/ … ff/            the messages
├── meta/                where each message was seen
│   └── a1/
│       └── a1b2c3….jsonl
└── state.json           where the next incremental run picks up
```

`meta/` answers the one question the messages cannot: which mailbox and which
folder each was seen in. **One file is one place** -- its first line names a
mailbox and a folder, the rest name the messages seen there. A message that
belongs to several places simply appears in several files, so nothing is
ambiguous. These files are content-addressed exactly like the messages, so each
one carries its own integrity check:

```console
$ sha384sum meta/a1/a1b2c3….jsonl     # matches the filename, or the file is damaged
```

They are written once and never modified. `state.json` holds the timestamps that
decide where the next incremental run resumes; it is small and always replaced
atomically, never edited in place.

Both are plain text. If you ever want to know what an archive thinks it contains,
you can read it without `mailvault` and without SQL.

Emails with the same Message-ID are considered identical from a user
perspective, but if their RFC 822 representation differs, they are stored
separately because the hashes differ. MS Exchange in particular tends to
produce different versions of the same email -- journal copies, for instance,
often differ from mailbox copies by an additional `Received` header and
replaced MIME multipart delimiters.


## Configuration file

`mailvault` reads its configuration from a TOML file. The file name is
irrelevant -- the content is always parsed as TOML.

### Basic example

A simple example for Google Mail:

```toml
[[job]]
name = "gmail.com"
server = "imap.gmail.com"
username = "john.doe@gmail.com"
password = "123456"
folders = ["All Mail"]
```

A more complete example with folder exclusions:

```toml
[[job]]
name = "example.org"
server = "imap.example.org"
username = "john.doe@example.org"
password = "123456"
port = 993
tls = true
ignore_folder_flags = ["Junk", "Drafts", "Trash"]
ignore_folder_names = ['.*/Calendar/?.*']
folders = ["INBOX", "Archive"]
```

An example for MS Exchange journal export:

```toml
[[job]]
name = "exchange.example.org"
server = "exchange.example.org"
username = "john.doe@example.org"
password = "123456"
tls_check_hostname = false
exchange_journal = true
delete_after_export = true
folders = ["INBOX"]
```

### MS Graph backend

As an alternative to IMAP, `mailvault` can access Microsoft 365 mailboxes via
the MS Graph API. This avoids the quirks of Microsoft's IMAP implementation and
uses OAuth2 client credentials for authentication, which is well suited for
unattended backup scenarios.

To use this backend, set `backend = "msgraph"` in the job configuration.
Authentication requires an Azure AD app registration with `Mail.Read` (or
`Mail.ReadWrite` if using `delete_after_export`) application permissions.

```toml
[[job]]
name = "m365-backup"
backend = "msgraph"
tenant_id = "your-azure-tenant-id"
client_id = "your-app-client-id"
client_secret_cmd = "pass show m365/client-secret"
username = "john.doe@example.com"
folders = ["Inbox", "Archive"]
```

The `username` is the email address of the mailbox to back up. All other
options (`folders`, `ignore_folder_names`, `exchange_journal`,
`delete_after_export`, etc.) work the same as with IMAP. Note that
`ignore_folder_flags` has no effect with MS Graph, as Graph folders do not
have IMAP-style flags.

### Global options

Global options can be set in a `[global]` section:

```toml
[global]
compress = true

[[job]]
name = "gmail.com"
server = "imap.gmail.com"
# ...
```

### Dynamic values

All string values in the configuration support environment variable
expansion using `${VAR}` or `${VAR:-default}` syntax:

```toml
[[job]]
name = "example.org"
server = "${IMAP_SERVER:-imap.example.org}"
username = "${IMAP_USER}"
```

Additionally, any string field can be replaced by a `_cmd` variant that
runs a shell command and uses its output as the value. This works for
any field, not just passwords:

```toml
[[job]]
name = "example.org"
server = "imap.example.org"
username = "john.doe@example.org"
password_cmd = "pass show email/example.org"
client_secret_cmd = "az keyvault secret show --name my-secret --query value -o tsv"
```

For security, `_cmd` fields are only evaluated when the `--allow-exec` flag
is passed on the command line. Without it, `_cmd` fields are silently ignored
with a warning.

### Parameters

| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `backend` | no | `"imap"` | Backend to use: `"imap"` or `"msgraph"` |
| `server` | IMAP | — | Hostname or IP address of the IMAP server |
| `username` | yes | — | Login username (IMAP) or email address (Graph) |
| `password` | IMAP* | — | Login password (*or use `password_cmd`) |
| `port` | no | 993 | IMAP server port |
| `tls` | no | `true` | Use encrypted connection (IMAP only) |
| `tenant_id` | Graph | — | Azure AD tenant ID |
| `client_id` | Graph | — | Azure AD application (client) ID |
| `client_secret` | Graph* | — | Azure AD client secret (*or use `client_secret_cmd`) |
| `folders` | no | all | List of folder names to export |
| `ignore_folder_flags` | no | — | Skip folders with any of these IMAP flags (IMAP only) |
| `ignore_folder_names` | no | — | Skip folders matching these names (supports regular expressions) |

### Options

| Option | Default | Description |
|--------|---------|-------------|
| `tls_check_hostname` | `true` | Verify the server hostname against the TLS certificate |
| `tls_verify_cert` | `true` | Verify the TLS certificate |
| `exchange_journal` | `false` | Extract original emails from MS Exchange journal messages |
| `delete_after_export` | `false` | Delete emails from the server after export (use with caution) |
| `incremental` | `true` | Only download messages added since the last backup run |
| `max_retries` | `5` | Retries for failed MS Graph requests (throttling, gateway and connection errors) |
| `compress` | `false` | Compress stored emails with zstd (global option) |
| `index_db` | `false` | Maintain a queryable `index.db` alongside the archive, refreshed after each backup (global option) |


## Metadata

Besides the messages, a backup records two things: where each message was seen,
into `meta/`, and where the next run should resume, into `state.json`. That is
all -- there is no database in the archive, and nothing is modified in place. See
[Why an archive holds no database](#why-an-archive-holds-no-database).

This is not optional. Both files are small, immutable or atomically replaced, and
they are what makes an incremental backup and `verify` possible at all. The
option that used to switch them off (`with_db`, later `with_metadata`) existed
because of the SQLite database, and went with it.

To query an archive with SQL, build a database from it when you need one: see
[Querying an archive with SQL](#querying-an-archive-with-sql).


## MS Windows

A pre-compiled `mailvault.exe` for Windows is provided as an asset on the
[GitHub Releases](https://github.com/sniner/mailvault/releases) page. You can
download it directly without needing Python or any dependencies.
