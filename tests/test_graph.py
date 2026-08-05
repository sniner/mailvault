"""Tests for the MS Graph backend, mainly its retry behaviour."""

from __future__ import annotations

import dataclasses
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from mailvault.backend import base, graph


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
        # Graph reports UTC with a trailing `Z`; fromisoformat parses it
        # directly on Python 3.11+.
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


# ---------------------------------------------------------------------------
# The $filter timestamp
# ---------------------------------------------------------------------------


def _json(status: int, payload: dict) -> httpx.Response:
    """A JSON response that survives raise_for_status(), which needs its request."""
    return httpx.Response(
        status,
        request=httpx.Request("GET", "https://graph.test"),
        json=payload,
    )


class TestDeltaPoint:
    """Reading the resume point back, and refusing everything that is not ours."""

    def test_our_own_point_is_read(self):
        issued = datetime(2026, 8, 1, tzinfo=UTC)
        point = graph._delta_point(
            {
                "kind": graph.DELTA_RESUME_KIND,
                "delta_link": "https://graph.test/delta?$deltatoken=abc",
                "issued": issued.isoformat(),
            }
        )

        assert point == ("https://graph.test/delta?$deltatoken=abc", issued)

    def test_a_point_from_another_backend_is_refused(self, caplog):
        with caplog.at_level(logging.INFO):
            assert graph._delta_point({"kind": "imap-uid", "uidvalidity": 1, "uid": 2}) is None
        assert "is not ours" in caplog.text

    def test_a_point_without_a_link_is_refused(self, caplog):
        assert graph._delta_point({"kind": graph.DELTA_RESUME_KIND}) is None
        assert "no usable delta link" in caplog.text

    def test_a_point_without_an_issue_time_still_works(self):
        """Only the log line about a rejected token needs it."""
        point = graph._delta_point(
            {"kind": graph.DELTA_RESUME_KIND, "delta_link": "https://graph.test/d"}
        )

        assert point == ("https://graph.test/d", None)


class TestDeltaToken:
    """What a completed round hands back."""

    def test_a_round_without_a_link_earns_nothing(self):
        assert _delta_token_for(None, previous=None, stored=3) is None

    def test_an_incremental_round_records_even_when_nothing_changed(self):
        """The server says "caught up"; walking the folder again would be waste."""
        token = _delta_token_for("https://graph.test/new", previous=("old", None), stored=0)

        assert token is not None
        assert token["delta_link"] == "https://graph.test/new"

    def test_a_first_round_that_archived_nothing_earns_nothing(self):
        """The Proton Bridge shape, in Graph terms: no mail shown, no claim made."""
        assert _delta_token_for("https://graph.test/new", previous=None, stored=0) is None

    def test_a_first_round_that_archived_something_records(self):
        token = _delta_token_for("https://graph.test/new", previous=None, stored=1)

        assert token is not None
        assert token["kind"] == graph.DELTA_RESUME_KIND
        assert token["issued"]


def _delta_token_for(link, previous, stored):
    return graph._delta_token(link, previous, stored)


