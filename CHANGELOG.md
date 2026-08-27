# Changelog

## 0.15.2 (2026-08-27)

### Fixed

- **A repair could leave a message in the archive that nothing in it names.** `verify --repair`
  stored the message and then went to the network a second time to ask where it belonged. A
  connection that broke in between was reported as "storing failed" while the bytes stayed --
  exactly the message `archive check` afterwards reports as belonging to no known place, and
  there is no later run that takes it in, because the store already has it. Where a message is
  now comes back with the message itself, so once the archive has been written to there is
  nothing left to ask

- **`archive check` no longer tells a reader to upgrade over a damaged log file.** A log file the
  chain names and this version cannot read was reported as "written by a newer mailvault --
  upgrade to read it, the archive is not missing anything", whatever the reason. There are five
  reasons and only one of them is a version: the file may equally be unopenable, not UTF-8,
  empty, or carry a header that will not parse. Those are now reported for what they are, with
  restoring the file as the move -- and a damaged file was already counted among the findings, so
  the count does not change

- **`db search --ids`, `--csv` and `--json` say when `--limit` cut the answer short.** The table
  has said so all along; the machine-readable formats handed over exactly N rows with nothing
  anywhere to say more had matched, so `db search --limit 100 --ids | xargs …` looked like a
  complete answer. They carry rows and nothing around them, so the note goes to the log -- the
  same place the complaint about a database that has fallen behind already goes

- **A search against an archive with no query database left an empty file behind.** `db search`
  refused with "build it again", and `db create` then declined because something was already
  there -- the advice the error had just given could not be followed without `--force`. The file
  is no longer created by asking, and `freshness` answers that a database which is not there
  cannot be read, instead of leaving that question to each caller

### Changed

- **A new query database is smaller and cheaper to write.** Six of its fourteen indexes repeated
  ones SQLite makes for itself on a UNIQUE column, so every message inserted maintained six extra
  B-trees that no query ever read. Measured on a projection of 4,000 messages: 23% off the file
  (3,670,016 bytes against 2,818,048) and not one query plan different. Existing databases keep
  working and are unaffected until the next `db create --force`

- **A query database is checked against the statements that built it, not only against the names
  of its objects.** A view or index carrying a known name in an older shape -- both of which
  earlier releases really did write -- answered queries differently and passed as complete

- **A Gmail backup no longer pays a round trip per message for its labels.** The labels come back
  in the same FETCH as the message body, where they used to be a call of their own for every
  single message. Measured against a live Gmail account: backing up a folder of five messages cost
  six FETCH commands and now costs one, and a run over an archive saves one round trip per message.
  A repair pays one selection and one FETCH per message instead of two of each

- **A repair that cannot read a Gmail message's labels no longer stores it with them missing.**
  It used to fetch the body, store it, and then ask the server separately where the message
  belonged; a failure of that second call was caught and the message was recorded under the
  folder being walked alone, with its other labels dropped -- silently, and for good, because no
  later run revisits a message the archive already holds. Body and labels now arrive together, so
  a failure happens before anything is stored: the message is counted as failed, reported, and
  fetched again by the next run

## 0.15.1 (2026-08-26)

### Fixed

- **The `check` report sent a reader off to build a query database that leaves out the very
  messages it was recommended for.** A message the log names nowhere is what `archive check`
  reports as belonging to no known place -- and the query database is built from the log, so
  `db create`, which the report named as the way to find them, is the one thing that does not.
  On a large archive that is half an hour of building a database and then not finding what it was
  built for. The report names `archive adopt` and says on the same line that a query database
  leaves them out until it has run

## 0.15.0 (2026-08-26)

### Breaking changes

- **`archive export` is now `mailvault get`.** The old name is gone; a script or cron job that
  calls `mailvault archive export …` has to say `mailvault get …` instead, and nothing else about
  it changes -- the same ids, the same `--output`, the same output. `archive` is the group that
  looks after an archive (`init`, `migrate`, `compact`, `check`); taking a message out of one is
  using it, which is what `backup` and `verify` are, and it belongs beside them

### Added

- **`get --path` says where a message lies instead of handing it over.** One path per line, which
  is the form a script wants when it has to point something else at the file. What it names is the
  archive's own entry -- write-protected, and a `.eml.zst` where the archive is compressed -- so it
  answers where the message is; `get` without it is what hands one over ready to read. Naming both
  `--path` and `--output` is refused: they are two answers to the same question

### Changed

- **A log file's header no longer says how many lines follow it.** The `messages` count in the
  first line of a `meta/….jsonl` file was what the lines below it already are, and it is knowable
  up front only because this writer happens to hold a whole place before it writes -- a format
  written a line at a time generally cannot say. The one thing the count caught, a file cut short
  exactly at a line boundary, the file's name catches as well: that name is the hash of the whole
  content. Files written before this keep the field and are read exactly as they were

## 0.14.3 (2026-08-23)

### Fixed

- **The line a backup leaves about a query database it cannot read named the way out in the
  middle of it.** What became of the file -- left untouched and not updated -- came after the
  command to run about it, in a second dashed clause. It names the state, then what that means
  for the file, then the move, in that order

## 0.14.2 (2026-08-23)

### Breaking changes

- **The query database is built in a new shape, and one from an earlier version is refused.**
  `index.db` is not read, not written and not lifted into the new shape -- `mailvault db create
  --force` builds it again, which is the only thing that ever happens to a projection: everything
  in it comes from the archive and nothing in it is lost. Until that has run, `db search` refuses
  with the same sentence, and a backup with `index_db = true` leaves the file exactly as it is and
  says so

  **What to check.** A scheduled `db update` starts exiting non-zero over it. That is the state
  being reported rather than a new failure, and one `db create --force` ends it

