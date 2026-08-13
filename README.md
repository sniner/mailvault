# mailvault -- Back up and archive email from IMAP and Microsoft 365 mailboxes

`mailvault` takes mail *out* of a mailbox and puts it somewhere it will still be
readable in ten years: one RFC 822 `.eml` file per message, in a directory you
can walk with `ls`, each named after the hash of its own content. There is no
container format to unpack, no database to keep alive, and nothing that has to
be installed before a message can be read again.

It talks to plain IMAP, Gmail, Microsoft 365 (over MS Graph, not merely its
IMAP) and Proton Mail through the local Bridge. What people use it for:

* **A backup of a hosted mailbox.** Run it nightly; every run after the first
  costs only the mail that has arrived since.
* **Getting mail out of a mailbox that is filling up.** With
  `delete_after_export` a message is removed from the server once it is
  archived — and only once its place has been written down durably.
* **Archiving an Exchange journal mailbox.** The journal envelopes are
  unwrapped, so what lands in the archive is the original mail.
* **Consolidating what has piled up elsewhere.** `.eml` files from other tools
  are imported into the same archive, where duplicates recognise themselves.

**The archive holds no database.** It is often the only copy of your mail, so
the interesting question is not what happens in the good case but what happens
when a write goes wrong — and a message file, named after its content and never
modified, can be checked and discarded on its own, while an SQLite file that
rewrites pages in place over SMB or NFS cannot. Everything a backup records is
either written once or replaced atomically. To *query* the archive, a database
is built from it on demand and thrown away afterwards: see [the optional query
database](#the-optional-query-database).

> [!IMPORTANT]
> **0.10.0 changes where things live inside an archive**, and every command
> refuses an archive that has not been lifted rather than looking for the mail
> where it is not. Run `mailvault archive migrate` once per archive — nothing
> is deleted.
>
> **A new archive now starts with `mailvault archive init`**, the way a
> repository starts with `git init`, and the configuration belongs *in* the
> archive as `mailvault.toml`. No command takes the archive as a positional
> argument any more: it is the directory you are standing in, or `--archive DIR`.
>
> See the [CHANGELOG](https://github.com/sniner/mailvault/blob/main/CHANGELOG.md)
> for the full list, and [Migrating an older
> archive](https://github.com/sniner/mailvault/blob/main/docs/deep-dive.md#migrating-an-older-archive)
> for what `migrate` does.


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
$ uv tool install mailvault==0.11.0
$ uv tool install git+https://github.com/sniner/mailvault
```

Wheels and a pre-compiled `mailvault.exe` for Windows -- no Python, no
dependencies -- are on the [GitHub
Releases](https://github.com/sniner/mailvault/releases) page.


## A new archive

An archive is a directory with a `FORMAT` file in it, and `archive init` is what
puts one there -- what `git init` is, down to making the directory if it is not
already there and using the one you are standing in when you name none:

```console
$ mailvault archive init /srv/archive/private
/srv/archive/private: archive created
mailvault.toml written -- fill in your mailboxes, then back up
```

Every other command asks first whether it is looking at an archive, and stops if
it is not.


## What goes into `mailvault.toml`

The configuration lives **in** the archive, as `mailvault.toml`. That is where
every command looks, so the two cannot drift apart and a copy of the archive
carries the recipe along with the mail. It is the one file in an archive that is
edited by hand; `init` leaves a commented one behind to fill in.

One `[[job]]` per mailbox:

```toml
[[job]]
name = "example.org"
server = "imap.example.org"
username = "john.doe@example.org"
password_cmd = "pass show email/example.org"
folders = ["INBOX", "Archive"]
```

`name` is what this mailbox is called in every report and in the archive's own
records -- pick one and keep it. The rest is the login. Leave `folders` out and
everything the server offers is backed up; `port` defaults to 993 and `tls` to
true.

A plain `password = "..."` works, and so does `password_cmd`, which runs a
command and takes its output. Any string field has such a `_cmd` variant, and
they are only evaluated when the command that reads the configuration is given
`--allow-exec`.

Microsoft 365 over MS Graph is a different set of keys in the same shape:

```toml
[[job]]
name = "m365"
backend = "msgraph"
tenant_id = "your-azure-tenant-id"
client_id = "your-app-client-id"
client_secret_cmd = "pass show m365/client-secret"
username = "john.doe@example.com"
```

A few settings apply to the whole run rather than to one mailbox and go into a
`[global]` section:

```toml
[global]
compress = true      # store the messages compressed with zstd
index_db = true      # keep the query database in step with every backup
```

Every option there is, and what each one does, is in the [configuration
reference](https://github.com/sniner/mailvault/blob/main/docs/deep-dive.md#configuration-reference).


## Backing up

From inside the archive, nothing else needs to be said. It is usually worth
looking at what the mailboxes offer first:

```console
$ cd /srv/archive/private
$ mailvault folders
example.org::INBOX
example.org::Archive
example.org::Archive/2022
example.org::Trash
```

Then run the backup:

```console
$ mailvault backup
2024-08-15 10:05:52,275 INFO -- START -- archive: /srv/archive/private
2024-08-15 10:05:52,276 INFO -- Job item: example.org
2024-08-15 10:05:52,527 INFO -- example.org::INBOX: found 3 messages
2024-08-15 10:05:52,799 INFO -- example.org::INBOX[1]: NEW: id=25652e390168...a234
2024-08-15 10:05:52,799 INFO -- example.org::INBOX[2]: NEW: id=fa1f63a13f91...c9ee
2024-08-15 10:05:52,799 INFO -- example.org::INBOX[3]: NEW: id=800be881dc38...7fa8
```

From anywhere else, name the archive -- `--archive` is what `git -C` is:

```console
$ mailvault --archive /srv/archive/private backup
```


## Keeping the archive up to date

Run it again. Each folder carries on where the last run left it, so a repeated
run costs only the mail that has arrived since, and anything already stored is
recognised rather than fetched twice:

```console
$ mailvault backup
2024-08-15 10:09:28,531 INFO -- example.org::INBOX: found 3 messages
2024-08-15 10:09:28,820 INFO -- example.org::INBOX[1]: EXISTS: id=25652e390168...a234
2024-08-15 10:09:28,820 INFO -- example.org::INBOX[2]: EXISTS: id=fa1f63a13f91...c9ee
2024-08-15 10:09:28,820 INFO -- example.org::INBOX[3]: EXISTS: id=800be881dc38...7fa8
```

That is the whole routine -- a cron entry and nothing else. Three things adjust
it:

```console
$ mailvault backup --job example.org      # only this job; may be repeated
$ mailvault backup --compress             # store the messages compressed
$ mailvault backup --full                 # re-read every folder, ignoring resume points
```

A failed download needs no attention: a folder that ended a run with messages it
did not deliver does not advance its resume point, and the next ordinary run
fetches it again. Nor is such a message ever deleted from the server.

`verify` is therefore not part of the routine either. It answers a different
question: not whether the last run worked, but whether the archive really holds
what the mailbox holds. That is worth asking when a message has left the archive
*after* it was stored -- one set aside by `archive check --quarantine`, or lost
in a copy or a restore -- because as far as the resume point is concerned that
folder is done, and no ordinary run will ask for it again:

```console
$ mailvault verify
example.org::INBOX: 77,592 on server, 43 not archived
example.org: 43 message(s) missing, run again with --repair

$ mailvault verify --repair
example.org::INBOX: 77,592 on server, 43 not archived, 43 restored
example.org: 43 of 43 message(s) restored
```

It compares headers rather than downloading everything, so checking a large
mailbox takes minutes, not hours. See [Verify and
repair](https://github.com/sniner/mailvault/blob/main/docs/deep-dive.md#verify-and-repair).


## The optional query database

The archive holds mail, not answers. To ask it questions, build its query
database once:

```console
$ mailvault db create
130,997 message(s) named by 60 log file(s), 219,690 of 219,690 location(s) applied
index.db: written
```

It lives in the archive as `index.db`, and **it is a copy**: everything in it
comes from the messages themselves and from the archive's record of where each
was seen. Nothing else in mailvault depends on it, and it can be thrown away and
built again at any time.

### Asking it questions

Every filter given has to match; text matches anywhere in the value and ignores
case:

```console
$ mailvault db search --from example.com --since 2024-01-01
2024-03-11  a3f1c8e04b71…  info@example.com                Invoice 4711
2024-05-02  9b0d47f2a180…  info@example.com                Delivery note 8842
2 message(s)
```

`--from`, `--to`, `--subject`, `--mailbox`, `--folder`, `--since`, `--until` and
`--limit`. The message id in the table is shortened to be read, not typed -- for
anything that goes on to another program there is `--ids`, so a search and an
export make a pipeline:

```console
$ mailvault db search --from example.com --ids \
    | xargs mailvault archive export --output ./invoices/
```

`--csv` and `--json` print the whole result with the ids in full. It is an
ordinary SQLite file too, with two views for convenience, `v_messages` and
`v_duplicates`:

```console
$ sqlite3 index.db "SELECT date, sender, subject FROM v_messages LIMIT 5"
```

### Keeping it up to date

`db update` takes in what the archive has recorded since -- a few small reads
rather than a pass over every message:

```console
$ mailvault db update
index.db: 3 log file(s) taken in, 412 message(s) added
```

Or have it done for you: `index_db = true` in the `[global]` section, or
`--index-db` on a single run, and every backup brings it up to date at the end.

You will not have to guess whether it is current. The database records how far
into the archive it has read, and a search says so before it prints anything if
the archive has moved on:

```console
index.db: behind the archive in 2 place(s) (example.com::INBOX, example.com::Sent)
          -- mail archived since is not in it, take it in with `mailvault db update`
```

### Building it again

When something about it looks wrong, do not investigate it -- replace it. It
holds no fact the archive does not:

```console
$ mailvault db create --force
```

`db drop` deletes it without asking and without a `--force`, for the same reason.

What it holds is the mail the archive's log accounts for. Mail imported before
`archive import` took a `--name` has no log entry, and building the database
again will not find it either -- give it a place with `archive adopt` first, and
it is in like everything else.


## Working on the archive

`mailvault archive` is the group of commands that work on the archive itself,
without touching a mailbox.

```console
$ mailvault archive stats
1,234 emails, 567.8 MiB total
```

**Import** existing `.eml` files -- from another tool, an old backup, a Docuware
export with `--docuware`. `--name` is what the archive records them under, and
it is the only way to ask afterwards which import a message came from. `--move`
removes the source files, `--dry-run` only counts:

```console
$ mailvault archive import --name docuware-2019 ./my_mails
20,431 message(s) read -- 38 imported, 20,393 already in /srv/archive/private
recorded as docuware-2019 -- `mailvault db update` takes it in, then `mailvault db search --folder docuware-2019` finds them
```

**Places** lists what the archive has mail from -- every mailbox and folder, every
import, everything `archive adopt` took in. These are the names `db search`
takes, and the ones already spoken for when you pick a new one:

```console
$ mailvault archive places
mailbox           folder                          messages  last seen
gmail.com         INBOX                             12,043  2026-08-12
gmail.com         [Google Mail]/Alle Nachrichten      4,001  2026-08-12
                  docuware-2019                       5,412  2026-08-02
                  orphaned                              110  2026-08-13
4 place(s), 17,455 message(s)
  the column adds up to more: a message can be in several places
```

A cell is empty where there is no name to print. No mailbox is what an import and
an adopted place look like -- there is none behind them. No folder is a mailbox
whose folder was never recorded, which is what an archive carried over from the
database mailvault kept before 0.8 has.

**Adopt** the messages that belong to no place -- what an import left behind
before it took a `--name`, and what a lost log entry leaves behind. `archive
check` reports them; this takes them in under a name you give:

```console
$ mailvault archive adopt --name orphaned --dry-run
110 message(s) belong to no place and would be recorded as orphaned
  nothing corrects the log afterwards, so the name has to be right
  nothing was written; leave out --dry-run to record them
```

The name is your statement, not the archive's: the import they came from if you
know it, `orphaned` to say that nobody knows any more. Messages that already have
a place are left alone. Where the directory an import read them from still
exists, importing it again is better -- that records only what really lay in it.

**Export** a single message, exactly as it was stored, by the id the reports
print. Name several and give `--output` a directory to get one file each:

```console
$ mailvault archive export 6f3ac1… | head -20
$ mailvault archive export 6f3ac1… -o message.eml
```

**Compress** or **decompress** the whole archive after the fact:

```console
$ mailvault archive compress
1,234 files compressed, 0 already compressed
```

**Check** that the archive is what it claims to be. Every message is read and
held against its checksum, which is the only way to find one whose bytes have
changed underneath it -- bit rot, a restore that dropped a file, a copy that ran
out of disk:

```console
$ mailvault archive check
130,997 message(s) stored, 130,887 of them accounted for by 60 log file(s) in 59 place(s)
sound -- every message was read and matches its checksum
```

It repairs nothing and exits non-zero when something is off. `--quarantine`
sets damaged messages aside so they count as missing again and can be fetched
back by `verify --repair`.

**Compact** the metadata log now and then. Every backup writes a small file per
folder, and over months they add up:

```console
$ mailvault archive compact
1,204 log file(s) -> 59 across 59 mailbox/folder place(s)
41,388 duplicate observation(s) dropped
```

**Migrate** an archive written by an older version, once, after upgrading:

```console
$ mailvault archive migrate
```


## Further reading

The [deep dive](https://github.com/sniner/mailvault/blob/main/docs/deep-dive.md)
has what is deliberately left out above:

* [Why an archive holds no
  database](https://github.com/sniner/mailvault/blob/main/docs/deep-dive.md#why-an-archive-holds-no-database)
  -- what goes wrong over SMB, and what was done about it
* [Where the archive is, and which file describes
  it](https://github.com/sniner/mailvault/blob/main/docs/deep-dive.md#where-the-archive-is-and-which-file-describes-it)
  -- `--archive`, `--config`, and how a mix-up is caught before the first login
* [Backing up in
  detail](https://github.com/sniner/mailvault/blob/main/docs/deep-dive.md#backing-up-in-detail)
  -- `delete_after_export` on Gmail and Microsoft 365, Proton Bridge, Exchange
  journal mailboxes, `verify --repair`
* [The query database in
  detail](https://github.com/sniner/mailvault/blob/main/docs/deep-dive.md#the-query-database-in-detail)
  -- SQL, the views, and archives that predate the location record
* [What an archive looks like
  inside](https://github.com/sniner/mailvault/blob/main/docs/deep-dive.md#what-an-archive-looks-like-inside)
  -- `mail/`, `meta/`, `heads/`, `FORMAT`, and how to read them without this
  program
* [Configuration
  reference](https://github.com/sniner/mailvault/blob/main/docs/deep-dive.md#configuration-reference)
  -- every parameter and every option
* [Migrating an older
  archive](https://github.com/sniner/mailvault/blob/main/docs/deep-dive.md#migrating-an-older-archive)
  and [coming from `ib-mailbox` and
  `ib-archive`](https://github.com/sniner/mailvault/blob/main/docs/deep-dive.md#coming-from-ib-mailbox-and-ib-archive)


## License

`mailvault` is free software under the [GNU General Public License v3.0 or
later](https://github.com/sniner/mailvault/blob/main/LICENSE.md).
