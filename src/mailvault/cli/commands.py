"""Command handlers for the `mailvault` CLI.

Each `run_*` function is the dispatch target for one command group in
`mailvault.cli.main`; the argument parsing lives there, the actual work in
`mailvault.jobs` and `mailvault.importer`.
"""

from __future__ import annotations

import argparse
import logging
import pathlib
import sys

from mailvault import conf, importer, jobs, utils
from mailvault.backend import base
from mailvault.jobs import guard
from mailvault.store import cas, heads, marker, metalog

log = logging.getLogger(__name__)

# Failures that are already understood by the time they get here: a broken
# config, a refused operation, a mailbox that said no. There is nothing to
# debug in them, so they are reported as one line and the traceback is left to
# the errors nobody anticipated -- where the call stack is the only clue. The
# traceback is still there under `--verbose` for the rare case it is wanted.
EXPECTED_ERRORS = (conf.ConfigError, jobs.JobError, base.MailboxError, marker.FormatError)

# The commands that work on an archive directory, as opposed to `folders`, which
# only ever talks to the server.
ARCHIVE_COMMANDS = {"backup", "verify"}

# The configuration an archive carries. Named after the tool rather than after
# its purpose -- `config.toml` would be the better name inside an archive, where
# nothing else competes for it, but this is the same file in both roles, and the
# other role is the directory one happens to be standing in. That is a shared
# name space: a `config.toml` there belongs to whatever else lives in that
# directory, and reading one by accident is not a theoretical worry.
DEFAULT_CONFIG_NAME = "mailvault.toml"


def archive_path(args: argparse.Namespace) -> pathlib.Path:
    """The archive a command works on: `--archive`, or the directory one is in.

    Two independent knobs, and nothing derived between them -- this is the only
    place an archive comes from. A configuration used to be able to name one,
    which cannot work across machines: the NAS hangs at a different path on each
    of them while the configuration sits in a home directory, so there is no
    path that is right on both. A configuration *inside* the archive has that
    distance by construction, and then there is nothing left for it to say.
    """
    if args.archive is not None:
        return args.archive
    return pathlib.Path.cwd()


def config_file(args: argparse.Namespace, archive: pathlib.Path) -> pathlib.Path:
    """The configuration to read: `--config`, or the one the archive carries."""
    if args.config is not None:
        return args.config
    return archive / DEFAULT_CONFIG_NAME


# The two commands that are allowed to meet a directory that is not an archive
# yet: one makes an archive out of it, the other lifts an older one into this
# layout. Everything else has an archive as its subject and says so.
WITHOUT_AN_ARCHIVE = {"init", "migrate"}


def require_archive(archive: pathlib.Path) -> None:
    """Stop a command that was pointed at something which is not an archive.

    The mark is the whole test, the way `.git` is for a repository. Before this,
    every command opened `<directory>/mail` and worked on whatever it found --
    which on an archive from before 0.10 is nothing at all, because the messages
    are still in the root. `archive check` then reported a healthy 131,000-message
    archive as a total loss, and `verify --repair` set about downloading the
    mailbox a second time.

    Both cases the mark cannot tell apart get named, because the answer differs:
    an older archive is lifted, a wrong directory is left alone.
    """
    if marker.is_archive(archive):
        return
    raise jobs.JobError(
        f"{archive}: not a mailvault archive -- no {marker.FORMAT_NAME} file."
        f" `mailvault archive init` makes one here; an archive from before"
        f" mailvault 0.10 is lifted by `mailvault archive migrate`"
    )


# --- folders / backup / verify -------------------------------------------------


def report_verify(job_name: str, results: list[jobs.VerifyResult], repaired: bool) -> None:
    for r in results:
        line = f"{job_name}::{r.folder}: {r.on_server:,} on server, {r.missing:,} not archived"
        if repaired:
            line += f", {r.restored:,} restored"
            if r.failed:
                line += f", {r.failed:,} failed"
        print(line)
    total_missing = sum(r.missing for r in results)
    total_restored = sum(r.restored for r in results)
    if not total_missing:
        print(f"{job_name}: archive is complete")
    elif not repaired:
        print(f"{job_name}: {total_missing:,} message(s) missing, run again with --repair")
    else:
        print(f"{job_name}: {total_restored:,} of {total_missing:,} message(s) restored")


