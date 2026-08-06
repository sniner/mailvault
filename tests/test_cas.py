import io
import os
import pathlib
import time

import pytest

from mailvault.store import cas


def test_cas_init_directory(tmp_path):
    _ = cas.ContentAddressedStorage(root_dir=tmp_path / "cas")
    assert (tmp_path / "cas").exists()


def test_cas_add_bytes(tmp_path):
    store = cas.ContentAddressedStorage(root_dir=tmp_path / "cas")
    status, hashval, path = store.add(b"hello world")
    assert status == "NEW"
    assert path.exists()
    assert path.read_bytes() == b"hello world"

    # Adding the same should return EXISTS
    status2, hashval2, path2 = store.add(b"hello world")
    assert status2 == "EXISTS"
    assert hashval == hashval2
    assert path == path2


def test_cas_add_file_object(tmp_path):
    store = cas.ContentAddressedStorage(root_dir=tmp_path / "cas")
    data = b"file object data"
    reader = io.BytesIO(data)
    status, hashval, path = store.add(reader)
    assert status == "NEW"
    assert path.read_bytes() == data

    # Same content via bytes should be EXISTS
    status2, _, _ = store.add(data)
    assert status2 == "EXISTS"


def test_cas_locate(tmp_path):
    store = cas.ContentAddressedStorage(root_dir=tmp_path / "cas")
    data = b"find me"
    store.add(data)

    path = store.locate(data)
    assert path is not None
    assert path.exists()

    # locate without existing file but exists=True
    path_missing = store.locate(b"missing", exists=True)
    assert path_missing is None

    path_uncheck = store.locate(b"missing", exists=False)
    assert path_uncheck is not None
    assert not path_uncheck.exists()


def test_cas_locate_by_hashval(tmp_path):
    store = cas.ContentAddressedStorage(root_dir=tmp_path / "cas")
    _, hashval, stored_path = store.add(b"locate by hash")
    found = store.locate(hashval, exists=True)
    assert found == stored_path


def test_cas_walk(tmp_path):
    store = cas.ContentAddressedStorage(root_dir=tmp_path / "cas")
    store.add(b"file1")
    store.add(b"file2")

    files = list(store.walk())
    assert len(files) == 2


def test_cas_prune_empty_dirs(tmp_path):
    store = cas.ContentAddressedStorage(root_dir=tmp_path / "cas")
    _, _, kept = store.add(b"keep me")
    _, _, gone = store.add(b"remove me")
    gone.unlink()

    removed = store.prune_empty_dirs()

    assert removed == store.depth  # every level of that entry's shard
    assert not gone.parent.exists()
    assert kept.exists()  # the shard that still holds something is untouched


def test_cas_prune_empty_dirs_keeps_the_root(tmp_path):
    store = cas.ContentAddressedStorage(root_dir=tmp_path / "cas")
    _, _, path = store.add(b"lonely")
    path.unlink()

    store.prune_empty_dirs()

    assert (tmp_path / "cas").is_dir()
    assert not list((tmp_path / "cas").iterdir())


def test_cas_suffix_default(tmp_path):
    store = cas.ContentAddressedStorage(root_dir=tmp_path / "cas")
    assert store.suffix == ".dat"


def test_cas_suffix_custom(tmp_path):
    store = cas.ContentAddressedStorage(root_dir=tmp_path / "cas", suffix=".eml")
    assert store.suffix == ".eml"
    _, _, path = store.add(b"email content")
    assert path.suffix == ".eml"


def test_cas_suffix_without_dot(tmp_path):
    store = cas.ContentAddressedStorage(root_dir=tmp_path / "cas", suffix="eml")
    assert store.suffix == ".eml"


def test_cas_depth(tmp_path):
    store = cas.ContentAddressedStorage(root_dir=tmp_path / "cas", depth=3)
    _, hashval, path = store.add(b"depth test")
    # With depth=3, path should have 3 two-char subdirectories
    relative = path.relative_to(tmp_path / "cas")
    # e.g. ab/cd/ef/abcdef....dat
    assert len(relative.parts) == 4  # 3 subdirs + filename


