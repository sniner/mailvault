"""Tests for `heads/`: what a place is called, and what survives a damaged file."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from mailvault.store import atomic, heads

WHEN = datetime(2026, 8, 7, 16, 8, 55, tzinfo=UTC)
UID = {"kind": "imap-uid", "uid": 4711}


def _head(job="gmail.com", folder="INBOX", **overrides: Any) -> heads.Head:
    fields: dict[str, Any] = dict(job=job, folder=folder, last_run=WHEN.isoformat(), resume=UID)
    fields.update(overrides)
    return heads.Head(**fields)


def _read(root, job="gmail.com", folder="INBOX") -> heads.Head:
    """The head a test expects to be there, so the failure lands on the assertion."""
    head = heads.read(root, job, folder)
    assert head is not None, f"no head for {job}::{folder}"
    return head


class TestTheName:
    """A readable part that may be cut, and an identity that may not."""

    def test_the_slug_reads_like_the_place_it_came_from(self):
        name = heads.head_name("gmail.com", "[Google Mail]/Alle Nachrichten")

        assert name.startswith("gmail_com-Google_Mail_Alle_Nachrichten.")

    def test_leading_and_trailing_junk_does_not_become_underscores(self):
        """`_Google_Mail__Alle` would be the naive result, and it reads worse."""
        assert heads._slug("[Google Mail]/Alle Nachrichten") == "Google_Mail_Alle_Nachrichten"

    def test_it_ends_in_eight_hex_characters(self):
        _slug, _, identity = heads.head_name("gmail.com", "INBOX").rpartition(".")

        assert len(identity) == 8
        assert int(identity, 16) >= 0

    def test_the_same_place_always_gets_the_same_name(self):
        assert heads.head_name("a", "b") == heads.head_name("a", "b")


class TestPlacesThatWouldCollide:
    """The whole reason there is a hash: the slug alone loses too much."""

    def test_a_dot_and_a_slash_are_different_hierarchy_separators(self):
        """Courier and Dovecot-classic use `.`, everyone else `/`."""
        one = heads.head_name("job", "INBOX/Sent")
        other = heads.head_name("job", "INBOX.Sent")

        assert heads._slug("INBOX/Sent") == heads._slug("INBOX.Sent"), "the slug cannot tell"
        assert one != other, "but the name must"

    def test_a_hyphen_and_a_space_normalise_the_same(self):
        hyphen = heads.head_name("job", "Alte-Projekte")

        assert hyphen != heads.head_name("job", "Alte Projekte")

    def test_the_separator_between_job_and_folder_cannot_be_shifted(self):
        """Without a separator that cannot occur, ("a", "b/c") and ("a/b", "c") agree."""
        assert heads.head_name("a", "b/c") != heads.head_name("a/b", "c")

    def test_case_is_kept_because_imap_folders_are_case_sensitive(self):
        assert heads.head_name("job", "Sent") != heads.head_name("job", "sent")

    def test_the_identity_is_taken_from_the_original_names(self):
        """Over the slug it would agree for exactly the pairs it exists to separate."""
        assert heads._identity("job", "INBOX/Sent") != heads._identity("job", "INBOX.Sent")


class TestAPlaceWithNoJob:
    """What `archive import` writes: a name, and no mailbox behind it."""

    def test_it_is_named_by_its_folder_alone(self):
        assert heads.head_name(None, "docuware-2019").startswith("docuware_2019.")

    def test_it_cannot_be_reached_by_naming_a_job(self):
        """A mailbox called like the personalisation must not land on it."""
        person = heads._NO_JOB.decode()

        assert heads.head_name(None, "INBOX") != heads.head_name(person, "INBOX")
        assert heads._identity(None, "INBOX") != heads._identity(person, "INBOX")
        assert heads._identity(None, "INBOX") != heads._identity("", "INBOX")

    def test_it_is_told_apart_from_a_job_of_that_name(self):
        """Both read `docuware_2019`; only the identity separates them."""
        jobless = heads.head_name(None, "docuware-2019")
        job = heads.head_name("docuware-2019", None)

        assert jobless.rpartition(".")[0] == job.rpartition(".")[0]
        assert jobless != job

    def test_a_place_with_a_job_keeps_the_name_it_has_always_had(self):
        """Existing archives must not have their heads renamed under them.

        The value is the one the module docstring has named since `heads/`
        existed, which is what makes it evidence rather than a copy of whatever
        the code does today.
        """
        assert heads.head_name("gmail.com", "INBOX") == "gmail_com-INBOX.1c0a75d8"

    def test_it_survives_being_written_and_read(self, tmp_path):
        heads.write(tmp_path, heads.Head(job=None, folder="docuware-2019", log="a" * 96))

        head = heads.read(tmp_path, None, "docuware-2019")

        assert head is not None
        assert (head.job, head.folder, head.log) == (None, "docuware-2019", "a" * 96)

    def test_it_is_not_one_of_the_archive_mailboxes(self, tmp_path):
        """The guard asks who has written here, and an import is not an answer."""
        heads.write(tmp_path, heads.Head(job=None, folder="docuware-2019"))
        heads.write(tmp_path, heads.Head(job="gmail.com", folder="INBOX"))

        assert heads.mailboxes(tmp_path) == {"gmail.com"}


class TestNamesAtTheEdges:
    def test_a_place_without_any_alphanumerics_still_gets_a_name(self):
        name = heads.head_name("→", "...")

        assert name.startswith(f"{heads.EMPTY_PART}-{heads.EMPTY_PART}.")

    def test_and_is_still_told_apart_from_another_such_place(self):
        assert heads.head_name("→", "...") != heads.head_name("...", "→")

    def test_only_one_side_empty_keeps_the_two_part_shape(self):
        assert heads.head_name("gmail.com", "→").startswith(f"gmail_com-{heads.EMPTY_PART}.")

    def test_a_very_deep_folder_is_cut_to_the_limit(self):
        deep = "/".join(f"Ordner{n}" for n in range(60))

        name = heads.head_name("example.com", deep)
        slug, _, identity = name.rpartition(".")

        assert len(slug) <= heads.SLUG_LIMIT
        assert len(identity) == 8

    def test_cutting_does_not_leave_a_dangling_separator(self):
        name = heads.head_name("job", "x" * 200)
        slug, _, _identity = name.rpartition(".")

        assert not slug.endswith(("_", "-"))

    def test_two_folders_that_agree_up_to_the_limit_are_still_separated(self):
        """What the cut throws away, the identity keeps."""
        prefix = "Sehr_Langer_Ordnername" * 5
        one = heads.head_name("job", prefix + "eins")
        other = heads.head_name("job", prefix + "zwei")

        assert one.rpartition(".")[0] == other.rpartition(".")[0], "the readable parts agree"
        assert one != other


class TestRoundTrip:
    def test_what_was_written_comes_back(self, tmp_path):
        heads.write(tmp_path, _head())

        got = heads.read(tmp_path, "gmail.com", "INBOX")

        assert got is not None
        assert got.job == "gmail.com"
        assert got.folder == "INBOX"
        assert got.resume == UID
        assert got.last_run_at() == WHEN

    def test_a_place_nobody_has_backed_up_has_no_head(self, tmp_path):
        assert heads.read(tmp_path, "gmail.com", "INBOX") is None

    def test_the_file_lands_under_the_name_of_its_place(self, tmp_path):
        heads.write(tmp_path, _head())

        assert (tmp_path / heads.head_name("gmail.com", "INBOX")).is_file()

    def test_writing_again_replaces_and_leaves_nothing_behind(self, tmp_path):
        heads.write(tmp_path, _head())
        heads.write(tmp_path, _head(resume={"kind": "imap-uid", "uid": 9999}))

        assert _read(tmp_path, "gmail.com", "INBOX").resume == {
            "kind": "imap-uid",
            "uid": 9999,
        }
        assert [p.name for p in tmp_path.iterdir()] == [heads.head_name("gmail.com", "INBOX")]

    def test_a_folder_with_no_resume_point_is_still_a_head(self, tmp_path):
        """A pass that archived nothing has a last_run and nothing to carry on from."""
        heads.write(tmp_path, heads.Head(job="j", folder="f", last_run=WHEN.isoformat()))

        got = heads.read(tmp_path, "j", "f")

        assert got is not None
        assert got.resume is None
        assert got.last_run_at() == WHEN


class TestACollision:
    """Two places under one name: expensive, never wrong."""

    def test_a_head_belonging_to_another_place_counts_as_none(self, tmp_path, caplog):
        path = heads.head_path(tmp_path, "job", "INBOX/Sent")
        atomic.write_text(path, json.dumps(_head(job="job", folder="INBOX.Sent").to_payload()))

        with caplog.at_level(logging.WARNING):
            got = heads.read_file(path)
            asked = heads.read(tmp_path, "job", "INBOX/Sent")

        assert got is not None, "the file itself is perfectly readable"
        assert asked is None, "but it does not answer for the place that was asked about"
        assert "two places share a name" in caplog.text


class TestADamagedHead:
    """Everything in here is recoverable by reading the folder in full."""

    @staticmethod
    def _write_raw(tmp_path, body: str):
        atomic.write_text(heads.head_path(tmp_path, "j", "f"), body)

    def test_junk_instead_of_json(self, tmp_path, caplog):
        self._write_raw(tmp_path, "not json at all")

        assert heads.read(tmp_path, "j", "f") is None
        assert "not valid JSON" in caplog.text

    def test_a_version_from_the_future(self, tmp_path, caplog):
        self._write_raw(tmp_path, json.dumps({"version": 99, "job": "j", "folder": "f"}))

        assert heads.read(tmp_path, "j", "f") is None
        assert "unknown head version" in caplog.text

    def test_a_head_that_does_not_say_which_place_it_is(self, tmp_path, caplog):
        self._write_raw(tmp_path, json.dumps({"version": 1, "last_run": WHEN.isoformat()}))

        assert heads.read(tmp_path, "j", "f") is None
        assert "does not say which place" in caplog.text

    def test_a_resume_point_without_a_kind_costs_only_the_resume_point(self, tmp_path, caplog):
        self._write_raw(
            tmp_path,
            json.dumps(
                {
                    "version": 1,
                    "job": "j",
                    "folder": "f",
                    "last_run": WHEN.isoformat(),
                    "resume": {"uid": 4711},
                }
            ),
        )

        got = heads.read(tmp_path, "j", "f")

        assert got is not None
        assert got.resume is None, "the folder is read in full, which is the safe outcome"
        assert got.last_run_at() == WHEN
        assert "unusable resume point" in caplog.text

    def test_an_unparsable_timestamp_is_not_a_date(self, tmp_path, caplog):
        heads.write(tmp_path, heads.Head(job="j", folder="f", last_run="letzten Dienstag"))

        assert _read(tmp_path, "j", "f").last_run_at() is None
        assert "unparsable timestamp" in caplog.text

    def test_a_naive_timestamp_is_read_as_local_time(self, tmp_path):
        """What an archive written by a version using `datetime.now()` carries."""
        naive = datetime(2026, 8, 7, 16, 8, 55)
        heads.write(tmp_path, heads.Head(job="j", folder="f", last_run=naive.isoformat()))

        got = _read(tmp_path, "j", "f").last_run_at()

        assert got is not None
        assert got.tzinfo is not None
        assert got == naive.astimezone()

    def test_one_damaged_head_costs_one_place(self, tmp_path):
        """The point of the exercise against a single state.json."""
        heads.write(tmp_path, _head(job="a", folder="INBOX"))
        heads.write(tmp_path, _head(job="b", folder="INBOX"))
        heads.head_path(tmp_path, "a", "INBOX").write_text("shredded", encoding="utf-8")

        assert heads.read(tmp_path, "a", "INBOX") is None
        assert heads.read(tmp_path, "b", "INBOX") is not None


class TestEnumeration:
    def test_every_place_is_found(self, tmp_path):
        heads.write(tmp_path, _head(job="gmail.com", folder="INBOX"))
        heads.write(tmp_path, _head(job="gmail.com", folder="Sent"))
        heads.write(tmp_path, _head(job="posteo.de", folder="Sent"))

        assert len(heads.head_files(tmp_path)) == 3
        assert {(h.job, h.folder) for h in heads.read_all(tmp_path)} == {
            ("gmail.com", "INBOX"),
            ("gmail.com", "Sent"),
            ("posteo.de", "Sent"),
        }

    def test_the_mailboxes_are_read_out_of_the_files(self, tmp_path):
        """Not off the names: a slug is lossy and an identity is a hash."""
        heads.write(tmp_path, _head(job="gmail.com", folder="INBOX"))
        heads.write(tmp_path, _head(job="posteo.de", folder="Sent"))

        assert heads.mailboxes(tmp_path) == {"gmail.com", "posteo.de"}

    def test_an_archive_without_heads_names_nobody(self, tmp_path):
        assert heads.mailboxes(tmp_path / "nothing-here") == set()

    def test_the_leftover_of_an_interrupted_write_is_not_a_head(self, tmp_path):
        heads.write(tmp_path, _head())
        leftover = heads.head_path(tmp_path, "gmail.com", "INBOX")
        leftover.with_name(leftover.name + atomic.TEMP_SUFFIX).write_bytes(b"half a head")

        assert heads.head_files(tmp_path) == [leftover]

    def test_an_unreadable_head_does_not_stop_the_others(self, tmp_path):
        heads.write(tmp_path, _head(job="a", folder="f"))
        heads.write(tmp_path, _head(job="b", folder="f"))
        heads.head_path(tmp_path, "a", "f").write_text("{", encoding="utf-8")

        assert {h.job for h in heads.read_all(tmp_path)} == {"b"}
