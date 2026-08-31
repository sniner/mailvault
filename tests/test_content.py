"""Tests for `mailutils.content` -- a message reduced to text and a list.

What the MCP server hands a model rests on this module: the body as text
whatever the MIME shape, attachments listed rather than inlined, and one
attachment fetched by what the listing said.
"""

from __future__ import annotations

from email.message import EmailMessage

import pytest

from mailvault.mailutils import content


def _message(body: str = "the body", attachments: int = 0) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = "Info <info@example.com>"
    msg["To"] = "stefan@example.org"
    msg["Cc"] = "cc@example.org"
    msg["Subject"] = "Invoice 4711"
    msg["Date"] = "Mon, 11 Mar 2024 10:00:00 +0000"
    msg["Message-Id"] = "<a@example.com>"
    msg.set_content(body)
    for number in range(attachments):
        msg.add_attachment(
            b"%PDF-" + bytes(str(number), "ascii"),
            maintype="application",
            subtype="pdf",
            filename=f"file-{number}.pdf",
        )
    return msg


class TestOverview:
    def test_headers_arrive_as_written_and_the_date_as_iso(self):
        read = content.overview(_message().as_bytes())

        assert read.subject == "Invoice 4711"
        assert read.sender == "Info <info@example.com>"
        assert read.to == "stefan@example.org"
        assert read.cc == "cc@example.org"
        assert read.date == "2024-03-11T10:00:00+00:00"
        assert read.message_id == "<a@example.com>"

    def test_a_header_the_message_does_not_carry_is_none_not_empty(self):
        msg = EmailMessage()
        msg.set_content("x")

        read = content.overview(msg.as_bytes())

        assert read.subject is None
        assert read.cc is None
        assert read.message_id is None

    def test_the_plain_body_wins_over_the_html_one(self):
        msg = _message("plain text")
        msg.add_alternative("<p>html</p>", subtype="html")

        read = content.overview(msg.as_bytes())

        assert read.body_type == "text/plain"
        assert "plain text" in read.body
        assert read.attachments == [], "the html rendering is the body again, not an attachment"

    def test_html_where_that_is_all_there_is(self):
        msg = EmailMessage()
        msg.add_alternative("<p>only html</p>", subtype="html")

        read = content.overview(msg.as_bytes())

        assert read.body_type == "text/html"
        assert "only html" in read.body

    def test_attachments_are_listed_never_inlined(self):
        read = content.overview(_message(attachments=2).as_bytes())

        assert [a.filename for a in read.attachments] == ["file-0.pdf", "file-1.pdf"]
        assert [a.index for a in read.attachments] == [1, 2]
        assert read.attachments[0].content_type == "application/pdf"
        assert read.attachments[0].size == len(b"%PDF-0")
        assert "%PDF" not in read.body

    def test_an_attached_message_is_one_attachment_not_its_parts(self):
        inner = _message("inner body", attachments=1)
        msg = _message("outer body")
        msg.add_attachment(inner)

        read = content.overview(msg.as_bytes())

        assert len(read.attachments) == 1
        assert read.attachments[0].content_type == "message/rfc822"
        assert "inner body" not in read.body

    def test_the_body_is_cut_at_the_limit_and_says_so(self):
        read = content.overview(_message("x" * 100).as_bytes(), body_limit=10)

        assert read.body == "x" * 10
        assert read.body_truncated is True
        assert read.body_size >= 100, "the full length, so a reader knows what was cut"

    def test_a_short_body_is_not_marked_truncated(self):
        read = content.overview(_message("short").as_bytes())

        assert read.body_truncated is False
        assert read.body_size == len(read.body)

    def test_a_charset_lie_costs_the_damage_not_the_message(self):
        raw = (
            b"From: a@example.com\r\n"
            b'Content-Type: text/plain; charset="no-such-charset"\r\n'
            b"\r\n"
            b"readable anyway\r\n"
        )

        read = content.overview(raw)

        assert "readable anyway" in read.body


class TestAttachment:
    def test_by_the_index_the_overview_listed(self):
        info, data = content.attachment(_message(attachments=2).as_bytes(), index=2)

        assert info.filename == "file-1.pdf"
        assert data == b"%PDF-1"
        assert info.size == len(data)

    def test_by_filename(self):
        info, data = content.attachment(
            _message(attachments=2).as_bytes(), filename="file-0.pdf"
        )

        assert info.index == 1
        assert data == b"%PDF-0"

    def test_an_index_the_message_does_not_have(self):
        with pytest.raises(ValueError, match="has 2, numbered 1 to 2"):
            content.attachment(_message(attachments=2).as_bytes(), index=3)

    def test_a_message_with_no_attachments_says_so(self):
        with pytest.raises(ValueError, match="has no attachments"):
            content.attachment(_message().as_bytes(), index=1)

    def test_a_filename_that_is_not_there_names_what_is(self):
        with pytest.raises(ValueError, match="file-0.pdf"):
            content.attachment(_message(attachments=1).as_bytes(), filename="other.pdf")

    def test_a_filename_two_attachments_carry_is_refused(self):
        msg = _message()
        for _ in range(2):
            msg.add_attachment(b"x", maintype="text", subtype="csv", filename="twice.csv")

        with pytest.raises(ValueError, match="2 attachments carry that name"):
            content.attachment(msg.as_bytes(), filename="twice.csv")

    def test_neither_index_nor_filename_is_refused(self):
        with pytest.raises(ValueError, match="one of the two"):
            content.attachment(_message(attachments=1).as_bytes())

    def test_both_at_once_is_refused_too(self):
        with pytest.raises(ValueError, match="one of the two"):
            content.attachment(
                _message(attachments=1).as_bytes(), index=1, filename="file-0.pdf"
            )

    def test_an_attached_message_comes_out_as_the_eml_it_is(self):
        inner = _message("inner body")
        msg = _message("outer")
        msg.add_attachment(inner)

        _info, data = content.attachment(msg.as_bytes(), index=1)

        assert b"inner body" in data
        assert b"Subject: Invoice 4711" in data
