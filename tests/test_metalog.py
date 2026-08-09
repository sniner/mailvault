"""Tests for mailvault.store.metalog (the append-only location log)."""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import UTC, datetime

from mailvault.store import cas, heads, metalog

WHEN = datetime(2026, 8, 1, 18, 2, 21, tzinfo=UTC)
STORE_ID = "df3823f1cd1638d0f374745bb0e200e3"


def _heads(log_root):
    """Where the chain heads live for a log root of `<archive>/meta`."""
    return log_root.parent / heads.DEFAULT_HEADS_DIR


def _write(root, mailbox="job", folder="INBOX", store_ids=(STORE_ID,)):
    writer = metalog.LogWriter(root, _heads(root))
    for store_id in store_ids:
        writer.add(mailbox, [folder] if folder is not None else [], store_id)
    return writer.seal(WHEN)


def _read_log(path) -> metalog.LogFile:
    """The log file a test expects to be readable, so a failure lands on the assertion."""
    logfile = metalog.read_log(path)
    assert logfile is not None, f"unreadable log file: {path}"
    return logfile


def _read_head(root, job, folder) -> heads.Head:
    """The head a test expects to be there."""
    head = heads.read(root, job, folder)
    assert head is not None, f"no head for {job}::{folder}"
    return head


class TestWriting:
    def test_nothing_observed_writes_no_file(self, tmp_path):
        """An unchanged folder must not litter the log with empty files."""
        writer = metalog.LogWriter(tmp_path / "meta", tmp_path / "heads")

        assert writer.seal(WHEN) == []
        assert not (tmp_path / "meta").exists()

    def test_name_is_the_hash_of_the_content(self, tmp_path):
        (path,) = _write(tmp_path / "meta")

        assert path.name == hashlib.sha384(path.read_bytes()).hexdigest() + ".jsonl"
        assert path.parent.name == path.name[:2]

    def test_corrupted_content_is_reported_but_still_read(self, tmp_path, caplog):
        """What syntax alone can never catch: a flipped bit in a valid line.

        The file is not discarded: a log never claims to be exhaustive, so what
        still parses is a subset of the truth, which is what it always was.
        """
        (path,) = _write(tmp_path / "meta", store_ids=["aaaa", "bbbb"])
        body = path.read_text(encoding="utf-8")
        path.write_text(body.replace("bbbb", "cccc"), encoding="utf-8")

        logfile = metalog.read_log(path)
        assert logfile is not None

        assert "damaged" in caplog.text
        assert logfile.store_ids == ["aaaa", "cccc"]

    def test_each_place_becomes_its_own_file(self, tmp_path):
        """One file is one (mailbox, folder) -- that is what makes it unambiguous."""
        writer = metalog.LogWriter(tmp_path / "meta", tmp_path / "heads")
        writer.add("job", ["INBOX", "\\Sent"], STORE_ID)
        writer.add("other", ["INBOX"], STORE_ID)

        paths = writer.seal(WHEN)

        assert len(paths) == 3
        places = {(f.mailbox, f.folder) for f in metalog.read_all(tmp_path / "meta")}
        assert places == {("job", "INBOX"), ("job", "\\Sent"), ("other", "INBOX")}

    def test_files_written_together_get_distinct_names(self, tmp_path):
        writer = metalog.LogWriter(tmp_path / "meta", tmp_path / "heads")
        for folder in ("a", "b", "c", "d", "e"):
            writer.add("job", [folder], STORE_ID)

        paths = writer.seal(WHEN)

        assert len({p.name for p in paths}) == 5

    def test_the_same_observations_again_are_a_new_link(self, tmp_path):
        """What the chain costs, said out loud.

        Before there was a chain, two seals of the same content produced the same
        bytes and therefore the same file -- content addressing folded them. Now
        the second names the first as its predecessor, so it differs and is its
        own file. That is not a regression to fix but what a chain *is*: seeing
        the same messages again is a second observation, and the log records
        observations.

        It costs little in practice. A folder with a resume point offers nothing
        the next run has not seen, so nothing is observed and nothing is sealed;
        and `compact` folds a place's whole chain back into one file.
        """
        root = tmp_path / "meta"
        (first,) = _write(root)
        (second,) = _write(root)

        assert first != second
        assert len(metalog.log_files(root)) == 2
        assert _read_log(second).prev == first.name.removesuffix(".jsonl")
        assert _read_log(first).prev is None

    def test_folder_with_separators_stays_out_of_the_filename(self, tmp_path):
        """Names like 'Archiv/2016' must never become path components."""
        (path,) = _write(tmp_path / "meta", folder="Archiv/2016")

        logfile = metalog.read_log(path)
        assert logfile is not None
        assert "/" not in path.name
        assert logfile.folder == "Archiv/2016"

    def test_backslash_folder_survives(self, tmp_path):
        (path,) = _write(tmp_path / "meta", folder="\\Sent")

        logfile = metalog.read_log(path)
        assert logfile is not None
        assert logfile.folder == "\\Sent"

    def test_byte_folder_names_are_decoded(self, tmp_path):
        """Gmail reports its folder names as raw bytes, which JSON cannot hold."""
        writer = metalog.LogWriter(tmp_path / "meta", tmp_path / "heads")
        writer.add("job", [b"\\Sent"], STORE_ID)

        (path,) = writer.seal(WHEN)

        logfile = metalog.read_log(path)
        assert logfile is not None
        assert logfile.folder == "\\Sent"

    def test_message_without_a_folder_is_recorded_against_the_mailbox(self, tmp_path):
        """Knowing less is not the same as knowing nothing."""
        writer = metalog.LogWriter(tmp_path / "meta", tmp_path / "heads")
        writer.add("job", [], STORE_ID)

        (path,) = writer.seal(WHEN)

        logfile = metalog.read_log(path)
        assert logfile is not None
        assert logfile.mailbox == "job"
        assert logfile.folder is None
        assert logfile.store_ids == [STORE_ID]

    def test_leaves_no_temporary_file_behind(self, tmp_path):
        root = tmp_path / "meta"
        _write(root)

        assert [p.suffix for p in root.glob("*/*")] == [".jsonl"]

    def test_declared_count_matches_what_is_written(self, tmp_path):
        (path,) = _write(tmp_path / "meta", store_ids=["aaa", "bbb"])

        header = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        assert header["messages"] == 2

    def test_writer_is_reusable_after_sealing(self, tmp_path):
        writer = metalog.LogWriter(tmp_path / "meta", tmp_path / "heads")
        writer.add("job", ["INBOX"], STORE_ID)
        writer.seal(WHEN)

        assert len(writer) == 0
        assert writer.places == 0
        assert writer.seal(WHEN) == []


