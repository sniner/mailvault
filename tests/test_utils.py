"""Tests for the small helpers in mailvault.utils.

`batched` is a facade: from Python 3.12 on it delegates to `itertools.batched`,
below that to a stand-in. These tests describe the contract rather than the
branch, so the CI matrix (3.11 … 3.14) checks both implementations against the
same expectations.
"""

from __future__ import annotations

import logging
import pathlib
import stat

import pytest

from mailvault import utils
from mailvault.utils import fs


@pytest.fixture(autouse=True)
def a_fresh_answer(monkeypatch):
    """Every test asks the filesystem itself, whatever an earlier one learned.

    `fs` remembers whether the write protection holds here, so a run does not
    ask once per message. Left alone, the first test to touch it would decide
    for all the others.
    """
    monkeypatch.setattr(fs, "_chmod_honoured", None)


def writable(path: pathlib.Path) -> bool:
    return bool(stat.S_IMODE(path.stat().st_mode) & fs.WRITE_BITS)


class TestBatched:
    def test_an_exact_multiple_splits_evenly(self):
        assert list(utils.batched(range(6), 3)) == [[0, 1, 2], [3, 4, 5]]

    def test_the_last_batch_is_short(self):
        assert list(utils.batched(range(7), 3)) == [[0, 1, 2], [3, 4, 5], [6]]

    def test_a_single_batch_holds_everything(self):
        assert list(utils.batched(range(3), 10)) == [[0, 1, 2]]

    def test_an_empty_iterable_yields_nothing(self):
        assert list(utils.batched([], 3)) == []

    def test_a_size_of_one_yields_singletons(self):
        assert list(utils.batched(range(3), 1)) == [[0], [1], [2]]

    @pytest.mark.parametrize("n", [0, -1])
    def test_a_batch_size_below_one_is_refused(self, n):
        """The one thing a stand-in must not do is behave differently.

        `itertools.batched` raises for these; the stand-in used to collect
        everything into a single batch instead, so the same call did two
        different things depending on which Python was running it.
        """
        with pytest.raises(ValueError):
            list(utils.batched(range(3), n))

    def test_it_always_yields_lists(self):
        """What makes the version gate invisible: the stdlib yields tuples."""
        batches = list(utils.batched(range(5), 2))
        assert all(isinstance(b, list) for b in batches)

    def test_a_plain_list_works_too(self):
        # There used to be a separate `chunks` for sequences; this covers both.
        assert list(utils.batched(["a", "b", "c"], 2)) == [["a", "b"], ["c"]]

    def test_it_consumes_only_what_it_yields(self):
        """The reason this takes an iterable: an archive walk need not fit in RAM."""
        seen: list[int] = []

        def source():
            for i in range(1000):
                seen.append(i)
                yield i

        first = next(utils.batched(source(), 10))

        assert first == list(range(10))
        assert len(seen) == 10


class TestCounted:
    """The noun agrees with the number, and the number is readable."""

    def test_one_takes_the_singular(self):
        assert utils.counted(1, "message") == "1 message"

    def test_anything_else_takes_the_plural(self):
        assert utils.counted(2, "message") == "2 messages"

    def test_none_of_them_is_plural_too(self):
        """Zero takes the plural in English; only one is singular."""
        assert utils.counted(0, "message") == "0 messages"

    def test_an_irregular_plural_is_named(self):
        assert utils.counted(3, "log entry", "log entries") == "3 log entries"
        assert utils.counted(1, "log entry", "log entries") == "1 log entry"

    def test_the_noun_may_be_more_than_one_word(self):
        """The `s` goes on the noun, which is where the phrase ends."""
        assert utils.counted(2, "resume point") == "2 resume points"

    def test_a_large_count_is_grouped(self):
        """Why the number is in here at all: nobody reads 131000 at a glance."""
        assert utils.counted(131000, "message") == "131,000 messages"


