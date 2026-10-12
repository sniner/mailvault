import collections.abc

import pytest

from mailvault.store import cas


@pytest.fixture(autouse=True)
def _run_hash() -> collections.abc.Iterator[None]:
    """Put the run's hash back after every test.

    A command sets it from the archive's configuration and nothing sets it back,
    because a run works on one archive. A test suite works on hundreds, and one
    test's sha256 archive must not decide what the next one hashes with.
    """
    yield
    cas.use_hash(cas.LEGACY_HASH)


@pytest.fixture
def dummy_eml_bytes() -> bytes:
    return b"""From: test@example.com
To: recipient@example.com
Subject: Test Email
Date: Wed, 20 Feb 2026 12:00:00 +0100

This is a test email body.
"""
