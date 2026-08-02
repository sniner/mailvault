"""Tests for the zstd facade."""

from __future__ import annotations

import io

import pytest

from mailvault.store import zstd


def test_a_backend_is_available_in_the_test_environment():
    assert zstd.available() is True
    zstd.require()  # does not raise


def test_writer_reader_round_trip_over_file_objects():
    data = b"header line\r\n\r\n" + b"body payload " * 500

    buf = io.BytesIO()
    with zstd.open_writer(buf) as writer:
        writer.write(data)
    # The facade must not close the caller's file object.
    assert not buf.closed

    buf.seek(0)
    with zstd.open_reader(buf) as reader:
        assert reader.read() == data


def test_reader_streams_in_chunks():
    data = b"abcdefgh" * 100
    comp = io.BytesIO()
    zstd.compress_stream(io.BytesIO(data), comp)

    comp.seek(0)
    with zstd.open_reader(comp) as reader:
        first = reader.read(4)
        rest = reader.read()
    assert first == b"abcd"
    assert first + rest == data


def test_compress_decompress_stream_round_trip_and_actually_shrinks():
    data = b"very compressible " * 1000
    comp = io.BytesIO()
    zstd.compress_stream(io.BytesIO(data), comp)
    assert 0 < comp.tell() < len(data)

    comp.seek(0)
    out = io.BytesIO()
    zstd.decompress_stream(comp, out)
    assert out.getvalue() == data


def test_require_raises_a_clear_error_when_no_backend(monkeypatch):
    monkeypatch.setattr(zstd, "_BACKEND", None)
    assert zstd.available() is False
    with pytest.raises(RuntimeError, match="zstd"):
        zstd.require()
