"""Tests for `mailvault db search` -- the filters and what comes out of them."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from mailvault.jobs.db import SearchQuery, search
from mailvault.store import index_db


def _archive(path):
    """A projection with three messages, enough to tell every filter apart."""
    with index_db.IndexDatabase(path) as db:
        first = db.add_message(
            "a" * 96,
            "<a@example.com>",
            datetime(2024, 3, 11, 10, 0, tzinfo=UTC),
            "Rechnung 4711",
        )
        db.add_message_sender(first, "info@example.com")
        db.add_message_recipients(first, "stefan@example.org")
        db.add_message_location(first, "example.com", "Archiv")

        second = db.add_message(
            "b" * 96,
            "<b@example.com>",
            datetime(2025, 5, 2, 9, 0, tzinfo=UTC),
            "Lieferschein",
        )
        db.add_message_sender(second, "versand@example.net")
        db.add_message_recipients(second, "info@example.com")
        db.add_message_location(second, "example.com", "INBOX")

        third = db.add_message("c" * 96, "", None, "")
        db.add_message_location(third, None, "docuware-2019")
    return path


@pytest.fixture
def db_path(tmp_path):
    return _archive(tmp_path / "index.db")


def _ids(hits):
    return sorted(hit.store_id[0] for hit in hits)


class TestFilters:
    def test_no_filter_finds_everything(self, db_path):
        assert _ids(search(db_path, SearchQuery())) == ["a", "b", "c"]

    def test_the_sender_is_matched_anywhere_and_case_is_ignored(self, db_path):
        assert _ids(search(db_path, SearchQuery(sender="XAMPLE.COM"))) == ["a"]

    def test_sender_and_recipient_are_not_the_same_question(self, db_path):
        """The same address on both sides of two different messages."""
        assert _ids(search(db_path, SearchQuery(sender="info@example.com"))) == ["a"]
        assert _ids(search(db_path, SearchQuery(recipient="info@example.com"))) == ["b"]

    def test_the_subject_is_matched_anywhere(self, db_path):
        assert _ids(search(db_path, SearchQuery(subject="4711"))) == ["a"]

    def test_a_place_can_be_asked_for_by_either_half(self, db_path):
        assert _ids(search(db_path, SearchQuery(mailbox="example.com"))) == ["a", "b"]
        assert _ids(search(db_path, SearchQuery(folder="Archiv"))) == ["a"]

    def test_a_place_with_no_mailbox_is_still_findable(self, db_path):
        """What an import writes: a named place and no mailbox."""
        assert _ids(search(db_path, SearchQuery(folder="docuware"))) == ["c"]

    def test_filters_are_combined_with_and(self, db_path):
        sender = "info@example.com"
        assert _ids(search(db_path, SearchQuery(sender=sender, folder="INBOX"))) == []
        assert _ids(search(db_path, SearchQuery(sender=sender, folder="Archiv"))) == ["a"]

    def test_dates_are_compared_by_day(self, db_path):
        assert _ids(search(db_path, SearchQuery(since="2025-01-01"))) == ["b"]
        assert _ids(search(db_path, SearchQuery(until="2024-12-31"))) == ["a"]
        assert _ids(search(db_path, SearchQuery(since="2024-03-11", until="2024-03-11"))) == [
            "a"
        ]

    def test_a_message_with_no_readable_date_matches_no_date_filter(self, db_path):
        """It is unknown, not old -- and answering either way would be a guess."""
        assert "c" not in _ids(search(db_path, SearchQuery(since="1970-01-01")))
        assert "c" not in _ids(search(db_path, SearchQuery(until="2099-01-01")))
        assert "c" in _ids(search(db_path, SearchQuery()))

    def test_wildcards_a_user_types_are_literal(self, tmp_path):
        """`%` is a character in a subject before it is a pattern."""
        path = tmp_path / "index.db"
        with index_db.IndexDatabase(path) as db:
            db.add_message("a" * 96, "", None, "100% sicher")
            db.add_message("b" * 96, "", None, "nichts dergleichen")

        assert _ids(search(path, SearchQuery(subject="100%"))) == ["a"]
        assert _ids(search(path, SearchQuery(subject="%"))) == ["a"]


class TestResults:
    def test_a_message_is_one_row_however_many_recipients(self, tmp_path):
        """The view fans out over addresses; a search must not."""
        path = tmp_path / "index.db"
        with index_db.IndexDatabase(path) as db:
            msg = db.add_message("a" * 96, "", None, "Rundmail")
            db.add_message_sender(msg, "from@example.com")
            db.add_message_recipients(msg, "one@example.com", "two@example.com")
            db.add_message_location(msg, "job", "INBOX")
            db.add_message_location(msg, "job", "Archiv")

        hits = search(path, SearchQuery())

        assert len(hits) == 1
        assert sorted(hits[0].places) == ["job::Archiv", "job::INBOX"]

    def test_the_oldest_comes_first_and_the_undated_last(self, db_path):
        assert [hit.store_id[0] for hit in search(db_path, SearchQuery())] == ["a", "b", "c"]

    def test_the_limit_stops_the_list(self, db_path):
        assert len(search(db_path, SearchQuery(limit=2))) == 2

    def test_the_full_message_id_comes_back(self, db_path):
        """What `archive export` takes -- the whole point of the two commands."""
        (hit,) = search(db_path, SearchQuery(subject="4711"))

        assert hit.store_id == "a" * 96
