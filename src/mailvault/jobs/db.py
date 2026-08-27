"""Build and maintain `index.db`, the archive's queryable projection.

The archive itself holds no database. This makes one on demand out of the two
things that do live there -- the messages, for everything they carry in
themselves, and the log, for which mailbox and folder each was seen in. What
comes out is a snapshot, accurate when built and stale from the next backup on.

Named for the command it serves, like every other module here. It used to be
`storedb`, from the days when the projection *was* `store.db` and the archive
kept its truth in SQLite -- which stopped being true in 0.8.0, leaving a module
whose name pointed at a file it has nothing to do with, right next to
`mailvault.legacy.store_db`, which does.

It can also be kept up to date incrementally (`refresh_db`): a convenience
projection beside the archive, refreshed after a backup, never a source of truth.
Which log files it has already applied is recorded in the database itself, so a
routine refresh reads only the files added since -- and if the database is
missing or unreadable, it is simply rebuilt.
"""

from __future__ import annotations

import collections.abc
import contextlib
import dataclasses
import logging
import pathlib
import shutil
import sqlite3
import tempfile

from mailvault import mailutils, utils
from mailvault.jobs.common import JobError
from mailvault.store import cas, heads, index_db, marker, metalog

log = logging.getLogger(__name__)

# Messages per transaction when building a database. Large enough that commits
# stop dominating, small enough that an interrupted run has not done much work
# it will have to repeat.
CREATE_DB_BATCH = 2000

# Default filename of the kept-fresh projection, beside the archive. Deliberately
# not `store.db`: that name is reserved for the legacy database the migration
# looks for, and a projection there would be exported into the log and renamed
# away on the next backup.
DEFAULT_QUERY_DB_NAME = "index.db"

# The two moves a reader can be told to make, and they are not interchangeable.
# Building again reads every message in the archive; updating reads what has been
# added since. Naming the expensive one where the cheap one would do is a wrong
# hint of the quieter kind: it works, and it costs half an hour it did not have
# to. Named here because command lines move -- these were `archive create-db`
# until the projection got its own namespace.
REBUILD_COMMAND = "mailvault db create --force"
CREATE_COMMAND = "mailvault db create"
UPDATE_COMMAND = "mailvault db update"
MIGRATE_COMMAND = "mailvault archive migrate"

# Appended to `--until` to make an upper bound the whole day named lies under.
# Every character a stored date can hold sorts before this one, so a message at
# `2026-08-20T23:59:59+02:00` is inside the bound `2026-08-20` and the first
# message of the day after is not.
_ABOVE_ANY_DATE = "\uffff"

# A sentinel, because None is a legitimate recorded value: a place whose chain
# has no head yet is recorded with one, and it must not compare equal to a place
# the projection has never seen.
_MISSING = object()


def _unreadable(db_name: str, outdated_shape: bool, note: str = "") -> str:
    """The line a projection this version cannot query is answered with.

    `note` is what became of the file, and it belongs inside the sentence rather
    than after it: a message names the state first and the move last, and a
    reader told what to run before being told what happened has to put the two
    back in order themselves.
    """
    if outdated_shape:
        state = "built by an earlier version of mailvault and not readable by this one"
    else:
        state = "not the query database this version reads"
    return f"{db_name}: {state}{note} -- build it again with `{REBUILD_COMMAND}`"


