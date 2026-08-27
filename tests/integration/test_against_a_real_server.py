"""What a real IMAP server does that a mock of one cannot be asked to.

Every test here backs up a mailbox of its own on the shared container, so they
neither collide nor need cleaning up between runs.

The scenarios were chosen by where the mocks are blind rather than by coverage:
UIDs and UIDVALIDITY, which is where mail goes missing silently; folder names,
which only exist as modified UTF-7 on the wire; and the whole backup -> check ->
repair cycle, which every unit test has only ever seen one piece of.
"""

from __future__ import annotations

import pathlib

import pytest

from mailvault import jobs
from mailvault.store import cas, heads, marker, metalog
from tests.integration import corpus

pytestmark = pytest.mark.integration


def _archive(tmp_path: pathlib.Path) -> pathlib.Path:
    marker.write(tmp_path)
    return tmp_path


def _fill(server, user: str, folder: str, messages: list[bytes]) -> None:
    """Put messages into a folder, creating it if the server has none."""
    client = server.client(user)
    try:
        if folder != "INBOX" and not client.folder_exists(folder):
            client.create_folder(folder)
        for message in messages:
            client.append(folder, message)
    finally:
        client.logout()


def _places(log_root: pathlib.Path) -> dict[tuple[str | None, str | None], int]:
    """Every place the log records, and how many messages it names in each."""
    found: dict[tuple[str | None, str | None], int] = {}
    for logfile in metalog.read_all(log_root):
        key = (logfile.mailbox, logfile.folder)
        found[key] = found.get(key, 0) + len(logfile.store_ids)
    return found


class TestABackupOverTheWire:
    def test_a_folder_is_archived_and_the_archive_is_sound(self, dovecot, tmp_path):
        """The whole cycle against a server that answers for itself.

        Every piece of this is covered by a unit test against a mock. What is not
        is that they fit together on top of a real SEARCH, a real FETCH and the
        UIDs a server hands out.
        """
        user = "backup"
        messages = corpus.messages()
        _fill(dovecot, user, "INBOX", messages)
        archive = _archive(tmp_path)

        report = jobs.backup(dovecot.job(user, ["INBOX"]), archive, incremental=False)

        assert report.stored == len(messages)
        assert report.failed == 0
        result = jobs.check(archive)
        assert result.sound, "a fresh archive of a real mailbox must not be a finding"
        assert result.entries == len(messages)
        assert result.orphans == [], "everything stored is named by the log"
        assert _places(archive / metalog.DEFAULT_LOG_DIR) == {(user, "INBOX"): len(messages)}

    def test_it_works_over_tls_with_a_certificate_nobody_signed(self, dovecot, tmp_path):
        """The branch of `connect` that builds its own SSL context, actually run.

        `tls_verify_cert = false` exists for exactly this: a server whose
        certificate is its own. Until now the only thing that had ever exercised
        the context it builds was a MagicMock, which accepts anything.
        """
        user = "tlsuser"
        _fill(dovecot, user, "INBOX", corpus.PLAIN)
        archive = _archive(tmp_path)

        report = jobs.backup(dovecot.tls_job(user, ["INBOX"]), archive, incremental=False)

        assert report.stored == len(corpus.PLAIN)
        assert jobs.check(archive).sound

    def test_a_folder_name_reaches_the_archive_as_it_reads(self, dovecot, tmp_path):
        """On the wire it is `Pers&APY-nlich`; in the log it has to be `Persönlich`.

        A folder name only exists as modified UTF-7 between the two ends, and
        nothing in mailvault does that encoding -- imapclient does, on the way
        out and on the way back. That the two agree has been assumed all along
        and never once observed.
        """
        user = "umlaut"
        folder = corpus.UMLAUT_FOLDER
        _fill(dovecot, user, folder, corpus.PLAIN)
        archive = _archive(tmp_path)

        jobs.backup(dovecot.job(user, [folder]), archive, incremental=False)

        recorded = _places(archive / metalog.DEFAULT_LOG_DIR)
        assert recorded == {(user, folder): len(corpus.PLAIN)}
        place = next(iter(recorded))
        assert place[1] is not None and "ö" in place[1], "decoded, not the wire form"
        assert jobs.check(archive).sound

    def test_the_same_message_in_two_folders_is_one_entry_and_two_places(
        self, dovecot, tmp_path
    ):
        """The archive's central promise, put to a server that assigns its own UIDs.

        The two copies are byte-identical and get different UIDs in different
        folders, so nothing but the content hash can tell that they are one
        message.
        """
        user = "twoplaces"
        _fill(dovecot, user, "INBOX", [corpus.IN_TWO_PLACES])
        _fill(dovecot, user, "Archive", [corpus.IN_TWO_PLACES])
        archive = _archive(tmp_path)

        jobs.backup(dovecot.job(user, ["INBOX", "Archive"]), archive, incremental=False)

        result = jobs.check(archive)
        assert result.sound
        assert result.entries == 1, "one message, stored once"
        assert _places(archive / metalog.DEFAULT_LOG_DIR) == {
            (user, "INBOX"): 1,
            (user, "Archive"): 1,
        }


