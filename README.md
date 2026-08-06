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

Global options (`--config`, `-v/--verbose`, `-q/--quiet`, `--log-file`,
`--allow-exec`, `--job`, `--allow-new-mailbox`) are given **before** the command.


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

### Which archive a configuration belongs to

The configuration and the destination are two independent arguments, and nothing
about `mailvault --config work.toml backup ~/mail/private` looks wrong until the
first message has been written. Two things stand in the way of that pairing.

A configuration can name the archive it belongs to:

```toml
[global]
destination = "/srv/archive/private"
```

`mailvault --config private.toml backup` is then enough. A relative path is
taken relative to the configuration file rather than the working directory, so
it means the same archive from cron as from a shell; `~` and `${VAR}` are
expanded. The option is optional, and naming a destination on the command line
still works and still wins -- for the one-off run into a scratch directory --
but the override is logged rather than passed over in silence.

Independently of that, `backup` and `verify` stop **before the first login** when
a job has never written into the archive they were pointed at:

```console
$ mailvault --config work.toml backup ~/mail/private
ERROR -- /Users/jd/mail/private: the archive holds gmail.com, posteo.de, and none of
its jobs (work.example.com) has ever written here -- this looks like the wrong
configuration for this archive. Check that the configuration and the archive belong
together, then pass --allow-new-mailbox to go ahead
```

The archive answers this itself: `state.json` (or, if that is missing, the
metadata log) records which mailboxes have written into it, and a job's `name` is
what it appears under. A genuinely new job is the one case this cannot tell apart
from a mix-up, so it costs one run with `--allow-new-mailbox`; from the run after
that it is known and needs nothing.

The check only ever looks in one direction. A mailbox in the archive with no job
in the configuration is not reported -- removing a job, commenting one out or
selecting a few with `--job` are everyday things, and none of them can put a
message where it does not belong. Only writing can, so only writing is checked.
An archive nobody has written into yet accepts anything: there is nothing there
to contaminate.

### Deleting from the server after export

With `delete_after_export = true` a message is removed from the mailbox once it
is archived — and only once its location has been written down durably, never
before. What "removed" then means is up to the server, and the two big hosted
providers do not mean what you probably expect:

| Backend | What `delete_after_export` actually does |
|---------|------------------------------------------|
| Plain IMAP | Marks `\Deleted` and expunges — the message is gone |
| Gmail | Depends on the account's IMAP setting; in the usual "move to the Trash" configuration the message lands in `[Gmail]/Trash` and stays there |
| Microsoft 365 | A *soft delete*: the message moves to **Deleted Items** and stays there |

On both hosted services the mailbox therefore does not actually shrink. The mail
is out of your way, but it still occupies the quota. Each has an option to
finish the job — one per backend, and each is refused on the other:

**Gmail** — name the trash folder, and it is emptied after every backup pass:

```toml
delete_after_export = true
trash_folder = "[Gmail]/Trash"
```

You have to supply the name because Gmail localises it: `[Gmail]/Papierkorb` on
a German account, `[Google Mail]/…` on some older ones. Use
`mailvault --config … folders` to see what yours is called.

> [!WARNING]
> `trash_folder` empties the folder **completely** — including messages you put
> there yourself and messages that were never archived. It is the one place
> where mailvault deletes mail it did not archive, so point it at a trash folder
> and nothing else.

**Microsoft 365** — delete for good instead of into Deleted Items:

```toml
delete_after_export = true
permanent_delete = true
```

This is the tidier of the two: it hard-deletes exactly the messages that were
just archived, one by one, and never touches anything else that happens to be
in the bin. Retention policies, litigation hold and the recoverable-items
dumpster still apply — this is not a way around them, and in a tenant with a
hold in place the mail remains recoverable by an administrator.

Neither option means anything without `delete_after_export`, and a job that sets
one anyway is refused rather than run: an option that decides the fate of mail
must never look effective while doing nothing.

### Proton Mail via Bridge

Proton Mail offers no IMAP of its own, so mailvault talks to the local **Proton
Bridge** -- host, port and credentials are the ones the Bridge itself shows you.

