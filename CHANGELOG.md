# Changelog

## Unreleased

### Added

- **A backup into the wrong archive is refused before it starts.** The configuration file and the
  destination are two independent arguments, and `--config work.toml backup ~/mail/private` looks
  perfectly fine until the first message is written. `backup` and `verify` now check, before the
  first login, that each selected job has written into that archive before -- the archive knows
  from `heads/`, or from the metadata log if that is gone. A job that has not stops the run
  and is named. The check looks only in the writing direction: a mailbox in the archive with no
  job in the configuration is nobody's business, so removing a job, commenting one out or picking
  a few with `--job` stay free of ceremony, and an archive nobody has written into takes anything

- **`--allow-new-mailbox`** is the way past it for the one case it cannot tell from a mix-up: a
  genuinely new job. One run with the flag, and from the next one it is known

- **`archive export`** writes a message back out, byte for byte as it went in -- to standard
  output, or to a file with `--output`. It takes the message id the reports print, and that is all
  it takes: whether the message lies compressed, and where in the archive it lies, is the store's
  business. The way to look at a message the reports can only name

- **`archive check` runs the integrity check by default.** `--contents` is gone; the check it
  asked for is what the command does, and **`--no-integrity-check`** leaves it out. It was made
  optional on the assumption that reading every message costs an order of magnitude more than
  walking the tree, and measurement says otherwise: on a 131,000-message archive over SMB the walk
  took 16 minutes and reading every message 17. A network share charges for round trips, not for
  bytes -- the walk pays one per shard directory, the read one per message, and at a couple of
  messages per shard those come out level. Being able to find a message whose bytes changed under
  it is worth a factor of two. `--quarantine` no longer needs a companion flag; it refuses to be
  combined with `--no-integrity-check` instead

- **`archive check` says whether the archive is all right**, in words, instead of leaving the
  verdict to an exit code nobody reads unless they went looking for it -- and it says which kind
  of run it was, because one with `--no-integrity-check` never read a message and cannot have
  found one whose bytes changed. Its counts are in plain terms too: `5 message(s) stored, filed in
  7 place(s) by 2 log file(s)`, since more places than messages is the normal case for a message
  filed in two folders, not a discrepancy to worry about. Findings say what is wrong rather than
  how the store found out -- a message is *damaged* when its content does not match its checksum,
  where it used to "not match the name it is filed under". The long passes number themselves
  (`step 1 of 3: looking through the archive`), so a run that takes half an hour says how much of
  it is still ahead

- **`archive import --dry-run`** reads and hashes every message and reports how many the archive
  would gain, without writing anything or removing a source file. Mail that has been through
  another program on its way here may not be byte-identical to what the archive already holds --
  a header added or stripped is enough -- and such a message is stored a second time under a
  different name, after which nothing tells it apart from one that really is new. A "would be
  imported" count where a small one was expected is the only warning there is, and this is how to
  see it beforehand. `import` now also reports what it did on a real run, and exits non-zero when
  a source file could not be read

- **The configuration lives in the archive.** An archive's own `mailvault.toml` is what every
  command reads, so a backup from inside it needs nothing at all: `cd /srv/archive/private &&
  mailvault backup`. A configuration and an archive can no longer drift apart, because they are
  the same directory -- and a backup of the archive now carries the recipe along with the mail
  instead of saving the mail and losing the recipe. `--config FILE` still names one from
  elsewhere; it then has to name the archive too, since reaching for a file somewhere else is
  what somebody does who is *not* standing in the archive

- **`--archive DIR`** is the other way to say which archive, and the only other way -- what
  `git -C` is. It applies to every command, so `mailvault --archive /srv/archive/private archive
  check` works from anywhere

- **An archive says which layout it is written in.** A `FORMAT` file in its root holds one
  self-explanatory line -- `mailvault archive format 1` -- so a `cat` five years from now answers
  the question without this program. Recognising a layout by its structure only works backwards: a
  newer one looks familiar in exactly the wrong way, because every directory the reader knows is
  present. A version that finds a number it does not know refuses the archive and says to upgrade,
  rather than misreading it. No file means the layout as it was before the mark existed

