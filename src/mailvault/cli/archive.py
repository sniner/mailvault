"""The `archive` group -- looking after an archive, without a mailbox in sight.

Everything here works on a directory and nothing else: make one, lift an older
one, take mail into it, say what is in it, check that it still holds what it
claims. Getting a message back out is not one of them -- that is using an archive
rather than maintaining it, and it lives in `message`.
"""

from __future__ import annotations

import argparse
import logging
import pathlib

from mailvault import jobs, utils
from mailvault.cli.common import (
    DEFAULT_CONFIG_NAME,
    archive_path,
    report_items,
    require_archive,
    shorten,
)
from mailvault.store import cas, heads, marker, metalog

log = logging.getLogger(__name__)


# The two commands that are allowed to meet a directory that is not an archive
# yet: one makes an archive out of it, the other lifts an older one into this
# layout. Everything else has an archive as its subject and says so.
WITHOUT_AN_ARCHIVE = {"init", "migrate"}

# How wide the two name columns may get before they are cut. A mailbox is a host
# name and stays short; a folder can be `[Google Mail]/Alle Nachrichten` or a
# nested path, and cutting it is better than a table that wraps.
MAILBOX_WIDTH = 30
FOLDER_WIDTH = 44


def _external(path: pathlib.Path, docuware: bool = False) -> jobs.ExternalMailArchive:
    """A directory of mails read from the outside, whichever layout it has."""
    cls = jobs.DocuwareMailArchive if docuware else jobs.ExternalMailArchive
    return cls(path)


def _human_size(size: int) -> str:
    units = ["bytes", "KiB", "MiB", "GiB", "TiB"]
    value = float(size)
    for unit in units[:-1]:
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    # The largest unit is where it stops, however big the number gets.
    return f"{value:.1f} {units[-1]}"


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

    points = utils.counted(result.resume_points, "resume point")
    print(f"{points} moved into {heads.DEFAULT_HEADS_DIR}/")
    if not result.needed:
        print("no metadata database, nothing to move out of one")
        return _report_rest(source, result)
    if not result.verified:
        print("the written log files did not verify, database left alone")
        return 1
    print(
        f"{utils.counted(result.messages, 'message')} moved into"
        f" {utils.counted(result.places, 'mailbox/folder place')}"
    )
    if result.snapshots:
        print(
            f"{utils.counted(result.snapshots, 'resume timestamp')} taken from the"
            f" database -- as a record of when, never as a point to carry on from"
        )
    if result.placeless:
        print(
            f"{result.placeless:,} of them recorded without a folder -- the old "
            f"database did not store which folder of which mailbox"
        )
    if result.undecidable:
        print(
            f"{utils.counted(result.undecidable, 'folder name')} could fit more than "
            f"one mailbox, left out rather than guessed"
        )
    if result.renamed_to is not None:
        print(f"the database is now {result.renamed_to.name} and is no longer used")
        print("delete it once you are satisfied with the archive")
    return _report_rest(source, result)


def _report_rest(source: pathlib.Path, result: jobs.MigrationResult) -> int:
    """The steps after the two older formats gave up what they held."""
    print(f"{utils.counted(result.shards_moved, 'shard')} moved into {cas.MAIL_DIR}/")
    if result.consolidated is not None:
        report_compact(result.consolidated)
    return _report_generation(source)


def _report_generation(source: pathlib.Path) -> int:
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


def report_compact(result: metalog.CompactResult) -> int:
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
        f"{utils.counted(result.files_before, 'log file')} -> {result.files_after:,} "
        f"across {utils.counted(result.places, 'place')}"
    )
    dropped = result.entries_before - result.entries_after
    if dropped:
        print(f"{utils.counted(dropped, 'duplicate observation')} dropped")
    if result.transient_removed:
        # Said out loud rather than swept up quietly: each one is a write that
        # was interrupted, and that is worth knowing about.
        leftovers = utils.counted(result.transient_removed, "leftover")
        print(f"{leftovers} of an interrupted write removed")
    return 0


