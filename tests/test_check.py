"""Tests for `archive check`: what it finds, what it leaves alone, what it moves."""

from __future__ import annotations

import os
import pathlib
import time
from datetime import UTC, datetime

import pytest

from mailvault.cli.archive import report_check
from mailvault.jobs.check import check, quarantine_entry
from mailvault.jobs.common import JobError
from mailvault.store import cas, heads, metalog
from tests.tamper import tamper

WHEN = datetime(2026, 8, 1, 18, 2, 21, tzinfo=UTC)


def _archive(root: pathlib.Path, messages: int = 3, logged: int | None = None):
    """Build an archive with `messages` entries, `logged` of them in the log."""
    store = cas.mail_store(root)
    ids = [
        store.add(f"Message-Id: <{n}@example.com>\r\n\r\nbody {n}\r\n".encode())[1]
        for n in range(messages)
    ]
    writer = metalog.LogWriter(root / metalog.DEFAULT_LOG_DIR, root / heads.DEFAULT_HEADS_DIR)
    for store_id in ids[: messages if logged is None else logged]:
        writer.add("job", ["INBOX"], store_id)
    writer.seal(WHEN)
    return store, ids


def _long_ago() -> tuple[int, float]:
    return (0, time.time() - cas.TRANSIENT_MIN_AGE - 60)


class TestASoundArchive:
    def test_finds_nothing_to_report(self, tmp_path):
        _archive(tmp_path)

        result = check(tmp_path)

        assert result.sound
        assert result.entries == 3
        assert result.referenced == 3
        assert result.places == 1
        assert result.log_files == 1
        assert not result.missing
        assert not result.foreign
        assert not result.orphans

    def test_a_message_in_several_folders_is_counted_once(self, tmp_path):
        """The Gmail case in small: one message, three places, still one message.

        The log holds three entries for it, and the result still counts them --
        what changed is that no report prints that number, because it is neither
        files nor messages nor folders.
        """
        store = cas.mail_store(tmp_path)
        _status, store_id, _path = store.add(b"Message-Id: <a@example.com>\r\n\r\nbody\r\n")
        writer = metalog.LogWriter(
            tmp_path / metalog.DEFAULT_LOG_DIR, tmp_path / heads.DEFAULT_HEADS_DIR
        )
        writer.add("job", ["INBOX", "Archive", "Sent"], store_id)
        writer.seal(WHEN)

        result = check(tmp_path)

        assert result.sound
        assert result.entries == 1
        assert result.referenced == 1
        assert result.places == 3
        assert result.observations == 3  # counted, never reported

    def test_the_integrity_check_finds_nothing_either(self, tmp_path):
        _archive(tmp_path)

        result = check(tmp_path)

        assert result.sound
        assert result.contents_checked
        assert not result.corrupt

    def test_a_run_that_skipped_the_integrity_check_says_so(self, tmp_path):
        """Otherwise "nothing found" would mean two different things."""
        _archive(tmp_path)

        assert check(tmp_path).contents_checked
        assert not check(tmp_path, contents=False).contents_checked


class TestWhatItFinds:
    def test_an_entry_the_log_names_but_the_archive_lacks(self, tmp_path):
        store, ids = _archive(tmp_path)
        gone = store.locate(ids[1], exists=True)
        assert gone is not None
        gone.unlink()

        result = check(tmp_path)

        assert result.missing == {ids[1]: "job::INBOX"}
        assert not result.sound

    def test_an_entry_whose_content_no_longer_matches_its_name(self, tmp_path):
        store, ids = _archive(tmp_path)
        victim = store.locate(ids[1], exists=True)
        assert victim is not None
        tamper(victim, b"not what the name says")

        assert check(tmp_path, contents=False).sound, "a walk cannot see this, only reading can"

        result = check(tmp_path)
        assert result.corrupt == [victim]
        assert not result.sound

    def test_a_log_file_that_no_longer_matches_its_own_name(self, tmp_path):
        _archive(tmp_path)
        (logfile,) = metalog.log_files(tmp_path / metalog.DEFAULT_LOG_DIR)
        tamper(logfile, logfile.read_bytes() + b'{"store_id":"aa"}\n')

        result = check(tmp_path)

        assert result.damaged_logs == [logfile]
        assert not result.sound

    def test_a_file_in_a_shard_that_is_not_an_entry(self, tmp_path):
        store, ids = _archive(tmp_path)
        entry = store.locate(ids[0], exists=True)
        assert entry is not None
        stray = entry.with_name("notes.eml")
        stray.write_bytes(b"put here by hand")

        result = check(tmp_path)

        assert result.foreign == [stray]
        assert result.sound, "somebody's file is not the archive being wrong"

    def test_a_message_no_log_file_references_is_named(self, tmp_path):
        """What `archive import` leaves behind: it writes no log at all.

        Named rather than counted, because "one message has no provenance" is
        not something anyone can act on without knowing which.
        """
        store, ids = _archive(tmp_path, messages=3, logged=2)

        result = check(tmp_path)

        assert result.orphans == [store.locate(ids[2], exists=True)]
        assert result.sound