@dataclasses.dataclass
class Freshness:
    """What is wrong with the projection somebody is about to read, if anything.

    Four complaints, and they differ in what can be done about them.

    `absent` says there is no file at all. It is here rather than at each call
    site because "may this be read" is the question this answers, and a file that
    is not there is the plainest reason it may not: a reader who asks `is_usable`
    before opening it -- the obvious reading of "whether a query may be run
    against it and its answer believed" -- used to be told yes about a path that
    throws the moment it is opened.

    `outdated_shape` and `incomplete` both mean the file is not a projection this
    version can query -- one was written by another version, the other has lost
    something a query needs -- and both are answered by building it again.

    `unmigrated_archive` says the archive still keeps its metadata in a database
    of its own. Nothing may be built or replaced there, because there is no log
    to build it from yet, and what looks like a stale projection may be the only
    record the archive has.

    `behind` names the places whose log has moved on since the projection was
    last brought up to date, so what it holds is true and incomplete.
    """

    absent: bool = False
    outdated_shape: bool = False
    incomplete: bool = False
    unmigrated_archive: bool = False
    behind: list[str] = dataclasses.field(default_factory=list)

    @property
    def is_usable(self) -> bool:
        """Whether a query may be run against it and its answer believed."""
        return not (
            self.absent or self.outdated_shape or self.incomplete or self.unmigrated_archive
        )

    def is_current(self) -> bool:
        return self.is_usable and not self.behind

    def complaint(self, db_name: str) -> str | None:
        """One line naming the state and the move, or None when there is none."""
        if self.absent:
            return (
                f"{db_name}: no query database in this archive -- build one with"
                f" `{CREATE_COMMAND}`"
            )
        if self.unmigrated_archive:
            return (
                f"{db_name}: does not fit this archive, and the archive is an older"
                f" one that still keeps its own record of the mail -- lift it with"
                f" `{MIGRATE_COMMAND}` first, and nothing here is thrown away in the"
                f" meantime"
            )
        if self.outdated_shape or self.incomplete:
            return _unreadable(db_name, self.outdated_shape)
        if not self.behind:
            return None
        places = ", ".join(self.behind[:3])
        if len(self.behind) > 3:
            places += f" and {len(self.behind) - 3:,} more"
        return (
            f"{db_name}: behind the archive in {utils.counted(len(self.behind), 'place')}"
            f" ({places}) -- mail archived since is not in it, take it in with"
            f" `{UPDATE_COMMAND}`"
        )


@dataclasses.dataclass
class ReplayResult:
    """Outcome of applying the metadata log to a database."""

    files: int = 0
    entries: int = 0
    applied: int = 0
    unknown: int = 0


@dataclasses.dataclass
class RebuildResult:
    """Outcome of rebuilding the database from the archive and its log.

    `undated` is asked of the finished database rather than counted while it
    fills, and that is the point of it. Counting the complaints instead -- one
    per header the parser could not read -- reports how often something was
    noticed, not how many messages came out without a date, and against the
    reference archive those were 16 and 110. A message can also arrive here
    with no `Date` header to be unreadable in the first place, which nothing
    would have complained about at all.
    """

    messages: int = 0
    undated: int = 0
    replay: ReplayResult = dataclasses.field(default_factory=ReplayResult)


@dataclasses.dataclass
class RefreshResult:
    """Outcome of bringing a kept-fresh projection up to date with the archive.

    `unreadable` says the file was left exactly as it was because it is not a
    projection this version queries -- another version's shape, or one an object
    is missing from. Nothing else in here is filled in when that happens.
    """

    rebuilt: bool = False
    unreadable: bool = False
    files: int = 0
    messages: int = 0
    applied: int = 0
    unknown: int = 0


def _log_hash(path: pathlib.Path) -> str:
    return path.name.removesuffix(".jsonl")


def _archive_heads(heads_root: pathlib.Path) -> dict[tuple[str | None, str | None], str | None]:
    """The chain head of every place, as the archive currently has it."""
    return {(head.job, head.folder): head.log for head in heads.read_all(heads_root)}


def _record_heads(db: index_db.IndexDatabaseConnection, heads_root: pathlib.Path) -> None:
    """Write down which head of each place this projection has now folded in.

    Called after the log has been applied, never before: what is recorded here
    is a claim about what the database contains, and a claim made in advance of
    the work is the one thing worse than no claim at all.
    """
    with db.transaction():
        for (mailbox, folder), head_log in _archive_heads(heads_root).items():
            db.execute(
                "INSERT INTO folded_head(mailbox, folder, log) VALUES (?, ?, ?) "
                "ON CONFLICT DO UPDATE SET log=excluded.log",
                (mailbox, folder, head_log),
            )


def _folded_heads(
    db: index_db.IndexDatabaseConnection,
) -> dict[tuple[str | None, str | None], str | None]:
    """The heads the projection recorded, or nothing when it has none."""
    rows = db.execute("SELECT mailbox, folder, log FROM folded_head").fetchall()
    return {(row[0], row[1]): row[2] for row in rows}


def _unfit(store_path: pathlib.Path, result: Freshness) -> Freshness:
    """Decide what a projection that cannot be queried means for this archive.

    An archive that carries the mark holds its metadata in the log, so a
    projection is a convenience and building it again is the answer. An archive
    without one predates all of that: there is no log to build a projection from,
    and a database lying in it may be the only record of where its mail came
    from. Which of the two it is has to be settled before anybody is told to
    throw a database away.
    """
    if not marker.is_archive(store_path):
        result.unmigrated_archive = True
    return result


