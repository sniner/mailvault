import io

import pytest

from mailvault import cas


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

    compressed, skipped = store.compress_all()
    assert compressed == 2
    assert skipped == 0

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

    compressed, skipped = store.compress_all()
    assert compressed == 0
    assert skipped == 1


def test_cas_compress_all_mixed(tmp_path):
    store_plain = cas.ContentAddressedStorage(root_dir=tmp_path / "cas", suffix=".eml")
    store_plain.add(b"plain file")

    store_zst = cas.ContentAddressedStorage(
        root_dir=tmp_path / "cas", suffix=".eml", compress=True
    )
    store_zst.add(b"compressed file")

    compressed, skipped = store_plain.compress_all()
    assert compressed == 1
    assert skipped == 1


def test_cas_decompress_all(tmp_path):
    store = cas.ContentAddressedStorage(root_dir=tmp_path / "cas", suffix=".eml", compress=True)
    data1 = b"first email"
    data2 = b"second email"
    store.add(data1)
    store.add(data2)

    decompressed, skipped = store.decompress_all()
    assert decompressed == 2
    assert skipped == 0

    files = list(store.walk())
    assert len(files) == 2
    assert all(f.name.endswith(".eml") and not f.name.endswith(".zst") for f in files)

    # Content should still be readable
    assert store.read(files[0]) in (data1, data2)
    assert store.read(files[1]) in (data1, data2)


def test_cas_decompress_all_skips_plain(tmp_path):
    store = cas.ContentAddressedStorage(root_dir=tmp_path / "cas", suffix=".eml")
    store.add(b"already plain")

    decompressed, skipped = store.decompress_all()
    assert decompressed == 0
    assert skipped == 1
