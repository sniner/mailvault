"""Tests for mailvault.jobs.ledger (how many copies has this place left to offer?)."""

from __future__ import annotations

from mailvault.jobs import ledger as mod


def _long_id(tail: str = "spnotify") -> str:
    """A Message-ID long enough to be affected by server-side truncation."""
    return "teamsmissedactivityemail-" + "a1b2c3d4-" * 20 + "@od" + tail


class TestMessageIdLedger:
    def test_an_archived_copy_can_be_claimed(self):
        ledger = mod.MessageIdLedger({"a@example.com": 1, "b@example.com": 1})
        assert ledger.take("a@example.com") is True
        assert ledger.take("c@example.com") is False

    def test_a_copy_is_claimed_only_once(self):
        """The point of counting: a second server copy finds nothing left."""
        ledger = mod.MessageIdLedger({"a@example.com": 1})
        assert ledger.take("a@example.com") is True
        assert ledger.take("a@example.com") is False

    def test_two_archived_copies_answer_twice(self):
        """Same Message-ID, different bytes: two objects, two claims."""
        ledger = mod.MessageIdLedger({"a@example.com": 2})
        assert ledger.take("a@example.com") is True
        assert ledger.take("a@example.com") is True
        assert ledger.take("a@example.com") is False

    def test_empty_values_never_match(self):
        assert mod.MessageIdLedger({"a@example.com": 1}).take("") is False
        assert mod.MessageIdLedger({}).take("") is False

    def test_empty_values_are_not_counted(self):
        assert len(mod.MessageIdLedger({"a@example.com": 1, "": 3})) == 1

    def test_the_length_is_what_is_left_to_claim(self):
        ledger = mod.MessageIdLedger({"a@example.com": 2, "b@example.com": 1})
        assert len(ledger) == 3
        ledger.take("a@example.com")
        assert len(ledger) == 2

    def test_truncated_long_id_matches_by_prefix(self):
        """Exchange caps the reported Message-ID, so the server value is a prefix."""
        archived = _long_id()
        ledger = mod.MessageIdLedger({archived: 1})
        assert ledger.take(archived[:255]) is True

    def test_short_id_is_never_prefix_matched(self):
        """A short prefix must not silently match a longer archived ID."""
        assert mod.MessageIdLedger({"abcdef@example.com": 1}).take("abc") is False

    def test_prefix_match_picks_the_right_entry(self):
        a, b = _long_id("aaa"), _long_id("bbb")
        ledger = mod.MessageIdLedger({a: 1, b: 1, "short@example.com": 1})
        assert ledger.take(a) is True
        assert ledger.take(b) is True
        # A long value that is nobody's prefix stays unmatched.
        assert ledger.take(_long_id("zzz") + "-x") is False

    def test_a_prefix_scan_passes_over_an_exhausted_entry(self):
        """The first entry starting with the value may already be claimed."""
        a, b = _long_id("aaa"), _long_id("aab")
        prefix = _long_id("aa")[:250]
        assert a.startswith(prefix) and b.startswith(prefix)
        ledger = mod.MessageIdLedger({a: 1, b: 1})

        assert ledger.take(prefix) is True
        assert ledger.take(prefix) is True
        assert ledger.take(prefix) is False

    def test_longer_value_does_not_match_shorter_archived(self):
        archived = _long_id()
        ledger = mod.MessageIdLedger({archived: 1})
        assert ledger.take(archived + "-more") is False

