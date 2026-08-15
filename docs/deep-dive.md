# mailvault deep dive

The [README](../README.md) shows what to type, and
[Providers](providers.md) what a particular mailbox wants. This is the reasoning
behind both: why the archive is shaped the way it is, what every option does, and
what happens in the cases the short version leaves out.

* [Why an archive holds no database](#why-an-archive-holds-no-database)
* [Where the archive is, and which file describes it](#where-the-archive-is-and-which-file-describes-it)
* [Backing up in detail](#backing-up-in-detail)
* [Maintaining the archive in detail](#maintaining-the-archive-in-detail)
* [The query database in detail](#the-query-database-in-detail)
* [What an archive looks like inside](#what-an-archive-looks-like-inside)
* [Configuration reference](#configuration-reference)
* [Migrating an older archive](#migrating-an-older-archive)
* [Coming from ib-mailbox and ib-archive](#coming-from-ib-mailbox-and-ib-archive)


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

What a backup does record is not optional, though. Where each message was seen
goes into `meta/`, where the next run should resume goes into `heads/`, and those
two are what make an incremental backup and `verify` possible at all. The option
that used to switch them off (`with_db`, later `with_metadata`) existed because
of the SQLite database, and went with it.


## Where the archive is, and which file describes it

Every command works on one archive, and there are exactly two ways to say which:

* the directory you are standing in, or
* `--archive DIR` — what `git -C` is, and it does the same job

And it has to *be* an archive: a directory with a `FORMAT` file, which
`mailvault archive init` writes — `git` answers "is this a repository" the same
way, and nothing else counts. `init` also leaves a `mailvault.toml` to fill in,
and never touches one that is already there.

Every other command asks this first and stops if the answer is no:

```console
$ mailvault archive check
ERROR -- /home/jd/notes: not a mailvault archive. Make one here with
`mailvault archive init`. If it is an old mailvault archive, migrate it with
`mailvault archive migrate`
```

The configuration is not a second thing to keep track of. **It lives in the
archive**, as `mailvault.toml`, and that is where every command looks unless
`--config FILE` names another one. So a backup from inside the archive needs
nothing at all:

```console
$ cd /srv/archive/private && mailvault backup
```

and from anywhere else it needs one thing:

```console
$ mailvault --archive /srv/archive/private backup
```

This is the reason for it. A configuration that names its archive by path cannot
be right on more than one machine: the same NAS is mounted somewhere else on the
laptop than on the desktop, so no path in the file fits both. A configuration
*inside* the archive has no distance to bridge — and a backup of the NAS now
carries the recipe along with the mail, instead of saving the mail and losing the
recipe.

The file is called `mailvault.toml` and not `config.toml` because of the first
case: the directory you happen to be standing in is a shared name space, and a
`config.toml` lying in it belongs to whatever else lives there. Only the current
directory is looked at, never the ones above it — otherwise you would eventually
read a file you had not noticed, and such a file can name a command to run.

`--config` from somewhere else does **not** fall back to the current directory:

```console
$ mailvault --config ~/private.toml backup
ERROR -- /home/jd/private.toml: a configuration was named, but no archive -- name
that too, with --archive
```

Reaching elsewhere for the configuration is what somebody does who is not
standing in the archive, so the directory they happen to be in is the last thing
that should decide where the mail goes.

> [!NOTE]
> The configuration is the one file in an archive that is edited by hand.
> Everything else is either written once and never touched again or replaced
> atomically. A `mailvault.toml` mangled by an editor over SMB costs you a run,
> never a message — but it is worth knowing that it is the one file that can be
> in a half-written state at all.

### What is said before the command, and what after

Two things are said **before** the command, because they are true of the whole
run whatever it is: which archive and configuration to work with (`--archive`,
`--config`) and how much it should say about itself (`-v/--verbose`,
`-q/--quiet`, `--log-file`). Everything else belongs to the command that does the
work and is written after it -- `mailvault backup --job proton.me`, not the other
way round.

### Whether a configuration and an archive belong together

Keeping the configuration in the archive removes most of this question: a file
that lies in the archive can hardly belong to another one. It does not remove all
of it, because `--config` can still name a file from anywhere.

So `backup` and `verify` stop **before the first login** when a job has never
written into the archive they were pointed at:

```console
$ mailvault --archive ~/mail/private --config work.toml backup
ERROR -- /Users/jd/mail/private: the archive holds gmail.com, posteo.de, and none of
its jobs (work.example.com) has ever written here -- this looks like the wrong
configuration for this archive. Check that the configuration and the archive belong
together, then pass --allow-new-mailbox to go ahead
```

The archive answers this itself: `heads/` (or, if that is missing, the
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


## Backing up in detail

### Full and incremental runs

`incremental = true` is the default: each folder carries on from the resume point
the last run left, so a repeated run costs only the mail that has arrived since.
Setting it to `false` in `[global]` re-reads every folder in full, every time.

For a one-off full run there is no need to touch the configuration:
`mailvault backup --full` re-reads every folder of every selected job, whatever
`incremental` says. It is the one full read that trusts nothing: every message is
downloaded and the content-addressed storage decides by hash what is new.
Everywhere else -- after an upgrade, or when a server voids its own resume
point -- the folder is listed and compared against the archive instead, and only
the difference is fetched.

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
`mailvault folders` to see what yours is called.

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

So gaps do not accumulate silently, and `verify` is not part of the routine.

Until 0.10.0 it had a standing job. The resume point was a date, and a message
copied or moved into an already-archived folder keeps its original internal
date -- so it landed *behind* that date and was never asked for again. On one
77,000-message mailbox that had swallowed 29 messages over the years, silently.
Since 0.10.0 the resume point is a UID watermark on IMAP and a delta link on
Microsoft 365, and a message moved in gets a new UID and turns up in the next
delta round. That hole is closed, and with it the reason to run `verify` on a
schedule.

What is left for it are the cases where a message leaves the archive *after* it
was stored. The mailbox side of that folder cannot notice: as far as the resume
point is concerned it is done.

- a message set aside by `archive check --quarantine`, whose bytes no longer
  matched the name they are stored under
- a message lost between the archive and wherever it was copied or restored to
- an archive last written before 0.10.0 whose folders have not been read in full
  since. Lifting it makes the next run do that by itself -- the first run after
  the upgrade lists every folder and fetches what is missing -- so this is the
  belt-and-braces version of something that has probably already happened

Beyond those, it is the one command that asks the mailbox instead of the
archive's own records, which is worth doing occasionally whatever the resume
points say. It compares the mailbox against the archive and reports what is
missing:

```console
$ mailvault verify
example.org::INBOX: 77,592 on server, 43 not archived
example.org: 43 messages missing, run again with --repair
```

The comparison only lists the folder's message headers, which costs a handful
of requests instead of one download per message — checking a large mailbox
takes minutes, not hours. With `--repair` the missing messages are downloaded
and added to the archive:

```console
$ mailvault verify --repair
example.org::INBOX: 77,592 on server, 43 not archived, 43 restored
example.org: 43 of 43 messages restored
```

Messages are matched by their `Message-ID`. A message whose `Message-ID` is
missing or ambiguous is treated as not archived and fetched again, which is
harmless: the content-addressed storage recognizes the duplicate and discards
it. `verify` does not support `exchange_journal` jobs, because there the archived
message and the server's journal envelope carry different `Message-ID`s.


## Maintaining the archive in detail

### Importing existing mail

`archive import` reads `.eml` files from a directory and adds them to the
archive. `--move` removes the source files after import, `--compress` stores them
compressed, and `--docuware` reads a Docuware email archive as the source.

`--name` is required, and it is what the archive records the mail under:

```console
$ mailvault --archive ./backup archive import --name docuware-2019 /mnt/export/docuware
```

That name answers a question nothing else can answer afterwards -- which import a
message came from -- and it is written where every other location is written, in
the metadata log. With one difference that decides the rest: **the mailbox stays
empty.** There is no mailbox behind an import and nobody to ask about it again,
so the name lives in the folder field, and `mailvault db search --folder
docuware-2019` is how you get those messages back. It also keeps the name clear
of your job names by construction: a job always has one, so having none cannot be
mistaken for one.

Importing the same source twice under the same name costs nothing but the
reading -- the archive holds each message once, and the second run records the
same place a second time, which `archive compact` folds back together. Two
different names keep two imports apart.

The source has to lie outside the archive, and naming the archive itself is
refused. With `--move` it would find every message already stored, answer each
with EXISTS, and then delete it from the source -- which is the archive.

Either way the run says what it did, and `--dry-run` says what it would do
without writing anything or removing a single source file:

```console
$ mailvault --archive ./backup archive import --dry-run ./my_mails
./my_mails: 20,431 messages read -- 38 would be imported, 20,393 already in ./backup
```

That second number is worth a look before a large import, especially when the
mail has been through another program on its way here. A message whose bytes
were altered -- a header added or stripped, line endings rewritten -- is not the
message the archive already holds, so it is stored a second time under a
different name, and afterwards nothing tells it apart from one that really is
new. If almost everything counts as new when you expected almost nothing to,
that is what happened.

Mail imported before `archive import` took a name has no metadata-log entry at
all: `db update` does not pick it up, and `archive check` counts it among the
messages the log does not account for. Importing the same source again under a
name repairs that, and it is the only thing that does -- the archive cannot
invent a provenance it was never told. It costs the reading of the source and
stores nothing twice.

### Asking what the archive has mail from

```console
$ mailvault --archive ./backup archive places
mailbox    folder                   messages  last seen
gmail.com  INBOX                      12,043  2026-08-12
gmail.com  [Google Mail]/All Mails     4,001  2026-08-12
           docuware-2019               5,412  2026-08-02
           orphaned                      110  2026-08-13
4 places, 17,565 messages
  the column adds up to more: a message can be in several places
```

Two columns rather than the `mailbox::folder` the findings print, because these
two are what `db search --mailbox` and `--folder` take.

A cell is empty where there is no name to print, and the two columns mean
different things by it. **No mailbox** is what an import and an adopted place
look like: there is nobody behind that name to ask again. **No folder** is a
mailbox whose folder was never recorded -- an archive lifted from the database
mailvault kept before 0.8 has one such place per mailbox, often its largest,
because that database stored which mailbox a message came from and not which
folder of it. Nothing can recover that: the folder was never in the archive. A
`backup --full` over the folders in question would record it from now on, at the
price of fetching every message in them again.

Neither is written out as a word. A cell that said "(not recorded)" would sit
where a name sits while not being one, and the first thing anybody would do with
it is type it into `--folder`.

The counts are of distinct messages, which is why the total is smaller than the
column: a message under three Gmail labels lies at three places and is one
message. The per-place counts are distinct too -- a folder read in full records
what it already recorded, so adding up the log files' own headers would report an
archive larger than it is.

A place with only a resume point and no observations is not listed. Gmail has
such places: the folder a job polls and the label the server reports are two
names for one thing, and only the second is where messages are recorded.

### Taking in what belongs to no place

An archive is the message store and the metadata log together: a message belongs
to it once a log file names it. One that lies in `mail/` and is named nowhere is
therefore not a damaged part of the archive but a file that is not part of it
yet -- closer to a file git does not track than to anything in `lost+found`.
`archive check` finds them, `archive adopt` takes them in:

```console
$ mailvault --archive ./backup archive adopt --name orphaned --dry-run
110 messages belong to no place and would be recorded as orphaned
  nothing corrects the log afterwards, so the name has to be right
  nothing was written; leave out --dry-run to record them
```

**The name is the statement of whoever types it.** `--name docuware-2019` says
"these came from that import"; `--name orphaned` says "I do not know where these
came from, and I am saying so". Both are true when the person means them, and
neither is one the archive could have worked out for itself -- which is why this
is a command and not an automatic repair. It is recorded exactly the way an
import is recorded, because it is the same statement: no mailbox, the name in
the folder.

What it cannot do is undo. The log is append-only and nothing corrects it, so a
name given to the wrong messages stays. Hence `--dry-run`, and hence a report
that says how many messages a run is about to speak for.

Messages that already have a place are left alone -- a second place beside a real
one would be a claim of its own, and one this command has no grounds for. Running
it twice is therefore harmless: the second run finds nothing.

Where the directory an import read from still exists, importing it again is the
better move and this is the wrong one: an import records only what really lay in
that directory, so it cannot be wrong about it.

A name that is already a place is not an error -- adopting the leftovers of an
import under that import's name is what this is for. It is said in both modes and
differently on purpose. Before the run it is a choice, and another name is one
keystroke away:

```
docuware-2019 is already a place and holds 5,412 messages; these would go in with them
```

Afterwards there is no choice left to name, so the line is a plain fact about the
outcome -- `recorded as docuware-2019, which now holds 5,415 messages`. That is
what catches a mistyped name: three messages adopted into a place that now holds
five thousand were not adopted where they were meant to go. `archive places`
answers the same question before anything is typed, which is the better way round.

### Exporting a single message

Every report names a message by its id, and that id is what `archive export`
takes. What comes out is the message exactly as it was stored -- whether it lies
compressed, and where in the archive it lies, is the store's business.

### Compressing and decompressing

One entry that cannot be converted does not stop the pass -- a single damaged
file should not cost you the conversion of a whole archive. Those files are
named, left exactly as they are, and the command exits non-zero, so a script
finds out about a conversion that only partly happened.

### Checking an archive

A message file is named after the hash of its content, is never modified and is
written so that it cannot appear half-way. What none of that covers is the time
afterwards: bit rot, a restore that dropped a file, a copy that ran out of disk.
The archive cannot notice any of it on its own, because everything it does asks
whether a *name* is there -- never whether the bytes behind it are still the ones
it was named for.

```console
$ mailvault --archive ./backup archive check
130,997 messages stored, 130,887 of them accounted for by 60 log files in 59 places
3 messages referenced in the log and missing from the archive
  6f3ac1…  mail.example.org::INBOX
NOT sound -- 3 findings above
```

The two message counts are there to be subtracted: what lies in the archive, and
what the log accounts for. The difference is mail whose place nothing records --
named further down, and nothing is damaged about it: it is what an import made
before `archive import` took a `--name` left behind, and what a lost log entry
leaves behind. `archive adopt` takes it in.

The last line is the verdict, and it says which kind of run it was:

```console
$ mailvault --archive ./backup archive check
130,997 messages stored, 130,887 of them accounted for by 60 log files in 59 places
sound -- every message was read and matches its checksum
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
step 1 of 3: 20,000 files seen
step 2 of 3: 60 log files account for 130,997 messages in 219,690 places
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

### Consolidating the metadata log

Every backup writes a small file into `meta/` for each folder that received mail.
Over months these add up, and everything that reads the log -- `db create`,
`db update`, `verify` -- reads all of them. Entries repeat as well: a folder read
in full rather than resumed -- `backup --full`, `incremental = false`, a resume
point the server voided -- records every message it finds, including the ones an
earlier file already names. `compact` folds them back down:

```console
$ mailvault --archive ./backup archive compact
1,204 log files -> 59 across 59 places
41,388 duplicate observations dropped
```

It rewrites one file per mailbox/folder holding each observation once, verifies
the new files, and only then removes the originals -- so it is lossless and safe
to interrupt: a half-done run just leaves both, and the next one finishes. Run it
occasionally; there is no hurry, but do not put it off for years.

Since it is the one pass that has the log open, it also clears away what an
interrupted write left behind there, and says so when it finds anything:

```console
2 leftovers of an interrupted write removed
```

Only files old enough that no running backup can still be writing them, and only
in `meta/` -- the messages are left alone, because looking through them means
walking every directory in the archive for a few kilobytes.


## The query database in detail

The [README](../README.md#the-optional-query-database) covers building it,
searching it and keeping it up to date. What is left:

**It is assembled from two sources.** The messages carry their own subject,
sender, recipients and date; the archive's metadata log records which folder of
which mailbox each was seen in. Nothing in the database is a fact the archive
does not already hold, which is why throwing it away costs nothing.

**The log is the list, and not the store.** Building goes through the log and
reads the messages it names, rather than walking the shard directories and
reading everything that lies there. Two things follow, and the first is the
reason for the second:

- a message the log names nowhere is not in the database. It is also not part of
  the archive yet -- the archive is the mail and the log together -- so this is
  the archive being incomplete and not the database falling short. `archive
  check` reports such messages and `archive adopt` takes them in
- it costs about half of what it used to. A walk pays a round trip per shard
  directory before anything is read, and there are about as many of those as
  there are messages: measured on 2,000 messages, 2,231 directory rounds that no
  longer happen. What is left is one open per message, which is the work itself.
  On a network share that was 16 of the 33 minutes a build took

**Building it on a network share.** Two things decide how long a build takes, and
only one of them is the mail. The other is the database file itself: SQLite
writes it in scattered pages, and a build is given a large page cache so it
writes each page about once instead of about nine times -- with the default two
megabytes it spends the whole run evicting pages a growing B-tree is about to
touch again. That is fixed and needs no option.

What is left is that those pages land on the share. `--temp-dir DIR` builds the
database under `DIR` and copies it into the archive when it is done, which turns
the scattered writes into one sequential copy:

```console
$ mailvault db create --force --temp-dir /var/tmp
```

It takes a directory rather than working one out. Whether the target is slow, and
where there is somewhere fast with room, are both things the person running it
knows and the program does not -- and `TMPDIR` is memory on some systems, which
is not a place to put a database nobody asked to put there. On a local archive
the detour buys nothing and costs a copy, so without the option the database is
built beside its target as before. Either way the last step is the same atomic
rename, and an existing database survives every failure before it.

**A message with an unreadable Date header matches neither `--since` nor
`--until`.** Its date is unknown, not old.

**It is an ordinary SQLite file**, so anything that speaks SQL can read it. Two
views come with it:

```console
$ sqlite3 ./backup/index.db "SELECT date, sender, subject FROM v_messages LIMIT 5"
$ sqlite3 ./backup/index.db "SELECT * FROM v_duplicates"
```

`v_messages` flattens a message together with its subject, sender, recipients
and the places it was seen, so one that went to several recipients or sits in
several folders appears in several rows. Everything is joined to the left on
purpose: a message with no readable recipient -- the group address, the
malformed header, the `Undisclosed recipients:;` -- is still in the view.

`v_duplicates` lists the messages that carry the same `Message-ID` and date but
lie in the archive under more than one `store_id`: the same mail, stored twice
because its bytes differ. See [why the same mail can be in there
twice](#why-the-same-mail-can-be-in-there-twice).

The columns are `message_id` (the row's own id, internal to the database),
`email_id` (the `Message-ID` header) and `store_id` (the content hash, which is
what `archive export` takes).

**Archives that predate the location record** have no metadata log to say where
a message came from, and such a message is not in the database at all: the log is
the list of what the archive accounts for. `archive adopt --name NAME` gives them
a place first, and then they are in it like everything else. `db create
--mailbox NAME` used to make the same claim inside the database only, and is
gone.

**A database written by another version of mailvault** is left alone and
reported rather than read or upgraded. Build that one again with
`db create --force`.


## What an archive looks like inside

Emails are stored as RFC 822 `.eml` files in a content-addressed directory
structure:

```
./archive/mail
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

The archive around them:

```
./archive
├── FORMAT               which layout this archive is written in
├── mailvault.toml       the configuration, optional
├── mail/                the messages
│   └── 00/ … ff/
├── meta/                where each message was seen
│   └── a1/
│       └── a1b2c3….jsonl
└── heads/               where the next run picks up, one file per place
    └── gmail_com-INBOX.3f9a1c2b
```

The messages have their own directory rather than the root, because the root is
what somebody standing in the archive sees, and 256 shard directories there bury
the handful of files worth looking at.

Messages and log files are written without their write bits:

```console
$ ls -l mail/00/00/00003c6ec5464cca9…7af8.eml
-r--r--r-- 1 you you 4711 … 00003c6ec5464cca9…7af8.eml
```

Their name is the hash of their content, so anything that changes one breaks its
name, and the mode says as much to whatever opens the file -- viewers that
"repair" the text file they are displaying are the reason it is worth saying at
all. Comfort, not protection: it stops a slip, not a decision, and it has nothing
to say about deletion. Not every filesystem carries a mode; on a desktop-mounted
SMB share the archive looks exactly as it did before, and nothing is lost by
that. `archive export` hands out a normal, writable file, which is the way to
look at a message.

### `meta/` — where each message was seen

`meta/` answers the one question the messages cannot: which mailbox and which
folder each was seen in. **One file is one place** -- its first line names a
mailbox and a folder, the rest name the messages seen there. A message that
belongs to several places simply appears in several files, so nothing is
ambiguous. These files are content-addressed exactly like the messages, so each
one carries its own integrity check:

```console
$ sha384sum meta/a1/a1b2c3….jsonl     # matches the filename, or the file is damaged
```

They are written once and never modified.

### `heads/` — where the next run picks up

`heads/` holds **one small file per place**, replaced atomically, never edited:

```json
{
  "job": "gmail.com",
  "folder": "INBOX",
  "last_run": "2026-08-05T19:00:00+00:00",
  "resume": { "kind": "imap-uid", "uidvalidity": 1239278212, "uid": 48127 },
  "log": "a1b2c3…"
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

`log` names the newest log file of that place, which is what lets `archive
check` notice that one has gone missing.

One file per place rather than one for all of them, because a damaged file then
costs one folder instead of every folder of every job. The name is a readable
part plus eight hex characters; only the readable part may be shortened, the
rest is the identity.

### `FORMAT` — which layout this is

`FORMAT` holds one line, and it is meant to be read without this program:

```console
$ cat ./archive/FORMAT
mailvault archive format 1
```

A layout can only be recognised by its structure *backwards* -- a newer one
looks familiar in exactly the wrong way, since all the directories a reader
knows are present. So an archive says what it is instead of being guessed at,
and a version that finds a number it does not know refuses the archive rather
than misreading it.

`meta/` and `heads/` are plain text. If you ever want to know what an archive
thinks it contains, you can read it without `mailvault` and without SQL.

### Why the same mail can be in there twice

Emails with the same Message-ID are considered identical from a user
perspective, but if their RFC 822 representation differs, they are stored
separately because the hashes differ. MS Exchange in particular tends to
produce different versions of the same email -- journal copies, for instance,
often differ from mailbox copies by an additional `Received` header and
replaced MIME multipart delimiters.


## Configuration reference

`mailvault` reads its configuration from the archive's own `mailvault.toml`,
or from the file `--config` names -- see [Where the archive is, and which file
describes it](#where-the-archive-is-and-which-file-describes-it). For a file
given with `--config` the name is irrelevant; the content is always parsed as
TOML either way.

### Examples

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
in a `[global]` section: `compress`, `index_db` and `incremental`. They are
marked *(global option)* in the tables below.

```toml
[global]
compress = true
incremental = true   # the default; set to false to re-fetch every folder in full

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

A variable that is not set and has no default is written out as it stands --
`${IMAP_USER}` becomes the user name, and the server answers with whatever it
makes of it. mailvault says so when it reads the configuration, naming the
option and the variable, because nothing after that point can tell that a
variable was meant.

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

For security, `_cmd` fields are only evaluated when `--allow-exec` is passed to
the command that reads the configuration (`mailvault backup --allow-exec`, and
the same for `folders` and `verify`). Without it, `_cmd` fields are ignored with
a warning.

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


## Migrating an older archive

An archive written by an earlier version is lifted to the current layout by one
command. It is the first thing to do after upgrading: until it has run, every
other command refuses the archive and points here.

```console
$ mailvault --archive ./backup archive migrate
46 resume points moved into heads/
130,887 messages moved into 59 mailbox/folder places
the database is now store.db.migrated and is no longer used
delete it once you are satisfied with the archive
256 shards moved into mail/
1,204 log files -> 59 across 59 places
mailvault archive format 1
```

What it lifts, in the order the pieces depend on each other: `state.json` into
`heads/`, then a pre-0.8.0 `store.db` into the metadata log, then the messages
out of the archive root and into `mail/`, then the log consolidated so every
place has one file to start its chain from -- and only then the mark.

**The mark is written last on purpose.** An interruption anywhere above leaves
the older number standing, so the next run picks the work up where it stopped;
everything before the mark is idempotent, so repeating it costs nothing. A mark
written first would claim a layout that only half exists.

Nothing is deleted. The database is renamed and left alone, so you keep a way
back until you remove it yourself. Moving the messages is at most 256 directory
renames -- a rename within a filesystem moves no data, so the cost does not grow
with the number of messages. Running the command again on a current archive
reads one small file and stops.

### If you used to query `store.db` with SQL

0.8.0 removed the SQLite database that used to live inside the archive; what it
knew is now kept in files that are only ever written once or replaced atomically.
An archive still carrying a `store.db` is lifted by the same command, and the
database is renamed rather than removed.

The archive no longer keeps its truth in a database. What it can do is build a
query database on demand, as `index.db` inside the archive: `mailvault db
create`, kept up to date by `db update` or by `index_db = true`, and searchable
without SQL at all — see [the query database in
detail](#the-query-database-in-detail).


## Coming from ib-mailbox and ib-archive

The former `ib-mailbox` and `ib-archive` commands are now subcommands of a single
`mailvault` command. The archive and the configuration are named **before** the
command, everything else after it.

| Previously | Now |
|------------|-----|
| `ib-mailbox --config c.toml folders` | `mailvault folders`, from inside the archive |
| `ib-mailbox --config c.toml backup <dest>` | `mailvault --archive <dest> backup` |
| `ib-mailbox --config c.toml verify [--repair] <dest>` | `mailvault --archive <dest> verify [--repair]` |
| `ib-archive stats\|import\|compress\|decompress <dir>` | `mailvault --archive <dir> archive stats\|import\|compress\|decompress` |
| `ib-archive db-from-archive --mailbox NAME <dir>` | `mailvault --archive <dir> archive adopt --name NAME`, then `db create` |
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