def freshness(store_path: pathlib.Path, db_path: pathlib.Path) -> Freshness:
    """Ask the projection whether it can be read, and whether it is still current.

    For whoever is about to read it. It answers two questions, and the first one
    decides whether the second is worth asking: is this a projection this version
    queries at all, and does it still hold what the archive holds.

    The archive's `heads/` names the current chain head of every place; the
    projection records the head it folded in. A difference means mail has been
    archived since, and a place the projection has never heard of means the same.
    Neither is an error -- the projection is rebuildable by definition -- but a
    reader who is not told will take an answer from it and believe the answer is
    complete.

    Nothing here writes to the database, including when it turns out to be of the
    wrong shape. Reading is not the moment to change a file, and a projection
    that can be recognised as unreadable is worth more than one quietly patched
    into looking current.
    """
    result = Freshness()
    if not db_path.exists():
        result.absent = True
        return result
    heads_root = store_path / heads.DEFAULT_HEADS_DIR
    try:
        with index_db.IndexDatabase(path=db_path) as db:
            if db.outdated:
                result.outdated_shape = True
                return _unfit(store_path, result)
            absent = db.missing()
            if absent:
                log.debug(
                    "%s: %s missing from the query database",
                    db_path.name,
                    ", ".join(str(obj) for obj in absent),
                )
                result.incomplete = True
                return _unfit(store_path, result)
            folded = _folded_heads(db)
    except sqlite3.DatabaseError:
        # Unreadable is its own answer, and `refresh_db` rebuilds it anyway.
        result.outdated_shape = True
        return _unfit(store_path, result)

    for place, head_log in _archive_heads(heads_root).items():
        if folded.get(place, _MISSING) != head_log:
            result.behind.append(heads.place_name(*place))
    result.behind.sort()
    return result


def _mark_logs_applied(db: index_db.IndexDatabaseConnection, paths: list[pathlib.Path]) -> None:
    with db.transaction():
        for path in paths:
            db.execute(
                "INSERT OR IGNORE INTO applied_log (hash) VALUES (?)",
                (_log_hash(path),),
            )


class _Rows:
    """The message rows this run has made, so each message is read once.

    A message lies at as many places as it was observed in, and the log names it
    once per place -- a Gmail message under three labels turns up in three files.
    Reading its headers three times would be three round trips over a share for
    an answer that cannot have changed.

    Misses are remembered too, and for the same reason: an entry the log names
    and the archive no longer holds costs one look, not one per place it was
    recorded in.

    It reports its own progress, because it is the one thing here that is slow:
    everything else in a build is arithmetic on values already in hand.
    """

    def __init__(
        self,
        db: index_db.IndexDatabaseConnection,
        store: cas.ContentAddressedStorage,
    ):
        self._db = db
        self._store = store
        # Empty for a fresh database, and the rows already there for a refresh.
        self._known = db.store_id_map()
        self._missing: set[str] = set()
        self.created = 0

    def of(self, store_id: str) -> int | None:
        """The row id of a message, making the row the first time it is asked for.

        None where the archive does not hold that message: the log records what
        was observed and never that it is still there, so an entry naming a
        message that has since been removed is an ordinary thing to meet.
        """
        msg_id = self._known.get(store_id)
        if msg_id is not None:
            return msg_id
        if store_id in self._missing:
            return None
        msg_id = _insert_message(self._db, self._store, store_id)
        if msg_id is None:
            self._missing.add(store_id)
            return None
        self._known[store_id] = msg_id
        self.created += 1
        if self.created % CREATE_DB_BATCH == 0:
            # Named for what it is doing, not merely counted. This reads every
            # message the log accounts for and takes minutes on a large archive,
            # and a bare "N messages read" leaves a reader watching a number
            # climb with no idea what it is for.
            log.info(
                "building the query database: %s read",
                utils.counted(self.created, "message"),
            )
        return msg_id


