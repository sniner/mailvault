"""Tests for the small helpers in mailvault.utils.

`batched` is a facade: from Python 3.12 on it delegates to `itertools.batched`,
below that to a stand-in. These tests describe the contract rather than the
branch, so the CI matrix (3.11 … 3.14) checks both implementations against the
same expectations.
"""

from __future__ import annotations

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