class TestCarryingOnWhereTheLastRunStopped:
    """Where mail goes missing without anyone noticing, so it is worth a server.

    The resume point is a UID, and a UID is only a UID because imapclient is in
    UID mode. Sequence numbers would look identical in every unit test and shift
    under the first deletion.
    """

    def test_a_second_run_reads_only_what_arrived_since(self, dovecot, tmp_path):
        user = "resume"
        _fill(dovecot, user, "INBOX", corpus.PLAIN)
        archive = _archive(tmp_path)
        job = dovecot.job(user, ["INBOX"])

        first = jobs.backup(job, archive, incremental=True)
        assert first.stored == len(corpus.PLAIN)

        _fill(dovecot, user, "INBOX", corpus.AWKWARD)
        second = jobs.backup(job, archive, incremental=True)

        assert second.stored == len(corpus.AWKWARD), "only the new ones"
        assert second.present == 0, "and none of them was already here"
        assert jobs.check(archive).entries == len(corpus.PLAIN) + len(corpus.AWKWARD)
        assert jobs.check(archive).sound

    def test_a_run_with_nothing_new_fetches_nothing(self, dovecot, tmp_path):
        """The ordinary nightly case, and the one a wrong resume point ruins
        quietly -- by re-reading everything, or by reading nothing ever again."""
        user = "nothingnew"
        _fill(dovecot, user, "INBOX", corpus.PLAIN)
        archive = _archive(tmp_path)
        job = dovecot.job(user, ["INBOX"])

        jobs.backup(job, archive, incremental=True)
        again = jobs.backup(job, archive, incremental=True)

        assert again.stored == 0
        assert again.failed == 0
        assert jobs.check(archive).entries == len(corpus.PLAIN)

    def test_a_deletion_on_the_server_does_not_take_the_next_run_with_it(
        self, dovecot, tmp_path
    ):
        """The one scenario that tells a UID from a sequence number.

        A sequence number is a position in the folder and shifts the moment
        anything before it is expunged; a UID never moves. For a folder that has
        only ever grown the two are the same number, which is why every test that
        does not delete passes either way -- the mocked ones, and the rest of the
        ones here.

        Measured against this server, with the connection put into sequence-number
        mode: the two messages appended after the deletion are stored by neither
        this run nor any later one. Five messages archived out of seven, no error
        anywhere, and the archive stays that way.
        """
        user = "deletion"
        archive = _archive(tmp_path)
        job = dovecot.job(user, ["INBOX"])
        _fill(dovecot, user, "INBOX", corpus.PLAIN)
        extra = [corpus.AWKWARD[0], corpus.AWKWARD[1]]
        _fill(dovecot, user, "INBOX", extra)

        first = jobs.backup(job, archive, incremental=True)
        assert first.stored == len(corpus.PLAIN) + len(extra)

        client = dovecot.client(user)
        try:
            client.select_folder("INBOX")
            oldest = client.search(["NOT", "DELETED"])[:2]
            client.delete_messages(oldest)
            client.expunge()
        finally:
            client.logout()
        # Whatever arrives now sits at a sequence number that is already taken by
        # a message the last run had seen.
        _fill(dovecot, user, "INBOX", [corpus.AWKWARD[2], corpus.IN_TWO_PLACES])

        second = jobs.backup(job, archive, incremental=True)

        assert second.stored == 2, "what arrived after the deletion, and nothing less"
        result = jobs.check(archive)
        assert result.entries == len(corpus.PLAIN) + len(extra) + 2
        assert result.sound

    def test_a_folder_rebuilt_under_a_new_uidvalidity_is_read_in_full(self, dovecot, tmp_path):
        """A UID means nothing once the server says its UID space is a new one.

        Deleting the folder and making it again is how a server really does this,
        and it is the case the remembered UID must be thrown away for: kept, it
        would sit above everything in the rebuilt folder and the run would
        archive nothing, for good, without an error anywhere.
        """
        user = "uidvalidity"
        archive = _archive(tmp_path)
        job = dovecot.job(user, ["Rebuilt"])
        _fill(dovecot, user, "Rebuilt", corpus.PLAIN)

        first = jobs.backup(job, archive, incremental=True)
        assert first.stored == len(corpus.PLAIN)

        client = dovecot.client(user)
        try:
            before = client.select_folder("Rebuilt", readonly=True)[b"UIDVALIDITY"]
            client.close_folder()
            client.delete_folder("Rebuilt")
            client.create_folder("Rebuilt")
            for message in corpus.AWKWARD:
                client.append("Rebuilt", message)
            after = client.select_folder("Rebuilt", readonly=True)[b"UIDVALIDITY"]
        finally:
            client.logout()
        assert before != after, "the server has to have moved its UID space"

        second = jobs.backup(job, archive, incremental=True)

        assert second.stored == len(corpus.AWKWARD), "the whole folder, read again"
        assert jobs.check(archive).sound