class TestReading:
    def test_roundtrip(self, tmp_path):
        (path,) = _write(
            tmp_path / "meta",
            mailbox="mail.example.org",
            folder="INBOX",
            store_ids=["aaa", "bbb"],
        )

        logfile = metalog.read_log(path)
        assert logfile is not None

        assert logfile.mailbox == "mail.example.org"
        assert logfile.folder == "INBOX"
        assert logfile.store_ids == ["aaa", "bbb"]

    def test_torn_final_line_is_skipped_and_the_rest_survives(self, tmp_path, caplog):
        """The expected shape of an interrupted write."""
        (path,) = _write(tmp_path / "meta", store_ids=["aaa", "bbb"])
        body = path.read_text(encoding="utf-8")
        path.write_text(body[: body.rindex("\n") - 12], encoding="utf-8")

        logfile = metalog.read_log(path)
        assert logfile is not None
        assert logfile.store_ids == ["aaa"]

    def test_truncation_on_a_line_boundary_is_reported(self, tmp_path, caplog):
        """A file cut at a newline parses cleanly and is still short."""
        (path,) = _write(tmp_path / "meta", store_ids=["aaa", "bbb", "ccc"])
        lines = path.read_text(encoding="utf-8").splitlines()
        path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")

        logfile = metalog.read_log(path)
        assert logfile is not None

        assert logfile.store_ids == ["aaa", "bbb"]
        assert "header declares 3 message(s) but 2 were readable" in caplog.text

    def test_unknown_version_is_rejected(self, tmp_path, caplog):
        path = tmp_path / "log.jsonl"
        path.write_text(json.dumps({"version": 99, "mailbox": "j"}) + "\n", encoding="utf-8")

        assert metalog.read_log(path) is None
        assert "is not one of 1, 2" in caplog.text

    def test_unreadable_header_discards_the_file(self, tmp_path, caplog):
        path = tmp_path / "log.jsonl"
        path.write_text('{"version": 1, "mail\n{"store_id":"x"}\n', encoding="utf-8")

        assert metalog.read_log(path) is None
        assert "unreadable header" in caplog.text

    def test_empty_file_is_rejected(self, tmp_path):
        path = tmp_path / "log.jsonl"
        path.write_text("", encoding="utf-8")

        assert metalog.read_log(path) is None

    def test_line_without_store_id_is_skipped(self, tmp_path, caplog):
        path = tmp_path / "log.jsonl"
        path.write_text(
            json.dumps({"version": 1, "mailbox": "j", "folder": "INBOX"})
            + "\n"
            + json.dumps({"nothing": "useful"})
            + "\n"
            + json.dumps({"store_id": "abc"})
            + "\n",
            encoding="utf-8",
        )

        logfile = metalog.read_log(path)
        assert logfile is not None
        assert logfile.store_ids == ["abc"]
        assert "no usable store_id" in caplog.text

    def test_store_id_that_is_not_a_hash_is_skipped(self, tmp_path, caplog):
        """A store id the store would refuse costs its line, not the file.

        The store cuts a path out of a store id and rejects anything that is not
        a hash. That value came out of a file which is allowed to be damaged, so
        it has to be dropped here -- passing it on would turn one broken line
        into a refusal at whoever asks the store next, and cost them the whole
        place they were reading.
        """
        path = tmp_path / "log.jsonl"
        path.write_text(
            json.dumps({"version": 1, "mailbox": "j", "folder": "INBOX"})
            + "\n"
            # A single flipped bit is enough: 'a' (0x61) becomes 'i' (0x69).
            + json.dumps({"store_id": "iaa"})
            + "\n"
            + json.dumps({"store_id": "aaa"})
            + "\n",
            encoding="utf-8",
        )

        logfile = metalog.read_log(path)
        assert logfile is not None
        assert logfile.store_ids == ["aaa"]
        assert "store_id is not a hash" in caplog.text