def _insert_message(
    db: index_db.IndexDatabaseConnection,
    store: cas.ContentAddressedStorage,
    store_id: str,
) -> int | None:
    """Read one archived message's headers and insert its row, returning its id.

    Only the headers are read: everything the database keeps about a message --
    sender, recipients, subject, date -- is in them, and the attachments behind
    them are the bulk of the bytes. The entry is opened by its id rather than
    looked up first, which is one round trip instead of two -- see
    `cas.ContentAddressedStorage.reading`.

    None when the archive holds no such entry.
    """
    try:
        raw = store.read_header_of(store_id)
    except FileNotFoundError:
        return None
    header = mailutils.decode_email_header(raw)
    from_addrs, to_addrs = mailutils.addresses(header)
    email_id = mailutils.message_id(header)
    date = mailutils.date(header)
    subject = mailutils.subject(header)

    msg_id = db.add_message(store_id, email_id, date, subject)
    db.add_message_sender(msg_id, *from_addrs)
    db.add_message_recipients(msg_id, *to_addrs)
    return msg_id


def _fold_log_file(
    db: index_db.IndexDatabaseConnection,
    rows: _Rows,
    logfile: metalog.LogFile,
) -> tuple[int, int]:
    """Record one log file's observations, returning (applied, unknown).

    One file is one place, and every line in it says that message was seen
    there. A file that names no mailbox is applied all the same -- the database
    can hold either half as NULL, and a folder without a mailbox is what an
    import and `archive adopt` write.

    One transaction per file rather than per entry: the write methods commit
    individually when called at the top level, which would mean a commit per
    message and is ruinous over a network share.
    """
    applied = unknown = 0
    with db.transaction():
        for store_id in logfile.store_ids:
            msg_id = rows.of(store_id)
            if msg_id is None:
                unknown += 1
                continue
            db.add_message_location(msg_id, logfile.mailbox, logfile.folder)
            applied += 1
    return applied, unknown


@contextlib.contextmanager
def _building(
    db_path: pathlib.Path, temp_dir: pathlib.Path | None
) -> collections.abc.Iterator[pathlib.Path]:
    """Yield the file to build into, and put it in place if the build succeeds.

    Beside the target by default, because a rename is only atomic within one
    filesystem and the database has to appear whole or not at all.

    `temp_dir` builds it somewhere else and copies it over at the end, which is
    for one case: a database on a network share. Even with the page cache a build
    is given (`index_db.CACHE_KIB`), SQLite writes a database in scattered
    pages, and every one of them is a round trip over a share. Building elsewhere
    turns the whole thing into one sequential copy at the end.

    Not decided by the program, because deciding it needs two things only the
    person running it knows: whether the target is slow, and where there is
    somewhere fast with room. `TMPDIR` is memory on some systems, and filling it
    with a database nobody asked to put there is the kind of surprise this
    program does not spring.

    The copy lands beside the target under the transient name and is renamed from
    there, so the last step is the same atomic rename either way and an existing
    database survives every failure before it.
    """
    if temp_dir is None:
        tmp_path = db_path.with_name(db_path.name + "._tmp_")
        tmp_path.unlink(missing_ok=True)
        try:
            yield tmp_path
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
        tmp_path.replace(db_path)
        return

    if not temp_dir.is_dir():
        raise JobError(f"{temp_dir}: --temp-dir has to be a directory that is there")
    workspace = pathlib.Path(tempfile.mkdtemp(dir=temp_dir, prefix="mailvault-db-"))
    try:
        built = workspace / db_path.name
        yield built
        # Copied first and renamed second: the rename is within the target's own
        # filesystem and therefore atomic, which a copy across two never is.
        staged = db_path.with_name(db_path.name + "._tmp_")
        staged.unlink(missing_ok=True)
        log.info("%s: built, copying it into the archive", built.name)
        shutil.copyfile(built, staged)
        staged.replace(db_path)
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def create_db(
    store_path: pathlib.Path,
    db_path: pathlib.Path,
    force: bool = False,
    temp_dir: pathlib.Path | None = None,
) -> RebuildResult:
    """Build a queryable database from the archive's metadata log.

    Not a rebuild of something the archive owns: the archive holds no database.
    This makes one, wherever the caller asks for it, out of what does live there.
    The log says which messages the archive accounts for and where each was seen;
    each of those is then read for what it carries in itself -- sender,
    recipients, subject, date.

    **The log is the list, not the store.** An archive is the mail and the log
    together, and a message the log names nowhere is not part of it yet -- so it
    is not in the projection either, and that is the archive being incomplete
    rather than the database being wrong. `archive check` reports such messages
    and `archive adopt` takes them in. Going through the log rather than walking
    the shards is also what makes this half as expensive: the walk pays a round
    trip per shard directory, and an id can be turned into an open file without
    it.

    What comes out is a snapshot. It is accurate for the moment it was built and
    goes stale from the next backup onwards; build it again when that matters, or
    keep it fresh with `refresh_db`.

    An existing file is refused unless `force` is given, and `force` replaces it
    rather than adding to it. Writing into a database that is already there would
    make the result an accumulation instead of a snapshot -- rows from an earlier
    run stay even when the archive no longer yields them, and a correction to how
    a header is read would never reach them.

    Built through a temporary file and renamed into place at the end, the same
    discipline the archive uses. An interrupted run leaves no half-built database
    where a whole one is expected, and the previous one survives it. `temp_dir`
    says where to build it; see `_building` for the one case that is worth it.
    """
    if db_path.exists() and not force:
        raise JobError(f"{db_path}: already exists, use --force to replace it")
    # Doubled on purpose, and it looks like dead code from the CLI: every `db`
    # subcommand goes through `cli.common.require_archive` first, which refuses
    # an unmarked directory before this is reached, so this line is not what a
    # user sees. It is what a caller of `mailvault.jobs` sees -- and what stands
    # between "there is no log here" and a rebuild that would throw away the only
    # record an archive from before 0.10 has of where its mail came from. The
    # same holds for `Freshness.unmigrated_archive` and `_unfit`.
    #
    # Reachability is not the test for a guard whose absence loses mail.
    if not marker.is_archive(store_path):
        raise JobError(
            f"{store_path}: an older archive that still keeps its own record of the"
            f" mail -- there is no log here to build a query database from, lift it"
            f" with `{MIGRATE_COMMAND}` first"
        )
    store = cas.mail_store(store_path)
    result = RebuildResult()
    with _building(db_path, temp_dir) as build_path:
        _build_db(store, store_path, build_path, result)
    return result