class TestWhatItLeavesAlone:
    def test_the_messages_live_under_mail_and_the_walk_stays_there(self, tmp_path):
        """The store has its own directory, so the walk needs no exceptions.

        Everything beside it -- the metadata log, the archive's own files,
        whatever its owner put there -- is simply not under the walk any more,
        where it used to have to be stepped around.
        """
        _archive(tmp_path)
        (tmp_path / "state.json").write_text("{}", encoding="utf-8")
        (tmp_path / "mailvault.toml").write_text("[global]\n", encoding="utf-8")
        (tmp_path / "index.db").write_bytes(b"not the store's business")
        (tmp_path / f"index.db{cas.TEMP_SUFFIX}").write_bytes(b"nor this")

        result = check(tmp_path)

        assert (tmp_path / cas.MAIL_DIR).is_dir()
        assert (tmp_path / metalog.DEFAULT_LOG_DIR).is_dir()
        assert result.foreign == []
        assert result.sound

    def test_a_transient_file_a_writer_may_still_hold(self, tmp_path):
        store, ids = _archive(tmp_path)
        entry = store.locate(ids[0], exists=True)
        assert entry is not None
        fresh = entry.with_name(f"{entry.name}.4711-0{cas.TEMP_SUFFIX}")
        fresh.write_bytes(b"half a message")

        result = check(tmp_path)

        assert fresh.exists(), "a backup running right now is not a finding"
        assert result.foreign == []
        assert result.transient_removed == 0


class TestSweeping:
    def test_an_old_leftover_is_removed_and_counted(self, tmp_path):
        store, ids = _archive(tmp_path)
        entry = store.locate(ids[0], exists=True)
        assert entry is not None
        stale = entry.with_name(f"{entry.name}.4711-0{cas.TEMP_SUFFIX}")
        stale.write_bytes(b"half a message")
        os.utime(stale, _long_ago())

        result = check(tmp_path)

        assert result.transient_removed == 1
        assert not stale.exists()
        assert result.sound, "tidying up is not a fault of the archive"


class TestQuarantine:
    @staticmethod
    def _with_a_damaged_entry(tmp_path):
        store, ids = _archive(tmp_path)
        victim = store.locate(ids[1], exists=True)
        assert victim is not None
        tamper(victim, b"not what the name says")
        return store, ids, victim

    def test_with_names_only_it_is_refused(self, tmp_path):
        _archive(tmp_path)

        with pytest.raises(JobError, match="cannot be combined with --no-integrity-check"):
            check(tmp_path, contents=False, quarantine=True)

    def test_the_entry_loses_its_name_and_keeps_its_bytes(self, tmp_path):
        store, ids, victim = self._with_a_damaged_entry(tmp_path)

        result = check(tmp_path, contents=True, quarantine=True)

        assert result.quarantined == [victim.with_name(victim.name + ".corrupt")]
        assert not victim.exists()
        assert result.quarantined[0].read_bytes() == b"not what the name says"
        assert store.locate(ids[1], exists=True) is None, "the store stops claiming it"

    def test_the_message_counts_as_missing_afterwards(self, tmp_path):
        _store, ids, _victim = self._with_a_damaged_entry(tmp_path)
        check(tmp_path, contents=True, quarantine=True)

        after = check(tmp_path, contents=True)

        assert ids[1] in after.missing
        assert not after.sound, "it has to stay reported until something fetches it"

    def test_a_quarantined_file_is_recognised_next_time(self, tmp_path):
        self._with_a_damaged_entry(tmp_path)
        check(tmp_path, contents=True, quarantine=True)

        after = check(tmp_path)

        assert after.quarantined_before == 1
        assert after.foreign == [], "not a stray file, and not reported as one every run"

    def test_a_second_quarantine_does_not_overwrite_the_first(self, tmp_path):
        _store, _ids, victim = self._with_a_damaged_entry(tmp_path)
        check(tmp_path, contents=True, quarantine=True)
        # The message came back and broke again: same name, same fate. The name
        # is free after the quarantine, so this writes a file rather than
        # changing one -- no protection to lift.
        victim.write_bytes(b"broken a second time")

        result = check(tmp_path, contents=True, quarantine=True)

        assert result.quarantined == [victim.with_name(victim.name + ".corrupt.1")]
        assert victim.with_name(victim.name + ".corrupt").read_bytes() == (
            b"not what the name says"
        )

    def test_a_rename_that_fails_costs_that_one_entry(self, tmp_path, monkeypatch, caplog):
        _store, _ids, victim = self._with_a_damaged_entry(tmp_path)

        def _refuse(*_args, **_kwargs):
            raise OSError("read-only file system")

        monkeypatch.setattr(pathlib.Path, "rename", _refuse)

        result = check(tmp_path, contents=True, quarantine=True)

        assert result.quarantined == []
        assert result.corrupt == [victim]
        assert "could not be quarantined" in caplog.text

    def test_it_gives_up_rather_than_looping(self, tmp_path, caplog):
        """Every name taken means something else is wrong; say so, do not spin."""
        _store, _ids, victim = self._with_a_damaged_entry(tmp_path)
        for suffix in (".corrupt", ".corrupt.1"):
            victim.with_name(victim.name + suffix).write_bytes(b"taken")

        assert quarantine_entry(victim, attempts=2) is None

        assert victim.exists()
        assert "every name is taken" in caplog.text


