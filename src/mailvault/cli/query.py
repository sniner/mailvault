"""The `db` group -- the archive's optional, throwaway query database.

Building it, keeping it in step, searching it, dropping it. Every command here
may say "that does not fit, build it again", which is exactly what no command
touching the archive itself may ever say.
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import json
import logging
import pathlib
import sys

from mailvault import jobs, utils
from mailvault.cli.common import (
    DEFAULT_DB_NAME,
    archive_path,
    require_archive,
    shorten,
)

log = logging.getLogger(__name__)


# How much of a message id a table shows. The full value is 96 characters and
# would be three lines of terminal for a column nobody reads across. It is
# printed bare, with no ellipsis after it: this much of an id is what `get`
# takes, and a mark saying "shortened" is a character that gets selected
# along with the id and handed on to whatever it was pasted into. Every
# machine-readable format prints the id whole, because a pipeline should not have
# to be lucky.
ID_PREVIEW = 12

# How much of a subject a table shows before it costs the line its shape.
SUBJECT_WIDTH = 60


def report_create_db(target: pathlib.Path, result: jobs.RebuildResult) -> int:
    """Say what went into the database, and name what could not.

    The first line says where its number comes from, and that is not decoration.
    It used to be the count of a walk over the whole store and is now the count
    of what the log accounts for -- the same command, the same wording, and on an
    archive with mail nothing records a place for, a smaller number. Somebody
    comparing two runs across that change has to be able to see why.

    The dateless messages are named here and nowhere else. `db update` builds on
    the same database every night and would repeat the same number until
    somebody stopped reading it -- and it is not a finding that changes: those
    messages carry what they carry, and the archive holds them whole either way.
    It is worth saying once, to whoever asked for the database to be built, and
    it says what it costs them rather than what went wrong reading a header.
    """
    replay = result.replay
    if replay.files:
        print(
            f"{utils.counted(result.messages, 'message')} named by"
            f" {utils.counted(replay.files, 'log file')},"
            f" {replay.applied:,} of {utils.counted(replay.entries, 'location')} applied"
        )
        if replay.unknown:
            print(
                f"{utils.counted(replay.unknown, 'log entry', 'log entries')} about "
                f"mail that is not in the archive, ignored"
            )
        if result.undated:
            carry = utils.counted(result.undated, "message carries", "messages carry")
            print(
                f"{carry} no date that could be read --"
                f" `db search --since/--until` will not find them"
            )
    else:
        # An empty database, and the reason has a move: an archive built entirely
        # from imports made before an import recorded what it brought in has
        # nothing here, and everything in it is waiting to be taken in.
        print("no metadata log -- nothing names a message, so the database is empty")
        print("  `archive check` says what lies here, `archive adopt` gives it a place")
    print(f"{target.name}: written")
    return 0


def report_update_db(target: pathlib.Path, result: jobs.RefreshResult) -> int:
    """Say what the update took in, and stay quiet when there was nothing.

    Non-zero for a database this version cannot read: it was not updated, and a
    caller that only reads the exit code must not file that as a success.
    """
    if result.unreadable:
        # `refresh_db` has already said what it is and what to do about it.
        return 1
    if result.rebuilt:
        messages = utils.counted(result.messages, "message")
        print(f"{target.name}: built from the whole archive, {messages}")
        return 0
    if not result.files:
        print(f"{target.name}: already up to date")
        return 0
    # Both numbers, because they come apart and the difference is the whole
    # answer. A log file about mail the database already had -- what `archive
    # adopt` writes, or a folder read in full a second time -- records locations
    # and adds no message, and "0 messages added" on its own reads like a run
    # that did nothing.
    print(
        f"{target.name}: {utils.counted(result.files, 'log file')} taken in,"
        f" {utils.counted(result.applied, 'location')} recorded,"
        f" {utils.counted(result.messages, 'message')} new"
    )
    if result.unknown:
        print(
            f"{utils.counted(result.unknown, 'log entry', 'log entries')} about "
            f"mail that is not in the archive, ignored"
        )
    return 0


def report_search(hits: list[jobs.SearchHit], query: jobs.SearchQuery) -> int:
    """Print what was found, in the shape a person reads.

    The count at the end is not decoration: a search that filtered nothing out
    prints the whole archive, and a reader who has scrolled past two hundred
    lines has lost track of whether that was all of them.
    """
    for hit in hits:
        day = (hit.date or "")[:10] or "??????????"
        print(
            f"{day}  {hit.store_id[:ID_PREVIEW]}  "
            f"{shorten(hit.sender, 32):32}  {shorten(hit.subject, SUBJECT_WIDTH)}"
        )
    if not hits:
        print("no message matches" if not query.is_empty() else "the database is empty")
        return 0
    print(utils.counted(len(hits), "message"))
    if query.limit is not None and len(hits) == query.limit:
        # Said out loud, because a limit that happens to be reached looks exactly
        # like a search that found that many.
        print(f"stopped at --limit {query.limit:,}; there may be more")
    return 0


def report_search_csv(hits: list[jobs.SearchHit]) -> int:
    writer = csv.writer(sys.stdout)
    writer.writerow(["store_id", "date", "sender", "subject", "places"])
    for hit in hits:
        writer.writerow(
            [
                hit.store_id,
                hit.date or "",
                hit.sender or "",
                hit.subject or "",
                " ".join(hit.places),
            ]
        )
    return 0


def report_search_json(hits: list[jobs.SearchHit]) -> int:
    json.dump(
        [dataclasses.asdict(hit) for hit in hits],
        sys.stdout,
        ensure_ascii=False,
        indent=2,
    )
    print()
    return 0


def _search_query(args: argparse.Namespace) -> jobs.SearchQuery:
    return jobs.SearchQuery(
        sender=args.sender,
        recipient=args.recipient,
        subject=args.subject,
        mailbox=args.mailbox,
        folder=args.folder,
        since=args.since,
        until=args.until,
        limit=args.limit,
    )


def run(args: argparse.Namespace) -> int:
    """Run a `db` subcommand against the archive's query database.

    Every one of them works on the `index.db` of the archive `--archive` names,
    or of the one being stood in. There is no second database and no way to name
    one: it is a feature of an archive, kept where the mail it describes is.
    """
    archive = archive_path(args)
    require_archive(archive)
    db_path = archive / DEFAULT_DB_NAME
    cmd = args.db_command

    if cmd == "create":
        if db_path.exists() and not args.force:
            raise jobs.JobError(
                f"{db_path.name}: already here. `db update` brings it up to date"
                f" for a fraction of what building it again costs; --force builds"
                f" it again anyway"
            )
        return report_create_db(
            db_path,
            jobs.create_db(archive, db_path, force=True, temp_dir=args.temp_dir),
        )
    elif cmd == "update":
        return report_update_db(db_path, jobs.refresh_db(archive, db_path))
    elif cmd == "drop":
        if jobs.drop_db(db_path):
            print(f"{db_path.name}: deleted -- `db create` builds it again")
        else:
            print(f"{db_path.name}: not here, nothing to delete")
        return 0
    elif cmd == "search":
        if not db_path.exists():
            raise jobs.JobError(
                f"{db_path.name}: no query database in this archive -- build one"
                f" with `mailvault db create`"
            )
        # Asked before the results are printed, so it is not scrolled away by
        # them: what follows is true and may be incomplete.
        #
        # It goes out with the answer it qualifies. `db search > hits` used to
        # keep the hits and leave the sentence saying they are not all of them
        # on the terminal, so the file claimed a completeness it did not have.
        # A machine format cannot carry it without an envelope around the data,
        # and there is none yet -- so there it stays in the log, and that is the
        # first thing a machine-readable result owes its consumer.
        state = jobs.freshness(archive, db_path)
        complaint = state.complaint(db_path.name)
        if complaint and not state.is_usable:
            raise jobs.JobError(complaint)
        if complaint:
            if args.ids or args.csv or args.json:
                log.warning("%s", complaint)
            else:
                print(complaint)
        query = _search_query(args)
        hits = jobs.search(db_path, query)
        if args.ids:
            for hit in hits:
                print(hit.store_id)
            return 0
        if args.csv:
            return report_search_csv(hits)
        if args.json:
            return report_search_json(hits)
        return report_search(hits, query)

    return 0
