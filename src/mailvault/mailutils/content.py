"""What a message holds, for a reader that wants text rather than bytes.

The raw `.eml` is the archive's answer; this one is for a reader that cannot be
handed a file -- the MCP server above all. The headers worth reading, the body
as text, and the attachments as a list to choose from: a body that arrived
base64-encoded in three MIME parts is exactly what such a reader must not have
to reassemble, and an attachment inlined into it would be paid for in context
whether it was wanted or not.
"""

from __future__ import annotations

import dataclasses
import email.message

from mailvault.mailutils import dates, headers, reading

# How much body text an overview carries before it is cut. Enough for any mail a
# person wrote; what runs over it is generated bulk, and the overview says what
# was cut and how much there is, so a reader who wants the rest knows to fetch
# the message whole.
BODY_LIMIT = 65_536


@dataclasses.dataclass
class AttachmentInfo:
    """One attachment as the overview lists it: enough to decide before fetching.

    `size` is the decoded size in bytes -- what a fetch would hand over, not what
    the attachment occupies inside the message.
    """

    index: int
    filename: str | None
    content_type: str
    size: int


@dataclasses.dataclass
class MessageContent:
    """A message reduced to what a reader of text can take in.

    `body` is the text of the message's body part, plain where the message
    offers it, HTML where that is all there is -- `body_type` says which. It is
    cut at a limit; `body_truncated` says whether it was, and `body_size` is the
    full length in characters either way. Attachments are listed, never
    inlined.
    """

    subject: str | None
    sender: str | None
    to: str | None
    cc: str | None
    date: str | None
    message_id: str | None
    body: str
    body_type: str | None
    body_size: int
    body_truncated: bool
    attachments: list[AttachmentInfo]


def _leaf_parts(
    msg: email.message.EmailMessage,
) -> list[email.message.EmailMessage]:
    """Every non-container part, in the order the message carries them.

    Not `walk()`, which descends into an attached message/rfc822 and would list
    the attachment's own body and attachments as if they were this message's.
    An attached message is one attachment, however much it holds.
    """
    if msg.get_content_maintype() == "multipart":
        parts: list[email.message.EmailMessage] = []
        for sub in msg.iter_parts():
            # The same narrowing as in `reading`: the default policy makes
            # EmailMessage instances, the annotation just does not say so.
            parts.extend(_leaf_parts(sub))  # type: ignore[arg-type]
        return parts
    return [msg]


def _text_of(part: email.message.EmailMessage) -> str:
    """The text of one part, whatever its transfer encoding and charset claim.

    A charset the interpreter does not know, or one the bytes do not honour,
    must not cost the reader the message: what cannot be decoded as claimed is
    decoded as UTF-8 with the damage marked, which reads wrong where it is wrong
    and right everywhere else.
    """
    try:
        content = part.get_content()
    except (LookupError, UnicodeDecodeError, KeyError):
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            return ""
        return payload.decode("utf-8", errors="replace")
    if isinstance(content, str):
        return content
    if isinstance(content, bytes):
        return content.decode("utf-8", errors="replace")
    return ""


def _part_bytes(part: email.message.EmailMessage) -> bytes:
    """The decoded bytes of one part -- what a fetch of it hands over."""
    payload = part.get_payload(decode=True)
    if isinstance(payload, bytes):
        return payload
    content = part.get_content()
    if isinstance(content, bytes):
        return content
    if isinstance(content, str):
        return content.encode()
    if isinstance(content, email.message.Message):
        # An attached message: handed over as the .eml it is.
        return content.as_bytes()
    return b""


def _attachment_parts(
    msg: email.message.EmailMessage,
) -> list[email.message.EmailMessage]:
    """The parts a reader would call attachments, in a stable order.

    Everything that is not the chosen body and either says it is an attachment
    or carries a filename -- which takes in inline images, because a part with a
    name is a thing somebody attached, whatever its disposition says. The
    alternative renderings of the body carry neither and are not attachments,
    they are the body again.
    """
    body = msg.get_body(preferencelist=("plain", "html"))
    return [
        part
        for part in _leaf_parts(msg)
        if part is not body and (part.is_attachment() or part.get_filename() is not None)
    ]


def _header_or_none(msg: email.message.EmailMessage, name: str) -> str | None:
    return headers.header_text(msg, name) or None


def _date_of(msg: email.message.EmailMessage) -> str | None:
    """The date in ISO form where it can be read, as written where it cannot."""
    parsed = dates.date(msg)
    if parsed is not None:
        return parsed.isoformat()
    return _header_or_none(msg, "Date")


def overview(raw: bytes, body_limit: int = BODY_LIMIT) -> MessageContent:
    """Reduce a stored message to headers, body text and an attachment list."""
    msg = reading.decode_email(raw)
    body_part = msg.get_body(preferencelist=("plain", "html"))
    if body_part is None:
        text, body_type = "", None
    else:
        # The same narrowing as everywhere in this package: the policy makes
        # EmailMessage, the stdlib annotation says Message.
        text = _text_of(body_part)  # type: ignore[arg-type]
        body_type = body_part.get_content_type()
    return MessageContent(
        subject=_header_or_none(msg, "Subject"),
        sender=_header_or_none(msg, "From"),
        to=_header_or_none(msg, "To"),
        cc=_header_or_none(msg, "Cc"),
        date=_date_of(msg),
        message_id=headers.message_id(msg) or None,
        body=text[:body_limit],
        body_type=body_type,
        body_size=len(text),
        body_truncated=len(text) > body_limit,
        attachments=[
            AttachmentInfo(
                index=number,
                filename=part.get_filename(),
                content_type=part.get_content_type(),
                size=len(_part_bytes(part)),
            )
            for number, part in enumerate(_attachment_parts(msg), start=1)
        ],
    )


def attachment(
    raw: bytes,
    index: int | None = None,
    filename: str | None = None,
) -> tuple[AttachmentInfo, bytes]:
    """One attachment, by the index the overview listed or by its filename.

    Exactly one of the two must be given. The errors are written for the caller
    who asked, which for the MCP server is the model on the other end -- each
    names what the message actually has.
    """
    if (index is None) == (filename is None):
        raise ValueError(
            "name the attachment by its index or by its filename -- one of the"
            " two, not both and not neither"
        )
    parts = _attachment_parts(reading.decode_email(raw))
    if index is not None:
        if not parts:
            raise ValueError(f"attachment {index}: the message has no attachments")
        if not 1 <= index <= len(parts):
            raise ValueError(
                f"attachment {index}: the message has {len(parts)}, numbered 1 to {len(parts)}"
            )
        part, number = parts[index - 1], index
    else:
        matches = [
            (number, part)
            for number, part in enumerate(parts, start=1)
            if part.get_filename() == filename
        ]
        if not matches:
            listed = ", ".join(part.get_filename() or "(unnamed)" for part in parts)
            raise ValueError(
                f"{filename}: not among the message's attachments"
                + (f" -- it carries: {listed}" if listed else " -- it carries none")
            )
        if len(matches) > 1:
            raise ValueError(
                f"{filename}: {len(matches)} attachments carry that name -- name"
                f" the one you mean by its index"
            )
        number, part = matches[0]
    data = _part_bytes(part)
    info = AttachmentInfo(
        index=number,
        filename=part.get_filename(),
        content_type=part.get_content_type(),
        size=len(data),
    )
    return info, data