- **The messages live in `mail/`**, not in the archive root. The root is what somebody standing in
  the archive sees, and 256 shard directories there bury the handful of files worth looking at. It
  also gives the root back to the archive: while the store claimed it, a stray file lying there was
  nobody's to judge and `archive check` had to pass over it in silence

- **The resume points live in `heads/`, one small file per place**, where `state.json` held them
  all in one structure. A backup writes after every folder, so a run over forty of them rewrote the
  whole thing forty times -- but the reason is what a damaged file costs: `state.json` was decoded
  as a whole, so one bad byte discarded every folder of every job and sent the next run over all of
  them in full. One file per place makes the same bad byte cost one folder

- **`archive migrate` lifts an archive of any earlier shape**, in one command: `state.json` into
  `heads/`, a pre-0.8.0 `store.db` into the metadata log, the messages into `mail/`, the log
  consolidated, and only then the mark. The next backup does it by itself. The mark is written last
  on purpose -- an interruption leaves the older number standing and the next run picks the work up,
  where a mark written first would claim a layout that only half exists. Moving the messages is at
  most 256 directory renames, so it does not grow with the size of the archive

- **A place's log files form a chain.** Each header names the file that held that place before it,
  and `heads/` names the newest. The chain is the check, never the enumeration -- reading still goes
  through the directory, so a broken link hides nothing. What it catches is narrow and worth saying
  exactly: a lost log file usually announces itself already, because its messages turn up in
  `archive check` as having no provenance. That does not happen when the same message is recorded
  elsewhere too -- a Gmail message filed under three labels lives in three files -- and then the
  loss of one of its places is completely silent

### Breaking changes

- **No command takes an archive as a positional argument any more.** The archive is the directory
  you are standing in, or `--archive DIR`. `mailvault backup ./backup` becomes
  `mailvault --archive ./backup backup`, and `mailvault archive check ./backup` becomes
  `mailvault --archive ./backup archive check`. `archive import` keeps its one positional, which
  was never the archive: it is the foreign directory being read from

- **`--config` is no longer required** by `folders`, `backup` and `verify`, and passing it to
  `backup` or `verify` without `--archive` is now an error rather than a run into whichever
  directory the shell happened to be in

- **Move your configuration into the archive it describes**, as `mailvault.toml`. Nothing does
  this for you and nothing looks for the old location, so a run without `--config` after
  upgrading says which file it wanted and did not find. `archive check` knows the file as a
  legitimate inhabitant and does not report it

- **The archive layout moved.** The migration is automatic on the next backup, or explicit with
  `archive migrate`; nothing is deleted either way. **Upgrade every machine that writes into the
  archive before lifting it.** The `FORMAT` file protects this version from a *newer* archive; it
  cannot protect an archive from an *older* mailvault, which knows nothing about it. A 0.9.x run
  against a lifted archive finds no messages where it looks and no resume state, takes the archive
  for empty, and downloads everything again into the old location -- nothing is lost, but you end
  up with the mail in two places and a full re-download to undo

### Changed

- **An incremental backup resumes from where the server says it is, not from a date.** IMAP now
  asks for everything above the highest UID it has archived, Microsoft 365 follows a delta link.
  Both close a hole a date filter cannot: a message copied or moved into a folder keeps its
  original date, so it lands *behind* the resume date and is never asked for again -- while it
  gets a new UID and turns up in the next delta round. On a real 77,000-message mailbox this had
  swallowed 29 messages over the years, silently. If you have been backing up with an earlier
  version, `verify --repair` will find what it missed

- **`state.json` is version 2**, splitting what used to be one timestamp into two things that were
  never the same: `last_run` says when a run last read a folder, `resume` says where the next one
  carries on. Version 1 files are still read and their timestamps kept as `last_run`, but they do
  not become resume points -- they came from the wall clock, and adopting them would inherit
  exactly the gap they could hide. The first run after upgrading therefore reads every folder in
  full, once, and says so