class TestDeltaRound:
    """Walking a round: paging, removals, and a rejected token."""

    @staticmethod
    def _client(monkeypatch, responses):
        harness = _make_client(monkeypatch, responses)
        harness.client._user = "user@example.org"
        harness.client.job_name = "m365"
        return harness

    def test_the_opening_request_carries_the_query_options(self, monkeypatch):
        harness = self._client(
            monkeypatch,
            [_json(200, {"value": [], "@odata.deltaLink": "https://graph.test/d"})],
        )

        harness.client._delta_round("INBOX", "folder-id", None)

        call = harness.request.call_args
        assert call.args[1].endswith("/mailFolders/folder-id/messages/delta")
        assert call.kwargs["params"] == {"$select": "id"}
        assert call.kwargs["headers"]["Prefer"].startswith("odata.maxpagesize=")

    def test_a_stored_link_is_followed_verbatim(self, monkeypatch):
        """Graph encodes the query options into the token, so nothing is re-added."""
        harness = self._client(
            monkeypatch,
            [_json(200, {"value": [], "@odata.deltaLink": "https://graph.test/d2"})],
        )

        harness.client._delta_round("INBOX", "folder-id", ("https://graph.test/d1", None))

        call = harness.request.call_args
        assert call.args[1] == "https://graph.test/d1"
        assert call.kwargs["params"] is None

    def test_pages_are_followed_until_the_delta_link(self, monkeypatch):
        harness = self._client(
            monkeypatch,
            [
                _json(
                    200, {"value": [{"id": "a"}], "@odata.nextLink": "https://graph.test/p2"}
                ),
                _json(
                    200, {"value": [{"id": "b"}], "@odata.deltaLink": "https://graph.test/d"}
                ),
            ],
        )

        items, link = harness.client._delta_round("INBOX", "folder-id", None)

        assert [i["id"] for i in items] == ["a", "b"]
        assert link == "https://graph.test/d"

    def test_removed_entries_are_skipped(self, monkeypatch):
        """They arrive for a deletion *or a move out*, asked for or not."""
        harness = self._client(
            monkeypatch,
            [
                _json(
                    200,
                    {
                        "value": [
                            {"id": "a"},
                            {"id": "b", "@removed": {"reason": "deleted"}},
                        ],
                        "@odata.deltaLink": "https://graph.test/d",
                    },
                )
            ],
        )

        items, _link = harness.client._delta_round("INBOX", "folder-id", None)

        assert [i["id"] for i in items] == ["a"]

    def test_a_rejected_token_is_reported_not_worked_around(self, monkeypatch, caplog):
        """410 is a normal recovery path, but what to do instead is not ours to pick.

        Restarting here would download the folder; the caller may be able to list
        it and fetch only the difference. So the round stops and says so.
        """
        harness = self._client(monkeypatch, [_json(410, {"error": {"code": "resyncRequired"}})])
        issued = datetime.now(UTC) - timedelta(hours=5)

        with caplog.at_level(logging.INFO), pytest.raises(graph._DeltaExpired):
            harness.client._delta_round(
                "INBOX", "folder-id", ("https://graph.test/stale", issued)
            )

        assert "delta token rejected (HTTP 410)" in caplog.text
        # The age is what tells us how long these actually live.
        assert "5.0h" in caplog.text

    def test_a_rejected_token_reaches_the_caller_as_a_lost_point(self, monkeypatch):
        harness = self._client(monkeypatch, [_json(410, {"error": {"code": "resyncRequired"}})])
        harness.client._folder_map = {"INBOX": "folder-id"}
        harness.client.exchange_journal = False
        harness.client.delete_after_export = False
        harness.client.error_folder = None

        result = harness.client.folder_backup(
            "INBOX",
            MagicMock(),
            resume={
                "kind": graph.DELTA_RESUME_KIND,
                "delta_link": "https://graph.test/stale",
            },
        )

        assert result.resume_lost is True
        assert result.stored == 0

    def test_an_expired_token_arrives_as_a_4xx_error_code_too(self, monkeypatch, caplog):
        """Graph documents expiry as "a 40X-series error with codes such as
        syncStateNotFound", not only as 410."""
        harness = self._client(
            monkeypatch, [_json(404, {"error": {"code": "syncStateNotFound"}})]
        )

        with caplog.at_level(logging.INFO), pytest.raises(graph._DeltaExpired):
            harness.client._delta_round(
                "INBOX", "folder-id", ("https://graph.test/stale", None)
            )

        assert "HTTP 404" in caplog.text

    def test_a_refused_request_is_not_mistaken_for_an_expired_token(self, monkeypatch):
        """403 is about credentials; swallowing it would hide a broken job."""
        harness = self._client(
            monkeypatch, [_json(403, {"error": {"code": "ErrorAccessDenied"}})]
        )

        with pytest.raises(httpx.HTTPStatusError):
            harness.client._delta_round(
                "INBOX", "folder-id", ("https://graph.test/stale", None)
            )

    def test_a_point_from_another_backend_is_a_lost_point(self, monkeypatch):
        """Reported before a single request goes out."""
        harness = self._client(monkeypatch, [])

        result = harness.client.folder_backup(
            "INBOX", MagicMock(), resume={"kind": "imap-uid", "uidvalidity": 1, "uid": 2}
        )

        assert result.resume_lost is True
        harness.request.assert_not_called()

    def test_a_round_without_a_delta_link_earns_nothing(self, monkeypatch, caplog):
        harness = self._client(monkeypatch, [_json(200, {"value": [{"id": "a"}]})])

        items, link = harness.client._delta_round("INBOX", "folder-id", None)

        assert [i["id"] for i in items] == ["a"]
        assert link is None
        assert "ended without a link" in caplog.text


def _resp(status: int, **kwargs) -> httpx.Response:
    """A response that survives raise_for_status(), which needs its request."""
    return httpx.Response(status, request=httpx.Request("POST", "https://graph.test"), **kwargs)


# ---------------------------------------------------------------------------
# Folder creation and the error folder
# ---------------------------------------------------------------------------