One habit of the Bridge is worth knowing: it accepts connections a few minutes
before its first sync has finished, and until then reports folders as empty
rather than as not ready yet. A folder that offers nothing gets no resume point,
so a backup started against a cold Bridge costs a repeated run and nothing
else.

### Exchange journal mailboxes

Exchange journaling wraps every mail it records in an envelope: the journal
report is the message, and the original mail is an attachment inside it. Backing
such a mailbox up as-is would archive the envelopes, not the mail. With
`exchange_journal = true` each item is unwrapped and the original is what goes
into the archive. This works on both backends -- IMAP and Microsoft 365.

Journal mailboxes collect other things too, though: a bounce, a notification, or
a mail someone filed there by hand. Those have no envelope to unwrap, so they
cannot be archived, and they would be examined again on every run.
`error_folder` is where they go:

```toml
[[job]]
name = "journal"
server = "exchange.example.org"
username = "journal@example.org"
exchange_journal = true
error_folder = "Journal/NotAJournalItem"
```

The folder is created if it does not exist, so a job that runs unattended does
not stop because someone tidied it away. Without `error_folder` such items are
reported and left in place — never deleted, in either case, since they were
never archived. This is the only situation in which mailvault moves mail around
in your mailbox; an ordinary backup reads, and with `delete_after_export`
deletes, but never relocates. Setting `error_folder` on a job that is not a
journal job therefore does nothing, and says so when the config is loaded.