class TestUnder:
    """A path as it reads inside the thing a run is about."""

    def test_a_path_below_the_root_loses_the_prefix(self):
        root = pathlib.Path("/srv/archive")

        assert utils.under(root, root / "meta" / "a1" / "a1b2.jsonl") == "meta/a1/a1b2.jsonl"

    def test_a_path_from_somewhere_else_is_returned_whole(self):
        """`archive import` reads elsewhere; shortening that would be a lie."""
        other = pathlib.Path("/mnt/docuware/2019/mail.eml")

        assert utils.under(pathlib.Path("/srv/archive"), other) == str(other)


class TestUnderDir:
    """The same shortening for a caller that only knows its own directory."""

    def test_it_cuts_at_the_named_directory(self):
        path = pathlib.Path("/srv/archive/meta/a1/a1b2.jsonl")

        assert utils.under_dir("meta", path) == "meta/a1/a1b2.jsonl"

    def test_the_depth_below_it_does_not_matter(self):
        """How deep a store shards its files stays the store's own business."""
        assert utils.under_dir("heads", pathlib.Path("/srv/a/heads/x")) == "heads/x"
        assert (
            utils.under_dir("mail", pathlib.Path("/srv/a/mail/a/b/c.eml")) == "mail/a/b/c.eml"
        )

    def test_the_last_one_of_that_name_wins(self):
        """An archive may well lie in a directory called like one of its own."""
        path = pathlib.Path("/srv/mail/archive/mail/a1/a1b2.eml")

        assert utils.under_dir("mail", path) == "mail/a1/a1b2.eml"

    def test_a_path_that_is_not_below_one_is_returned_whole(self):
        path = pathlib.Path("/tmp/loose/a1b2.jsonl")

        assert utils.under_dir("meta", path) == str(path)