- **`IndexDatabase` opens a database and no longer creates one.** For anything using the library
  rather than the command: `IndexDatabase(path, create=True)` writes the schema into a new file,
  and that is the only way one is made. `setup()` is gone -- `create()` writes the schema,
  `missing()` names the objects a file does not have, and `usable` answers whether it can be
  queried at all. The `bulk` argument is gone with it, because every connection now gets the page
  cache a build used to ask for. `RefreshResult.outdated` is now `RefreshResult.unreadable`: it
  covers every shape that cannot be queried, not only an older version's

### Added

- **`archive export` takes the beginning of a message id.** A report prints the first twelve
  characters of one, and those are now enough -- as much of an id as names a single message works,
  the way a short commit hash does. A beginning that fits several messages is refused by saying so
  rather than by handing over whichever came first, and one too short to look up asks for six
  characters, which is where a prefix starts naming a single message

### Fixed

- **Opening the query database rebuilt two of its indexes, every single time.** A migration that
  had to drop and recreate the recipient indexes ran on every open, including for a search that
  only meant to read. On a large archive on an SMB share that was 16.5 s per open, and a search
  opens the database twice. Opening it now writes nothing at all: what is there is checked, and a
  file that is not the current shape is named and left alone

- **A date filter looked through every message in the archive.** `--since` and `--until` were
  computed for each row, so no index could answer them and every search that named a day read the
  whole database. They are compared against the stored date itself now, and there is an index over
  it. The day named is still the whole day it names, down to its last second, whatever offset the
  message was written with

### Changed

- **`db search` prints the shortened id with nothing after it.** The ellipsis said the id was
  shortened and got selected along with it, so what was pasted into `archive export` was an id it
  could not read. The column is a handle; nothing clings to it

- **A search over a network share costs seconds instead of a minute.** Along with the two fixes
  above, the database is written in larger pages and read with a page cache to match -- over a
  share, a page is a round trip, and the default two megabytes of cache held thirty-two of them.
  Measured on the same archive: `db search --since` went from 60 s to 0.4 s, `--from` (a substring
  match, so it has to look at every message) from 30 s to 3.6 s, and reading out every message in
  it takes 4.7 s

## 0.14.1 (2026-08-22)

### Fixed

- **`verify --repair` gave a Gmail message back without its labels.** A backup records every place
  a message is in -- on Gmail that is each of its labels, which the backend reports along with the
  message. The repair path wrote the one folder it happened to be walking instead, so a message
  restored after a loss came back stripped of its other places. Where a message was seen is the one
  fact an archive cannot work out again from what it holds, and nothing said a word about it: the
  run reported the message restored. Backends now answer `places_of` and the repair asks

- **An archive written by a newer mailvault is no longer reported as damaged.** A log file this
  version cannot read is skipped with a warning -- and the chain that names it then counted it as
  *gone*, so `archive check` answered `NOT sound` and exited 1 over a file that is present and
  intact. It is now told apart from a file that really is missing and says what to do about it:
  upgrade. The chain still stops there, because the link to the file before it is inside the file

- **`archive import --dry-run` counted a repeated message as new every time.** The dry run asks the
  store, and nothing is written to the store while it runs -- so the same message twice in the
  source was new twice, where the real import stores it once and recognises the second. That is
  exactly the ratio the dry run exists to show, and it was the one it got wrong. It now keeps its
  own account of what it would have written

- **A `db search` answer says when it may be short.** The query database can be behind the
  archive, in which case what a search finds is true and not all of it -- and the sentence saying
  so went to the log while the hits went to stdout. `mailvault db search --sender x > hits` kept
  the hits and left the caveat on the terminal, so the file claimed a completeness it did not
  have. It now goes out with the answer it qualifies. `--json`, `--csv` and `--ids` keep it in the
  log: a sentence cannot be put into those without an envelope around the data, and there is none
  yet

- **`1 message carry no date` now says `carries`.** The line `db create` gained in 0.14.0 read as
  a plural whatever the count was

## 0.14.0 (2026-08-22)

### Breaking changes

- **A backup that left something behind exits non-zero.** A folder that could not be read, or whose
  messages could not all be stored, has never stopped the folders after it -- which is right -- but
  the run then ended with exit code 0 and was indistinguishable from one where everything worked.
  It now ends with 1. **A cron job that reacts to a non-zero exit will start reporting runs that
  were already falling short before this, quietly**

- **`verify` answers with its exit code as well.** An archive with a gap in it ended with 0, so a
  nightly `verify` that found three thousand messages missing looked exactly like one that found
  none -- while `archive check` has always ended non-zero over the same finding. It now does too,
  with or without `--repair`. The further copies are deliberately not counted: they are duplicates
  of mail an archive holds once, a folder can hold thousands, and a run failing over them every
  night would teach its owner to stop reading the exit code. A repair that fetched mail and could
  not write down where it belongs is now said out loud and counted as well

  **What to check.** Nothing has to be changed for the archive's sake -- both commands do what
  they always did, and say the same thing in words that they now say in the exit code. What may
  need looking at is anything that reads it: a cron entry that mails on failure will start
  mailing, and the first such mail is worth reading rather than silencing. It reports a run that
  was already falling short before this, quietly. There is no flag to get the old codes back;
  a wrapper ending in `|| true` is the honest way to say "I know, and I do not want to hear it"

### Added

- **A backup says what it did when it is done.** `mailvault backup` reported nothing at all: a
  run's entire account of itself was in the log, so finding out whether anything was missing meant
  reading a night's worth of lines, and a script had no way of asking. It now ends with a line of
  its own -- `example.com: 6 messages seen, 2 stored, 4 already archived` -- and what was removed
  from the server where a job deletes after export. Three numbers rather than one, because
  `2 stored` on its own is read as "two mails arrived": mail filed from one folder into another is
  offered again at its new place, and it is the third number that tells a quiet night from a
  tidy-up. A folder the run could not finish is named under it, along with what becomes of it: it
  is read again next run. A run with nothing to do says `nothing new in 15 folders` rather than
  saying nothing

