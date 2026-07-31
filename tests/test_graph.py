"""Tests for the MS Graph backend, mainly its retry behaviour."""

from __future__ import annotations

import dataclasses
from datetime import UTC
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from mailvault import graph


@dataclasses.dataclass
class _Harness:
    """An MSGraphClient wired to mocks, plus the mocks worth asserting on."""

    client: Any
    request: MagicMock
    refresh: MagicMock


def _make_client(monkeypatch, responses: list, max_retries: int = 3) -> _Harness:
    """Build an MSGraphClient without touching MSAL or the network.

    `responses` is handed to the mocked request(); list items may be Response
    objects or exceptions to raise.
    """
    client = graph.MSGraphClient.__new__(graph.MSGraphClient)
    request = MagicMock(side_effect=responses)
    refresh = MagicMock()
    http = MagicMock()
    http.request = request
    client._http = http
    client.max_retries = max_retries
    client._refresh_auth = refresh
    monkeypatch.setattr(graph.time, "sleep", lambda _: None)
    return _Harness(client=client, request=request, refresh=refresh)


# ---------------------------------------------------------------------------
# Backoff calculation
# ---------------------------------------------------------------------------


class TestBackoff:
    def test_delay_grows_exponentially(self):
        delays = [graph._backoff_delay(i) for i in range(4)]
        assert delays == [2.0, 4.0, 8.0, 16.0]

    def test_delay_is_capped(self):
        assert graph._backoff_delay(20) == graph.RETRY_MAX_DELAY

    def test_retry_after_header_wins(self):
        resp = httpx.Response(429, headers={"Retry-After": "7"})
        assert graph._retry_delay(resp, 0) == 7.0


class TestParseDatetime:
    def test_parses_z_suffix(self):
        # Graph reports UTC as `...Z`; the parser normalises it so a single code
        # path handles the timestamp regardless of Python version quirks.

        dt = graph._parse_graph_datetime("2024-01-01T12:00:00Z")
        assert dt is not None
        assert dt.utcoffset() == UTC.utcoffset(None)
        assert (dt.year, dt.month, dt.day, dt.hour) == (2024, 1, 1, 12)

    def test_none_and_empty(self):
        assert graph._parse_graph_datetime(None) is None
        assert graph._parse_graph_datetime("") is None

    def test_retry_after_is_capped(self):
        resp = httpx.Response(429, headers={"Retry-After": "9999"})
        assert graph._retry_delay(resp, 0) == graph.RETRY_MAX_DELAY

    def test_retry_after_date_falls_back_to_backoff(self):
        # Retry-After may also be an HTTP date, which we do not parse.
        resp = httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
        assert graph._retry_delay(resp, 1) == graph._backoff_delay(1)

    def test_no_header_falls_back_to_backoff(self):
        assert graph._retry_delay(httpx.Response(503), 2) == graph._backoff_delay(2)


# ---------------------------------------------------------------------------
# _request
# ---------------------------------------------------------------------------


class TestRequestRetry:
    def test_gateway_timeout_is_retried(self, monkeypatch):
        h = _make_client(
            monkeypatch, [httpx.Response(504), httpx.Response(200, json={"ok": True})]
        )
        resp = h.client._request("GET", "https://example.invalid/msg")
        assert resp.status_code == 200
        assert h.request.call_count == 2

    @pytest.mark.parametrize("status", sorted(graph.RETRY_STATUS))
    def test_all_transient_states_are_retried(self, monkeypatch, status):
        h = _make_client(monkeypatch, [httpx.Response(status), httpx.Response(200)])
        assert h.client._request("GET", "https://example.invalid/msg").status_code == 200
        assert h.request.call_count == 2

    def test_permanent_error_is_not_retried(self, monkeypatch):
        h = _make_client(monkeypatch, [httpx.Response(404), httpx.Response(200)])
        assert h.client._request("GET", "https://example.invalid/msg").status_code == 404
        assert h.request.call_count == 1

    def test_retries_are_limited(self, monkeypatch):
        h = _make_client(monkeypatch, [httpx.Response(504)] * 5, max_retries=2)
        resp = h.client._request("GET", "https://example.invalid/msg")
        # The caller sees the last failure and can raise_for_status() on it.
        assert resp.status_code == 504
        assert h.request.call_count == 3

    def test_no_retries_when_disabled(self, monkeypatch):
        h = _make_client(monkeypatch, [httpx.Response(504)], max_retries=0)
        assert h.client._request("GET", "https://example.invalid/msg").status_code == 504
        assert h.request.call_count == 1

    def test_transport_error_is_retried(self, monkeypatch):
        h = _make_client(monkeypatch, [httpx.ConnectTimeout("timed out"), httpx.Response(200)])
        assert h.client._request("GET", "https://example.invalid/msg").status_code == 200
        assert h.request.call_count == 2

    def test_transport_error_propagates_after_last_attempt(self, monkeypatch):
        h = _make_client(monkeypatch, [httpx.ConnectTimeout("timed out")] * 4, max_retries=2)
        with pytest.raises(httpx.ConnectTimeout):
            h.client._request("GET", "https://example.invalid/msg")
        assert h.request.call_count == 3

    def test_unauthorized_triggers_single_refresh(self, monkeypatch):
        h = _make_client(monkeypatch, [httpx.Response(401), httpx.Response(200)])
        assert h.client._request("GET", "https://example.invalid/msg").status_code == 200
        h.refresh.assert_called_once()

    def test_refresh_does_not_consume_retry_budget(self, monkeypatch):
        # 401 -> refresh, then two retryable failures still fit into max_retries=2.
        h = _make_client(
            monkeypatch,
            [
                httpx.Response(401),
                httpx.Response(503),
                httpx.Response(504),
                httpx.Response(200),
            ],
            max_retries=2,
        )
        assert h.client._request("GET", "https://example.invalid/msg").status_code == 200
        assert h.request.call_count == 4

    def test_repeated_unauthorized_is_returned(self, monkeypatch):
        # A second 401 means the fresh token is not the problem: give up.
        h = _make_client(monkeypatch, [httpx.Response(401), httpx.Response(401)])
        assert h.client._request("GET", "https://example.invalid/msg").status_code == 401
        assert h.request.call_count == 2