> [!NOTE]
> On Microsoft 365 the application registration needs **`Mail.ReadWrite`** for
> this, not just `Mail.Read` — moving a message and creating a folder are both
> writes. Without it the job stops with a message naming the missing permission
> rather than failing obscurely. On IMAP no `MOVE` capability is required:
> where the server lacks it (Exchange's own IMAP service often does), the older
> `COPY` + `\Deleted` sequence it replaced is used instead.

### Verify and repair

A failed download repairs itself. Servers do drop connections and answer the
odd request with a `504 Gateway Timeout`, so a folder can end a run with
messages it did not deliver — but that folder's snapshot is then **not**
advanced, and the next ordinary run fetches it again from where the last one
resumed. Nothing needs to be noticed, and nothing needs to be done.

A message that did not make it into the archive is also never deleted from the
server. With `delete_after_export`, only messages that were stored *and* whose
location reached the metadata log on disk are removed; a failed download is not
among them and stays on the server for the retry.

So gaps do not accumulate silently, and `verify` is not part of the routine. It
covers the exceptions that the snapshot cannot catch by itself:

- archives carried over from older versions of mailvault
- mail moved into an already-archived folder with an internal date older than
  that folder's snapshot, where the date filter of every later run skips it

For those, `verify` compares the mailbox against the archive and reports what is
missing:

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

Either way the run says what it did, and `--dry-run` says what it would do
without writing anything or removing a single source file:

```console
$ mailvault archive import --dry-run ./my_mails ./backup
./my_mails: 20,431 message(s) read -- 38 would be imported, 20,393 already in ./backup
```

That second number is worth a look before a large import, especially when the
mail has been through another program on its way here. A message whose bytes
were altered -- a header added or stripped, line endings rewritten -- is not the
message the archive already holds, so it is stored a second time under a
different name, and afterwards nothing tells it apart from one that really is
new. If almost everything counts as new when you expected almost nothing to,
that is what happened.

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

One entry that cannot be converted does not stop the pass -- a single damaged
file should not cost you the conversion of a whole archive. Those files are
named, left exactly as they are, and the command exits non-zero, so a script
finds out about a conversion that only partly happened.

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


### Consolidating the metadata log

Every backup writes a small file into `meta/` for each folder that received mail,
and because incremental runs overlap, the same messages are recorded again in
each run's file. Over months these add up, and everything that reads the log --
`create-db`, `verify`, `--index-db` -- reads all of them. `compact` folds them
back down:

```console
$ mailvault archive compact ./backup
./backup: 1,204 log file(s) -> 59 across 59 mailbox/folder place(s)
./backup: 41,388 duplicate observation(s) dropped
```

It rewrites one file per mailbox/folder holding each observation once, verifies
the new files, and only then removes the originals -- so it is lossless and safe
to interrupt: a half-done run just leaves both, and the next one finishes. Run it
occasionally; there is no hurry, but do not put it off for years.

Since it is the one pass that has the log open, it also clears away what an
interrupted write left behind there, and says so when it finds anything:

```console
./backup: 2 leftover(s) of an interrupted write removed
```

Only files old enough that no running backup can still be writing them, and only
in `meta/` -- the messages are left alone, because looking through them means
walking every directory in the archive for a few kilobytes.


### Checking an archive

A message file is named after the hash of its content, is never modified and is
written so that it cannot appear half-way. What none of that covers is the time
afterwards: bit rot, a restore that dropped a file, a copy that ran out of disk.
The archive cannot notice any of it on its own, because everything it does asks
whether a *name* is there -- never whether the bytes behind it are still the ones
it was named for.

```console
$ mailvault archive check ./backup
./backup: 130,997 message(s) stored, filed in 219,690 place(s) by 60 log file(s)
./backup: 3 message(s) referenced in the log are missing
  6f3ac1…  mail.example.org::INBOX
./backup: NOT sound -- 3 finding(s) above
```

More places than messages is the normal case, not a discrepancy: a message filed
in two folders is one entry the log references twice.

The last line is the verdict, and it says which kind of run it was:

```console
$ mailvault archive check ./backup
./backup: 130,997 message(s) stored, filed in 219,690 place(s) by 60 log file(s)
./backup: sound -- every message was read and matches its checksum
```

It walks the archive first: every file lying in a shard is an entry, every
message the metadata log references is there, every log file still matches its
own name. That costs a pass over the directory tree.

The integrity check reads every message and hashes it, which is the only way to
find one whose bytes have changed under it. It sounds like the expensive half and
barely is: it reads twenty times the bytes of the walk above it, but a network
share does not charge for bytes -- the walk pays a round trip per shard directory
and the read one per message, and at a couple of messages per shard those come
out level. Measured over SMB on a 131,000-message archive: 16 minutes for the
walk, 17 for reading every message.

That is why it is on by default. `--no-integrity-check` leaves it out for whoever
wants the tree checked without the second half of the wait, and such a run says
so, so that "nothing found" cannot mean two different things.

The passes that take a while number themselves, so a long run says how much of
it is still ahead:

```
step 1 of 3: 20,000 file(s) seen
step 2 of 3: 60 log file(s) file 130,997 message(s) in 219,690 place(s)
step 3 of 3: 4,000 of 130,997 checked
```

The command **repairs nothing** and exits non-zero when the archive is not what
it claims. The only thing it removes is the transient file of a write that was
interrupted, by the same rule `compact` uses. A file that is not an entry is
reported and left where it is -- it may well be someone's.

`--quarantine` is the one exception, and it cannot be combined with
`--no-integrity-check`. A message whose content does not match its checksum is
the one finding where doing nothing is bad:
the archive goes on answering that the message is present, so nothing ever
fetches it again. This renames it to `<hash>.eml.corrupt` -- it keeps every byte,
it just stops claiming to be that message. Out of the way, the message counts as
missing again, and `verify --repair` or a `backup --full` brings it back.


## Migrating from ib-*

The former `ib-mailbox` and `ib-archive` commands are now subcommands of a single
`mailvault` command. Global options are given **before** the command.

| Previously | Now |
|------------|-----|
| `ib-mailbox --config c.toml folders` | `mailvault --config c.toml folders` |
| `ib-mailbox --config c.toml backup <dest>` | `mailvault --config c.toml backup <dest>` |
| `ib-mailbox --config c.toml verify [--repair] <dest>` | `mailvault --config c.toml verify [--repair] <dest>` |
| `ib-archive stats\|import\|addresses\|compress\|decompress <dir>` | `mailvault archive stats\|import\|addresses\|compress\|decompress <dir>` |
| `ib-archive db-from-archive --mailbox NAME <dir>` | `mailvault archive create-db <dir> <database>` |
| `ib-copy --config c.toml copy [--idle]` | — removed, see below |

The third tool, `ib-copy`, has no successor. It transferred mail between two IMAP
mailboxes, was declared "work in progress and not yet usable" when it was first
committed in 2022, and never became usable; it was removed in 0.9.0. For that job
use [imapsync](https://github.com/imapsync/imapsync) or
[mbsync](https://isync.sourceforge.io/), which do it properly. The last release
that still carried it is
[v0.8.2](https://github.com/sniner/mailvault/releases/tag/v0.8.2).

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

They are written once and never modified. `state.json` records, per folder, when
a run last read it and where the next one carries on; it is small and always
replaced atomically, never edited in place.

```json
"INBOX": {
  "last_run": "2026-08-05T19:00:00+00:00",
  "resume": { "kind": "imap-uid", "uidvalidity": 1239278212, "uid": 48127 }
}
```

`last_run` is the wall clock and purely for reading -- nothing resumes from it.
The resume point is what decides what gets fetched, and its shape belongs to the
backend that made it: a UID watermark on IMAP, a delta link on Microsoft 365.
Both mean "everything up to here is in the archive" in the server's own terms,
which a date never could -- a message copied or moved into a folder keeps its
original date and would fall behind any date filter, but it gets a new UID and
shows up in a delta round.

If a resume point cannot be read, or the server says it is no longer valid --
IMAP reports a changed `UIDVALIDITY`, Graph rejects a delta link with `410` --
the folder is read in full. Not by downloading it again: the archive is listed
and compared, and only what is missing is fetched.

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

The `username` is the email address of the mailbox to back up. Most other
options (`folders`, `ignore_folder_names`, `exchange_journal`, `error_folder`,
etc.) work the same as with IMAP. Three do not:

* `ignore_folder_flags` has no effect — Graph folders have no IMAP-style flags
* `delete_after_export` is a soft delete: the message moves to Deleted Items and
  stays there, see [Deleting from the server](#deleting-from-the-server-after-export)
* `trash_folder` is an IMAP option and is refused here — `permanent_delete` is its
  counterpart on this backend

### Global options

Some options apply to the whole run rather than to a single mailbox and are set
in a `[global]` section: `compress`, `index_db`, `incremental`, and
`destination`. They are marked *(global option)* in the tables below.

```toml
[global]
compress = true
incremental = true   # the default; set to false to re-fetch every folder in full
destination = "/srv/archive/private"   # optional, see above

[[job]]
name = "gmail.com"
server = "imap.gmail.com"
# ...
```

For a one-off full run there is no need to touch the configuration:
`mailvault backup --full <destination>` re-reads every folder of every selected
job, whatever `incremental` says. It is the one full read that trusts nothing:
every message is downloaded and the content-addressed storage decides by hash
what is new. Everywhere else -- after an upgrade, or when a server voids its own
resume point -- the folder is listed and compared against the archive instead,
and only the difference is fetched.

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
| `exchange_journal` | `false` | Extract original emails from MS Exchange journal messages (see [Exchange journal mailboxes](#exchange-journal-mailboxes)) |
| `error_folder` | — | Where to file items that are not journal envelopes; only meaningful with `exchange_journal` |
| `trash_folder` | — | IMAP/Gmail only: folder emptied after each backup pass; requires `delete_after_export` (see [Deleting from the server](#deleting-from-the-server-after-export)) |
| `permanent_delete` | `false` | MS Graph only: delete for good instead of into Deleted Items; requires `delete_after_export` (see [Deleting from the server](#deleting-from-the-server-after-export)) |
| `delete_after_export` | `false` | Delete emails from the server after export — on Gmail and M365 this only moves them to the trash, see [Deleting from the server](#deleting-from-the-server-after-export) (use with caution) |
| `max_retries` | `5` | Retries for failed MS Graph requests (throttling, gateway and connection errors) |
| `incremental` | `true` | Only download messages added since the last backup run (global option) |
| `compress` | `false` | Compress stored emails with zstd (global option) |
| `index_db` | `false` | Maintain a queryable `index.db` alongside the archive, refreshed after each backup (global option) |
| `destination` | — | The archive this configuration belongs to, used when the command line names none; relative to the configuration file (global option, see [Which archive a configuration belongs to](#which-archive-a-configuration-belongs-to)) |


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