def report_import(
    source: pathlib.Path,
    destination: pathlib.Path,
    result: jobs.ImportResult,
) -> int:
    """Say what the import did, or what it would have done.

    Both counts are named even when one of them is zero. "How many were already
    there" is what tells a dry run apart from a disaster: a source that has been
    through a converter on its way here looks exactly like a source full of new
    mail, and the difference only shows in the ratio.

    The name is said back for the same reason it is asked for: it is the only
    handle these messages have afterwards, and a line that names it also names
    where to type it. Where fewer were recorded than read, that is the finding
    and it goes first -- the mail is in the archive either way, and it is what
    the archive knows about it that fell short.
    """
    total = result.stored + result.present
    verb = "would be imported" if result.dry_run else "imported"
    print(
        f"{utils.counted(total, 'message')} read -- {result.stored:,} {verb},"
        f" {result.present:,} already in {destination}"
    )
    # An import with no name records nothing and falls short of nothing.
    unrecorded = total - result.recorded if result.name is not None else 0
    if total and result.name is not None:
        if result.dry_run:
            print(f"they would be recorded as {result.name}")
        elif unrecorded:
            print(
                f"{unrecorded:,} of them are in the archive with nothing recording"
                f" where they came from -- the metadata log could not be written."
                f" Import the same source again under {result.name} to record them"
            )
        else:
            print(
                f"recorded as {result.name} -- `mailvault db update` takes it in,"
                f" then `mailvault db search --folder {result.name}` finds them"
            )
    report_items(
        [utils.under(source, p) for p in result.failed], "message", "could not be read"
    )
    # A different outcome from the failures above: this mail is in the archive
    # and recorded, and what is left over is the source file --move was asked to
    # take away. Importing the same source again is harmless and removes them.
    report_items(
        [utils.under(source, p) for p in result.undeleted],
        "source file",
        "could not be deleted, the mail is archived --"
        " import the same source again to be rid of them",
    )
    shortfall = result.failed or result.undeleted
    return 1 if shortfall or (unrecorded and not result.dry_run) else 0


def report_places(summary: metalog.LogSummary) -> int:
    """List what the archive has mail from, one line per place.

    Two name columns rather than the `mailbox::folder` the findings print,
    because these two are what the reader types into `db search`, and because an
    empty mailbox column says by itself what kind of place that is -- an import
    or what `archive adopt` took in, neither of which has a mailbox behind it.

    The total is not the column added up, and where they differ the difference is
    named: a message under three Gmail labels lies at three places and is one
    message. Two numbers that do not add up and no word about it is how a report
    sends a reader looking for a fault that is not there.
    """
    if not summary.places:
        print("no place recorded yet -- a backup records one per folder,")
        print("`archive import` and `archive adopt` one per name they are given")
        return 0

    mailboxes = [shorten(place.mailbox, MAILBOX_WIDTH) for place in summary.places]
    folders = [shorten(place.folder, FOLDER_WIDTH) for place in summary.places]
    counts = [f"{place.messages:,}" for place in summary.places]
    first = max(len("mailbox"), *(len(name) for name in mailboxes))
    second = max(len("folder"), *(len(name) for name in folders))
    third = max(len("messages"), *(len(count) for count in counts))

    print(f"{'mailbox':<{first}}  {'folder':<{second}}  {'messages':>{third}}  last seen")
    for place, mailbox, folder, count in zip(summary.places, mailboxes, folders, counts):
        day = (place.last_seen or "")[:10] or "?"
        print(f"{mailbox:<{first}}  {folder:<{second}}  {count:>{third}}  {day}")

    print(
        f"{utils.counted(len(summary.places), 'place')},"
        f" {utils.counted(summary.messages, 'message')}"
    )
    if sum(place.messages for place in summary.places) != summary.messages:
        print("  the column adds up to more: a message can be in several places")
    return 0


