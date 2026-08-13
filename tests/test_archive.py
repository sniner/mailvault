from __future__ import annotations

import pathlib

import pytest

from mailvault import conf, importer, jobs
from mailvault.jobs import guard
from mailvault.store import cas, heads, metalog


def test_mail_archive_walk(tmp_path, dummy_eml_bytes):
    # Setup test repository
    eml_file = tmp_path / "test.eml"
    eml_file.write_bytes(dummy_eml_bytes)

    # Also create a non-eml file
    dummy_file = tmp_path / "test.txt"
    dummy_file.write_text("Hello")

    arch = importer.ExternalMailArchive(root_dir=tmp_path)
    files = list(arch.walk())

    assert len(files) == 1
    assert files[0] == eml_file


def test_mail_archive_stats(tmp_path, dummy_eml_bytes):
    eml_file = tmp_path / "test.eml"
    eml_file.write_bytes(dummy_eml_bytes)

    arch = importer.ExternalMailArchive(root_dir=tmp_path)
    count, size = arch.stats()

    assert count == 1
    assert size == len(dummy_eml_bytes)


def test_mail_archive_sees_compressed(tmp_path, dummy_eml_bytes):
    # Regression: stats/import must not be blind to zstd-compressed archives
    # written with --compress (files end in .eml.zst).
    store = cas.ContentAddressedStorage(root_dir=tmp_path, suffix=".eml", compress=True)
    store.add(dummy_eml_bytes)

    arch = importer.ExternalMailArchive(root_dir=tmp_path)
    files = list(arch.walk())
    assert len(files) == 1
    assert files[0].name.endswith(".eml.zst")

    count, _ = arch.stats()
    assert count == 1


def test_mail_archive_import_compressed_roundtrip(tmp_path, dummy_eml_bytes):
    # A compressed source archive imported into a plain store must yield the
    # original (decompressed) message content, not the zstd bytes.
    src = tmp_path / "src"
    src.mkdir()
    cas.ContentAddressedStorage(root_dir=src, suffix=".eml", compress=True).add(dummy_eml_bytes)

    dst_dir = tmp_path / "dst"
    dst = cas.ContentAddressedStorage(root_dir=dst_dir, suffix=".eml")
    importer.ExternalMailArchive(root_dir=src).archive_to_cas(dst, move=False)

    imported = list(dst.walk())
    assert len(imported) == 1
    assert dst.read(imported[0]) == dummy_eml_bytes


def test_mail_archive_to_cas(tmp_path, dummy_eml_bytes):
    eml_file = tmp_path / "test.eml"
    eml_file.write_bytes(dummy_eml_bytes)

    cas_dir = tmp_path / "cas"
    store = cas.ContentAddressedStorage(root_dir=cas_dir)

    arch = importer.ExternalMailArchive(root_dir=tmp_path)
    arch.archive_to_cas(store, move=False)

    assert list(store.walk())  # Should have one file
    assert eml_file.exists()  # move=False

    # Test move
    arch.archive_to_cas(store, move=True)
    assert not eml_file.exists()


def test_import_dry_run_writes_nothing_and_counts_both_kinds(tmp_path, dummy_eml_bytes):
    src = tmp_path / "src"
    src.mkdir()
    known = src / "known.eml"
    known.write_bytes(dummy_eml_bytes)
    fresh = src / "fresh.eml"
    fresh.write_bytes(b"From: someone\r\n\r\nnot in the archive yet")

    store = cas.ContentAddressedStorage(root_dir=tmp_path / "cas", suffix=".eml")
    store.add(dummy_eml_bytes)
    arch = importer.ExternalMailArchive(root_dir=src)

    result = arch.archive_to_cas(store, move=True, dry_run=True)

    assert result.dry_run
    assert result.stored == 1, "the one the archive does not have"
    assert result.present == 1, "the one it does"
    assert not result.failed
    assert len(list(store.walk())) == 1, "a dry run writes nothing"
    assert known.exists() and fresh.exists(), "and removes nothing, --move or not"


def test_a_dry_run_predicts_what_the_real_import_then_does(tmp_path, dummy_eml_bytes):
    src = tmp_path / "src"
    src.mkdir()
    (src / "one.eml").write_bytes(dummy_eml_bytes)
    (src / "two.eml").write_bytes(b"From: someone\r\n\r\nanother one")

    store = cas.ContentAddressedStorage(root_dir=tmp_path / "cas", suffix=".eml")
    arch = importer.ExternalMailArchive(root_dir=src)

    predicted = arch.archive_to_cas(store, dry_run=True)
    actual = arch.archive_to_cas(store)

    assert (predicted.stored, predicted.present) == (actual.stored, actual.present)