def test_cas_depth_zero(tmp_path):
    store = cas.ContentAddressedStorage(root_dir=tmp_path / "cas", depth=0)
    _, _, path = store.add(b"flat storage")
    relative = path.relative_to(tmp_path / "cas")
    assert len(relative.parts) == 1  # just the filename


def test_cas_reader_rejects_invalid_type(tmp_path):
    store = cas.ContentAddressedStorage(root_dir=tmp_path / "cas")
    with pytest.raises(TypeError):
        store.add("not bytes or io")  # type: ignore


# ---------------------------------------------------------------------------
# Compression (zstd)
# ---------------------------------------------------------------------------


def test_cas_compress_add(tmp_path):
    store = cas.ContentAddressedStorage(root_dir=tmp_path / "cas", suffix=".eml", compress=True)
    data = b"compressible " * 100
    status, hashval, path = store.add(data)
    assert status == "NEW"
    assert path.exists()
    assert path.name.endswith(".eml.zst")
    # Compressed file should be smaller
    assert path.stat().st_size < len(data)


def test_cas_compress_add_survives_the_flush(tmp_path):
    """A compressed entry is flushed through its compressor and still readable.

    The compressor is closed while the file underneath it is still open, so the
    frame it writes is covered by the flush and the fsync that follow.
    """
    store = cas.ContentAddressedStorage(root_dir=tmp_path / "cas", suffix=".eml", compress=True)
    data = b"compressible " * 100
    status, _hashval, path = store.add(data)
    assert status == "NEW"
    assert path.name.endswith(".eml.zst")
    assert store.read(path) == data


def test_cas_compress_read(tmp_path):
    store = cas.ContentAddressedStorage(root_dir=tmp_path / "cas", suffix=".eml", compress=True)
    data = b"read me back compressed"
    _, _, path = store.add(data)
    assert store.read(path) == data


def test_cas_compress_duplicate(tmp_path):
    store = cas.ContentAddressedStorage(root_dir=tmp_path / "cas", suffix=".eml", compress=True)
    data = b"duplicate test"
    status1, hash1, _ = store.add(data)
    status2, hash2, _ = store.add(data)
    assert status1 == "NEW"
    assert status2 == "EXISTS"
    assert hash1 == hash2


def test_cas_compress_locate(tmp_path):
    store = cas.ContentAddressedStorage(root_dir=tmp_path / "cas", suffix=".eml", compress=True)
    data = b"locate compressed"
    _, hashval, stored_path = store.add(data)
    found = store.locate(hashval, exists=True)
    assert found == stored_path


def test_cas_compress_walk(tmp_path):
    store = cas.ContentAddressedStorage(root_dir=tmp_path / "cas", suffix=".eml", compress=True)
    store.add(b"file1")
    store.add(b"file2")
    files = list(store.walk())
    assert len(files) == 2
    assert all(f.name.endswith(".eml.zst") for f in files)


def test_cas_read_uncompressed(tmp_path):
    store = cas.ContentAddressedStorage(root_dir=tmp_path / "cas", suffix=".eml")
    data = b"plain text email"
    _, _, path = store.add(data)
    assert store.read(path) == data


def test_cas_mixed_find_existing(tmp_path):
    """Adding uncompressed, then trying to add compressed -> EXISTS."""
    store_plain = cas.ContentAddressedStorage(
        root_dir=tmp_path / "cas", suffix=".eml", compress=False
    )
    data = b"mixed mode test"
    status1, hash1, path1 = store_plain.add(data)
    assert status1 == "NEW"
    assert path1.name.endswith(".eml")

    # Same data, compressed store -> should find existing uncompressed file
    store_zst = cas.ContentAddressedStorage(
        root_dir=tmp_path / "cas", suffix=".eml", compress=True
    )
    status2, hash2, path2 = store_zst.add(data)
    assert status2 == "EXISTS"
    assert hash1 == hash2
    assert path2 == path1  # returns the existing uncompressed path


