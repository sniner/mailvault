"""Tests for mailvault.jobs.ledger (which server messages does the Message-ID settle?)."""

from __future__ import annotations

from mailvault.jobs import ledger as mod

ARCHIVED = mod.Match.ARCHIVED
UNKNOWN = mod.Match.UNKNOWN
OUTNUMBERED = mod.Match.OUTNUMBERED


def _long_id(tail: str = "spnotify") -> str:
    """A Message-ID long enough to be affected by server-side truncation."""
    return "teamsmissedactivityemail-" + "a1b2c3d4-" * 20 + "@od" + tail


def _match(counts: dict[str, int], *values: str) -> list[mod.Match]:
    return mod.match_by_message_id(counts, list(values))


class TestMatchByMessageId:
    def test_an_archived_copy_settles_its_server_message(self):
        counts = {"a@example.com": 1, "b@example.com": 1}
        assert _match(counts, "a@example.com", "c@example.com") == [ARCHIVED, UNKNOWN]

    def test_two_archived_copies_settle_two_server_messages(self):
        """Same Message-ID, different bytes: two objects, two matches."""
        assert _match({"a@example.com": 2}, "a@example.com", "a@example.com") == [
            ARCHIVED,
            ARCHIVED,
        ]

    def test_more_archived_copies_than_listed_is_fine(self):
        assert _match({"a@example.com": 3}, "a@example.com") == [ARCHIVED]

    def test_an_outnumbered_id_leaves_every_copy_unsettled(self):
        """Issue #14: the id cannot say which server copy is the archived one.

        Handing the archived copy to the first one listed left a different
        version listed first unfetched for good, while the archived version
        listed second was downloaded and found identical.
        """
        assert _match({"x@example.com": 1}, "x@example.com", "x@example.com") == [
            OUTNUMBERED,
            OUTNUMBERED,
        ]

    def test_an_outnumbered_id_does_not_unsettle_other_ids(self):
        counts = {"x@example.com": 1, "a@example.com": 1}
        values = ["a@example.com", "x@example.com", "x@example.com"]
        assert _match(counts, *values) == [ARCHIVED, OUTNUMBERED, OUTNUMBERED]

    def test_an_unknown_id_is_not_an_outnumbered_one(self):
        assert _match({"x@example.com": 1}, "y@example.com") == [UNKNOWN]

    def test_empty_values_never_match(self):
        assert _match({"a@example.com": 1}, "") == [UNKNOWN]
        assert _match({}, "") == [UNKNOWN]

    def test_empty_archived_ids_are_not_counted(self):
        assert _match({"": 3}, "") == [UNKNOWN]

    def test_truncated_long_id_matches_by_prefix(self):
        """Exchange caps the reported Message-ID, so the server value is a prefix."""
        archived = _long_id()
        assert _match({archived: 1}, archived[:255]) == [ARCHIVED]

    def test_short_id_is_never_prefix_matched(self):
        """A short prefix must not silently match a longer archived ID."""
        assert _match({"abcdef@example.com": 1}, "abc") == [UNKNOWN]

    def test_prefix_match_picks_the_right_entry(self):
        a, b = _long_id("aaa"), _long_id("bbb")
        counts = {a: 1, b: 1, "short@example.com": 1}
        # A long value that is nobody's prefix stays unmatched.
        stranger = _long_id("zzz") + "-x"
        assert _match(counts, a, b, stranger) == [ARCHIVED, ARCHIVED, UNKNOWN]

    def test_a_prefix_scan_passes_over_an_exhausted_entry(self):
        """The first entry starting with the value may already be claimed."""
        a, b = _long_id("aaa"), _long_id("aab")
        prefix = _long_id("aa")[:250]
        assert a.startswith(prefix) and b.startswith(prefix)
        assert _match({a: 1, b: 1}, prefix, prefix) == [ARCHIVED, ARCHIVED]

    def test_an_outnumbered_prefix_unsettles_every_entry_it_reached(self):
        """Three server copies for two archived entries: none of the three is settled."""
        a, b = _long_id("aaa"), _long_id("aab")
        prefix = _long_id("aa")[:250]
        assert _match({a: 1, b: 1}, prefix, prefix, prefix) == [OUTNUMBERED] * 3

    def test_an_exact_match_on_an_entry_a_prefix_outnumbered_is_unsettled(self):
        a = _long_id("aaa")
        prefix = _long_id("aa")[:250]
        assert _match({a: 1}, a, prefix) == [OUTNUMBERED, OUTNUMBERED]

    def test_longer_value_does_not_match_shorter_archived(self):
        archived = _long_id()
        assert _match({archived: 1}, archived + "-more") == [UNKNOWN]


class TestSameMessageId:
    def test_equal_ids_are_the_same(self):
        assert mod.same_message_id("a@example.com", "a@example.com")

    def test_different_ids_are_not(self):
        assert not mod.same_message_id("graph@example.com", "mime@example.com")

    def test_a_truncated_long_id_is_the_same(self):
        archived = _long_id()
        assert mod.same_message_id(archived[:255], archived)

    def test_a_short_prefix_is_not(self):
        assert not mod.same_message_id("abc", "abcdef@example.com")

    def test_an_empty_value_is_never_the_same(self):
        assert not mod.same_message_id("", "")