def _build_db(
    store: cas.ContentAddressedStorage,
    store_path: pathlib.Path,
    db_path: pathlib.Path,
    result: RebuildResult,
) -> None:
    """Fill a fresh database from the archive's log and the messages it names."""
    log_root = store_path / metalog.DEFAULT_LOG_DIR
    with index_db.IndexDatabase(path=db_path, create=True) as db:
        rows = _Rows(db, store)
        for logfile in metalog.read_all(log_root):
            result.replay.files += 1
            if logfile.mailbox is None and logfile.folder is None:
                log.warning("%s: names no place at all, skipped", metalog.where(logfile.path))
                continue
            result.replay.entries += len(logfile.store_ids)
            applied, unknown = _fold_log_file(db, rows, logfile)
            result.replay.applied += applied
            result.replay.unknown += unknown
        result.messages = rows.created
        result.undated = _undated(db)
        # Prime the bookkeeping so a later refresh reads only files added since,
        # and so a later *reader* can tell whether the archive has moved on.
        _mark_logs_applied(db, metalog.log_files(log_root))
        _record_heads(db, store_path / heads.DEFAULT_HEADS_DIR)


def _undated(db: index_db.IndexDatabaseConnection) -> int:
    """How many messages ended up with no date, asked of the database itself."""
    row = db.execute("SELECT count(*) FROM message WHERE date IS NULL").fetchone()
    return int(row[0]) if row else 0