class TestTheLogChain:
    """A heap of files cannot notice one of them is gone. A chain can."""

    @staticmethod
    def _place(root, store_ids, folder="INBOX"):
        writer = metalog.LogWriter(
            root / metalog.DEFAULT_LOG_DIR, root / heads.DEFAULT_HEADS_DIR
        )
        for store_id in store_ids:
            writer.add("job", [folder], store_id)
        return writer.seal(WHEN)

    def test_an_intact_chain_is_no_finding(self, tmp_path):
        store, ids = _archive(tmp_path)
        self._place(tmp_path, [ids[0]], folder="Sent")

        result = check(tmp_path)

        assert result.broken_chains == []
        assert result.unchained == []
        assert result.sound

    def test_a_log_file_the_chain_names_but_the_archive_lacks(self, tmp_path):
        """The whole point: the messages stay reachable through their other place,
        so nothing turns into an orphan and nothing else would ever say a word."""
        store, ids = _archive(tmp_path)
        (first,) = self._place(tmp_path, [ids[0]], folder="Sent")
        self._place(tmp_path, [ids[1]], folder="Sent")
        first.unlink()

        result = check(tmp_path)

        assert len(result.broken_chains) == 1
        assert first.name.removesuffix(".jsonl") in result.broken_chains[0]
        assert "job::Sent" in result.broken_chains[0]
        assert not result.sound, "a file that is gone is the archive not being what it claims"
        assert result.orphans == [], (
            "and nothing else says a word: the messages are still filed under INBOX,"
            " so losing the record of their second place leaves no other trace"
        )

    def test_a_file_from_a_newer_mailvault_is_there_not_gone(self, tmp_path):
        """An archive ahead of the program looking at it is not a broken archive.

        The chain stops all the same -- the link to the file before it is inside
        the file -- but nothing is missing, so the run must not answer `NOT
        sound` and exit 1 over a file that is present and intact.
        """
        store, ids = _archive(tmp_path)
        (first,) = self._place(tmp_path, [ids[0]], folder="Sent")
        self._place(tmp_path, [ids[1]], folder="Sent")
        # Written by a version this one does not know. The file keeps its name,
        # which is the hash of the content it had -- so it is unreadable *and*
        # damaged, and only the first of those is the point here.
        header = first.read_text(encoding="utf-8")
        written = f'"version": {metalog.LOG_VERSION}'
        assert written in header, "the field this test rewrites"
        tamper(first, header.replace(written, '"version": 99', 1))

        result = check(tmp_path)

        assert result.broken_chains == [], "it is there; 'gone' is the wrong word"
        assert len(result.unreadable_chains) == 1
        assert "job::Sent" in result.unreadable_chains[0]

    def test_a_file_no_chain_reaches_is_reported_but_not_a_fault(self, tmp_path):
        """Left behind when a head could not be updated after the file landed.

        It is still read -- the glob and not the chain enumerates the log -- so
        nothing is lost and nothing is wrong.
        """
        store, ids = _archive(tmp_path)
        self._place(tmp_path, [ids[0]], folder="Sent")
        stale = heads.head_path(tmp_path / heads.DEFAULT_HEADS_DIR, "job", "Sent")
        stale.unlink()

        result = check(tmp_path)

        assert len(result.unchained) == 1
        assert result.broken_chains == []
        assert result.sound, "the chain being behind is not the archive being wrong"

    def test_an_archive_without_heads_is_not_asked_about_chains(self, tmp_path):
        """Every archive is in this state until it is migrated, and saying so
        once per log file would be true and useless."""
        _archive(tmp_path)
        for path in (tmp_path / heads.DEFAULT_HEADS_DIR).iterdir():
            path.unlink()

        result = check(tmp_path)

        assert result.unchained == []
        assert result.broken_chains == []
        assert result.sound

    def test_the_report_names_both_kinds(self, tmp_path, capsys):
        store, ids = _archive(tmp_path)
        (first,) = self._place(tmp_path, [ids[0]], folder="Sent")
        self._place(tmp_path, [ids[1]], folder="Sent")
        first.unlink()

        report_check(tmp_path, check(tmp_path))

        out = capsys.readouterr().out
        assert "named by the chain and gone" in out