def test_an_unreadable_message_is_named_rather_than_counted(tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    broken = src / "broken.eml"
    broken.write_bytes(b"whatever")

    store = cas.ContentAddressedStorage(root_dir=tmp_path / "cas", suffix=".eml")
    monkeypatch.setattr(
        importer, "_read_eml", lambda path: (_ for _ in ()).throw(OSError("unreadable"))
    )

    result = importer.ExternalMailArchive(root_dir=src).archive_to_cas(store)

    assert result.failed == [broken]
    assert (result.stored, result.present) == (0, 0)


def _provenance(archive: pathlib.Path, name: str = "docuware-2019") -> importer.Provenance:
    """What the CLI hands the importer: a name and the log to write it in."""
    return importer.Provenance(
        name=name,
        log=metalog.LogWriter(
            archive / metalog.DEFAULT_LOG_DIR, archive / heads.DEFAULT_HEADS_DIR
        ),
    )


def _sources(root: pathlib.Path, count: int) -> list[pathlib.Path]:
    root.mkdir(parents=True, exist_ok=True)
    paths = []
    for number in range(count):
        path = root / f"{number}.eml"
        path.write_bytes(f"From: someone\r\n\r\nmessage {number}".encode())
        paths.append(path)
    return paths


class TestWhatAnImportRecords:
    """Which import a message came from is in no `.eml`, so it is written down."""

    def test_the_name_is_recorded_as_a_place_with_no_mailbox(self, tmp_path):
        archive = tmp_path / "archive"
        _sources(tmp_path / "src", 2)
        store = cas.mail_store(archive)

        importer.ExternalMailArchive(root_dir=tmp_path / "src").archive_to_cas(
            store, provenance=_provenance(archive)
        )

        (path,) = metalog.log_files(archive / metalog.DEFAULT_LOG_DIR)
        logfile = metalog.read_log(path)
        assert logfile is not None
        assert logfile.mailbox is None, "there is no mailbox behind an import"
        assert logfile.folder == "docuware-2019"
        assert len(logfile.store_ids) == 2

    def test_the_place_gets_a_head_and_a_chain(self, tmp_path):
        archive = tmp_path / "archive"
        _sources(tmp_path / "src", 1)

        importer.ExternalMailArchive(root_dir=tmp_path / "src").archive_to_cas(
            cas.mail_store(archive), provenance=_provenance(archive)
        )

        head = heads.read(archive / heads.DEFAULT_HEADS_DIR, None, "docuware-2019")
        assert head is not None
        assert head.job is None
        assert head.resume is None, "there is no server to carry on from"
        (path,) = metalog.log_files(archive / metalog.DEFAULT_LOG_DIR)
        assert head.log == path.name.removesuffix(".jsonl")

    def test_mail_the_archive_already_holds_is_recorded_too(self, tmp_path, dummy_eml_bytes):
        """Importing again under a name is how older imports get their provenance."""
        archive = tmp_path / "archive"
        src = tmp_path / "src"
        src.mkdir()
        (src / "known.eml").write_bytes(dummy_eml_bytes)
        store = cas.mail_store(archive)
        store.add(dummy_eml_bytes)

        result = importer.ExternalMailArchive(root_dir=src).archive_to_cas(
            store, provenance=_provenance(archive)
        )

        assert (result.stored, result.present) == (0, 1)
        assert result.recorded == 1, "that it lay in this import is true either way"

    def test_it_is_written_down_in_batches(self, tmp_path, monkeypatch):
        """A batch is what `--move` may let go of, so it must reach the log first."""
        monkeypatch.setattr(importer, "SEAL_BATCH", 2)
        archive = tmp_path / "archive"
        _sources(tmp_path / "src", 5)

        result = importer.ExternalMailArchive(root_dir=tmp_path / "src").archive_to_cas(
            cas.mail_store(archive), provenance=_provenance(archive)
        )

        assert result.recorded == 5
        files = metalog.log_files(archive / metalog.DEFAULT_LOG_DIR)
        assert len(files) == 3, "two full batches and the remainder"

    def test_the_batches_form_one_chain(self, tmp_path, monkeypatch):
        monkeypatch.setattr(importer, "SEAL_BATCH", 2)
        archive = tmp_path / "archive"
        _sources(tmp_path / "src", 4)

        importer.ExternalMailArchive(root_dir=tmp_path / "src").archive_to_cas(
            cas.mail_store(archive), provenance=_provenance(archive)
        )

        head = heads.read(archive / heads.DEFAULT_HEADS_DIR, None, "docuware-2019")
        assert head is not None
        store = metalog.open_store(archive / metalog.DEFAULT_LOG_DIR)
        walked = 0
        hashval = head.log
        while hashval is not None:
            path = store.locate(hashval)
            assert path is not None, "the chain names a file that is not there"
            logfile = metalog.read_log(path)
            assert logfile is not None
            walked += 1
            hashval = logfile.prev
        assert walked == 2

    def test_a_dry_run_records_nothing(self, tmp_path):
        archive = tmp_path / "archive"
        _sources(tmp_path / "src", 2)

        result = importer.ExternalMailArchive(root_dir=tmp_path / "src").archive_to_cas(
            cas.mail_store(archive), provenance=_provenance(archive), dry_run=True
        )

        assert result.name == "docuware-2019", "it still says what it would be called"
        assert result.recorded == 0
        assert metalog.log_files(archive / metalog.DEFAULT_LOG_DIR) == []
        assert heads.head_files(archive / heads.DEFAULT_HEADS_DIR) == []


class TestMoveWaitsForTheLog:
    """A file whose provenance is not written down must not be the one that goes."""

    def test_sources_go_only_after_the_batch_is_sealed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(importer, "SEAL_BATCH", 2)
        archive = tmp_path / "archive"
        sources = _sources(tmp_path / "src", 3)
        sealed: list[int] = []

        real_seal = metalog.LogWriter.seal

        def seal(self, date):
            # What is on disk at the moment of the seal: the sources of the
            # batch being written must all still be there.
            sealed.append(sum(path.exists() for path in sources))
            return real_seal(self, date)

        monkeypatch.setattr(metalog.LogWriter, "seal", seal)
        importer.ExternalMailArchive(root_dir=tmp_path / "src").archive_to_cas(
            cas.mail_store(archive), provenance=_provenance(archive), move=True
        )

        assert sealed == [3, 1], "nothing removed before its seal, everything after"
        assert not any(path.exists() for path in sources)

    def test_a_failed_seal_leaves_the_sources_alone(self, tmp_path, monkeypatch):
        archive = tmp_path / "archive"
        sources = _sources(tmp_path / "src", 2)

        def refuse(self, date):
            raise OSError("no space left on device")

        monkeypatch.setattr(metalog.LogWriter, "seal", refuse)
        result = importer.ExternalMailArchive(root_dir=tmp_path / "src").archive_to_cas(
            cas.mail_store(archive), provenance=_provenance(archive), move=True
        )

        assert result.recorded == 0
        assert (result.stored, result.present) == (2, 0), "the mail is in the archive"
        assert all(path.exists() for path in sources), "and its source is still there"


class TestAnImportedArchiveIsWholeInItself:
    """The payoff: what an import brings in is no longer an archive full of orphans."""

    def _import(self, tmp_path, count: int = 3) -> pathlib.Path:
        archive = tmp_path / "archive"
        _sources(tmp_path / "src", count)
        importer.ExternalMailArchive(root_dir=tmp_path / "src").archive_to_cas(
            cas.mail_store(archive), provenance=_provenance(archive)
        )
        return archive

    def test_check_finds_no_orphans_and_nothing_wrong(self, tmp_path):
        archive = self._import(tmp_path)

        result = jobs.check(archive)

        assert result.orphans == []
        assert result.referenced == 3
        assert result.places == 1
        assert result.sound, f"findings: {result.missing} {result.broken_chains}"

    def test_the_place_reads_as_its_name(self, tmp_path):
        """`None::docuware-2019` is not a line for people."""
        archive = self._import(tmp_path, count=1)

        result = jobs.check(archive)

        assert list(result.missing.values()) == []
        assert heads.place_name(None, "docuware-2019") == "docuware-2019"

    def test_compact_moves_the_head_with_it(self, tmp_path, monkeypatch):
        """Otherwise the head names a file compact has just removed."""
        monkeypatch.setattr(importer, "SEAL_BATCH", 1)
        archive = self._import(tmp_path)

        metalog.compact(archive / metalog.DEFAULT_LOG_DIR, archive / heads.DEFAULT_HEADS_DIR)
        result = jobs.check(archive)

        assert result.broken_chains == []
        assert result.unchained == []
        assert len(metalog.log_files(archive / metalog.DEFAULT_LOG_DIR)) == 1

    def test_the_guard_still_refuses_a_configuration_it_cannot_check(self, tmp_path):
        """An import records no mailbox, so there is still nothing to compare against."""
        archive = self._import(tmp_path)

        with pytest.raises(jobs.JobError, match="records no mailbox"):
            guard.check_jobs(archive, [conf.JobConfig(name="whoever")])


def test_docuware_archive_walk(tmp_path, dummy_eml_bytes):
    arch_dir = tmp_path / "dw"
    arch_dir.mkdir()

    eml1 = arch_dir / "small.eml"
    eml1.write_bytes(b"small")

    eml2 = arch_dir / "large.eml"
    eml2.write_bytes(dummy_eml_bytes)

    arch = importer.DocuwareMailArchive(root_dir=arch_dir)
    files = list(arch.walk())

    # DocuwareArchive returns the largest .eml file in the directory
    assert len(files) == 1
    assert files[0] == eml2
