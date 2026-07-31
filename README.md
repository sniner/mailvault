# imapbackup - A toolkit for email backup and archiving

> [!CAUTION]
> **Version 0.3.0 contains several breaking changes** (configuration format,
> renamed subcommands, changed defaults). If you are upgrading from a previous
> version, please read the [CHANGELOG](CHANGELOG.md) before updating.


## Installation

After years of Python packaging being an adventure in its own right --
virtualenvs, pip, pipx, setup.py, setuptools, poetry, and whatever else came
and went -- [uv](https://docs.astral.sh/uv/) has finally brought sanity to the
table. The recommended way to install `imapbackup` is straight from the
repository:

```console
$ uv tool install git+https://github.com/sniner/imapbackup
```

This installs the three CLI tools (`ib-mailbox`, `ib-archive`, `ib-copy`)
into an isolated environment and makes them available on your `PATH` -- no
manual virtualenv juggling required.

To include support for Microsoft 365 mailboxes via MS Graph:

```console
$ uv tool install "imapbackup[graph] @ git+https://github.com/sniner/imapbackup"
```

Add `@v0.4.0` to the URL to pin a specific release.

> **Note:** this project is not published on PyPI -- the name `imapbackup`
> there belongs to an unrelated project. Install from this repository or from
> the release artifacts, not from PyPI.

Wheels and pre-compiled Windows executables are also available on the
[GitHub Releases](https://github.com/sniner/imapbackup/releases) page.


## Overview

`imapbackup` provides three command line tools:

* `ib-mailbox` backs up emails from IMAP mailboxes to a local archive and verifies the result
* `ib-archive` manages the local email archive (import, compress, statistics, etc.)
* `ib-copy` copies emails between IMAP mailboxes (experimental)


## `ib-mailbox`

`ib-mailbox` downloads emails from one or more IMAP mailboxes and stores them
in a local content-addressed archive. The backup can be repeated at regular
intervals without creating duplicates, as long as you always export from the
same mailbox.

A configuration file defines the IMAP accounts and options for the backup job
(see [Configuration file](#configuration-file) below).

First, you may want to get an overview of all available folders:

```console
$ ib-mailbox --config example.toml folders
example.org::Trash
example.org::Archive
example.org::Archive/2022
example.org::Archive/2021
example.org::Archive/2020
example.org::INBOX
```

Then run the backup:

```console
$ ib-mailbox --config example.toml backup ./backup
2024-08-15 10:05:52,275 INFO -- START
2024-08-15 10:05:52,276 INFO -- Processing mailbox: example.org
2024-08-15 10:05:52,527 INFO -- example.org::INBOX: found 3 messages
2024-08-15 10:05:52,799 INFO -- example.org::INBOX[1]: NEW: id=25652e390168...a234
2024-08-15 10:05:52,799 INFO -- example.org::INBOX[2]: NEW: id=fa1f63a13f91...c9ee
2024-08-15 10:05:52,799 INFO -- example.org::INBOX[3]: NEW: id=800be881dc38...7fa8
```

On subsequent runs, already archived messages are recognized and skipped:

```console
$ ib-mailbox --config example.toml backup ./backup
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
$ ib-mailbox --config example.toml verify ./backup
example.org::INBOX: 77,592 on server, 43 not archived
example.org: 43 message(s) missing, run again with --repair
```

The comparison only lists the folder's message headers, which costs a handful
of requests instead of one download per message — checking a large mailbox
takes minutes, not hours. With `--repair` the missing messages are downloaded
and added to the archive:

```console
$ ib-mailbox --config example.toml verify --repair ./backup
example.org::INBOX: 77,592 on server, 43 not archived, 43 restored
example.org: 43 of 43 message(s) restored
```

Messages are matched by their `Message-ID`. A message whose `Message-ID` is
missing or ambiguous is treated as not archived and fetched again, which is
harmless: the content-addressed storage recognizes the duplicate and discards
it. `verify` requires a job with `with_db = true` and does not support
`exchange_journal` jobs, because there the archived message and the server's
journal envelope carry different `Message-ID`s.


## `ib-archive`

`ib-archive` provides several subcommands for working with the local archive.

### Import emails

Import existing `.eml` files into the archive. For example, to consolidate
emails from `./my_mails` into `./backup`:

```console
$ ib-archive --verbose import ./my_mails ./backup
```

Use `--move` to remove source files after import, `--compress` to store them
compressed, and `--docuware` if the source is a Docuware email archive.

### Statistics

Show the number of emails and total size of an archive:

```console
$ ib-archive stats ./backup
./backup: 1,234 emails, 567.8 MiB total
```

### Compress / Decompress

Retroactively compress all uncompressed files in an archive with zstd, or
revert compressed files back to plain `.eml`:

```console
$ ib-archive compress ./backup
./backup: 1,234 files compressed, 0 already compressed

$ ib-archive decompress ./backup
./backup: 1,234 files decompressed, 0 already plain
```

### Email addresses

List all sender and recipient addresses found in the archive:

```console
$ ib-archive addresses ./backup
```

### Build metadata database

If the metadata database is missing (e.g. because `with_db` was not enabled
initially) or has become corrupt, you can create or rebuild it from the
archive files:

```console
$ ib-archive db-from-archive --mailbox example.org ./backup
```

Since the `.eml` files in the archive do not contain any information about
which backup job they originated from, all emails are assigned to the single
`--mailbox` identifier you specify. It should match the job name from your
configuration file.


## `ib-copy`

> [!WARNING]
> **Experimental / Proof of Concept**
> This tool is in an early experimental stage and may have hardcoded
> limitations (e.g., `--idle` mode only watches the `INBOX`). Use with
> caution and test with non-critical data first.

`ib-copy` transfers emails from one IMAP mailbox to another. It requires a
TOML configuration file with two accounts, one with `role = "source"` and the
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
$ ib-copy --config copy.toml copy
```

Use `--idle` to keep the connection open and continuously transfer new incoming
emails. If `move_to_archive` is enabled on the source, copied emails are moved
into the `archive_folder` instead of remaining in the inbox.


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

Emails with the same Message-ID are considered identical from a user
perspective, but if their RFC 822 representation differs, they are stored
separately because the hashes differ. MS Exchange in particular tends to
produce different versions of the same email -- journal copies, for instance,
often differ from mailbox copies by an additional `Received` header and
replaced MIME multipart delimiters.


## Configuration file

`ib-mailbox` and `ib-archive` accept configuration files in TOML or YAML
format. TOML is recommended for new configurations. `ib-copy` requires TOML.

### TOML format

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

As an alternative to IMAP, `ib-mailbox` can access Microsoft 365 mailboxes
via the MS Graph API. This avoids the quirks of Microsoft's IMAP
implementation and uses OAuth2 client credentials for authentication, which
is well suited for unattended backup scenarios.

To use this backend, install the `graph` extra (see
[Installation](#installation)) and set `backend = "msgraph"` in the job
configuration. Authentication requires an Azure AD app registration with
`Mail.Read` (or `Mail.ReadWrite` if using `delete_after_export`)
application permissions.

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

### YAML format

The same Google Mail example in YAML:

```yaml
gmail.com:
    server: "imap.gmail.com"
    username: "john.doe@gmail.com"
    password: "123456"
    folders:
        - All Mail
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
| `with_db` | `true` | Maintain a metadata SQLite database in the archive |
| `incremental` | `true` | Only download messages added since the last backup run (requires `with_db`) |
| `max_retries` | `5` | Retries for failed MS Graph requests (throttling, gateway and connection errors) |
| `compress` | `false` | Compress stored emails with zstd (global option) |


## Metadata database

When `with_db` is enabled (the default), an SQLite database is created inside
the archive containing header fields such as date, Message-ID, sender,
recipient, and subject.

For an archive without a database, you can create one retroactively with
`ib-archive db-from-archive`.


## MS Windows

Pre-compiled `.exe` files for Windows are provided as assets in the
[GitHub Releases](https://github.com/sniner/imapbackup/releases) page.
You can download `ib-mailbox.exe`, `ib-archive.exe`, and `ib-copy.exe`
directly without needing Python or any dependencies.
