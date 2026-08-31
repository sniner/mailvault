"""Tests for `mailvault mcp` -- the startup refusals and the four tools.

The whole file needs the optional `mcp` extra and skips without it; CI installs
every extra, so a skip here only ever happens on a deliberately slim install.
"""

from __future__ import annotations

import argparse
import base64
import pathlib
from datetime import UTC, datetime
from email.message import EmailMessage

import pytest

pytest.importorskip("mcp", reason="the mcp extra is not installed")

import anyio
import mcp.types
from mcp.server.mcpserver.exceptions import ToolError

from mailvault import jobs, mcpserver
from mailvault.cli.mcp import is_loopback, parse_listen
from mailvault.cli.mcp import run as run_mcp
from mailvault.store import cas, heads, marker, metalog


class TestParseListen:
    def test_host_and_port_come_apart(self):
        assert parse_listen("127.0.0.1:56789") == ("127.0.0.1", 56789)

    def test_ipv6_keeps_its_colons_and_loses_its_brackets(self):
        assert parse_listen("[::1]:56789") == ("::1", 56789)

    @pytest.mark.parametrize("value", ["56789", ":56789", "127.0.0.1:", "127.0.0.1:mcp"])
    def test_what_is_not_host_port_is_refused(self, value):
        with pytest.raises(jobs.JobError, match="--listen"):
            parse_listen(value)

    @pytest.mark.parametrize("value", ["host:0", "host:65536"])
    def test_a_port_off_the_scale_is_refused(self, value):
        with pytest.raises(jobs.JobError, match="1 to 65535"):
            parse_listen(value)


class TestIsLoopback:
    @pytest.mark.parametrize(
        "host", ["127.0.0.1", "127.1.2.3", "::1", "localhost", "LocalHost"]
    )
    def test_what_stays_on_this_machine(self, host):
        assert is_loopback(host)

    @pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "::", "mcp.example.com"])
    def test_what_may_not(self, host):
        # A hostname may resolve anywhere, so it counts as remote -- the flag
        # this feeds errs toward refusing.
        assert not is_loopback(host)


def _archive(tmp_path: pathlib.Path, *messages: EmailMessage) -> list[str]:
    """A real archive: store, log, heads, and a query database built from them."""
    marker.write(tmp_path)
    store = cas.mail_store(tmp_path)
    writer = metalog.LogWriter(
        tmp_path / metalog.DEFAULT_LOG_DIR, tmp_path / heads.DEFAULT_HEADS_DIR
    )
    ids = []
    for msg in messages:
        _status, store_id, _path = store.add(msg.as_bytes())
        writer.add("example.com", ["INBOX"], store_id)
        ids.append(store_id)
    writer.seal(datetime(2026, 8, 1, tzinfo=UTC))
    jobs.create_db(tmp_path, tmp_path / "index.db", force=True)
    return ids


def _message(subject: str, sent: str, attachments: int = 0) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = "info@example.com"
    msg["To"] = "stefan@example.org"
    msg["Subject"] = subject
    msg["Date"] = sent
    msg.set_content(f"body of {subject}")
    for number in range(attachments):
        msg.add_attachment(
            b"%PDF-" + bytes(str(number), "ascii"),
            maintype="application",
            subtype="pdf",
            filename=f"file-{number}.pdf",
        )
    return msg


def _mcp_args(archive, listen=None, allow_remote=False):
    return argparse.Namespace(archive=archive, listen=listen, allow_remote=allow_remote)


class TestStartup:
    """What `mcp` refuses before a client is listening on the other end."""

    def test_without_a_query_database_the_server_does_not_start(self, tmp_path):
        marker.write(tmp_path)

        with pytest.raises(jobs.JobError, match="no query database.*db create"):
            run_mcp(_mcp_args(tmp_path))

    def test_a_non_loopback_listen_needs_allow_remote(self, tmp_path):
        _archive(tmp_path, _message("x", "Mon, 11 Mar 2024 10:00:00 +0000"))

        with pytest.raises(jobs.JobError, match="--allow-remote"):
            run_mcp(_mcp_args(tmp_path, listen="0.0.0.0:56789"))

    def test_a_directory_that_is_not_an_archive_is_refused_first(self, tmp_path):
        with pytest.raises(jobs.JobError, match="not a mailvault archive"):
            run_mcp(_mcp_args(tmp_path))