def test_cas_locate_finds_compressed_from_plain(tmp_path):
    """A plain-mode store can locate a compressed file."""
    store_zst = cas.ContentAddressedStorage(
        root_dir=tmp_path / "cas", suffix=".eml", compress=True
    )
    data = b"cross locate"
    _, hashval, _ = store_zst.add(data)

    store_plain = cas.ContentAddressedStorage(
        root_dir=tmp_path / "cas", suffix=".eml", compress=False
    )
    found = store_plain.locate(hashval, exists=True)
    assert found is not None
    assert found.name.endswith(".eml.zst")


def test_cas_walk_mixed(tmp_path):
    """Walk finds both compressed and uncompressed files."""
    store_plain = cas.ContentAddressedStorage(
        root_dir=tmp_path / "cas", suffix=".eml", compress=False
    )
    store_plain.add(b"plain file")

    store_zst = cas.ContentAddressedStorage(
        root_dir=tmp_path / "cas", suffix=".eml", compress=True
    )
    store_zst.add(b"compressed file")

    # Either store should find both
    files = list(store_plain.walk())
    assert len(files) == 2


def test_cas_compress_all(tmp_path):
    store = cas.ContentAddressedStorage(root_dir=tmp_path / "cas", suffix=".eml")
    data1 = b"first email"
    data2 = b"second email"
    store.add(data1)
    store.add(data2)

    result = store.compress_all()
    assert result.converted == 2
    assert result.skipped == 0
    assert result.failed == []

    # All files should now be .zst
    files = list(store.walk())
    assert len(files) == 2
    assert all(f.name.endswith(".eml.zst") for f in files)

    # Content should still be readable
    assert store.read(files[0]) in (data1, data2)
    assert store.read(files[1]) in (data1, data2)


def test_cas_compress_all_skips_compressed(tmp_path):
    store = cas.ContentAddressedStorage(root_dir=tmp_path / "cas", suffix=".eml", compress=True)
    store.add(b"already compressed")

    result = store.compress_all()
    assert result.converted == 0
    assert result.skipped == 1


def test_cas_compress_all_mixed(tmp_path):
    store_plain = cas.ContentAddressedStorage(root_dir=tmp_path / "cas", suffix=".eml")
    store_plain.add(b"plain file")

    store_zst = cas.ContentAddressedStorage(
        root_dir=tmp_path / "cas", suffix=".eml", compress=True
    )
    store_zst.add(b"compressed file")

    result = store_plain.compress_all()
    assert result.converted == 1
    assert result.skipped == 1


def test_cas_decompress_all(tmp_path):
    store = cas.ContentAddressedStorage(root_dir=tmp_path / "cas", suffix=".eml", compress=True)
    data1 = b"first email"
    data2 = b"second email"
    store.add(data1)
    store.add(data2)

    result = store.decompress_all()
    assert result.converted == 2
    assert result.skipped == 0

    files = list(store.walk())
    assert len(files) == 2
    assert all(f.name.endswith(".eml") and not f.name.endswith(".zst") for f in files)

    # Content should still be readable
    assert store.read(files[0]) in (data1, data2)
    assert store.read(files[1]) in (data1, data2)


def test_cas_decompress_all_skips_plain(tmp_path):
    store = cas.ContentAddressedStorage(root_dir=tmp_path / "cas", suffix=".eml")
    store.add(b"already plain")

    result = store.decompress_all()
    assert result.converted == 0
    assert result.skipped == 1


# ---------------------------------------------------------------------------
# What a name is allowed to be
# ---------------------------------------------------------------------------


def test_cas_locate_rejects_a_hash_that_is_not_one(tmp_path):
    """A store id that is not a hash must not become a path.

    `..` cut into shard components climbs out of the store, and an absolute
    component would leave it entirely -- so a mix-up has to be caught before a
    path is derived from it, not after.
    """
    store = cas.ContentAddressedStorage(root_dir=tmp_path / "cas", suffix=".eml")
    for bogus in ("../../etc/passwd", "/etc/passwd", "", "not-a-hash", "abcdef.."):
        with pytest.raises(ValueError):
            store.locate(bogus)