def _run_job(
    job: conf.JobConfig,
    args: argparse.Namespace,
    config: conf.Config,
    destination: pathlib.Path | None = None,
) -> None:
    log.info(f"Job item: {job.name}")

    if args.command == "folders":
        jobs.folder_list(job)
    elif destination is None:
        # `run_mailbox` resolves the archive for every command that works on one,
        # so this does not happen from the CLI. It is here because the argument
        # may be absent and running the job anyway would mean guessing where.
        raise jobs.JobError(f"{args.command}: no archive directory")
    elif args.command == "backup":
        compress = args.compress or config.compress
        index_db = args.index_db or config.index_db
        # The one switch that turns something off rather than on, so it cannot
        # follow the `args.x or config.x` pattern of the two above: `--full` is
        # a veto on the configured default, not an addition to it.
        incremental = config.incremental and not args.full
        jobs.backup(
            job,
            destination,
            compress=compress,
            index_db=index_db,
            incremental=incremental,
        )
    elif args.command == "verify":
        compress = args.compress or config.compress
        results = jobs.verify(job, destination, repair=args.repair, compress=compress)
        report_verify(job.name, results, repaired=args.repair)


def run_mailbox(args: argparse.Namespace) -> int:
    """Run a folders/backup/verify command over the selected config jobs."""
    exit_code = 0
    needs_archive = args.command in ARCHIVE_COMMANDS
    if needs_archive and args.config is not None and args.archive is None:
        # Reaching for a configuration somewhere else is what somebody does who
        # is *not* standing in the archive, so the directory they happen to be in
        # is the last thing that should decide where the mail goes. Nothing else
        # is left to derive it from, so this asks instead of guessing.
        raise conf.ConfigError(
            f"{args.config}: a configuration was named, but no archive -- name that "
            f"too, with --archive"
        )

    archive = archive_path(args)
    if needs_archive:
        require_archive(archive)
    path = config_file(args, archive)
    try:
        config = conf.load(path, allow_exec=args.allow_exec)
    except conf.ConfigError:
        # Naming the file that was looked for is not enough when nobody asked
        # for it: a reader is left wondering why that path of all paths. What
        # they need to be told is the rule that produced it. Only for a file
        # that is not there -- a broken one in the archive keeps its own
        # message, which says what is wrong with it.
        if args.config is not None or path.exists():
            raise
        raise conf.ConfigError(
            f"no {DEFAULT_CONFIG_NAME} here -- an archive carries its own"
            f" configuration. Stand in the archive, name it with --archive, or name a"
            f" configuration with --config"
        ) from None
    selected = config.jobs
    if args.job:
        selected = [j for j in selected if j.name in args.job]
        unknown = set(args.job) - {j.name for j in selected}
        for name in unknown:
            log.error("Unknown job: %s", name)
            exit_code = 1

    # This comes before the first job, and deliberately so: it decides whether
    # this configuration and this archive belong together, and the answer is
    # worth nothing once a message has been written -- or, with
    # `delete_after_export`, removed from the server.
    destination = None
    if needs_archive:
        destination = archive
        guard.check_jobs(destination, selected, allow_new=args.allow_new_mailbox)

    for job in selected:
        # One broken job must not stop the remaining ones, but the run as a
        # whole reports failure so callers/cron can react.
        try:
            _run_job(job, args, config, destination)
        except EXPECTED_ERRORS as exc:
            # A misconfigured or refused job is a user error, not a crash --
            # reported as one line here for the same reason `main` does it.
            log.error("Job '%s' failed: %s", job.name, exc)
            log.debug("Job '%s' failed", job.name, exc_info=exc)
            exit_code = 1
        except Exception as exc:
            log.exception("Job '%s' failed: %s", job.name, exc)
            exit_code = 1
    return exit_code


# --- archive -------------------------------------------------------------------


def _external(path: pathlib.Path, docuware: bool = False) -> importer.ExternalMailArchive:
    """A directory of mails read from the outside, whichever layout it has."""
    cls = importer.DocuwareMailArchive if docuware else importer.ExternalMailArchive
    return cls(path)