class TestPuttingBackWhatWasLost:
    def test_repair_restores_the_places_the_log_no_longer_holds(self, dovecot, tmp_path):
        """`verify --repair` against a server, which is the only way to see it work.

        The mail stays and the log goes, so every message on the server is one
        the archive does not account for -- and the repair has to fetch each of
        them and write down where it belongs.
        """
        user = "repair"
        messages = corpus.messages()
        _fill(dovecot, user, "INBOX", messages)
        archive = _archive(tmp_path)
        job = dovecot.job(user, ["INBOX"])
        jobs.backup(job, archive, incremental=False)

        for directory in (metalog.DEFAULT_LOG_DIR, heads.DEFAULT_HEADS_DIR):
            for path in (archive / directory).rglob("*"):
                if path.is_file():
                    path.unlink()
        assert jobs.check(archive).orphans, "every message is an orphan now"

        results = jobs.verify(job, archive, repair=True)

        assert results[0].restored == len(messages)
        assert results[0].failed == 0
        result = jobs.check(archive)
        assert result.sound
        assert result.orphans == [], "the log names them again"
        assert _places(archive / metalog.DEFAULT_LOG_DIR) == {(user, "INBOX"): len(messages)}

    def test_a_message_only_the_server_has_is_fetched_and_the_rest_left_alone(
        self, dovecot, tmp_path
    ):
        """A gap in the middle, which is what a repair is really for."""
        user = "gap"
        _fill(dovecot, user, "INBOX", corpus.PLAIN)
        archive = _archive(tmp_path)
        job = dovecot.job(user, ["INBOX"])
        jobs.backup(job, archive, incremental=False)

        # One more on the server, and an archive that has never heard of it.
        _fill(dovecot, user, "INBOX", [corpus.IN_TWO_PLACES])

        results = jobs.verify(job, archive, repair=True)

        assert results[0].missing == 1
        assert results[0].restored == 1
        assert jobs.check(archive).entries == len(corpus.PLAIN) + 1
        assert jobs.check(archive).sound


class TestWhatTheArchiveMadeOfIt:
    def test_the_awkward_headers_survive_the_round_trip(self, dovecot, tmp_path):
        """From the wire into the query database, which is where they are read.

        Each of these has been through the parser in a unit test. What is new is
        that the bytes came off a real server first -- and that the answers are
        held against the ids, which are stable because the corpus is.
        """
        pytest.importorskip("sqlite3")
        user = "headers"
        _fill(dovecot, user, "INBOX", corpus.AWKWARD)
        archive = _archive(tmp_path)
        jobs.backup(dovecot.job(user, ["INBOX"]), archive, incremental=False)
        db_path = archive / "index.db"

        jobs.create_db(archive, db_path)

        hits = jobs.search(db_path, jobs.SearchQuery(subject="Grüße"))
        assert [hit.subject for hit in hits] == ["Grüße"], "decoded out of RFC 2047"

        undated = jobs.search(db_path, jobs.SearchQuery(subject="Unreadable date"))
        assert undated and undated[0].date is None, "unknown, not invented"

        no_zone = jobs.search(db_path, jobs.SearchQuery(subject="No zone"))
        assert no_zone and no_zone[0].date is not None

        assert len(jobs.search(db_path, jobs.SearchQuery())) == len(corpus.AWKWARD)

    def test_every_stored_entry_matches_the_bytes_the_server_sent(self, dovecot, tmp_path):
        """The store is content-addressed, so this is the whole guarantee.

        A message that changed on the way in -- a line ending rewritten, a header
        refolded -- would still be stored and still pass `check`, because the
        name would be the hash of whatever arrived. Only holding it against what
        the server has catches that.
        """
        user = "bytes"
        _fill(dovecot, user, "INBOX", corpus.PLAIN)
        archive = _archive(tmp_path)
        jobs.backup(dovecot.job(user, ["INBOX"]), archive, incremental=False)

        client = dovecot.client(user)
        try:
            client.select_folder("INBOX", readonly=True)
            uids = client.search(["NOT", "DELETED"])
            on_server = {
                data[b"BODY[]"] for data in client.fetch(uids, ["BODY.PEEK[]"]).values()
            }
        finally:
            client.logout()

        store = cas.mail_store(archive)
        stored = {path.read_bytes() for path in (archive / cas.MAIL_DIR).rglob("*.eml")}

        assert stored == on_server, "what the archive holds is what the server sent"
        assert all(store.verify(path) for path in (archive / cas.MAIL_DIR).rglob("*.eml"))