def test_cas_locate_accepts_an_uppercase_hash(tmp_path):
    """Entries are named in lowercase, so a hash from elsewhere is folded to it."""
    store = cas.ContentAddressedStorage(root_dir=tmp_path / "cas", suffix=".eml")
    _, hashval, path = store.add(b"case test")

    assert store.locate(hashval.upper(), exists=True) == path


def test_cas_depth_deeper_than_the_hash_is_refused(tmp_path):
    """A depth that no digest can be cut into fails at construction, not on write."""
    with pytest.raises(ValueError):
        cas.ContentAddressedStorage(root_dir=tmp_path / "cas", depth=49)  # sha384 is 96 chars
    with pytest.raises(ValueError):
        cas.ContentAddressedStorage(root_dir=tmp_path / "cas", depth=-1)
    # The deepest a sha384 store can shard is still fine.
    cas.ContentAddressedStorage(root_dir=tmp_path / "deep", depth=48)


def test_cas_walk_skips_what_is_not_an_entry(tmp_path):
    """Only files named after a hash are entries -- `create-db` trusts that."""
    store = cas.ContentAddressedStorage(root_dir=tmp_path / "cas", suffix=".eml")
    _, _, path = store.add(b"a real message")

    (store.root_dir / ".DS_Store").write_bytes(b"junk")
    (path.parent / "Re Fwd holiday photos.eml").write_bytes(b"copied in by hand")
    (path.parent / "not-hex-at-all.eml").write_bytes(b"nor this")
    (path.parent / (path.name + ".12345-0._tmp_")).write_bytes(b"interrupted run")

    assert list(store.walk()) == [path]


def test_cas_hashval_of_reads_the_name_back(tmp_path):
    store = cas.ContentAddressedStorage(root_dir=tmp_path / "cas", suffix=".eml")
    _, hashval, path = store.add(b"name me")
    zst = cas.ContentAddressedStorage(root_dir=tmp_path / "cas", suffix=".eml", compress=True)
    _, zhashval, zpath = zst.add(b"and me, compressed")

    assert store.hashval_of(path) == hashval
    assert store.hashval_of(zpath) == zhashval
    assert store.hashval_of(path.with_name("notes.eml")) is None
    assert store.hashval_of(path.with_suffix(".txt")) is None


# ---------------------------------------------------------------------------
# Integrity
# ---------------------------------------------------------------------------


def test_cas_verify(tmp_path):
    store = cas.ContentAddressedStorage(root_dir=tmp_path / "cas", suffix=".eml")
    _, _, path = store.add(b"original content")
    assert store.verify(path)

    path.write_bytes(b"tampered with")
    assert not store.verify(path)


def test_cas_verify_compressed(tmp_path):
    """The name is the hash of the content, not of the compressed file."""
    store = cas.ContentAddressedStorage(root_dir=tmp_path / "cas", suffix=".eml", compress=True)
    _, _, path = store.add(b"compressible " * 100)

    assert store.verify(path)


def test_cas_verify_needs_an_entry(tmp_path):
    store = cas.ContentAddressedStorage(root_dir=tmp_path / "cas", suffix=".eml")
    stray = store.root_dir / "notes.eml"
    stray.write_bytes(b"by hand")

    with pytest.raises(ValueError):
        store.verify(stray)


def test_cas_hashval_matches_what_add_stores_under(tmp_path):
    store = cas.ContentAddressedStorage(root_dir=tmp_path / "cas", suffix=".eml")
    _, hashval, _ = store.add(b"hash me")

    assert store.hashval(b"hash me") == hashval


# ---------------------------------------------------------------------------
# Writing: what an interrupted or concurrent run leaves behind
# ---------------------------------------------------------------------------