def _human_size(size: int) -> str:
    units = ["bytes", "KiB", "MiB", "GiB", "TiB"]
    value = float(size)
    unit = units[0]
    for unit in units:
        if value < 1024 or unit == units[-1]:
            break
        value /= 1024
    return f"{value:.1f} {unit}"


def report_init(archive: pathlib.Path, result: jobs.InitResult) -> int:
    """Say that there is an archive here now, and what is left to do.

    Which directories were made is the archive's own business -- `git init` says
    it made a repository, not which files went into `.git`. What the reader does
    need is the one thing still missing: an archive nobody has configured cannot
    back anything up.
    """
    print(f"{archive}: {'archive created' if result.created else 'already an archive'}")
    if result.config is not None:
        if result.config_existed:
            print(f"{result.config.name} is already there and was left alone")
        else:
            print(f"{result.config.name} written -- fill in your mailboxes, then back up")
    return 0


def report_migration(source: pathlib.Path, result: jobs.MigrationResult) -> int:
    """Say what was lifted, and what the archive is now.

    Every step is named even where it moved nothing. A migration that says only
    what it happened to find leaves a reader unable to tell "there was nothing
    to do" from "that part did not run".
    """
    if result.generation == marker.CURRENT_FORMAT:
        print(f"already {marker.describe(marker.CURRENT_FORMAT)}, nothing to do")
        return 0

    print(f"{result.resume_points:,} resume point(s) moved into {heads.DEFAULT_HEADS_DIR}/")
    if not result.needed:
        print("no metadata database, nothing to move out of one")
        return _report_rest(source, result)
    if not result.verified:
        print("the written log files did not verify, database left alone")
        return 1
    print(
        f"{result.messages:,} message(s) moved into {result.places:,} mailbox/folder place(s)"
    )
    if result.snapshots:
        print(
            f"{result.snapshots:,} resume timestamp(s) taken from the database"
            f" -- as a record of when, never as a point to carry on from"
        )
    if result.placeless:
        print(
            f"{result.placeless:,} of them recorded without a folder -- the old "
            f"database did not store which folder of which mailbox"
        )
    if result.undecidable:
        print(
            f"{result.undecidable:,} folder name(s) fit more than one mailbox "
            f"and were left out rather than guessed"
        )
    if result.renamed_to is not None:
        print(f"the database is now {result.renamed_to.name} and is no longer used")
        print("delete it once you are satisfied with the archive")
    return _report_rest(source, result)


def _report_rest(source: pathlib.Path, result: jobs.MigrationResult) -> int:
    """The steps after the two older formats gave up what they held."""
    print(f"{result.shards_moved:,} shard(s) moved into {cas.MAIL_DIR}/")
    if result.consolidated is not None:
        report_compact(source, result.consolidated)
    return _report_generation(source, result)


def _report_generation(source: pathlib.Path, result: jobs.MigrationResult) -> int:
    """Say what the archive says about itself now, or why it says nothing yet.

    The mark is the verdict, and that is why it is also the exit code: it is
    written last, so an archive that carries it got through every step. A cron
    job whose migration stopped half way must not be told the run went well.
    """
    now = marker.read(source)
    if now == marker.CURRENT_FORMAT:
        print(f"{marker.describe(now)}")
        return 0
    print(
        "NOT marked -- something above did not finish, so the next"
        " run picks the migration up again"
    )
    return 1


def report_create_db(
    source: pathlib.Path,
    target: pathlib.Path,
    result: jobs.RebuildResult,
) -> None:
    """Say what went into the database, and name what could not."""
    replay = result.replay
    print(f"{result.messages:,} message(s) read from the archive")
    if replay.files:
        print(
            f"metadata log: {replay.files:,} file(s), "
            f"{replay.applied:,} of {replay.entries:,} location(s) applied"
        )
        if replay.unknown:
            print(
                f"{replay.unknown:,} log entry/entries name messages that are "
                f"not in the archive, ignored"
            )
    else:
        print("no metadata log found, mailbox and folder are NOT in the database")
    print(f"{target}: written -- a snapshot, stale from the next backup onwards")