class TestEnsureFolder:
    @staticmethod
    def _client(monkeypatch, responses, folder_map):
        harness = _make_client(monkeypatch, responses)
        harness.client._user = "user@example.org"
        harness.client.job_name = "job"
        harness.client._folder_map = dict(folder_map)
        return harness

    def test_an_existing_folder_is_not_created(self, monkeypatch):
        harness = self._client(monkeypatch, [], {"Errors": "id-errors"})

        assert harness.client._ensure_folder("Errors") == "id-errors"
        harness.request.assert_not_called()

    def test_a_missing_top_level_folder_is_created(self, monkeypatch):
        """A background job must not stop because someone deleted the folder."""
        created = _resp(201, json={"id": "id-new"})
        harness = self._client(monkeypatch, [created], {"INBOX": "id-inbox"})

        assert harness.client._ensure_folder("Errors") == "id-new"
        method, url = harness.request.call_args[0][:2]
        assert method == "POST"
        assert url.endswith("/mailFolders")
        assert harness.request.call_args.kwargs["json"] == {"displayName": "Errors"}
        # And it is remembered, so the next message does not create it again.
        assert harness.client._folder_map["Errors"] == "id-new"

    def test_a_child_folder_is_created_under_its_parent(self, monkeypatch):
        created = _resp(201, json={"id": "id-child"})
        harness = self._client(monkeypatch, [created], {"Journal": "id-journal"})

        assert harness.client._ensure_folder("Journal/Errors") == "id-child"
        url = harness.request.call_args[0][1]
        assert url.endswith("/mailFolders/id-journal/childFolders")
        assert harness.request.call_args.kwargs["json"] == {"displayName": "Errors"}

    def test_a_missing_parent_is_an_error(self, monkeypatch):
        harness = self._client(monkeypatch, [], {})

        with pytest.raises(base.MailboxError, match="parent 'Journal' does not exist"):
            harness.client._ensure_folder("Journal/Errors")

    def test_a_missing_permission_says_which_one(self, monkeypatch):
        """Creating is the survivable case; not being allowed to is not."""
        harness = self._client(monkeypatch, [_resp(403)], {})

        with pytest.raises(base.MailboxError, match="Mail.ReadWrite"):
            harness.client._ensure_folder("Errors")


class TestGraphRelocate:
    @staticmethod
    def _client(monkeypatch, responses):
        harness = _make_client(monkeypatch, responses)
        harness.client._user = "user@example.org"
        harness.client.job_name = "job"
        harness.client._folder_map = {"Errors": "id-errors"}
        return harness

    def test_each_message_is_moved(self, monkeypatch):
        harness = self._client(monkeypatch, [_resp(200), _resp(200)])

        harness.client._relocate("INBOX", ["m1", "m2"], "Errors")

        assert harness.request.call_count == 2
        assert harness.request.call_args.kwargs["json"] == {"destinationId": "id-errors"}

    def test_nothing_to_move_talks_to_nobody(self, monkeypatch):
        harness = self._client(monkeypatch, [])

        harness.client._relocate("INBOX", [], "Errors")
        harness.request.assert_not_called()

    def test_one_failure_does_not_stop_the_rest(self, monkeypatch):
        harness = self._client(
            monkeypatch,
            [_resp(500), _resp(200)],
        )
        harness.client.max_retries = 0

        harness.client._relocate("INBOX", ["m1", "m2"], "Errors")
        assert harness.request.call_count == 2

    def test_a_missing_permission_stops_the_run(self, monkeypatch):
        harness = self._client(monkeypatch, [_resp(403)])

        with pytest.raises(base.MailboxError, match="Mail.ReadWrite"):
            harness.client._relocate("INBOX", ["m1", "m2"], "Errors")


# ---------------------------------------------------------------------------
# Deleting: soft by default, permanent on request
# ---------------------------------------------------------------------------


class TestPurge:
    @staticmethod
    def _client(monkeypatch, responses, permanent: bool):
        harness = _make_client(monkeypatch, responses)
        harness.client._user = "user@example.org"
        harness.client.job_name = "job"
        harness.client.permanent_delete = permanent
        return harness

    def test_the_default_is_a_soft_delete(self, monkeypatch):
        """Plain DELETE moves the message to Deleted Items and leaves it there."""
        harness = self._client(monkeypatch, [_resp(204)], permanent=False)

        harness.client.purge("INBOX", ["m1"])

        method, url = harness.request.call_args[0][:2]
        assert method == "DELETE"
        assert url.endswith("/messages/m1")

    def test_permanent_delete_uses_the_permanent_action(self, monkeypatch):
        harness = self._client(monkeypatch, [_resp(204)], permanent=True)

        harness.client.purge("INBOX", ["m1"])

        method, url = harness.request.call_args[0][:2]
        assert method == "POST"
        assert url.endswith("/messages/m1/permanentDelete")

    def test_it_deletes_only_what_it_was_given(self, monkeypatch):
        # Unlike emptying a trash folder, this touches nothing else in the bin.
        harness = self._client(monkeypatch, [_resp(204), _resp(204)], permanent=True)

        harness.client.purge("INBOX", ["m1", "m2"])

        urls = [call[0][1] for call in harness.request.call_args_list]
        assert [u.rsplit("/messages/", 1)[1] for u in urls] == [
            "m1/permanentDelete",
            "m2/permanentDelete",
        ]

    def test_a_failure_leaves_the_message_in_place(self, monkeypatch):
        """The safe direction: still there, never gone unarchived."""
        harness = self._client(monkeypatch, [_resp(404), _resp(204)], permanent=True)
        harness.client.max_retries = 0

        harness.client.purge("INBOX", ["m1", "m2"])  # must not raise

        assert harness.request.call_count == 2