def refresh_db(store_path: pathlib.Path, db_path: pathlib.Path) -> RefreshResult:
    """Bring a kept-fresh projection up to date with the archive, incrementally.

    A convenience projection, not a source of truth. If it is missing or not a
    usable database it is built from scratch; otherwise only the log files it has
    not applied yet are read in, so a routine refresh after a backup costs a
    handful of small reads plus a header read for each newly archived message.

    A message reaches the projection because a log file records it, so everything
    that writes one feeds it: a backup, `archive import`, `archive adopt`. What
    does not is mail that no log file names at all -- what an import brought in
    before it took a `--name`. `archive adopt` gives it a place, and then this
    picks it up like anything else.
    """
    result = RefreshResult()
    if not db_path.exists():
        # Said before the work starts, not after: building one means reading
        # every message in the archive, which is minutes to half an hour of a
        # backup that had nothing else left to do. Why it is happening at all is
        # the part a reader cannot guess.
        log.info(
            "%s: no query database yet, building one from the whole archive",
            utils.under(store_path, db_path),
        )
        result.rebuilt = True
        result.messages = create_db(store_path, db_path, force=True).messages
        return result

    store = cas.mail_store(store_path)
    log_root = store_path / metalog.DEFAULT_LOG_DIR
    heads_root = store_path / heads.DEFAULT_HEADS_DIR
    try:
        with index_db.IndexDatabase(path=db_path) as db:
            if not db.usable:
                # Left exactly as it is, and said out loud. Rebuilding it here
                # would be a backup deciding on its own to spend half an hour
                # reading every message in the archive, for a file that is a
                # convenience -- and doing it without being asked, at the end of
                # a run that had nothing else to do. The projection is not a
                # source of truth; whoever wants it back says so.
                result.unreadable = True
                log.warning(
                    "%s",
                    _unreadable(
                        utils.under(store_path, db_path),
                        db.outdated,
                        ", left untouched and NOT updated",
                    ),
                )
                log.debug(
                    "%s: shape %d, this version writes %d; missing: %s",
                    db_path.name,
                    db.shape,
                    index_db.SCHEMA_VERSION,
                    ", ".join(str(obj) for obj in db.missing()) or "nothing",
                )
                return result
            _apply_new_logs(db, store, log_root, result)
            _record_heads(db, heads_root)
    except sqlite3.DatabaseError as exc:
        log.warning(
            "%s: not a usable database (%s), building one from the whole archive",
            utils.under(store_path, db_path),
            exc,
        )
        db_path.unlink(missing_ok=True)
        result.rebuilt = True
        result.messages = create_db(store_path, db_path, force=True).messages
    return result


def _apply_new_logs(
    db: index_db.IndexDatabaseConnection,
    store: cas.ContentAddressedStorage,
    log_root: pathlib.Path,
    result: RefreshResult,
) -> None:
    """Fold every log file not yet applied into the database.

    Idempotent and self-healing: the set of applied files is read from the
    database, so a refresh interrupted halfway simply resumes, and a projection
    that fell behind (the option was off for a while) catches up on all of them.
    A message row is created the first time a store id is seen; after that only
    its location is added.
    """
    applied = {row[0] for row in db.execute("SELECT hash FROM applied_log")}
    rows = _Rows(db, store)
    for path in metalog.log_files(log_root):
        if _log_hash(path) in applied:
            continue
        logfile = metalog.read_log(path)
        if logfile is None or (logfile.mailbox is None and logfile.folder is None):
            # Unreadable or naming no place at all: leave it unmarked so a later,
            # repaired file is retried rather than skipped for good.
            continue
        result.files += 1
        # What the file said and the note that the file was read are one step,
        # so neither can reach disk without the other; the transaction inside
        # `_fold_log_file` joins this one. `execute` does not commit, so a note
        # written outside a transaction owes its durability to whatever call
        # happens to open one next -- which for the last file is nothing.
        with db.transaction():
            folded, unknown = _fold_log_file(db, rows, logfile)
            db.execute(
                "INSERT OR IGNORE INTO applied_log (hash) VALUES (?)",
                (_log_hash(path),),
            )
        result.applied += folded
        result.unknown += unknown
    result.messages = rows.created


def drop_db(db_path: pathlib.Path) -> bool:
    """Delete the projection; True when there was one to delete.

    The one destructive command in the program that needs no confirmation and no
    dry run. Nothing is lost that the archive cannot produce again -- which is
    the whole difference between this and every other file in there.
    """
    if not db_path.exists():
        return False
    db_path.unlink()
    return True


@dataclasses.dataclass
class SearchQuery:
    """What to look for. Every field given has to match; empty means unfiltered.

    The text fields match anywhere in the value and ignore case, because that is
    what somebody typing `--from example` means. `since` and `until` are dates and
    compare against the day a message is dated, whatever timezone it carried.
    """

    sender: str | None = None
    recipient: str | None = None
    subject: str | None = None
    mailbox: str | None = None
    folder: str | None = None
    since: str | None = None
    until: str | None = None
    limit: int | None = None

    def is_empty(self) -> bool:
        return not any(
            (
                self.sender,
                self.recipient,
                self.subject,
                self.mailbox,
                self.folder,
                self.since,
                self.until,
            )
        )