def report_compact(source: pathlib.Path, result: metalog.CompactResult) -> int:
    """Say how much the log shrank and how many duplicate entries went.

    Non-zero when the consolidated files did not verify: the log is unchanged
    and nothing was lost, but the pass did not do what it was asked, and a
    scheduler reading only the exit code would file it as a success.
    """
    if result.files_before == 0:
        print("no metadata log to compact")
        return 0
    if not result.verified:
        print("consolidated files did not verify, nothing was removed")
        return 1
    print(
        f"{result.files_before:,} log file(s) -> {result.files_after:,} "
        f"across {result.places:,} place(s)"
    )
    dropped = result.entries_before - result.entries_after
    if dropped:
        print(f"{dropped:,} duplicate observation(s) dropped")
    if result.transient_removed:
        # Said out loud rather than swept up quietly: each one is a write that
        # was interrupted, and that is worth knowing about.
        print(f"{result.transient_removed:,} leftover(s) of an interrupted write removed")
    return 0


# How many of a kind a report names before it stops listing them. A check on a
# damaged archive can find tens of thousands; the count is the finding, the
# names are there to give someone a place to start.
REPORT_LIMIT = 20


def _report_items(finding: str, items: list[str]) -> None:
    """Print a finding's count and the first few of whatever it found.

    What each line names depends on what the finding is about. A message is
    named by its id, because that is what every other command takes and the
    only handle its owner has any use for; where the file happens to lie is the
    store's business. A finding *about a file* -- one that is not a message at
    all, or a log file -- names the path, because there the file is the thing.
    """
    if not items:
        return
    print(f"{len(items):,} {finding}")
    for item in items[:REPORT_LIMIT]:
        print(f"  {item}")
    if len(items) > REPORT_LIMIT:
        print(f"  ... and {len(items) - REPORT_LIMIT:,} more")


def report_import(
    source: pathlib.Path,
    destination: pathlib.Path,
    result: importer.ImportResult,
) -> int:
    """Say what the import did, or what it would have done.

    Both counts are named even when one of them is zero. "How many were already
    there" is what tells a dry run apart from a disaster: a source that has been
    through a converter on its way here looks exactly like a source full of new
    mail, and the difference only shows in the ratio.
    """
    total = result.stored + result.present
    verb = "would be imported" if result.dry_run else "imported"
    print(
        f"{total:,} message(s) read -- {result.stored:,} {verb},"
        f" {result.present:,} already in {destination}"
    )
    _report_items(
        "message(s) could not be read", [utils.under(source, p) for p in result.failed]
    )
    return 1 if result.failed else 0


def _report_orphans(result: jobs.CheckResult, store_ids: list[str]) -> None:
    """Say what a message with no place recorded is, and what follows from it.

    Nothing, is what follows, and that is the whole message. These are archived,
    intact and readable; the one thing missing is the note which folder they came
    from, and no command can put it back, because it never was in the archive.
    Saying so is the finding -- a reader who is told only "110 not referenced"
    goes looking for the repair that does not exist.

    So this one prints no list. A store id is the right handle for `archive
    export` and useless to a person deciding whether their archive is all right,
    and twenty of a hundred and ten is neither a list to work from nor short
    enough to skim. They go to the debug log, whole.
    """
    if not store_ids:
        return
    print(
        f"{len(store_ids):,} message(s) belong to no known place -- stored and"
        " intact, but nothing records which folder they came from"
    )
    print(
        "  they are found like any other message: `archive create-db` builds a"
        " query database with sender, subject and date"
    )
    print("  mail brought in with `archive import` is always like this")
    log.debug("no place recorded: %s", ", ".join(store_ids))