- **Reading a folder in full no longer means downloading it again.** Where the archive already
  holds mail at that place -- after the upgrade above, or when a server voids its own resume point
  -- the folder is listed and compared, and only what is missing is fetched. Listing 77,000
  messages takes half a minute; downloading them does not. `backup --full` is unchanged and stays
  the read that trusts nothing

### Fixed

- **Gmail recorded mail as being somewhere else.** `X-GM-LABELS` leaves out the label of the
  folder currently selected -- measured: of 80 messages in `INBOX` not one reported a label, while
  the same messages fetched from All Mail report `\Inbox`. The labels alone were taken as the whole
  location, so backing up any Gmail folder but All Mail recorded every message as being "somewhere
  in All Mail" instead of in the folder being backed up. The folder being read is now always among
  the places recorded. Three more things follow from that one line: `verify` looked up what was
  archived by folder name and found nothing, so a Gmail job reported the *entire* folder as
  unarchived; the backup's catch-up used the same key and therefore read Gmail folders in full
  instead of listing them; and a repaired message is now filed where a backup would file it

- **`verify` says what it is doing while it does it.** Every line it logged reported completion, so
  the two passes that take the time announced themselves only once they were over -- minutes of
  silence on a large archive, and a run that looks like a hung process is one nobody trusts. Both
  now report before and during. Worth naming explicitly: the long one is reading the *local*
  archive, not the mailbox

- **A message could hide behind another with the same Message-ID.** `verify` and the new full read
  ask how many copies of a Message-ID the archive holds rather than whether it holds one, so a
  second message that shares the id but differs in its bytes is no longer taken for one already
  archived

## 0.9.4 (2026-08-06)

### Added

- **`mailvault archive check` holds an archive against what it claims.** The store is built so
  that an entry cannot be written half-way, but nothing covered the time afterwards -- bit rot, a
  restore that dropped a file, a copy that ran out of disk, an entry written by a version that did
  not yet flush. The archive could not notice any of it on its own, because it asks whether a
  *name* is there, never whether the bytes behind it are still the ones it was named for. This
  checks that every file lying in a shard is an entry, that every entry the metadata log names is
  there, and that every log file still matches its own name. It repairs nothing and exits non-zero
  when the archive is not what it says it is

- **`--contents` reads every entry and holds it against the name it is filed under.** The only way
  to find one whose bytes have changed under it -- and an order of magnitude more work than the
  rest, so it is asked for rather than assumed. A run without it says so in its last line, because
  otherwise "nothing found" would mean two different things on two different days

- **`--quarantine` takes the name away from an entry that fails that check.** Renamed to
  `<hash>.eml.corrupt`, never deleted: a message with a flipped bit is still almost all of the
  message, and what has to stop is the *claim*, not the bytes. Out of the store's name space the
  message counts as missing again, so `verify --repair` or `backup --full` will fetch it. Refused
  without `--contents`, where it could not find anything to act on and would look effective while
  doing nothing

## 0.9.3 (2026-08-06)

### Added

- **`archive compact` clears away what an interrupted write left behind in `meta/`**, and reports
  how many it found -- each one is a write that did not finish, which is worth saying rather than
  tidying up quietly. Only files old enough that no running backup can still hold them, and only
  in the metadata log: sweeping the messages too would mean walking every directory in the archive
  for a few kilobytes, on a command that otherwise touches nothing but the log

### Fixed

- **Two runs writing into one archive at the same time can no longer damage an entry.** Every
  message was written to a transient file whose name was derived from the message itself, so two
  runs storing the same message -- two mailboxes holding the same mail, a backup and a repair
  overlapping, one run started twice by accident -- opened the *same* file and wrote into it at
  once. The result was renamed into place looking like a perfectly normal entry, and its content
  did not match its name. Transient files now belong to one writer alone; racing for the same
  entry is what makes the store deduplicate and stays harmless