def _call(server, name: str, arguments: dict) -> mcp.types.CallToolResult:
    result = anyio.run(server.call_tool, name, arguments)
    # No tool here asks the client anything back, so the other arm of the
    # union -- an InputRequiredResult -- would be a bug worth failing on.
    assert isinstance(result, mcp.types.CallToolResult)
    return result


def _structured(server, name: str, arguments: dict) -> dict:
    result = _call(server, name, arguments)
    assert result.structured_content is not None
    return result.structured_content


class TestTools:
    @pytest.fixture
    def archive(self, tmp_path):
        ids = _archive(
            tmp_path,
            _message("Invoice 4711", "Mon, 11 Mar 2024 10:00:00 +0000", attachments=1),
            _message("Delivery note", "Thu, 02 May 2025 09:00:00 +0000"),
        )
        return tmp_path, ids

    def test_search_answers_with_the_hit_and_no_complaints(self, archive):
        tmp_path, ids = archive
        server = mcpserver.build_server(tmp_path)

        result = _structured(server, "search", {"subject": "invoice"})

        assert result["count"] == 1
        hit = result["hits"][0]
        assert hit["store_id"] == ids[0]
        assert hit["sender"] == "info@example.com"
        assert hit["places"] == ["example.com::INBOX"]
        assert result["note"] is None
        assert result["stale"] is None

    def test_search_says_when_the_limit_cut_the_list(self, archive):
        tmp_path, _ids = archive
        server = mcpserver.build_server(tmp_path)

        result = _structured(server, "search", {"limit": 1})

        assert result["count"] == 1
        assert "there may be more" in result["note"]

    def test_search_says_when_the_database_is_behind(self, archive):
        """Mail archived after the database was built must not go missing quietly."""
        tmp_path, _ids = archive
        store = cas.mail_store(tmp_path)
        writer = metalog.LogWriter(
            tmp_path / metalog.DEFAULT_LOG_DIR, tmp_path / heads.DEFAULT_HEADS_DIR
        )
        _status, late_id, _path = store.add(b"From: late@example.com\r\n\r\nlate")
        writer.add("example.com", ["INBOX"], late_id)
        writer.seal(datetime(2026, 8, 2, tzinfo=UTC))
        server = mcpserver.build_server(tmp_path)

        result = _structured(server, "search", {})

        assert "behind the archive" in result["stale"]
        assert "db update" in result["stale"]

    def test_get_message_reads_headers_body_and_the_attachment_list(self, archive):
        tmp_path, ids = archive
        server = mcpserver.build_server(tmp_path)

        result = _structured(server, "get_message", {"message_id": ids[0][:12]})

        assert result["store_id"] == ids[0]
        assert result["subject"] == "Invoice 4711"
        assert "body of Invoice 4711" in result["body"]
        assert result["body_truncated"] is False
        assert result["attachments"] == [
            {
                "index": 1,
                "filename": "file-0.pdf",
                "content_type": "application/pdf",
                "size": 6,
            }
        ]

    def test_get_attachment_hands_over_the_bytes(self, archive):
        tmp_path, ids = archive
        server = mcpserver.build_server(tmp_path)

        result = _call(server, "get_attachment", {"message_id": ids[0][:12], "index": 1})

        block = result.content[0]
        assert isinstance(block, mcp.types.EmbeddedResource)
        assert isinstance(block.resource, mcp.types.BlobResourceContents)
        assert block.resource.mime_type == "application/pdf"
        assert base64.b64decode(block.resource.blob) == b"%PDF-0"

    def test_places_lists_what_search_can_filter_by(self, archive):
        tmp_path, _ids = archive
        server = mcpserver.build_server(tmp_path)

        result = _structured(server, "places", {})

        assert result["messages"] == 2
        assert result["places"] == [
            {
                "mailbox": "example.com",
                "folder": "INBOX",
                "messages": 2,
                "last_seen": "2026-08-01T00:00:00+00:00",
            }
        ]

    def test_a_refused_id_reaches_the_model_with_its_text(self, archive):
        """Only a ToolError keeps its message; anything else is a wordless crash."""
        tmp_path, _ids = archive
        server = mcpserver.build_server(tmp_path)

        with pytest.raises(ToolError, match="not in this archive"):
            _call(server, "get_message", {"message_id": "ff" * 6})

    def test_a_wrong_attachment_index_does_too(self, archive):
        tmp_path, ids = archive
        server = mcpserver.build_server(tmp_path)

        with pytest.raises(ToolError, match="numbered 1 to 1"):
            _call(server, "get_attachment", {"message_id": ids[0][:12], "index": 9})
