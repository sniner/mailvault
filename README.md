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

**The archive holds no database.** Everything a backup writes is written once or
replaced atomically, never rewritten in place — which is exactly what goes wrong
over SMB or NFS, and the archive is often the only copy of your mail. To *query*
it, a database is built on demand and can be thrown away again: [the optional
query database](#the-optional-query-database). The reasoning is in [the deep
dive](https://github.com/sniner/mailvault/blob/main/docs/deep-dive.md#why-an-archive-holds-no-database).


## Installation

From [PyPI](https://pypi.org/project/mailvault/), with
[uv](https://docs.astral.sh/uv/):

```console
$ uv tool install mailvault
```

That puts the `mailvault` command on your `PATH` in an environment of its own.
Microsoft 365 over MS Graph is built in, no extra needed. `pipx install
mailvault` and `pip install mailvault` work just as well, and so does naming a
particular release or the development state:

```console
$ uv tool install mailvault==0.12.0
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
it is not. An archive written before 0.10 needs `mailvault archive migrate` once
-- the commands say so when it is due, and nothing is deleted.


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

What a particular provider wants -- Gmail's labels, the app registration behind
Microsoft 365, the Proton Bridge, iCloud's unguessable hostname -- is one page
of its own: [Providers](https://github.com/sniner/mailvault/blob/main/docs/providers.md),
with a `[[job]]` to copy for each.

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
2024-08-15 10:05:52,276 INFO -- Job: example.org
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

A failed download needs no attention: that folder does not advance its resume
point, the next ordinary run fetches it again, and nothing is deleted from the
server that did not make it into the archive.

`verify` is therefore not part of the routine. It answers a different question --
whether the archive still holds what the mailbox holds -- and is worth asking
when a message left the archive *after* it was stored:

```console
$ mailvault verify
example.org::INBOX: 77,592 on server, 43 not archived
example.org: 43 messages missing, run again with --repair

$ mailvault verify --repair
example.org::INBOX: 77,592 on server, 43 not archived, 43 restored
example.org: 43 of 43 messages restored
```

It compares headers rather than downloading everything, so a large mailbox takes
minutes, not hours. See [Verify and
repair](https://github.com/sniner/mailvault/blob/main/docs/deep-dive.md#verify-and-repair).


## The optional query database

The archive holds mail, not answers. To ask it questions, build its query
database once:

```console
$ mailvault db create
130,997 messages named by 60 log files, 219,690 of 219,690 locations applied
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
2 messages
```

`--from`, `--to`, `--subject`, `--mailbox`, `--folder`, `--since`, `--until` and
`--limit`. The message id in the table is shortened to be read, not typed -- for
anything that goes on to another program there is `--ids`, so a search and an
export make a pipeline:

```console
$ mailvault db search --from example.com --ids \
    | xargs mailvault archive export --output ./invoices/
```

`--csv` and `--json` print the whole result with the ids in full. It is also an
ordinary SQLite file, so anything that speaks SQL can read it -- [the views it
brings
along](https://github.com/sniner/mailvault/blob/main/docs/deep-dive.md#the-query-database-in-detail).

### Keeping it up to date

`db update` takes in what the archive has recorded since -- a few small reads
rather than a pass over every message:

```console
$ mailvault db update
index.db: 3 log files taken in, 1,206 locations recorded, 412 messages new
```

Or have it done for you: `index_db = true` in the `[global]` section, or
`--index-db` on a single run, and every backup brings it up to date at the end.

You will not have to guess whether it is current. The database records how far
into the archive it has read, and a search says so before it prints anything if
the archive has moved on:

```console
index.db: behind the archive in 2 places (example.com::INBOX, example.com::Sent)
          -- mail archived since is not in it, take it in with `mailvault db update`
```

### Building it again

When something about it looks wrong, do not investigate it -- replace it. It
holds no fact the archive does not:

```console
$ mailvault db create --force
```

`db drop` deletes it without asking and without a `--force`, for the same reason.

What it holds is the mail the archive's log accounts for. An archive filled by an
import made before `archive import` took a `--name` has none, and
[`archive adopt`](https://github.com/sniner/mailvault/blob/main/docs/deep-dive.md#taking-in-what-belongs-to-no-place)
is what gives that mail a place.


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
$ mailvault archive import --name imported-2019 ./my_mails
20,431 messages read -- 38 imported, 20,393 already in /srv/archive/private
recorded as imported-2019 -- `mailvault db update` takes it in, then `mailvault db search --folder imported-2019` finds them
```

**Places** lists what the archive has mail from -- every mailbox and folder, and
every import. These are the names `db search` takes:

```console
$ mailvault archive places
mailbox    folder                   messages  last seen
gmail.com  INBOX                      12,043  2026-08-12
gmail.com  [Google Mail]/All Mails     4,001  2026-08-12
           imported-2019               5,412  2026-08-02
3 places, 17,455 messages
  the column adds up to more: a message can be in several places
```

An empty mailbox column is an import: there is no mailbox behind it.

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
130,997 messages stored, 130,887 of them accounted for by 60 log files in 59 places
sound -- every message was read and matches its checksum
```

It repairs nothing and exits non-zero when something is off. `--quarantine` sets
damaged messages aside so they count as missing again and can be fetched back by
`verify --repair`. Where it reports messages that belong to no place,
[`archive adopt`](https://github.com/sniner/mailvault/blob/main/docs/deep-dive.md#taking-in-what-belongs-to-no-place)
takes them in under a name you give.

**Compact** the metadata log now and then. Every backup writes a small file per
folder, and over months they add up:

```console
$ mailvault archive compact
1,204 log files -> 59 across 59 places
41,388 duplicate observations dropped
```

**Migrate** an archive written by an older version, once, after upgrading:

```console
$ mailvault archive migrate
```


## Further reading

[Providers](https://github.com/sniner/mailvault/blob/main/docs/providers.md) is
the practical one: what plain IMAP, Gmail, Microsoft 365, Proton Mail and iCloud
each want in `mailvault.toml`, and what each of them does that the others do not.

[Use cases](https://github.com/sniner/mailvault/blob/main/docs/usecases.md) is
the other practical one: whole recipes for situations that take more than one
option -- rolling old mail off a mailbox that is filling up, to begin with.

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
* [Maintaining the archive in
  detail](https://github.com/sniner/mailvault/blob/main/docs/deep-dive.md#maintaining-the-archive-in-detail)
  -- importing under a name, taking in what belongs to no place, compacting the
  metadata log, quarantine
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