### Changed

- **The archive a run works on is named on a line of its own.** `START -- archive: /srv/archive`
  hung the subject of the run on the end of the word START, where it read as an aside. It is now
  `START` followed by `Archive: /srv/archive`, which is how the run labels its other subject a
  moment later (`Job: example.org`)

### Fixed

- **`index.db: 2 new message(s) from 2 log file(s)` counted something else than it seemed to.** At
  the end of a backup that line reads as "two mails arrived". What it counts is rows the query
  database gained, and a message filed into a second folder gains no row while gaining a place --
  so a run that recorded six locations could report two. It now says both:
  `index.db: query database updated, 2 messages new to it, 6 places recorded from 2 log files`

- **`db create` said 18 times what it could say once, and the once was a different number.** A
  build over a large archive wrote one warning per message whose `Date` header it could not read,
  through the middle of the progress output. It now says it at the end, and asks the finished
  database rather than counting its own complaints -- "110 messages carry no date that could be
  read", with what that costs: `db search --since/--until` will not find them. Those are not the
  same number -- on a large archive the parser complained 16 times about 110 messages that came
  out with no date, a message with no `Date` header at all never drawing a complaint to count. The
  individual headers are still reported, at debug level

- **`found 0 of 1 message` said one had got away.** Over IMAP a folder reported what this pass
  had to fetch against how many messages the folder holds -- two answers to two different
  questions, in the shape of a ratio that reads as "one is there and I could not get it". An
  incremental run over a folder that is fully archived wrote that line every night, for good. It
  now reports what the pass will work through and nothing else, which is what the same line over
  MS Graph has always said

## 0.13.1 (2026-08-17)

### Fixed

- **A reader who has read enough is no longer answered with a traceback.** `mailvault archive
  export ID | less`, quit before the last page, ended the run with `BrokenPipeError: [Errno 32]
  Broken pipe` and a call stack under it -- a report on the program's own workings for something
  the reader did on purpose. A pipe with nobody at the other end is the normal end of `| head` and
  `| less`, and is now treated as one: the run ends where the reading did, without a word at any
  level, and leaves the exit code a program killed by SIGPIPE leaves behind (141). The same for
  the reports a run prints: `mailvault verify | head -1` used to write one traceback per job into
  the pipe nobody was reading any more

## 0.13.0 (2026-08-16)

### Breaking changes

- **A configuration value has to be the type its option holds, and a run stops where it is
  not.** Nothing checked them before: `validate` asks which options are there and whether they go
  together, never what they are, and a dataclass takes whatever it is handed. So `port = "993"`
  failed deep inside `imapclient`, `folders = "INBOX"` was iterated letter by letter -- sending
  mailvault after five folders `I`, `N`, `B`, `O` and `X` and reporting each as missing -- and
  `tls = "yes"` was true by accident, a non-empty string being true, which means `tls = "no"` was
  every bit as true. Every option now says what belongs in it: `'folders' must be a list of
  strings, not a string -- a single one goes in brackets too: folders = ["INBOX"]`, and one wrong
  entry in an otherwise good list is named as well.

  **What to change.** Quotes around a number or a boolean come off, and a single folder goes in
  brackets:

  ```toml
  port = 993                 # not "993"
  tls = true                 # not "yes" -- and "no" was true as well
  folders = ["INBOX"]        # not "INBOX"
  ```

  `tls` is the one worth checking twice: a job carrying `tls = "no"` was running with TLS *on*
  the whole time, whatever its owner meant, and now says so instead of stopping quietly at the
  wrong answer

- **A mailbox backend implements `empty_trash()`.** It is part of the `MailboxClient` protocol,
  called once per job after the last folder has been purged. Nothing outside this package
  implements that protocol as far as anyone knows, so this is a note rather than a migration

### Added

- **`docs/providers.md`: what a particular mailbox wants in `mailvault.toml`.** One page, one
  `[[job]]` to copy per provider, and the things nobody guesses: Gmail's labels are folders and
  `All Mail` is the only one worth backing up, Microsoft 365 takes no password but an Azure app
  registration whose permission covers every mailbox in the tenant, the Proton Bridge reports its
  folders as empty until the first sync has finished, and iCloud lives at `imap.mail.me.com`.
  Linked from the README and the deep dive

- **`docs/usecases.md`: whole recipes for situations that take more than one option.** The
  first one is rolling old mail off a mailbox that is filling up -- a folder the old mail is
  moved into, plus a second `[[job]]` over that folder with `delete_after_export`, carrying the
  same `name` as the mailbox's ordinary job so both stay one mailbox in the archive. That makes
  the two share a resume point per folder, so their `folders` must not overlap: the page says
  what silently goes wrong when they do, and what to do instead where a job cannot name its
  folders at all. It also says why there is no `older_than` option and why there will not be
  one: a run that skips the newest mail cannot also record that it is done with the folder

### Changed

- **The line that opens a job reads `Job: example.org`.** It said `Job item:`, a phrase that
  appears nowhere else in anything mailvault prints. Worth knowing for anyone matching on the
  log; the README's sample output says the same thing now

- **Counting the archive during `verify` and a catch-up opens each message once instead of
  twice.** The pass that reads every archived message's headers -- the longest silence in the
  whole operation, and one that runs over the network share -- located each entry before opening
  it. A store id names two candidate files (`.eml` and `.eml.zst`) and nothing says which is
  there, so that lookup cost up to two `stat()` calls per message before the read that followed.
  The entry is now opened by its id, which is what the query-database build has done all along

### Fixed

- **A typo in an error message: `hash string to short` is `too short`**