- **An entry now survives a power cut, name included.** The content reaches the device before the
  entry is renamed into place and the directory entry naming it afterwards -- neither used to
  happen for messages, and only the first for the metadata log. Both are failures nothing in the
  archive could have found later: a rename that overtakes the content publishes a file under a
  name claiming to be the hash of bytes that never arrived, and the store answers "is this message
  here?" by looking at names, not by reading them. The reasoning this was once left off for -- a
  lost message is fetched again next run -- does not hold either: the resume point has already
  moved past it, so an incremental run only re-fetches it if it falls inside IMAP's one-day
  overlap window, and on Microsoft 365 not at all. One flush per message is nothing next to the
  download that produced it

- **`archive compress` and `archive decompress` no longer delete the original before the converted
  entry is durable.** Convert, rename, unlink -- with nothing flushed in between, a power cut in
  that window could take both copies. It is the one path in the archive where a message could be
  lost outright rather than merely re-fetched, and it runs over every entry there is

- **A store id that is not a hash is refused instead of being followed.** Store ids come back from
  the database, from the metadata log and from the command line, and a path is derived from one by
  cutting it into directory names -- so `../..` cut into components climbed out of the store
  entirely. Now rejected where such a value enters, and an uppercase hash is accepted rather than
  quietly not found. In the metadata log, which is allowed to be damaged and says so, such a line
  is skipped with a warning instead: one unusable entry must not cost the readable ones beside it

- **`archive compress` and `archive decompress` report the entries they could not convert**, name
  them, and exit non-zero. A pass keeps going when one entry fails -- one damaged file should not
  stop a run over a whole archive -- but it used to count only what worked, so a partly failed
  conversion was indistinguishable from a complete one unless you read the log

- **A file that is not an entry no longer becomes a database row.** `create-db` turns each file
  name in the store back into a store id, and the walk handed it everything ending in `.eml` --
  a message copied in by hand under its subject, the leftover of an interrupted run. Only files
  actually named after a hash count as entries now

- **`read_header` stops exactly at the limit it was given** instead of wherever the last block
  happened to end

- **A shard depth the hash is too short for fails when the store is opened**, not on every write;
  a negative depth is an error rather than being silently turned into the default

### Changed

- **`compress_all` and `decompress_all` return a `ConversionResult`** (`converted`, `skipped`,
  `failed`) rather than a pair of counts. `result.converted` and `result.skipped` are what the two
  numbers used to be

- **`ContentAddressedStorage` no longer takes an `fsync` argument.** Flushing an entry to the
  device is what the store does, not something a caller decides: an entry that is there but wrong,
  or gone without the run that wrote it noticing, is not a state any caller should be able to opt
  into

- **`ContentAddressedStorage` gained `verify`, `hashval` and `hashval_of`** -- check an entry
  against the name it is filed under, name content without storing it, and read a store id back
  out of a path. Checking a file against its own name is the guarantee the whole design exists
  for, and it was the one thing the store could not do

## 0.9.2 (2026-08-05)

### Added

- **`backup --full`** re-reads every folder in full for one run, without having to edit
  `incremental` in the configuration and put it back afterwards. A full run is also the
  authoritative one: it sees the mailbox without a date filter, so it sets each folder's resume
  timestamp to exactly the mail it found -- backwards too, which is what puts a timestamp right
  that an earlier run had set too far ahead

### Fixed

- **A source that is not serving its mail yet no longer costs you that mail.** The resume timestamp
  was the time the run happened, which quietly assumed the server had shown everything it had.
  Proton Bridge does not: it accepts IMAP connections minutes before its first sync completes and
  answers, without any error, that the folder is empty. The run then recorded "archived up to
  today", and on the next run every message actually sitting in that mailbox -- all of it older
  than today -- fell before the date filter and was never fetched again. The backup looked clean
  both times. The timestamp is now the date of the newest message a run really archived, so a
  folder that offered nothing gets no timestamp and is read in full next time. The same applies to
  any source that comes up slowly: an IMAP proxy with a cold cache, a server still mounting a
  mailbox

- **An incremental run no longer trusts a message dated in the future.** A sender with a wrong
  clock could push a folder's resume point past the moment the folder was actually read, skipping
  whatever arrived in between