class TestDiscovery:
    def test_files_are_found_across_shards(self, tmp_path):
        root = tmp_path / "meta"
        _write(root, folder="one")
        _write(root, folder="two")

        found = metalog.log_files(root)
        assert len(found) == 2
        assert all(p.parent.parent == root for p in found)

    def test_transient_and_non_log_files_are_ignored(self, tmp_path):
        root = tmp_path / "meta"
        _write(root)
        (root / "aa").mkdir(exist_ok=True)
        (root / "aa" / "half._tmp_").write_text("half", encoding="utf-8")
        (root / ".hidden").write_text("x", encoding="utf-8")

        assert len(metalog.log_files(root)) == 1

    def test_missing_directory_is_not_an_error(self, tmp_path):
        assert metalog.log_files(tmp_path / "nope") == []
        assert metalog.has_logs(tmp_path / "nope") is False

    def test_read_all_skips_unusable_files(self, tmp_path):
        root = tmp_path / "meta"
        _write(root)
        (root / "ff").mkdir(exist_ok=True)
        (root / "ff" / "ff00.jsonl").write_text("broken", encoding="utf-8")

        assert len(list(metalog.read_all(root))) == 1


class TestHeaderOnly:
    """The header alone answers where a file belongs, without its message lines."""

    def test_it_reads_the_place(self, tmp_path):
        (path,) = _write(tmp_path / "meta", mailbox="gmail.com", folder="Sent")

        header = metalog.read_header(path)

        assert header is not None
        assert header["mailbox"] == "gmail.com"
        assert header["folder"] == "Sent"

    def test_an_unusable_file_yields_nothing(self, tmp_path):
        path = tmp_path / "broken.jsonl"
        path.write_text("not json\n", encoding="utf-8")

        assert metalog.read_header(path) is None

    def test_an_empty_file_yields_nothing(self, tmp_path):
        path = tmp_path / "empty.jsonl"
        path.write_text("", encoding="utf-8")

        assert metalog.read_header(path) is None

    def test_mailboxes_gathers_the_names(self, tmp_path):
        root = tmp_path / "meta"
        _write(root, mailbox="gmail.com", folder="INBOX")
        _write(root, mailbox="gmail.com", folder="Sent")
        _write(root, mailbox="posteo.de", folder="INBOX")

        assert metalog.mailboxes(root) == {"gmail.com", "posteo.de"}

    def test_mailboxes_of_an_empty_archive(self, tmp_path):
        assert metalog.mailboxes(tmp_path / "meta") == set()

    def test_a_damaged_file_costs_only_itself(self, tmp_path):
        root = tmp_path / "meta"
        _write(root, mailbox="gmail.com")
        (root / "ff").mkdir(exist_ok=True)
        (root / "ff" / "ff00.jsonl").write_text("broken", encoding="utf-8")

        assert metalog.mailboxes(root) == {"gmail.com"}


