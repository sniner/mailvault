"""Tests for mailvault.backend.base.

TRANSITIONAL: `DateResumeTracker` carries the date mechanism of 0.9.2 as a
resume token while the protocol moves ahead of the backends. It goes when IMAP
resumes from a UID watermark and Graph from a delta link, and these tests go
with it -- but until then the two guards it holds are the only thing standing
between a date filter and the mail it can skip.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from mailvault.backend import base

OBSERVED_AT = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
EARLIER = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)


def _token(date: datetime) -> dict:
    return {"kind": base.DATE_KIND, "date": date.isoformat()}


class TestSince:
    """What the incoming token turns into for the backend's own filter."""

    def test_a_date_token_becomes_the_filter(self):
        tracker = base.DateResumeTracker(_token(EARLIER), OBSERVED_AT)

        assert tracker.since == EARLIER

    def test_no_token_means_read_in_full(self):
        assert base.DateResumeTracker(None, OBSERVED_AT).since is None

    def test_a_foreign_kind_means_read_in_full(self):
        """The rule that covers a swapped backend and a format from the future."""
        foreign = {"kind": "imap-uid", "uidvalidity": 1, "uid": 2}

        assert base.DateResumeTracker(foreign, OBSERVED_AT).since is None

    def test_an_unparsable_date_means_read_in_full(self, caplog):
        tracker = base.DateResumeTracker({"kind": base.DATE_KIND, "date": "soon"}, OBSERVED_AT)

        assert tracker.since is None
        assert "unparsable date" in caplog.text

    def test_a_naive_date_is_read_as_local_time(self):
        naive = {"kind": base.DATE_KIND, "date": "2025-10-16T19:16:59.494153"}

        parsed = base.DateResumeTracker(naive, OBSERVED_AT).since

        assert parsed is not None
        assert parsed.tzinfo is not None


class TestToken:
    """What the pass hands back, which is only ever what it actually saw."""

    def test_a_pass_that_stored_nothing_earns_nothing(self):
        """The Proton Bridge case, in the one place that can still see it."""
        tracker = base.DateResumeTracker(None, OBSERVED_AT)

        assert tracker.token() is None

    def test_the_newest_message_wins(self):
        tracker = base.DateResumeTracker(None, OBSERVED_AT)
        newest = OBSERVED_AT - timedelta(hours=1)

        tracker.saw(EARLIER)
        tracker.saw(newest)
        tracker.saw(EARLIER)

        assert tracker.token() == _token(newest)

    def test_a_message_dated_in_the_future_cannot_carry_the_point_past_now(self):
        """A server with a wrong clock must not skip whatever arrives next."""
        tracker = base.DateResumeTracker(None, OBSERVED_AT)

        tracker.saw(datetime(2099, 1, 1, tzinfo=UTC))

        assert tracker.token() == _token(OBSERVED_AT)

    def test_an_incremental_pass_never_moves_backwards(self):
        """The window reaches a day behind the point, so an older find is normal."""
        tracker = base.DateResumeTracker(_token(OBSERVED_AT), OBSERVED_AT)

        tracker.saw(EARLIER)

        assert tracker.token() is None

    def test_a_full_pass_may_move_backwards(self):
        """Given no point, there is nothing to hold back from -- so it is authoritative.

        This is what repairs a point an earlier version set too far ahead: the
        pass read without a filter, so what it found *is* the coverage.
        """
        tracker = base.DateResumeTracker(None, OBSERVED_AT)

        tracker.saw(EARLIER)

        assert tracker.token() == _token(EARLIER)

    def test_a_naive_timestamp_is_taken_as_local_time(self):
        tracker = base.DateResumeTracker(None, OBSERVED_AT)

        tracker.saw(datetime(2026, 8, 1, 9, 0))

        token = tracker.token()
        assert token is not None
        assert datetime.fromisoformat(token["date"]).tzinfo is not None

    def test_a_message_without_a_date_contributes_nothing(self):
        tracker = base.DateResumeTracker(None, OBSERVED_AT)

        tracker.saw(None)

        assert tracker.token() is None
