from __future__ import annotations

import collections.abc
import logging
import os
import pathlib

from mailvault import cas, mailutils

log = logging.getLogger(__name__)

# Suffixes an archived email can carry: plain and zstd-compressed.
_EML_SUFFIXES = (".eml", ".eml.zst")


def _read_eml(path: pathlib.Path) -> bytes:
    """Read an archived email, decompressing `.zst` files transparently."""
    if path.suffix == ".zst":
        import zstandard

        dctx = zstandard.ZstdDecompressor()
        with open(path, "rb") as f:
            with dctx.stream_reader(f) as reader:
                return reader.read()
    return path.read_bytes()


class MailArchive:
    def __init__(self, root_dir: pathlib.Path):
        self.root_dir = root_dir

    def walk(self) -> collections.abc.Generator[pathlib.Path, None, None]:
        """Yield paths to all archived emails (plain and zstd-compressed)."""
        for path, _, files in os.walk(self.root_dir):
            for f in files:
                if f.endswith(_EML_SUFFIXES):
                    yield pathlib.Path(path, f)

    def archive_to_cas(self, store: cas.ContentAddressedStorage, move: bool = False) -> None:
        """Import all emails into the content-addressed store."""
        for eml in self.walk():
            try:
                result, uid, _ = store.add(_read_eml(eml))
            except Exception as exc:
                log.error("Error adding %s to store: %s", eml, exc)
                continue
            else:
                log.info("%s: %s: %s", eml, result, uid)
                if move:
                    eml.unlink()
                    log.debug("%s: file deleted", eml)

    def addresses(self) -> collections.abc.Generator[tuple[str, str], None, None]:
        """Yield unique sender/recipient addresses from all emails in the archive."""
        addrs = set()
        for eml in self.walk():
            from_addr, to_addr = mailutils.addresses(
                mailutils.decode_email_header(_read_eml(eml))
            )
            for addr in from_addr:
                if addr not in addrs:
                    addrs.add(addr)
                    yield "<", addr
            for addr in to_addr:
                if addr not in addrs:
                    addrs.add(addr)
                    yield ">", addr

    def stats(self) -> tuple[int, int]:
        """Return (count, total_size_in_bytes) for all emails in the archive."""
        size = 0
        count = 0
        for eml in self.walk():
            count += 1
            size += eml.stat().st_size
        return count, size


class DocuwareMailArchive(MailArchive):
    def walk(self) -> collections.abc.Generator[pathlib.Path, None, None]:
        """Yield paths to .eml files in a Docuware archive (one per directory, largest wins)."""
        for path, _, files in os.walk(self.root_dir):
            eml = [pathlib.Path(path, f) for f in files if f.endswith(".eml")]
            if len(eml) > 1:
                eml_file = max([(f.stat().st_size, f) for f in eml], key=lambda x: x[0])[1]
            elif len(eml) == 1:
                eml_file = eml[0]
            else:
                continue
            yield eml_file