def report_check(source: pathlib.Path, result: jobs.CheckResult) -> int:
    """Say what the archive turned out to be, and whether that is all right.

    The verdict at the end is not decoration, and neither is its wording. A
    check that did not read the contents cannot have found an entry whose bytes
    changed under it, so a clean run means two different things depending on how
    it was asked -- and a reader who is told only "sound" has no way to know
    which of the two they got. The exit code says the same thing, but nobody
    reads an exit code they did not go looking for.

    The two message counts are here to be subtracted from each other: what lies
    in the archive, and what the log accounts for. The difference is the orphans
    listed further down, and seeing it as arithmetic is what makes that list
    something other than a number to be alarmed by.

    A third count used to stand between them -- log entries, one per message per
    place, so a Gmail message under three labels counted three times. It is
    gone. It was neither files nor messages nor folders, it was six figures wide
    next to two-figure neighbours, and nothing a reader could decide followed
    from it either way. A number that answers no question is not neutral: it
    gets read as an answer to whichever question the reader had.
    """
    store = cas.mail_store(source)

    def ids(paths: list[pathlib.Path]) -> list[str]:
        """The message ids of entries -- what `archive export` and the log take."""
        return [store.hashval_of(path) or str(path) for path in paths]

    print(
        f"{result.entries:,} message(s) stored, {result.referenced:,} of them"
        f" accounted for by {result.log_files:,} log file(s) in {result.places:,} place(s)"
    )
    _report_items(
        "message(s) referenced in the log are missing",
        [f"{store_id}  {where}" for store_id, where in result.missing.items()],
    )
    _report_items(
        "log file(s) are damaged -- the content does not match its checksum",
        [utils.under(source, path) for path in result.damaged_logs],
    )
    _report_items(
        "log file(s) the chain names are gone -- nothing records what they held",
        result.broken_chains,
    )
    _report_items(
        "log file(s) no chain reaches -- they are still read, the chain is behind",
        [utils.under(source, path) for path in result.unchained],
    )
    _report_items(
        "message(s) are damaged -- the content does not match its checksum",
        ids(result.corrupt),
    )
    _report_items("message(s) could not be read", ids(result.unreadable))
    _report_items(
        "file(s) in the archive are not messages",
        [utils.under(source, path) for path in result.foreign],
    )
    _report_orphans(result, ids(result.orphans))
    if result.quarantined_before:
        print(f"{result.quarantined_before:,} message(s) set aside by an earlier run")
    if result.transient_removed:
        print(f"{result.transient_removed:,} leftover(s) of an interrupted write removed")
    if result.quarantined:
        print(
            f"{len(result.quarantined):,} damaged message(s) set aside -- they"
            " count as missing now, fetch them with `verify --repair` or `backup --full`"
        )
    if not result.sound:
        print(f"NOT sound -- {result.findings:,} finding(s) above")
    elif result.contents_checked:
        print("sound -- every message was read and matches its checksum")
    else:
        print(
            "sound as far as this went -- the integrity check was skipped,"
            " so no damaged message could have been found"
        )
    return 0 if result.sound else 1


def report_conversion(
    source: pathlib.Path,
    result: cas.ConversionResult,
    done: str,
    already: str,
) -> int:
    """Say what a conversion pass did, and exit non-zero when part of it failed.

    A pass keeps going when one entry fails, so without this the command would
    report how many files it converted and stay silent about the ones it did
    not: the archive would look converted when it is not, and a script driving
    the command would never find out.
    """
    print(f"{result.converted:,} files {done}, {result.skipped:,} {already}")
    for path in result.failed:
        print(f"{utils.under(source, path)}: could not be converted, left as it is")
    if not result.failed:
        return 0
    print(f"{len(result.failed):,} file(s) failed, see the log for the reason")
    return 1


def _entry_path(store: cas.ContentAddressedStorage, wanted: str) -> pathlib.Path:
    """Find the entry a message id names.

    A path is accepted too, and only its file name is looked at -- someone who
    has been in the directory with `ls` should not be sent away. But an id is
    what the reports print and what every other command takes; where an entry
    lies is the store's business and no part of the interface.
    """
    # A store id has no suffix, so `hashval_of` declines it and the fallback
    # takes over; only what the directories say is ignored, which is why a path
    # copied from a report written on another machine still finds its entry.
    hashval = store.hashval_of(pathlib.Path(wanted))
    if hashval is None:
        if not cas.is_hashval(wanted):
            raise jobs.JobError(f"{wanted}: neither a store id nor the path of an entry")
        hashval = cas.normalize_hashval(wanted)
    found = store.locate(hashval, exists=True)
    if found is None:
        raise jobs.JobError(f"{hashval}: not in this archive")
    return found