class TestCompact:
    @staticmethod
    def _write(root, store_ids, mailbox="job", folder="INBOX", when=WHEN):
        writer = metalog.LogWriter(root, _heads(root))
        for store_id in store_ids:
            writer.add(mailbox, [folder], store_id)
        writer.seal(when)

    def test_sweeps_up_after_an_interrupted_write(self, tmp_path):
        """Compaction is the pass that has the log open, so it tidies it.

        Only the log: sweeping the mail store would mean walking a hundred
        thousand directories, which is not what this command is for.
        """
        root = tmp_path / "meta"
        self._write(root, ["a", "b"])
        (logfile,) = metalog.log_files(root)
        leftover = logfile.with_name(f"{logfile.name}.4711-0{cas.TEMP_SUFFIX}")
        leftover.write_bytes(b'{"version":1,"mailbox":"job"')
        os.utime(leftover, (0, time.time() - cas.TRANSIENT_MIN_AGE - 60))

        result = metalog.compact(root, _heads(root))

        assert result.transient_removed == 1
        assert not leftover.exists()
        assert [f.store_ids for f in metalog.read_all(root)] == [["a", "b"]]

    def test_consolidates_and_deduplicates_a_place(self, tmp_path):
        root = tmp_path / "meta"
        self._write(root, ["a", "b"])
        self._write(root, ["b", "c"])  # 'b' repeats, as after a full read
        self._write(root, ["c", "d"])  # 'c' repeats
        assert len(metalog.log_files(root)) == 3

        result = metalog.compact(root, _heads(root))

        assert result.files_before == 3
        assert result.files_after == 1
        assert result.places == 1
        assert result.entries_before == 6
        assert result.entries_after == 4  # a, b, c, d
        (logfile,) = list(metalog.read_all(root))
        assert set(logfile.store_ids) == {"a", "b", "c", "d"}

    def test_leaves_no_empty_shard_directories(self, tmp_path):
        root = tmp_path / "meta"
        self._write(root, ["a", "b"])
        self._write(root, ["b", "c"])
        self._write(root, ["c", "d"])
        shards_before = [d for d in root.iterdir() if d.is_dir()]

        metalog.compact(root, _heads(root))

        assert len(shards_before) > 1  # the runs really did land in several shards
        assert [d for d in root.iterdir() if d.is_dir() and not list(d.iterdir())] == []

    def test_separate_places_stay_separate(self, tmp_path):
        root = tmp_path / "meta"
        self._write(root, ["a"], folder="INBOX")
        self._write(root, ["b"], folder="Sent")

        result = metalog.compact(root, _heads(root))

        assert result.files_after == 2
        assert result.places == 2
        places = {(lf.mailbox, lf.folder): set(lf.store_ids) for lf in metalog.read_all(root)}
        assert places == {("job", "INBOX"): {"a"}, ("job", "Sent"): {"b"}}

    def test_is_idempotent(self, tmp_path):
        root = tmp_path / "meta"
        self._write(root, ["a", "b"])
        self._write(root, ["b", "c"])
        metalog.compact(root, _heads(root))
        files = set(metalog.log_files(root))

        result = metalog.compact(root, _heads(root))

        assert set(metalog.log_files(root)) == files  # nothing rewritten or removed
        assert result.files_before == result.files_after
        assert result.entries_before == result.entries_after  # nothing left to dedupe

    def test_keeps_the_newest_date(self, tmp_path):
        root = tmp_path / "meta"
        self._write(root, ["a"], when=datetime(2026, 8, 1, tzinfo=UTC))
        self._write(root, ["b"], when=datetime(2026, 8, 3, tzinfo=UTC))
        self._write(root, ["c"], when=datetime(2026, 8, 2, tzinfo=UTC))

        metalog.compact(root, _heads(root))

        (logfile,) = list(metalog.read_all(root))
        assert logfile.date == datetime(2026, 8, 3, tzinfo=UTC).isoformat()

    def test_empty_log_is_a_no_op(self, tmp_path):
        result = metalog.compact(tmp_path / "meta", tmp_path / "heads")

        assert result.files_before == 0
        assert result.files_after == 0

    def test_an_unreadable_file_is_kept_not_folded_away(self, tmp_path):
        root = tmp_path / "meta"
        self._write(root, ["a", "b"])
        (root / "ff").mkdir(exist_ok=True)
        broken = root / "ff" / "ff00.jsonl"
        broken.write_text("broken", encoding="utf-8")

        result = metalog.compact(root, _heads(root))

        assert broken.exists()  # left in place, its contents not lost
        assert result.entries_after == 2
        places = {(lf.mailbox, lf.folder): set(lf.store_ids) for lf in metalog.read_all(root)}
        assert places[("job", "INBOX")] == {"a", "b"}