def report_adopt(result: jobs.AdoptResult) -> int:
    """Say what was taken into the archive, or what would have been.

    The count is the whole finding, and it is what the reader has to weigh: it
    is how many messages this run speaks for. So it comes first, the name is in
    the same sentence, and the dry run says in as many words that the log is not
    corrected afterwards -- that sentence has to arrive while the run can still
    be called off, which is the only moment it is worth anything.

    A name that is already a place is said differently in the two modes, and on
    purpose. Before the run it is a **choice**: these would go in with what is
    already there, and a different name is one keystroke away. Afterwards it can
    only be a **fact**, and it is written as one -- what the place holds now. A
    warning after the writing would be the kind of line that names a state and no
    move, and there is no move left to name. What it is still good for is the
    mistyped name: three messages adopted into a place that now holds five
    thousand says at a glance that this was not the name that was meant.

    Nothing was found is a good outcome and reads like one. It is also the
    outcome that says the archive is whole in itself, which is worth more than
    "0 adopted" would be.
    """
    if not result.found:
        print("every message in the archive has a place, nothing to take in")
        return 0

    if result.dry_run:
        print(
            f"{utils.counted(result.found, 'message belongs', 'messages belong')} to no"
            f" place and would be recorded as {result.name}"
        )
        if result.held:
            print(
                f"  {result.name} is already a place and holds"
                f" {utils.counted(result.held, 'message')}; these would go in with them"
            )
        print("  nothing corrects the log afterwards, so the name has to be right")
        print("  nothing was written; leave out --dry-run to record them")
        return 0

    unrecorded = result.found - result.recorded
    if unrecorded:
        print(
            f"{unrecorded:,} of {utils.counted(result.found, 'message')} stayed"
            f" unrecorded -- the metadata log could not be written. Nothing else"
            f" changed, so the same command records them once the log can be written"
        )
        return 1
    held_now = utils.counted(result.held + result.recorded, "message")
    place = f"{result.name}, which now holds {held_now}" if result.held else result.name
    found = utils.counted(result.found, "message belongs", "messages belong")
    print(f"{found} to no place, recorded as {place}")
    print(
        f"  `mailvault db update` takes it in, then `mailvault db search"
        f" --folder {result.name}` finds them"
    )
    return 0


def _report_orphans(store_ids: list[str]) -> None:
    """Say what a message with no place recorded is, and what follows from it.

    These are archived, intact and readable; the one thing missing is the note
    where they came from, and it is missing because it never was in the archive,
    not because something lost it. Saying so is the finding -- a reader who is
    told only "110 not referenced" goes looking for a repair, and what they need
    to know first is that there is nothing to repair.

    Two moves, in the order they are worth having, and the better one second
    because it comes with a condition. `archive adopt` always works and records
    what the person running it says; importing the same directory again works
    only where that directory still exists, and is better exactly there, because
    what it records cannot be wrong. Naming it the other way round would offer a
    move that leads nowhere for most readers, which costs more than it is worth.

    This one prints no list. A store id is the right handle for `get`
    and useless to a person deciding whether their archive is all right, and
    twenty of a hundred and ten is neither a list to work from nor short enough
    to skim. They go to the debug log, whole.
    """
    if not store_ids:
        return
    print(
        f"{utils.counted(len(store_ids), 'message belongs', 'messages belong')} to no"
        " known place -- stored and intact, but nothing records where that mail"
        " came from"
    )
    print(
        "  they are found like any other message: `db create` builds a"
        " query database with sender, subject and date"
    )
    print("  `archive adopt --name NAME` takes them into a place you name")
    print(
        "  where an import read them from a directory that is still there,"
        " importing it again under a --name is better: it records only what"
        " really lay in it"
    )
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
        """The message ids of entries -- what `get` and the log take."""
        return [store.hashval_of(path) or str(path) for path in paths]

    print(
        f"{utils.counted(result.entries, 'message')} stored, {result.referenced:,} of"
        f" them accounted for by {utils.counted(result.log_files, 'log file')}"
        f" in {utils.counted(result.places, 'place')}"
    )
    report_items(
        [f"{store_id}  {where}" for store_id, where in result.missing.items()],
        "message",
        "referenced in the log and missing from the archive",
    )
    report_items(
        [utils.under(source, path) for path in result.damaged_logs],
        "log file",
        "damaged -- the content does not match its checksum",
    )
    report_items(
        result.broken_chains,
        "log file",
        "named by the chain and gone -- nothing records what was written there",
    )
    report_items(
        result.unreadable_chains,
        "log file",
        "named by the chain and written by a newer mailvault -- upgrade to read it,"
        " the archive is not missing anything",
    )
    report_items(
        [utils.under(source, path) for path in result.unchained],
        "log file",
        "no chain reaches -- still read, the chain is behind",
    )
    report_items(
        ids(result.corrupt),
        "message",
        "damaged -- the content does not match its checksum",
    )
    report_items(ids(result.unreadable), "message", "could not be read")
    report_items(
        [utils.under(source, path) for path in result.foreign],
        "file in the archive that is not a message",
        plural="files in the archive that are not messages",
    )
    _report_orphans(ids(result.orphans))
    if result.quarantined_before:
        aside = utils.counted(result.quarantined_before, "message")
        print(f"{aside} set aside by an earlier run")
    if result.transient_removed:
        leftovers = utils.counted(result.transient_removed, "leftover")
        print(f"{leftovers} of an interrupted write removed")
    if result.quarantined:
        print(
            f"{utils.counted(len(result.quarantined), 'damaged message')} set aside --"
            " counted as missing now, fetch again with `verify --repair` or"
            " `backup --full`"
        )
    if not result.sound:
        print(f"NOT sound -- {utils.counted(result.findings, 'finding')} above")
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
    print(f"{utils.counted(result.converted, 'file')} {done}, {result.skipped:,} {already}")
    for path in result.failed:
        print(f"{utils.under(source, path)}: could not be converted, left as it is")
    if not result.failed:
        return 0
    print(f"{utils.counted(len(result.failed), 'file')} failed, see the log for the reason")
    return 1


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