## 0.9.1 (2026-08-04)

### Fixed

- **A wrong password no longer prints a traceback.** A refused login is not a crash: the server
  said what was wrong ("no such user"), and eight frames through `imapclient` add nothing to that.
  The same goes for a server that cannot be reached, for a Microsoft 365 tenant that refuses to
  issue a token, and for a Graph job that lacks the `Mail.ReadWrite` permission it was told to
  use -- each is now the one line that says it, and the run still exits non-zero and carries on
  with the remaining jobs. The traceback is reserved for the errors nobody anticipated, where the
  call stack is the only clue there is, and `--verbose` still brings it back for the rest

## 0.9.0 (2026-08-04)

### Added

- **`permanent_delete` for Microsoft 365 jobs.** With `delete_after_export`, Graph only *soft*
  deletes: the message moves to Deleted Items and keeps occupying the quota, so a mailbox being
  cleared out never actually shrank. This deletes for good instead -- exactly the messages that
  were archived, one by one, leaving anything else in the bin alone. It is the counterpart to
  Gmail's `trash_folder` and, unlike it, touches nothing it did not archive. Retention policies and
  holds still apply. Requires `delete_after_export`, and is refused on any other backend

- **`error_folder` now works on Microsoft 365, not only on IMAP.** Journaling is a Microsoft
  feature, but the escape hatch for items in a journal mailbox that turn out not to be journal
  envelopes existed only on the IMAP side; on Graph such an item was reported and left lying
  around. Both backends file it away now. The folder is **created if it does not exist**, so an
  unattended job does not stop because someone tidied it away -- on Graph this needs the
  `Mail.ReadWrite` permission, and without it the job stops naming exactly that instead of failing
  obscurely

- **A missing IMAP `MOVE` capability no longer disables the error folder.** `MOVE` is RFC 6851 and
  not part of IMAP4rev1 -- Exchange's own IMAP service, of all things, tends not to offer it, which
  is precisely where journal mailboxes live. Where it is missing, the `COPY` + `\Deleted` sequence
  it replaced is used instead. Without `UIDPLUS` the flag is left standing rather than issuing a
  plain `EXPUNGE`, which would drop every deleted message in the folder and not just this one

### Changed

- **A job that sets `trash_folder` without `delete_after_export` is now refused**, as is one that
  sets it on the Graph backend, where it never did anything. That folder is emptied *completely*,
  including mail its owner put there and mail that was never archived -- and a job that did not ask
  to delete anything has no business doing that. It is the one place where mailvault removes mail
  it did not archive, so it now requires the option that says deleting is intended. The same rules
  apply to `permanent_delete` on any backend but Graph: an option that decides the fate of mail
  must never look effective while doing nothing

- **What `delete_after_export` really does is documented per backend.** On plain IMAP the message
  is expunged and gone. On Gmail it lands in the trash, which is what `trash_folder` is for. On
  Microsoft 365 it is a soft delete into Deleted Items and stays there -- mailvault does not empty
  that folder, so the mailbox does not actually shrink. None of this was written down anywhere,
  and it is exactly the kind of surprise nobody wants while clearing out a mailbox

- **`error_folder` is documented, and reports when it does nothing.** It is the escape hatch for
  `exchange_journal` and for nothing else -- an ordinary backup reads and, on request, deletes, but
  never relocates. Setting it on a job that is not a journal job says so when the config is loaded.
  `trash_folder` is documented too

- **The mailbox backends only do what a backup needs now.** `get_messages`, `save_message`,
  `delete_message` and the IMAP `IDLE` watch existed solely for `copy` and were removed with it, so
  a backend no longer offers to write to or delete from a mailbox except through the
  delete-after-export path, which still deletes only once the metadata log is sealed

### Removed