- **The Microsoft 365 access token only ever goes to Graph.** A URL is checked against Graph's
  own host before the request is sent. Two of them do not come from mailvault: `@odata.nextLink`
  arrives in a response body, and the delta link that resumes a folder comes back out of a head
  file in the archive -- which the README puts on a network share, opened by more than one
  installation. The bearer token sits on the HTTP client and therefore travels to whatever host a
  URL names, so an edited head file could have pointed this mailbox's OAuth token at a server of
  someone else's choosing. Such a resume point is now treated as worthless and the folder is read
  in full, which is a path that already existed; a request anywhere else is refused outright. The
  log line names the host and not the link, because the link carries a token of its own

- **`archive import --move` steps over a source file it cannot delete, instead of ending
  there.** One file somebody had open, or a directory with the wrong write bit, took the whole
  import down: the error came out of the deletion, past the point where the mail was stored and
  its provenance sealed, and what was lost was the rest of the run and the report with it. The
  file is named and the import carries on. It is reported apart from the messages that could not
  be read, because it is a different outcome -- that mail is in the archive and recorded, and only
  the tidying up fell short. Importing the same source again is harmless and takes the leftovers
  with it

- **An environment variable that is not set is named, instead of being handed on in braces.** A
  `${VAR}` nothing answers is written out as it stands, so `password = "${MAILVAULT_PASS}"` with
  the variable unset sends `${MAILVAULT_PASS}` to the server -- and what comes back is whatever
  that server makes of a wrong password, which names neither the variable nor the fact that a
  variable was meant. Reading the configuration now says which option uses which variable, and
  that `${VAR:-default}` is the way to give it a fallback. The value is still used as it stands:
  this says what happened, it does not decide for anyone

- **`[job]` where `[[job]]` was meant is now said in those words.** Both spellings are valid
  TOML, and the single brackets make one table where mailvault expects a list of them -- so the
  configuration parsed, and what came out was `'str' object has no attribute 'get'` and a
  traceback that named neither the file nor the bracket. It is a `ConfigError` now, saying which
  bracket to write and that there is one section per mailbox. A `job` key holding a value rather
  than a section is named the same way

- **Gmail's trash is emptied after the mail has been deleted, not before.** A job with
  `trash_folder` frees the quota it was set up to free: Gmail answers an expunge by moving the
  message into the trash, where it goes on counting against the mailbox, and emptying that folder
  is what finishes the deletion. It ran at the end of each folder's *read* pass, which since the
  deletion moved behind the metadata seal is one station too early -- it cleared what the previous
  run had left and let this run's mail settle in behind it, so a mailbox that was filling up was
  only ever half emptied, always one run behind. The trash is now emptied once per job, after the
  last folder has been purged. Nothing to change in the configuration; a run over an archive whose
  trash still holds an earlier run's mail clears it out on the way

## 0.12.2 (2026-08-14)

### Changed

