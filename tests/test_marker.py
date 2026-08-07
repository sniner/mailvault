"""Tests for the `FORMAT` mark: what an archive says about its own layout."""

from __future__ import annotations

import pytest

from mailvault.store import atomic, marker


class TestReading:
    def test_an_archive_without_one_is_generation_zero(self, tmp_path):
        """No file and a file saying `0` would mean almost the same thing.

        One rule instead of two: counting starts at 1, and the absence of a mark
        is the layout as it was before there was one.
        """
        assert marker.read(tmp_path) == marker.UNMARKED
        assert marker.UNMARKED == 0

    def test_what_was_written_comes_back(self, tmp_path):
        marker.write(tmp_path)

        assert marker.read(tmp_path) == marker.CURRENT_FORMAT

    def test_the_line_is_readable_without_this_code(self, tmp_path):
        """A `cat` five years from now has to answer the question by itself."""
        marker.write(tmp_path)

        assert (tmp_path / "FORMAT").read_text(encoding="utf-8") == (
            "mailvault archive format 1\n"
        )

    def test_surrounding_whitespace_does_not_matter(self, tmp_path):
        atomic.write_text(tmp_path / marker.FORMAT_NAME, "  mailvault archive format 1  \n\n")

        assert marker.read(tmp_path) == 1


class TestWhatItRefuses:
    """Guessing is what this file exists to stop, so it never falls back to it."""

    @pytest.mark.parametrize(
        "content",
        ["1", "VERSION 1", "mailvault archive format", "mailvault archive format one", ""],
    )
    def test_a_mark_that_says_something_else(self, tmp_path, content):
        atomic.write_text(tmp_path / marker.FORMAT_NAME, content)

        with pytest.raises(marker.FormatError, match="does not say what it should say"):
            marker.read(tmp_path)

    def test_a_generation_from_the_future_names_the_way_out(self, tmp_path):
        """A number on its own leaves a reader with no move to make."""
        marker.write(tmp_path, marker.CURRENT_FORMAT + 1)

        with pytest.raises(marker.FormatError, match="Upgrade mailvault"):
            marker.check_readable(tmp_path)

    def test_the_current_generation_passes(self, tmp_path):
        marker.write(tmp_path)

        assert marker.check_readable(tmp_path) == marker.CURRENT_FORMAT

    def test_an_older_generation_passes_too(self, tmp_path):
        """It is what a migration is for -- refusing it would leave no way up."""
        assert marker.check_readable(tmp_path) == marker.UNMARKED


def test_writing_replaces_an_earlier_mark(tmp_path):
    marker.write(tmp_path, 1)
    marker.write(tmp_path, 2)

    assert marker.read(tmp_path) == 2
    assert [p.name for p in tmp_path.iterdir()] == [marker.FORMAT_NAME]
