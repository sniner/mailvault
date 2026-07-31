"""Tests for the backend-agnostic mailbox session factory."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from mailvault import conf
from mailvault.backend import session


def _make_job(**overrides: Any) -> conf.JobConfig:
    defaults: dict[str, Any] = dict(name="job", server="imap.example.com", username="user")
    defaults.update(overrides)
    return conf.JobConfig(**defaults)


class TestCreateClient:
    def test_imap_backend_connects(self):
        job = _make_job(backend="imap")
        with patch.object(session, "ImapClient") as imap_cls:
            client = session.create_client(job)
        imap_cls.connect.assert_called_once_with(job)
        assert client is imap_cls.connect.return_value

    def test_msgraph_backend_is_instantiated(self):
        job = _make_job(
            backend="msgraph",
            tenant_id="t",
            client_id="c",
            client_secret="s",
            username="user@example.com",
        )
        with patch.object(session, "MSGraphClient") as graph_cls:
            client = session.create_client(job)
        graph_cls.assert_called_once_with(job)
        assert client is graph_cls.return_value

    def test_unknown_backend_raises(self):
        job = _make_job(backend="pigeon")
        with pytest.raises(conf.ConfigError, match="unknown backend"):
            session.create_client(job)

    def test_missing_graph_credentials_raise(self):
        job = _make_job(backend="msgraph", username="user@example.com")
        with pytest.raises(conf.ConfigError, match="requires"):
            session.create_client(job)


class TestOpenMailbox:
    def test_closes_client_on_exit(self):
        job = _make_job(backend="imap")
        client = MagicMock()
        with patch.object(session, "ImapClient") as imap_cls:
            imap_cls.connect.return_value = client
            with session.open_mailbox(job) as mb:
                assert mb is client
            client.close.assert_called_once()

    def test_closes_client_even_on_error(self):
        job = _make_job(backend="imap")
        client = MagicMock()
        with patch.object(session, "ImapClient") as imap_cls:
            imap_cls.connect.return_value = client
            with pytest.raises(ValueError):
                with session.open_mailbox(job):
                    raise ValueError("boom")
            client.close.assert_called_once()
