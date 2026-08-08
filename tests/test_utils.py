"""Tests for the small helpers in mailvault.utils.

`batched` is a facade: from Python 3.12 on it delegates to `itertools.batched`,
below that to a stand-in. These tests describe the contract rather than the
branch, so the CI matrix (3.11 … 3.14) checks both implementations against the
same expectations.
"""

from __future__ import annotations

import pathlib

from mailvault import utils


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