@dataclasses.dataclass
class SearchHit:
    """One message the search found, with what a person needs to recognise it."""

    store_id: str
    date: str | None
    sender: str | None
    subject: str | None
    places: list[str]


def _like(value: str) -> str:
    """A substring match, with the wildcards a user typed left as literals.

    Somebody searching for `100%` means the three characters, and a value that
    silently turned into a pattern would quietly match the wrong messages.
    """
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _conditions(query: SearchQuery) -> tuple[list[str], list[str]]:
    """The WHERE clauses and their values, one pair per filter that was given."""
    where: list[str] = []
    values: list[str] = []

    def address_in(table: str, value: str) -> None:
        where.append(
            f"EXISTS (SELECT 1 FROM {table} x JOIN address a USING (address_id)"
            f" WHERE x.message_id = m.message_id AND a.address LIKE ? ESCAPE '\\')"
        )
        values.append(_like(value))

    def place_in(column: str, table: str, value: str) -> None:
        where.append(
            f"EXISTS (SELECT 1 FROM message_location loc JOIN {table} p"
            f" USING ({column}) WHERE loc.message_id = m.message_id"
            f" AND p.name LIKE ? ESCAPE '\\')"
        )
        values.append(_like(value))

    if query.sender:
        address_in("message_sender", query.sender)
    if query.recipient:
        address_in("message_recipient", query.recipient)
    if query.subject:
        where.append("subj.text LIKE ? ESCAPE '\\'")
        values.append(_like(query.subject))
    if query.mailbox:
        place_in("mailbox_id", "mailbox", query.mailbox)
    if query.folder:
        place_in("folder_id", "folder", query.folder)
    # Compared against the column itself, so the index over it can answer the
    # range instead of every message in the archive being read to be measured.
    # A stored date begins with the day it names -- `2026-08-20T21:03:11+02:00`
    # -- so comparing the whole value against a day is the comparison wanted,
    # and the offset it carries can no longer move an hour across a boundary the
    # reader thinks of in days.
    if query.since:
        where.append("m.date >= ?")
        values.append(query.since)
    if query.until:
        where.append("m.date < ?")
        values.append(query.until + _ABOVE_ANY_DATE)
    return where, values


_SEARCH_SQL = """
SELECT
    m.store_id,
    m.date,
    (SELECT a.address FROM message_sender s JOIN address a USING (address_id)
      WHERE s.message_id = m.message_id ORDER BY a.address LIMIT 1) AS sender,
    subj.text AS subject,
    (SELECT group_concat(
        CASE
            WHEN mb.name IS NULL THEN f.name
            WHEN f.name IS NULL THEN mb.name
            ELSE mb.name || '::' || f.name
        END, char(10))
      FROM message_location loc
      LEFT JOIN mailbox mb USING (mailbox_id)
      LEFT JOIN folder f USING (folder_id)
      WHERE loc.message_id = m.message_id) AS places
FROM message m
LEFT JOIN subject subj USING (subject_id)
"""


def search(db_path: pathlib.Path, query: SearchQuery) -> list[SearchHit]:
    """Find the messages the projection records matching every filter given.

    One row per message and not per address: a message with four recipients is
    one message. Ordered by date, oldest first, with the messages whose date
    could not be read at the end -- they are unknown, not old.
    """
    if not db_path.exists():
        # The guard belongs with the question, not with each caller who asks it.
        # Without it `IndexDatabase` created the file: `connect()` makes one, the
        # schema is not written on open any more, and what was left behind was an
        # empty file that `db create` then refused as "already here" -- the error
        # this very call raises tells the reader to build one, and the stray file
        # made that impossible without --force.
        raise JobError(Freshness(absent=True).complaint(db_path.name) or "")

    where, values = _conditions(query)
    sql = _SEARCH_SQL
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY m.date IS NULL, m.date, m.store_id"
    if query.limit is not None:
        sql += f" LIMIT {int(query.limit):d}"

    with index_db.IndexDatabase(path=db_path) as db:
        if not db.usable:
            raise JobError(_unreadable(db_path.name, db.outdated))
        rows = db.execute(sql, values).fetchall()
    return [
        SearchHit(
            store_id=row["store_id"],
            date=row["date"],
            sender=row["sender"],
            subject=row["subject"],
            places=row["places"].split("\n") if row["places"] else [],
        )
        for row in rows
    ]
