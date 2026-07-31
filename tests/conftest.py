import pytest


@pytest.fixture
def dummy_eml_bytes() -> bytes:
    return b"""From: test@example.com
To: recipient@example.com
Subject: Test Email
Date: Wed, 20 Feb 2026 12:00:00 +0100

This is a test email body.
"""