class TestTheChain:
    """Each place's files name their predecessor; the newest is named by its head."""

    @staticmethod
    def _seal(root, store_ids, mailbox="job", folder="INBOX"):
        writer = metalog.LogWriter(root, _heads(root))
        for store_id in store_ids:
            writer.add(mailbox, [folder], store_id)
        return writer.seal(WHEN)

    def test_the_first_file_of_a_place_starts_one(self, tmp_path):
        root = tmp_path / "meta"

        (path,) = self._seal(root, ["aa"])

        assert _read_log(path).prev is None

    def test_the_head_names_the_newest(self, tmp_path):
        root = tmp_path / "meta"
        self._seal(root, ["aa"])
        (second,) = self._seal(root, ["bb"])

        head = _read_head(_heads(root), "job", "INBOX")

        assert head.log == second.name.removesuffix(".jsonl")

    def test_following_it_back_reaches_every_file_of_the_place(self, tmp_path):
        root = tmp_path / "meta"
        names = [self._seal(root, [sid])[0].name.removesuffix(".jsonl") for sid in "abc"]

        walked = []
        hashval = _read_head(_heads(root), "job", "INBOX").log
        while hashval is not None:
            walked.append(hashval)
            path = metalog.open_store(root).locate(hashval)
            assert path is not None
            hashval = _read_log(path).prev

        assert walked == list(reversed(names))

    def test_two_places_keep_two_chains(self, tmp_path):
        root = tmp_path / "meta"
        self._seal(root, ["aa"], folder="INBOX")
        self._seal(root, ["bb"], folder="Sent")

        inbox = _read_head(_heads(root), "job", "INBOX")
        sent = _read_head(_heads(root), "job", "Sent")

        assert inbox.log != sent.log
        assert sent.log is not None
        path = metalog.open_store(root).locate(sent.log)
        assert path is not None
        assert _read_log(path).prev is None

    def test_a_place_without_a_mailbox_carries_no_chain(self, tmp_path):
        """The type allows it, so it is answered rather than assumed away."""
        root = tmp_path / "meta"
        writer = metalog.LogWriter(root, _heads(root))
        writer.add(None, ["INBOX"], STORE_ID)

        (path,) = writer.seal(WHEN)

        assert _read_log(path).prev is None
        assert heads.head_files(_heads(root)) == []

    def test_a_place_without_a_folder_gets_a_head_all_the_same(self, tmp_path):
        """What the store.db migration writes: mailbox known, folder not."""
        root = tmp_path / "meta"
        writer = metalog.LogWriter(root, _heads(root))
        writer.add("job", [], STORE_ID)
        (path,) = writer.seal(WHEN)

        head = heads.read(_heads(root), "job", None)

        assert head is not None
        assert head.log == path.name.removesuffix(".jsonl")

    def test_a_version_1_file_simply_carries_no_chain(self, tmp_path):
        """An archive is full of files written over years; refusing them is worse."""
        path = tmp_path / "log.jsonl"
        header = {"version": 1, "mailbox": "job", "folder": "INBOX", "messages": 0}
        path.write_text(json.dumps(header) + "\n", encoding="utf-8")

        logfile = metalog.read_log(path)

        assert logfile is not None
        assert logfile.prev is None


class TestCompactAndTheChain:
    def test_the_consolidated_file_starts_the_chain_over(self, tmp_path):
        """It holds everything that came before, so naming what is about to be
        removed would point at something deliberately gone."""
        root = tmp_path / "meta"
        TestTheChain._seal(root, ["aa"])
        TestTheChain._seal(root, ["bb"])

        metalog.compact(root, _heads(root))

        (path,) = metalog.log_files(root)
        assert _read_log(path).prev is None

    def test_and_the_head_is_moved_onto_it(self, tmp_path):
        root = tmp_path / "meta"
        TestTheChain._seal(root, ["aa"])
        TestTheChain._seal(root, ["bb"])

        metalog.compact(root, _heads(root))

        (path,) = metalog.log_files(root)
        head = _read_head(_heads(root), "job", "INBOX")
        assert head.log == path.name.removesuffix(".jsonl")

    def test_compacting_twice_changes_nothing(self, tmp_path):
        root = tmp_path / "meta"
        TestTheChain._seal(root, ["aa"])
        TestTheChain._seal(root, ["bb"])
        metalog.compact(root, _heads(root))
        before = _read_head(_heads(root), "job", "INBOX").log

        metalog.compact(root, _heads(root))

        assert _read_head(_heads(root), "job", "INBOX").log == before
        assert len(metalog.log_files(root)) == 1