class TestSetReadOnly:
    """The write bits come off what the archive is finished with."""

    def test_the_write_bits_are_gone_afterwards(self, tmp_path):
        """What a program that opens the file finds: nothing to write into."""
        path = tmp_path / "entry.eml"
        path.write_bytes(b"a stored message")

        assert utils.set_read_only(path)

        assert not writable(path)

    def test_what_is_in_the_file_is_untouched(self, tmp_path):
        path = tmp_path / "entry.eml"
        path.write_bytes(b"a stored message")

        utils.set_read_only(path)

        assert path.read_bytes() == b"a stored message"

    def test_a_chmod_that_changes_nothing_is_reported_as_such(self, tmp_path, monkeypatch):
        """The failure worth catching: success reported, mode as it was.

        A desktop-mounted SMB share does exactly this, which is why the answer
        comes from a `stat` afterwards and never from the call.
        """
        path = tmp_path / "entry.eml"
        path.write_bytes(b"lying on a share that cannot do this")
        monkeypatch.setattr(pathlib.Path, "chmod", lambda self, mode: None)

        assert not utils.set_read_only(path)

        assert writable(path)

    def test_it_reads_back_what_it_did_once_and_not_per_message(self, tmp_path, monkeypatch):
        """Three calls for the first entry of a run, two for every one after it.

        The read-back is what makes a silent no-op visible, and once is what it
        takes: the answer belongs to the filesystem, not to the file.
        """
        paths = []
        for serial in range(3):
            path = tmp_path / f"entry-{serial}.eml"
            path.write_bytes(b"one of many messages")
            paths.append(path)
        stat_of = pathlib.Path.stat
        stats: list[pathlib.Path] = []

        def counting_stat(self: pathlib.Path, *, follow_symlinks: bool = True):
            stats.append(self)
            return stat_of(self, follow_symlinks=follow_symlinks)

        monkeypatch.setattr(pathlib.Path, "stat", counting_stat)
        for path in paths:
            assert utils.set_read_only(path)

        assert len(stats) == 4, "2 for the first message, 1 for each of the others"

    def test_it_stops_asking_once_it_knows(self, tmp_path):
        """Two round trips per message, on the filesystems where they are dear."""
        attempts: list[pathlib.Path] = []

        def chmod_that_does_nothing(self: pathlib.Path, mode: int) -> None:
            attempts.append(self)

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(pathlib.Path, "chmod", chmod_that_does_nothing)
            for serial in range(3):
                path = tmp_path / f"entry-{serial}.eml"
                path.write_bytes(b"one of many messages")
                assert not utils.set_read_only(path)

        assert len(attempts) == 1

    def test_a_file_that_is_not_there_is_not_an_error(self, tmp_path):
        """A run gets the mail home; this is comfort and may not stop one."""
        assert not utils.set_read_only(tmp_path / "never-written.eml")

    def test_a_chmod_that_is_refused_says_so_once_and_for_the_run(
        self, tmp_path, monkeypatch, caplog
    ):
        """A gvfs SMB mount answers EOPNOTSUPP rather than doing nothing.

        The log has to name the conclusion, not only the first file it happened
        on -- otherwise the one line about the archive never appears.
        """

        def refusing_chmod(self: pathlib.Path, mode: int) -> None:
            raise OSError(95, "Operation not supported")

        monkeypatch.setattr(pathlib.Path, "chmod", refusing_chmod)
        paths = []
        for serial in range(2):
            path = tmp_path / f"entry-{serial}.eml"
            path.write_bytes(b"one of many messages")
            paths.append(path)

        with caplog.at_level(logging.DEBUG, logger="mailvault.utils.fs"):
            for path in paths:
                assert not utils.set_read_only(path)

        assert "write protection does not hold here" in caplog.text
        assert caplog.text.count("write protection") == 1

    def test_a_refusal_later_on_gives_up_for_the_rest_of_the_run(self, tmp_path, monkeypatch):
        """A chmod that worked and then stops working is not asked a third time.

        An archive two hosts write to is where that happens: only the owner may
        chmod a file. The rest of the run keeps its mail and loses the comfort.
        """
        protected = tmp_path / "mine.eml"
        protected.write_bytes(b"written by this host")
        assert utils.set_read_only(protected)
        attempts: list[pathlib.Path] = []

        def not_yours(self: pathlib.Path, mode: int) -> None:
            attempts.append(self)
            raise PermissionError(1, "Operation not permitted")

        monkeypatch.setattr(pathlib.Path, "chmod", not_yours)
        for name in ("someone-elses.eml", "mine-too.eml"):
            path = tmp_path / name
            path.write_bytes(b"written by whoever")
            assert not utils.set_read_only(path)

        assert len(attempts) == 1, "the second file is not tried at all"
        assert writable(tmp_path / "mine-too.eml")


class TestRemoveFile:
    """Deleting what the archive has protected, on both kinds of filesystem."""

    def test_a_protected_file_goes(self, tmp_path):
        path = tmp_path / "entry.eml"
        path.write_bytes(b"about to be consolidated away")
        utils.set_read_only(path)

        utils.remove_file(path)

        assert not path.exists()

    def test_a_refusal_is_answered_by_lifting_the_protection(self, tmp_path, monkeypatch):
        """What Windows does: a read-only file cannot go while it is one."""
        path = tmp_path / "entry.eml"
        path.write_bytes(b"about to be consolidated away")
        utils.set_read_only(path)
        unlink = pathlib.Path.unlink

        def windows_unlink(self: pathlib.Path, missing_ok: bool = False) -> None:
            if self.exists() and not writable(self):
                raise PermissionError(13, "read-only")
            unlink(self, missing_ok=missing_ok)

        monkeypatch.setattr(pathlib.Path, "unlink", windows_unlink)

        utils.remove_file(path)

        assert not path.exists()

    def test_a_file_that_is_not_there_is_the_caller_s_to_expect(self, tmp_path):
        missing = tmp_path / "gone.eml"

        with pytest.raises(FileNotFoundError):
            utils.remove_file(missing)

        utils.remove_file(missing, missing_ok=True)