def test_cas_failed_write_leaves_nothing_behind(tmp_path):
    """A write that fails halfway must leave neither an entry nor a leftover.

    The content is hashed first and written second, so the interesting failure
    is the one in the second pass: by then the destination name is known and a
    file is already open under it.
    """

    class FailsAfterHashing(io.BytesIO):
        """Readable once -- for the hash -- and broken from the rewind on."""

        def __init__(self, data: bytes):
            super().__init__(data)
            self.rewinds = 0

        def seek(self, *args, **kwargs):
            self.rewinds += 1
            return super().seek(*args, **kwargs)

        def read(self, size=-1, /):
            # 1: the store rewinds before hashing, 2: it rewinds after.
            if self.rewinds >= 2:
                raise OSError("the network went away")
            return super().read(size)

    store = cas.ContentAddressedStorage(root_dir=tmp_path / "cas", suffix=".eml")
    source = FailsAfterHashing(b"a message that gets cut off")

    with pytest.raises(OSError):
        store.add(source)

    assert source.rewinds >= 2, "the failure has to happen while writing, not while hashing"
    assert [p for p in store.root_dir.rglob("*") if p.is_file()] == []
    assert store.locate(store.hashval(b"a message that gets cut off"), exists=True) is None


def test_cas_transient_files_do_not_collide(tmp_path, monkeypatch):
    """Two writers storing the same message must not share a transient file.

    They race for the same destination name by design -- that is what makes the
    store deduplicate. The transient file is the one thing that must be theirs
    alone, or one writer's blocks land in the other's file and the mixture gets
    renamed into place.
    """
    store = cas.ContentAddressedStorage(root_dir=tmp_path / "cas", suffix=".eml")
    destination = store.root_dir / "aa" / "aabbcc.eml"
    destination.parent.mkdir(parents=True)

    with cas._writing_to(destination) as first:
        with cas._writing_to(destination) as second:
            first.write(b"one writer")
            second.write(b"another writer")
            assert first.name != second.name

    assert destination.read_bytes() == b"one writer"


def test_cas_write_is_not_seen_before_it_is_complete(tmp_path):
    """Nothing appears under the entry's name until the whole entry is there."""
    store = cas.ContentAddressedStorage(root_dir=tmp_path / "cas", suffix=".eml")
    destination = store.root_dir / "aa" / "aabbcc.eml"

    with cas._writing_to(destination) as f:
        f.write(b"half a message")
        assert not destination.exists()

    assert destination.read_bytes() == b"half a message"


def test_cas_content_is_on_the_device_before_the_entry_has_a_name(tmp_path, monkeypatch):
    """The content is flushed before the rename, the directory entry after it.

    Both halves matter, and so does the order. A rename that overtakes the
    content can publish a file whose bytes are not the ones its name claims --
    which nothing would ever notice, because the store answers "is it there?"
    by looking at names. A directory entry that never reaches the device leaves
    the bytes behind with nothing pointing at them.

    Asserted through what the two calls can see: when the content is flushed
    the entry must not exist yet, and when the directory is flushed it must.
    """
    destination = tmp_path / "cas" / "aa" / "aabbcc.eml"
    seen: list[str] = []

    real_fsync = os.fsync

    def recording_fsync(fd):
        seen.append(f"content, entry exists: {destination.exists()}")
        return real_fsync(fd)

    monkeypatch.setattr(cas.os, "fsync", recording_fsync)
    monkeypatch.setattr(
        cas.atomic,
        "sync_directory",
        lambda path: seen.append(f"directory, entry exists: {destination.exists()}"),
    )

    with cas._writing_to(destination) as f:
        f.write(b"a whole message")

    assert seen == ["content, entry exists: False", "directory, entry exists: True"]