- **What is counted is now said in English: `1 message`, not `1 message(s)`.** Every report and
  log line that names a number picks the word to go with it, and groups the number while it is
  at it -- `found 1 message`, `1,204 log files -> 59 across 59 places`, `1 log entry about mail
  that is not in the archive`. A folder holding exactly one message was reported as `found 1
  messages`, on IMAP and on Graph; everywhere else it read `message(s)`, `entry/entries` or
  `copy/copies`, never wrong and never quite readable either. Some findings are worded
  differently as a result, because a sentence that has to fit both numbers cannot say "are
  missing": `archive check` reports `3 messages referenced in the log and missing from the
  archive`, and a log file the chain names and that is gone is `named by the chain and gone`.
  Anything that reads mailvault's output with a pattern wants looking at once

## 0.12.1 (2026-08-14)

### Fixed

- **iCloud mail can be backed up: a message is asked for the way RFC 3501 spells it.** Whole
  messages were fetched as `RFC822`, the deprecated spelling of the same thing, and iCloud answers
  a fetch for it by leaving the message out of its answer altogether -- every message of the run
  failed with `b'RFC822'`, which named neither the message nor the cause. It is `BODY.PEEK[]` now,
  which every IMAP4rev1 server has to understand, and the PEEK form of it also means a backup no
  longer marks as read every message it reads. Where a server answers without the message anyway,
  that one message is named and counted as lost while the rest of the batch is archived, instead
  of the folder ending there

- **A job with no password says so, instead of letting the server say something else.** A
  `password_cmd` that was not allowed to run leaves the job with an empty password, and until now
  that empty password was sent: what came back was whatever the server made of a login with
  nothing in it -- iCloud, for one, answers with a recital of the LOGIN syntax, which names
  neither the account nor the cause. The login is now refused before the connection is opened,
  naming the job's user and the two ways to give it a password

## 0.12.0 (2026-08-13)

### Breaking changes

- **`db create` builds from the metadata log, not from a walk over the store, and is about
  half as expensive.** The archive is the mail and the log together; a message the log names
  nowhere is not part of it yet, and so it is no longer in the query database either. That is not
  the database falling short but the archive being incomplete: `archive check` reports such
  messages and `archive adopt` takes them in. Anyone whose archive holds mail from an import made
  before `archive import` took a `--name` should run `archive adopt` first, or the database comes
  out smaller than it used to. The first report line says where its number comes from, so the
  change is visible rather than puzzling. Measured together with the page cache below, on a large
  archive over SMB: **109 minutes down to 38**, and where the old build got steadily slower the
  longer it ran, the new one holds its pace
- **`db create --mailbox` is gone.** It filed messages the archive recorded no place for under a
  mailbox name -- a claim that lasted until the next rebuild and lived only in the database. What
  it was for is now `archive adopt --name NAME`, which makes the same statement in the archive
  itself, once
- **`archive import` now requires `--name`.** The name is what the archive records the imported
  mail under, and it is the answer to a question nothing else could answer afterwards: which
  import a message came from. Existing command lines have to add it --
  `archive import --name docuware-2019 /mnt/export`. A name you would recognise later is the whole
  requirement; the same name twice is how two runs from the same source are kept together, two
  names is how they are kept apart. It is required for `--dry-run` too, so that the run which
  reports what would happen is the same run in every other respect

### Added

- **`db create --temp-dir DIR` builds the database somewhere else and copies it in when it is
  done.** For an archive on a network share: even written once, a database is written in scattered
  pages, and every one of them is a round trip. Building it on a local disk leaves a single
  sequential copy to go over the wire. It takes a directory rather than working one out, because
  where there is somewhere fast with room is not something the program can know -- and on a local
  archive the detour would only cost a copy, so the default is unchanged
- **`archive places` lists what the archive has mail from.** Every mailbox and folder, every
  import, and everything `archive adopt` took in, with how many messages each holds and when it
  was last written to. These are the names `db search --mailbox` and `--folder` take, and the ones
  already spoken for when picking a new one -- until now the only way to find them out was to read
  the log. Two name columns rather than the `mailbox::folder` the findings print, because those two
  are what gets typed, and a cell stays empty where there is no name to print -- no mailbox is an
  import or an adopted place, no folder is a mailbox whose folder was never recorded. The counts
  are of distinct messages, so the total comes out smaller than the column adds up to, and the
  report says why rather than leaving it odd
- **`archive adopt --name NAME` takes in the messages that belong to no place.** An archive is
  the message store and the metadata log together, and a message that lies in `mail/` while no log
  file names it is not a damaged part of it but a file that is not part of it yet. `archive check`
  reports them and now names this as the move. The name is the statement of whoever types it --
  the import they came from, or `orphaned` to say that nobody knows any more -- and it is recorded
  exactly the way an import is recorded, because it is the same statement. Messages that already
  have a place are left alone, so running it twice is harmless. Nothing corrects the log
  afterwards, which is what `--dry-run` is for and why the report says how many messages a run is
  about to speak for. A name that is already a place is said before the run as a choice ("these
  would go in with them") and afterwards as a fact ("which now holds 5,415 message(s)") -- which
  is what catches a mistyped name. Where the directory an import read from still exists, importing
  it again is the better move: what that records cannot be wrong
- **An import records where its mail came from, so imported mail is no longer mail nothing knows
  anything about.** It goes into the metadata log the way a backup's observations do, with one
  difference: the mailbox stays empty, because there is no mailbox behind an import and nobody to
  ask about it again. The name lives in the folder field, which is where `mailvault db search
  --folder docuware-2019` finds it, and which keeps it clear of your job names by construction --
  a job always has a name, so having none cannot be mistaken for one. `archive check` no longer
  counts imported mail among the messages nothing records a place for: an archive built from
  imports used to report a number the size of itself
- **Mail imported by an earlier version can be given its provenance after the fact.** Import the
  same source again under a name: every message is recorded, whether the archive already holds it
  or not, and nothing is stored twice. It costs the reading of the source and is the only thing
  that repairs this -- an archive cannot invent a provenance it was never told
- **What an import has recorded is written down as it goes**, in batches rather than once at the
  end, and `--move` removes a source file only after the batch it belongs to is written. An
  interrupted import of 100,000 messages no longer costs the provenance of everything it had read
  so far, and a log that cannot be written holds the source files back entirely

### Changed

- **Building the query database writes about a ninth of what it used to.** SQLite's page cache
  holds two megabytes by default, which a build overruns within its first few thousand messages;
  from there on it keeps evicting the pages a growing B-tree is about to touch again, and writes
  each of them over and over. Measured on 30,000 messages and an 18.4 MiB database: 166.8 MiB
  written before, 18.7 MiB now -- the file once instead of nine times. On a local disk this makes
  no difference to the clock, because the operating system absorbs it; over a network share it was
  the reason a build got steadily slower the longer it ran
- **`db update` says how many locations it recorded, not only how many messages were new.** A log
  file about mail the database already had -- what `archive adopt` writes, or a folder read in full
  a second time -- records locations and adds no message, and "0 message(s) added" on its own read
  like a run that had done nothing at all
- **Stored messages and metadata log files are written read-only.** An entry is named after the
  hash of its content, so anything that changes it breaks its name -- the mode now says that to
  whatever opens the file. Comfort, not protection: it is aimed at the viewer that "repairs" the
  text file it is displaying, not at anyone who means it, and it does not stop deletion. Where the
  filesystem does not carry the mode -- a desktop-mounted SMB share is the usual case -- nothing
  changes and nothing is reported. `archive export` still hands out a normal, writable file

### Fixed

- **`archive compact` no longer explains itself with an overlap that no longer exists.** Its help
  text and the deep dive said the log repeats entries "across the incremental overlap" -- true
  while a run resumed from a date and re-read the day it had already read, which ended with the
  server-issued resume points in 0.10.0. An incremental pass now records exactly what it fetched,
  and entries repeat only where a folder is read in full instead: `backup --full`,
  `incremental = false`, or a resume point the server voided. The command is unchanged; the reason
  it gives for existing is now the real one

## 0.11.0 (2026-08-09)

### Added

- **`mailvault db` -- the archive can be searched.** `db create` builds the query database,
  `db update` takes in what has been archived since, `db search` finds messages in it, and
  `db drop` deletes it. Its own command group rather than a corner of `archive`, because the
  database is not part of the archive the way the mail and the log are: it holds nothing that is
  not already in there, it is built on demand, and every command in the group is free to say
  "that does not fit, build it again" -- which is precisely what no command touching the archive
  itself may ever say

- **`db search` asks in plain terms**, without SQL: `--from`, `--to`, `--subject`, `--mailbox`,
  `--folder`, `--since`, `--until`, `--limit`. Every filter given has to match, text matches
  anywhere in the value and ignores case, and a `%` you type is a per cent sign rather than a
  wildcard. A message whose Date header could not be read matches neither `--since` nor `--until`:
  its date is unknown, not old. One row per message however many recipients or folders it has

- **A search and an export make a pipeline.** The table shortens the message id to be read rather
  than typed; `--ids` prints them in full and prints nothing else, so
  `db search --from example.com --ids | xargs mailvault archive export -o ./out/` is the
  whole story.
  `--csv` and `--json` print the full result, ids in full, for everything else

### Changed

- **`index.db` records a place as one fact.** Which folder of which mailbox a message was seen in
  used to be split across two independent tables, one naming the mailboxes and one the folders --
  so a message in two mailboxes came out as two mailboxes and two folders, from which no query can
  say which folder belonged to which. On a large archive that is the majority of its messages.
  There is now one `message_location` row per place, fed straight from the log, which has held the
  pairing since 0.9.0. Either half may be missing and neither is ever guessed: a mailbox with no
  folder is an archive whose history never recorded one, and a folder with no mailbox is a place
  named by an import. The `label` table is `folder`, finishing a rename the rest of the program
  did long ago -- Gmail's labels and IMAP's folders differ in how many a message may have, not in
  what they are

- **`v_messages` holds every message the archive holds.** It inner-joined sender, recipient and
  subject, so a message it could not complete was simply not in it -- and a message with no
  readable recipient is not a rarity in mail going back to the nineties, it is the group address
  and the malformed header. `SELECT count(*)` on the view therefore answered a question nobody
  asked while looking like an answer to the one they did. Every join is now a left join, and the
  view gained a `folder` column

- **A projection built by an earlier version is reported, not quietly extended.** `index.db` now
  records which shape it was written in, and one this version does not read is left untouched --
  not created into, not written, not stamped -- with a line saying so and what to run. Without
  that marker an older one would gain the new tables, keep the old, and leave the new ones empty
  for good: `applied_log` reports every log file as already folded in, so nothing would ever fill
  them, on a database that answers every query without complaint. There is no upgrade path and
  there should not be -- everything in the projection can be rebuilt from the archive, so a
  mismatch costs a rebuild rather than a migration. Rebuilding it is left to whoever asks for it:
  a backup deciding on its own to read every message in the archive is half an hour nobody
  requested

- **`index.db` says when it has fallen behind the archive.** It records, per mailbox and folder,
  which point of the metadata log it has taken in; the archive names the current one in `heads/`.
  A difference means mail has been archived since, and so does a folder the projection has never
  heard of. Nothing about such a database looks wrong -- it answers every query, and the answers
  are true, they are just not complete -- so the only way anybody finds out is being told, before
  they base something on it rather than after

- **The README is a README again, and the details moved to `docs/deep-dive.md`.** It had grown to
  a thousand lines in which the answer to "what is this and how do I start" lay somewhere between
  the SMB write semantics and the parameter tables. What is left is the short way through: what
  mailvault is for, how to install it, how to make an archive, what goes into `mailvault.toml`,
  how to back up and keep it current, the query database, and the `archive` commands. Everything
  that explains *why* -- and the full configuration reference -- is in the deep dive, one link away

### Removed

- **`archive create-db` is gone; it is `db create`.** With it goes its second argument: the
  database is a feature of an archive and lives in it, so there is nothing left to name. A second
  `db create` is refused and points at `db update`, which costs a few small reads where building
  again reads every message in the archive; `--force` builds it again anyway

- **The dead `snapshot` table is gone from `index.db`.** It held the resume timestamps of an
  archive that kept its truth in SQLite; since 0.8.0 those live in `heads/`, and nothing has
  written the table since. Every rebuild created it empty. The reader that still needed it moved
  to where it belongs, alongside the rest of the code for archives written by older versions

### Fixed

- **`archive migrate --help` no longer says a backup will do it for you.** It has not been true
  since 0.10.0, where every command but `init` and `migrate` began refusing an archive that has
  not been lifted -- so the one reader who most needed the text, somebody working out whether they
  have to run it, was told they could skip it and then met a refusal from whatever they ran next.
  It now says what the refusal says: this is the first thing to do after upgrading

- **`archive check` no longer sends its reader to a command that is gone.** Where it reports
  messages belonging to no known place, it offered `archive create-db` as the way to find them
  again -- renamed to `db create` in this release, so the advice ended in "invalid choice". A hint
  that leads nowhere costs more than no hint: the reader spends the time *and* stops believing the
  next one

- **The metadata log is read once per run, not once per job.** An archive has one log and it names
  every place in it, so each job was reading all of it to keep the part that is theirs. On a
  five-job `verify` that is five identical reads reporting five identical totals -- 2.9 seconds of
  a 60-second run, and it grows with the log. Both `backup` and `verify` now share one reading,
  and it still happens only when something actually asks

- **A resume point the source rejects is forgotten instead of offered again.** It was only ever
  set, never cleared, so a folder whose point went void -- a UIDVALIDITY change, a `410 Gone` --
  kept it, offered it on the next run, had it refused again, and reconciled itself from scratch
  every night from then on. Nothing was ever lost by it and nothing ever got better either. A
  point the source refuses buys no coverage, so it goes; a quiet pass that simply had no new
  point to offer still leaves the old one standing, which is the case this must not break

- **An empty folder no longer costs a reading of the whole metadata log.** A folder with nothing on
  the server earns no resume point, and without one the next run considers catching it up by
  listing -- which meant reading every file of the log, once per job, to find out that the place
  holds nothing. On a real archive four empty `Sent` folders spent 2.6 seconds of a 12.5-second
  backup on it, on every run, and the log grows between compactions while they stay empty. The
  archive's own record of the place answers it for the price of one small file. Where there is no
  such record the log is read as before: an archive whose resume points were lost is exactly what
  the catch-up is for

- **A `verify --repair` that recovers nothing no longer changes the archive.** Every message it
  fetched had its place written to the metadata log, including the copies that turned out to be
  duplicates of something already recorded there -- an observation the log already held, which the
  next `compact` would take straight back out. Over a folder holding duplicates that is one
  needless entry per copy, a new log file and a new link in the chain, on every run. A location is
  now written only where it is not recorded yet; the case that matters is untouched, because a
  message archived under *another* folder is not recorded under this one and still gets its entry

- **The repair no longer says "restored" about messages it did not restore.** A copy the archive
  already held was reported as restored, once per message -- a line per copy claiming the opposite
  of what happened. Now only what changed the archive is named, the download says how far it has
  got every 250 messages, and a folder with nothing to fetch stops reporting "0 restored"

- **A backup only refreshes `index.db` when the job wrote something.** It ran once per job
  regardless, and a job with no new mail has nothing to add -- but finding that out means listing
  the whole metadata log directory, which over a network share is 3.9 seconds of a run that had
  nothing left to do. With five jobs that was twelve seconds per backup for zero new messages.
  The information was there all along and simply not asked for. A job that fails part way through
  still refreshes what it managed to write: "failed" does not mean "wrote nothing", and skipping
  those messages until some later run happened to pick them up was an accident rather than a
  decision

- **`verify` no longer reports byte-identical duplicates as missing mail.** A server folder can hold
  the same message twice, byte for byte; an archive that deduplicates holds it once, which is
  what it is for. Counting copies made every copy after the first a missing message -- a folder
  that was not missing a single message could report hundreds of them, and the same number after
  every run for good. `--repair` fetched all of them, stored none, and the next run said the same
  thing again. The two are now separate: `0 not archived, 12 further copies of archived
  message(s)`, and a run that finds no gaps says the archive is complete no matter how many
  further copies it saw. They are still fetched by `--repair`, because a second
  copy is occasionally the byte-different version that really is absent and only its bytes can say
  so -- but they are reported as what they turn out to be, and the ones that differed are counted
  separately from the gaps that were closed

- **A backup into an archive built by `archive import` is no longer waved through.** The guard asks
  which mailboxes an archive has seen and lets anything write into one that names none. An imported
  archive names none -- an import writes messages, not a log, because nobody told it which mailbox
  they came from -- so the fullest archive in the house was treated as the emptiest, and any
  configuration could write into it. With `delete_after_export` the server copies went afterwards.
  "No names" and "no mail" are now asked separately, and an archive that holds mail under no name
  is refused until `--allow-new-mailbox` says otherwise

### Removed

- **`archive addresses` is gone.** It read and parsed every message in the archive and printed the
  sender and recipient addresses it had not printed yet, each tagged `from` or `to`: no counts, no
  order, no way to ask for a subset, and no way back from an address to a message. It comes from
  the `ib-archive` days, when a heap of `.eml` files was all there was and there was nothing to
  query -- its `--docuware` switch says as much, and since 0.10.0 that switch could not even be
  used, because every `archive` subcommand refuses a directory that is not a marked archive. What
  it did is a query, and queries belong in the query database: build one with `archive create-db`
  (or keep it in step with `--index-db`) and ask it for `SELECT address FROM address`, where the
  answer can be counted, sorted, narrowed and joined back to the messages it came from

## 0.10.0 (2026-08-09)

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
  walking the tree, and measurement says otherwise: on a large archive over SMB the walk took 16
  minutes and reading every message 17. A network share charges for round trips, not for
  bytes -- the walk pays one per shard directory, the read one per message, and at a couple of
  messages per shard those come out level. Being able to find a message whose bytes changed under
  it is worth a factor of two. `--quarantine` no longer needs a companion flag; it refuses to be
  combined with `--no-integrity-check` instead

- **`archive check` says whether the archive is all right**, in words, instead of leaving the
  verdict to an exit code nobody reads unless they went looking for it -- and it says which kind
  of run it was, because one with `--no-integrity-check` never read a message and cannot have
  found one whose bytes changed. Its counts are in plain terms too, and there are only counts a
  reader can act on: `5 message(s) stored, 4 of them accounted for by 2 log file(s) in 3
  place(s)`. Those two message counts are there to be subtracted -- the difference is the list of
  messages with no provenance further down. What used to stand between them was the number of log
  entries, which counts a message once per folder it was filed in and so ran to six figures on a
  real archive: neither files nor messages nor folders, nothing following from it either way, and
  duly read as a file count. Findings say what is wrong rather than
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

- **The archive is named once, at the start of a run, and nowhere else.** Every line after it is
  about that archive, and repeating the path on each of them buried the statement behind it -- over
  a network share the prefix was routinely longer than what it prefixed. A file inside the archive
  is now named as it reads *inside* it, `meta/a1/a1b2….jsonl` rather than the whole path; one that
  lies elsewhere, as `archive import` reads from, keeps its full path, because shortening it
  against an archive it has nothing to do with would say the wrong thing about where it is. This
  holds for the log lines as well as the reports -- a damaged message, an unreadable log file, a
  head that says the wrong place, a message set aside by `--quarantine`. What still names the
  archive in full is the one message that is *about* the archive: a configuration that has never
  written here says which archive it was pointed at, and that is the statement, not a prefix

### Breaking changes

- **An archive is a directory with a `FORMAT` file in it, and `mailvault archive init` is what
  makes one** -- what `git init` is, and answered the same way: a repository is a directory with
  a `.git`, and nothing else counts. `init` lays out the archive and writes a `mailvault.toml`
  to fill in; an existing configuration is never touched. Every other command asks first and
  refuses a directory that is not an archive, naming both ways on: `init` for a new one,
  `archive migrate` for one from before 0.10. Only those two accept an unmarked directory.
  Before this, each command simply opened `<directory>/mail` and worked on what it found there,
  which on an unmigrated archive is nothing at all -- `archive check` reported a healthy archive
  as a total loss, and `verify --repair` set about downloading the mailbox a second time

- **No command takes an archive as a positional argument any more.** The archive is the directory
  you are standing in, or `--archive DIR`. `mailvault backup ./backup` becomes
  `mailvault --archive ./backup backup`, and `mailvault archive check ./backup` becomes
  `mailvault --archive ./backup archive check`. `archive import` keeps its one positional, which
  was never the archive: it is the foreign directory being read from

- **`--config` is no longer required** by `folders`, `backup` and `verify`, and passing it to
  `backup` or `verify` without `--archive` is now an error rather than a run into whichever
  directory the shell happened to be in

- **`--job`, `--allow-exec` and `--allow-new-mailbox` are written after the command**, not
  before it: `mailvault backup --job proton.me`, where it used to have to be
  `mailvault --job proton.me backup`. That is the order everybody reaches for anyway, and the
  old one was never anything but an accident of where the options were declared -- they say
  which jobs to run and what the configuration may do, which are statements about the command
  doing the work and mean nothing to `archive check`. The help text admitted as much by having
  to list the commands each of them applied to. What stays before the command is what is true
  of the whole run: `--archive`, `--config`, `-v/-q` and `--log-file`

- **Move your configuration into the archive it describes**, as `mailvault.toml`. Nothing does
  this for you and nothing looks for the old location, so a run without `--config` after
  upgrading says which file it wanted and did not find. `archive check` knows the file as a
  legitimate inhabitant and does not report it

- **The archive layout moved. Run `mailvault archive migrate` once per archive**, before anything
  else -- every command refuses an archive that has not been lifted, and says so. Nothing is
  deleted by the migration. **Upgrade every machine that writes into the
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

- **A catch-up holds its resume point back when the log did not reach disk.** The pass that
  brings a folder back in step by listing it -- what an archive lifted from a version-1
  `state.json` does on its first run -- reported only whether the downloads worked. A write
  that failed after them (a full share, a quota, a read-only remount) left the messages stored
  with nothing recording where they belong, while the resume point moved past them: no later
  run asks for them again, and `archive check` reports them as belonging to no known place for
  good. The ordinary pass has guarded exactly this since 0.8.0; the catch-up threw the answer
  away

- **A Graph resume point with an unreadable timestamp no longer costs the folder its backup.**
  Anything but a proper ISO time in the point's `issued` field -- and it is a file on disk,
  which anything may have happened to -- raised out of the backend, was caught by the
  per-folder handler, and dropped that folder from the run. Silently, and every night after,
  because nothing rewrites the head that caused it. Now it degrades to "read the folder in
  full", which is what `heads` promises for anything unusable. A timestamp without a timezone
  counts as unusable too: an age cannot be taken from it

- **`archive migrate` and `archive compact` exit non-zero when they did not finish.** Both
  printed their failure -- "consolidated files did not verify", "NOT marked" -- and returned 0,
  so a cron job filed the run as a success. For the migration the mark is the verdict and now
  also the exit code: it is written last, so an archive carrying it got through every step

- **The refusal of a newer archive format is a message again, not a traceback.**
  `marker.FormatError` was missing from the errors the CLI knows, so the one sentence written
  for it -- "written by a newer version of mailvault … Upgrade mailvault" -- was buried in
  stack frames. That is the case it exists for: two machines, one shared archive, one of them
  still on the old version

- **`archive import` refuses a source that is the archive itself.** `mailvault archive import
  --move .` found every message already stored, answered each with EXISTS, and then deleted it
  from the source -- which was the archive. A ten-message archive ended with none, and the report
  said `10 message(s) read -- 0 imported, 10 already in .`, exit 0. It became a plausible slip
  when the archive stopped being a positional argument and turned into the directory one is
  standing in. Refused now with or without `--move`, and in both directions: a source inside the
  archive, or an archive inside the source

- **Ten more kinds of broken `Date` header are read** -- a weekday no parser knows (`Thur`), a
  month named in German (`Sa, 14 Dez 2002`), the all-numeric `27.11.2002`, and a date that
  carries no time at all (`Mon, 11 Mar 2002 PST`). On a large archive that is 13 of the remaining
  warnings down to 3. These readings are tried only after every plainer one has failed,
  and each is still chosen so that it cannot turn one date into a different one: the weekday is
  optional in RFC 5322 and simply dropped, which handles every language at once; `05.03.2002`
  stays unread, because it is March to half the world and May to the other half; and **no reading
  ever fills in a year**. What a date-guessing library would do instead was measured -- given
  `Do, 5 Dez 2002` it answers 2002-05-08, taking the day from the day the run happens to take
  place on, and given a header with no year it answers this year. A wrong date is worse than a
  missing one, because a missing one is visible

- **The backup says why it is reading the whole archive.** With `--index-db` on and no `index.db`
  yet, a backup that had nothing left to fetch went on to read every message there is -- twenty
  minutes on a large archive, announced by nothing but a number climbing in steps of two thousand.
  It now says that there is no query database yet and that it is building one from the archive,
  before it starts, and every line of the count says what it is counting for

- **What the commands say about themselves is about the mail, not about the machinery.** `backup`
  offered to "back up mails to the local content-addressed archive", which names an
  implementation nobody using it has to know and leaves out what it actually does: add to the
  archive what the mailboxes hold and it does not, carrying each folder on from where the last
  run left it. `archive migrate` still described moving things out of `store.db` and into
  `state.json`, which is not what it has done for two versions. `archive check` explained itself
  in shards and entries. All of them now say what happens and why it is worth having, and
  `archive migrate` says what it does today

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
  a hundred megabytes rewritten in place on every run was worth switching off.
  What remains in its place is a few kilobytes of immutable files, while turning
  it off would disable incremental backups and `verify` -- which is not what
  anyone wants from an option about metadata. A configuration that still sets it
  says so on load, rather than having the field quietly dropped as if it were a typo

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