- **The `copy` command is gone, with its configuration and its `--idle` mode.** It transferred mail
  between two IMAP mailboxes and never touched the archive -- the one thing every other command in
  this tool exists for. It was committed in 2022 described as "work in progress and not yet usable"
  and never became usable: in four years it received no fix and no feature, and its `--idle` mode
  re-copied the entire INBOX on every notification, which duplicates mail unless the source is
  drained as it goes. For transferring mail between mailboxes use
  [imapsync](https://github.com/imapsync/imapsync) or [mbsync](https://isync.sourceforge.io/),
  which do it properly. The last release that carried it is 0.8.2

  A configuration written for it still loads, and its backup jobs keep working -- but `[copy]`,
  `role`, `move_to_archive` and `archive_folder` are each reported as retired and do nothing. There
  is no replacement in this tool; remove them

### Fixed

- **`archive compact` leaves no empty directories behind.** Consolidating the metadata log emptied
  most of its shard directories and then left them standing, so `meta/` kept a skeleton of the runs
  it had folded away. Compaction now removes them. Nothing else is touched: a directory that still
  holds anything at all stays, and the mail store never needs this because entries only ever arrive
  there

- **Filing a non-journal item into the error folder could abort the folder's backup.** The
  relocation was attempted in the middle of the backup pass, which holds the folder open
  read-only -- a server must refuse to move mail out of it, and the error was not caught. Affected
  items are now collected and moved once the pass is over, the same way deletion already waits.
  Nobody is likely to have hit this: the option is only reachable with `exchange_journal`, and it
  was silently disabled on every server without `MOVE`

## 0.8.2 (2026-08-03)

### Breaking changes

- **`copy` is configured in a `[copy]` section now, not on the jobs themselves.** It named its two
  mailboxes by tagging them `role = "source"` / `role = "destination"`, which put a command that
  never touches an archive into the middle of the job list and left `--job` unable to address it.
  The section names the jobs instead, so a `[[job]]` again says only how to reach a mailbox:

  ```toml
  [copy]
  source = "source_account"
  destination = "destination_account"
  move_to_folder = "Old/%Y"
  ```

  A name matching no job, or the same job on both ends, is now refused before anything connects --
  the latter would have copied a mailbox onto itself

- **`move_to_archive` and `archive_folder` are one option, `[copy] move_to_folder`.** Naming the
  folder is what turns moving on, so the separate switch is gone, and the old name claimed the word
  "archive" for something that is not one: everywhere else in this tool an archive is the local
  store, while this is a folder on the source server. Replace `move_to_archive = true` plus
  `archive_folder = "Archive/%Y"` with `move_to_folder = "Old/%Y"` in the `[copy]` section

  Each retired option is reported by name when the configuration is loaded, so nothing changes
  behaviour quietly -- but a configuration that still uses them has no `[copy]` section, and `copy`
  stops rather than guessing

## 0.8.1 (2026-08-02)

### Breaking changes

- **`incremental` is a global option now, set once under `[global]` rather than per job.** It
  decides how a run resumes, not how one mailbox is reached, so a value per job only invited the
  two to disagree. The default is unchanged (`true`). A job that still sets `incremental` says so
  on load and the line is dropped, rather than being quietly obeyed for that one mailbox -- move
  it into `[global]`

### Changed

- **A legacy archive is migrated before the mailbox is opened, and says so.** The first backup of
  a pre-0.8 archive has to move `store.db` onto the log, which on a large archive takes a moment;
  it used to happen silently after the job's first line, so the run looked stuck. It now prints
  that it is migrating and that this happens once, and it does the work -- which is purely local
  -- before connecting to the server, so no mailbox connection is held open across it

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

- **`archive compact`** consolidates the metadata log: it folds the many small per-folder files
  backups leave -- with entries repeated across the incremental overlap -- into one file per
  mailbox/folder holding each observation once. Lossless and safe to interrupt; the originals are
  removed only after the consolidated files are written and verified

- **`backup --index-db`** keeps a queryable SQLite database up to date beside the archive
  (`index.db`), refreshed after each backup. A convenience projection, never a source of truth:
  only the log files added since the last refresh are folded in, and a database that is missing or
  unreadable is rebuilt from scratch. Mail added by `archive import` writes no log and is not
  picked up -- rebuild with `archive create-db` for that. Also settable as `index_db` under
  `[global]` in the config

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