def test_cas_prune_transient_files_removes_only_old_leftovers(tmp_path):
    """An interrupted write leaves a file nothing else would ever remove.

    Age is what separates it from one a writer still has open -- the pid in the
    name cannot, because an archive is reachable from more than one host.
    """
    store = cas.ContentAddressedStorage(root_dir=tmp_path / "cas", suffix=".eml")
    _status, _hashval, entry = store.add(b"a message that made it")
    stale = entry.with_name(f"{entry.name}.4711-0{cas.TEMP_SUFFIX}")
    fresh = entry.with_name(f"{entry.name}.4711-1{cas.TEMP_SUFFIX}")
    for path in (stale, fresh):
        path.write_bytes(b"half a message")
    os.utime(stale, (0, time.time() - cas.TRANSIENT_MIN_AGE - 60))

    assert store.prune_transient_files() == 1

    assert not stale.exists()
    assert fresh.exists()
    assert store.read(entry) == b"a message that made it"


def test_cas_prune_transient_files_leaves_other_writers_alone(tmp_path):
    """The store removes what it wrote, not everything with the suffix.

    `state.json` and `index.db` are replaced through a transient file of their
    own, and they sit in the same directory tree.
    """
    store = cas.ContentAddressedStorage(root_dir=tmp_path / "cas", suffix=".eml")
    long_ago = (0, time.time() - cas.TRANSIENT_MIN_AGE - 60)
    strangers = [
        store.root_dir / f"index.db{cas.TEMP_SUFFIX}",
        store.root_dir / f"state.json{cas.TEMP_SUFFIX}",
        store.root_dir / f"notes.eml.1-0{cas.TEMP_SUFFIX}",
    ]
    for path in strangers:
        path.write_bytes(b"not mine")
        os.utime(path, long_ago)

    assert store.prune_transient_files() == 0
    assert all(path.exists() for path in strangers)


def test_cas_a_failed_write_syncs_nothing(tmp_path, monkeypatch):
    """A write that never produced an entry has no directory entry to flush."""
    destination = tmp_path / "cas" / "aa" / "aabbcc.eml"
    synced: list[pathlib.Path] = []
    monkeypatch.setattr(cas.atomic, "sync_directory", synced.append)

    with pytest.raises(OSError), cas._writing_to(destination) as f:
        f.write(b"half a message")
        raise OSError("the network went away")

    assert synced == []
    assert not destination.exists()
    assert [p for p in (tmp_path / "cas").rglob("*") if p.is_file()] == []


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def test_cas_read_header_stops_at_the_blank_line(tmp_path):
    store = cas.ContentAddressedStorage(root_dir=tmp_path / "cas", suffix=".eml")
    _, _, path = store.add(b"From: someone\r\nSubject: hi\r\n\r\nbody\r\n\r\nmore")

    assert store.read_header(path) == b"From: someone\r\nSubject: hi\r\n\r\n"


def test_cas_read_header_honours_the_limit_exactly(tmp_path):
    """Content with no blank line is cut at the limit, not at a block boundary."""
    store = cas.ContentAddressedStorage(root_dir=tmp_path / "cas", suffix=".eml")
    _, _, path = store.add(b"no separator anywhere " * 5000)

    assert store.read_header(path, limit=10) == b"no separat"
    assert store.read_header(path, limit=0) == b""


def test_cas_read_header_of_a_compressed_entry(tmp_path):
    store = cas.ContentAddressedStorage(root_dir=tmp_path / "cas", suffix=".eml", compress=True)
    _, _, path = store.add(b"From: someone\r\n\r\n" + b"attachment" * 100_000)

    assert store.read_header(path) == b"From: someone\r\n\r\n"


def test_cas_conversion_failure_is_reported_not_swallowed(tmp_path):
    """A pass keeps going, but the caller learns which entries it left behind."""
    store = cas.ContentAddressedStorage(root_dir=tmp_path / "cas", suffix=".eml", compress=True)
    _, _, good = store.add(b"a real entry")
    _, _, broken = store.add(b"about to be corrupted")
    broken.write_bytes(b"this is not a zstd frame")

    result = store.decompress_all()

    assert result.converted == 1
    assert result.failed == [broken]
    assert broken.exists(), "a file that could not be converted is left as it is"
    assert store.read(good.with_suffix("")) == b"a real entry"