def _provenance(archive: pathlib.Path, name: str) -> jobs.Provenance:
    """What the import records itself as, and where it writes it down.

    A dry run is handed one too and writes nothing with it, so that the run which
    reports what would happen is the same run in every other respect.
    """
    jobs.check_place_name(name)
    return jobs.Provenance(
        name=name,
        log=metalog.LogWriter(
            archive / metalog.DEFAULT_LOG_DIR, archive / heads.DEFAULT_HEADS_DIR
        ),
    )


def run(args: argparse.Namespace) -> int:
    """Run an `archive` subcommand (stats/import/compress/check/...).

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
        # `git init [<directory>]`, and the same default: where you are standing.
        target = args.directory if args.directory is not None else archive
        return report_init(target, jobs.init_archive(target, DEFAULT_CONFIG_NAME))
    elif cmd == "stats":
        count, size = _external(archive, args.docuware).stats()
        print(f"{count:,} emails, {_human_size(size)} total")
    elif cmd == "import":
        _refuse_importing_the_archive(archive, args.source)
        source = _external(args.source, args.docuware)
        destination = cas.mail_store(archive, compress=args.compress)
        return report_import(
            args.source,
            archive,
            source.archive_to_cas(
                destination,
                provenance=_provenance(archive, args.name),
                move=args.move,
                dry_run=args.dry_run,
            ),
        )
    elif cmd == "places":
        return report_places(metalog.summarize(archive / metalog.DEFAULT_LOG_DIR))
    elif cmd == "adopt":
        return report_adopt(jobs.adopt(archive, args.name, dry_run=args.dry_run))
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
    elif cmd == "migrate":
        return report_migration(archive, jobs.migrate_archive(archive))
    elif cmd == "compact":
        return report_compact(
            metalog.compact(
                archive / metalog.DEFAULT_LOG_DIR,
                archive / heads.DEFAULT_HEADS_DIR,
            )
        )
    elif cmd == "check":
        return report_check(
            archive,
            jobs.check(
                archive, contents=not args.no_integrity_check, quarantine=args.quarantine
            ),
        )

    return 0