def export_entries(
    source: pathlib.Path,
    wanted: list[str],
    output: pathlib.Path | None,
) -> int:
    """Write out what an entry holds, decompressed, exactly as it was stored.

    The way to look at a message the reports can only name. What lies in the
    archive under that id may be a zstd frame in a sharded directory, and none
    of that is anyone's business outside the store -- this hands over the
    message.

    Raw and unmodified: whatever comes out here hashes back to the name it came
    from, so it is also the way to hand a message to another tool without the
    archive having an opinion about it.
    """
    store = cas.mail_store(source)
    paths = [_entry_path(store, one) for one in wanted]

    if output is None:
        if len(paths) > 1:
            raise jobs.JobError(
                "export: several messages need --output, or they would arrive as one"
                " stream with nothing between them"
            )
        sys.stdout.buffer.write(store.read(paths[0]))
        return 0

    if len(paths) > 1 or output.is_dir():
        if not output.is_dir():
            raise jobs.JobError(f"{output}: not a directory")
        for path in paths:
            target = output / path.name.removesuffix(".zst")
            target.write_bytes(store.read(path))
            print(f"{target}")
        return 0

    output.write_bytes(store.read(paths[0]))
    print(f"{output}")
    return 0


def _refuse_importing_the_archive(archive: pathlib.Path, source: pathlib.Path) -> None:
    """Refuse an import whose source and destination are the same mail.

    `import` reads from somewhere else -- that is what makes it an import. Since
    the archive stopped being a positional argument and became the directory one
    is standing in, `mailvault archive import --move .` is a plausible slip
    rather than an absurdity, and it is the one command in the program that
    deletes mail: every message is found, answered with EXISTS because it is
    already there, and then removed from the source. Which is the archive.

    Refused with or without `--move`. Without it the run is merely a long way of
    doing nothing, and one rule is easier to rely on than one that depends on a
    flag.
    """
    here = archive.resolve()
    there = source.resolve()
    if here == there or there.is_relative_to(here) or here.is_relative_to(there):
        raise jobs.JobError(
            f"{source}: an import reads from somewhere else, and this is the archive"
            f" itself. With --move it would find every message already stored and"
            f" then delete it. Name a source outside {archive}"
        )


def run_archive(args: argparse.Namespace) -> int:
    """Run an `archive` subcommand (stats/import/addresses/compress/create-db/...).

    Every one of these works on the archive `--archive` names, or on the
    directory one is standing in. `import` is the only one with a directory of
    its own left to name: what it reads from is somebody else's archive, and
    that is a different thing from the one being written to.
    """
    cmd = args.archive_command
    archive = archive_path(args)
    if cmd not in WITHOUT_AN_ARCHIVE:
        require_archive(archive)

    if cmd == "init":
        return report_init(archive, jobs.init_archive(archive, DEFAULT_CONFIG_NAME))
    elif cmd == "export":
        return export_entries(archive, args.entry, args.output)
    elif cmd == "stats":
        count, size = _external(archive, args.docuware).stats()
        print(f"{count:,} emails, {_human_size(size)} total")
    elif cmd == "addresses":
        for where, addr in _external(archive, args.docuware).addresses():
            print(where, addr)
    elif cmd == "import":
        _refuse_importing_the_archive(archive, args.source)
        source = _external(args.source, args.docuware)
        destination = cas.mail_store(archive, compress=args.compress)
        return report_import(
            args.source,
            archive,
            source.archive_to_cas(destination, move=args.move, dry_run=args.dry_run),
        )
    elif cmd == "compress":
        store = cas.mail_store(archive)
        return report_conversion(
            archive, store.compress_all(), "compressed", "already compressed"
        )
    elif cmd == "decompress":
        store = cas.mail_store(archive)
        return report_conversion(
            archive, store.decompress_all(), "decompressed", "already plain"
        )
    elif cmd == "create-db":
        result = jobs.create_db(
            archive,
            args.database,
            mailbox=args.mailbox,
            force=args.force,
        )
        report_create_db(archive, args.database, result)
    elif cmd == "migrate":
        return report_migration(archive, jobs.migrate_archive(archive))
    elif cmd == "compact":
        return report_compact(
            archive,
            metalog.compact(
                archive / metalog.DEFAULT_LOG_DIR, archive / heads.DEFAULT_HEADS_DIR
            ),
        )
    elif cmd == "check":
        return report_check(
            archive,
            jobs.check(
                archive, contents=not args.no_integrity_check, quarantine=args.quarantine
            ),
        )

    return 0
