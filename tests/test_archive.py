from mailvault import importer
from mailvault.store import cas


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
    # Regression: stats/addresses/import must not be blind to zstd-compressed
    # archives written with --compress (files end in .eml.zst).
    store = cas.ContentAddressedStorage(root_dir=tmp_path, suffix=".eml", compress=True)
    store.add(dummy_eml_bytes)

    arch = importer.ExternalMailArchive(root_dir=tmp_path)
    files = list(arch.walk())
    assert len(files) == 1
    assert files[0].name.endswith(".eml.zst")

    count, _ = arch.stats()
    assert count == 1

    # addresses() must transparently decompress rather than parse zstd bytes.
    addrs = {addr for _, addr in arch.addresses()}
    assert "test@example.com" in addrs


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
