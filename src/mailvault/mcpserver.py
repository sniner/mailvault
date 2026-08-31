"""The MCP server -- the archive's query layer, served to an AI client.

A second frontend beside the CLI, answering the same questions from the same
places: searches from `index.db`, messages and attachments from the store, the
places from the metadata log. Strictly read-only -- nothing here touches a
mailbox, sees a credential, or writes into the archive.

This module is the one place in mailvault that imports the `mcp` SDK, which is
an optional extra; `cli.mcp` imports it only once the command actually runs.
Everything the tools answer with is computed by `jobs` and `mailutils` -- what
lives here is the wiring, and the docstrings: those are handed verbatim to the
model on the other end, so they are user-facing text and written as such.
"""

from __future__ import annotations

import base64
import dataclasses
import logging
import pathlib

import mcp.types
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from mailvault import cli, jobs
from mailvault.jobs import retrieval
from mailvault.mailutils import content
from mailvault.store import cas, metalog

log = logging.getLogger(__name__)

# How many hits a search returns when the model does not say. The CLI leaves
# this open because a terminal scrolls; a model pays for every line in context,
# and an unfiltered search over a large archive would hand it the whole archive.
# The result says when it was cut, so nothing is silently withheld.
DEFAULT_SEARCH_LIMIT = 100


@dataclasses.dataclass
class SearchResult:
    """What a search returns: the hits, and the two notes they may need.

    `note` says the limit cut the list short; `stale` says the query database
    is behind the archive. Both are None when there is nothing to say.
    """

    hits: list[jobs.SearchHit]
    count: int
    note: str | None
    stale: str | None


@dataclasses.dataclass
class MessageResult:
    """One message: its store id, and what `mailutils.content` read out of it."""

    store_id: str
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
    attachments: list[content.AttachmentInfo]


@dataclasses.dataclass
class PlacesResult:
    """Every place the archive has mail from, and the total it accounts for."""

    places: list[metalog.PlaceSummary]
    messages: int
    note: str | None


def _full_id(store: cas.ContentAddressedStorage, path: pathlib.Path) -> str:
    """The whole store id of an entry, read back off its path."""
    return store.hashval_of(path) or path.name.split(".", 1)[0]


def _read_message(archive: pathlib.Path, message_id: str) -> tuple[str, bytes]:
    """Resolve an id the way `get` does and hand back the whole message."""
    store = cas.mail_store(archive)
    path = retrieval.entry_path(store, message_id)
    return _full_id(store, path), store.read(path)


def build_server(archive: pathlib.Path) -> MCPServer:
    """The server for one archive, its four tools closed over the path.

    The startup checks -- is this an archive, is there a query database, can it
    be read -- have run before this is called; what is built here assumes both
    and answers from them.
    """
    db_path = archive / jobs.DEFAULT_QUERY_DB_NAME
    server = MCPServer(
        "mailvault",
        version=cli.get_version(),
        instructions=(
            "Read-only access to a mailvault email archive. `search` finds"
            " messages by sender, recipient, subject, date or place;"
            " `get_message` reads one; `get_attachment` fetches one of its"
            " attachments; `places` lists the mailboxes, folders and imports"
            " that `search` can filter by. Nothing here can change the archive"
            " or reach a mailbox."
        ),
    )

    @server.tool()
    def search(
        sender: str | None = None,
        recipient: str | None = None,
        subject: str | None = None,
        mailbox: str | None = None,
        folder: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = DEFAULT_SEARCH_LIMIT,
    ) -> SearchResult:
        """Find archived messages; every filter given has to match.

        `sender`, `recipient` and `subject` match anywhere in the value and
        ignore case. `mailbox` and `folder` name where a message was seen --
        the `places` tool lists the names they match against. `since` and
        `until` are days as YYYY-MM-DD, both inclusive; a message with no
        readable date matches neither. Hits come oldest first, capped at
        `limit`: the result's `note` says when the cap cut the list short, and
        its `stale` says when the database is behind the archive, so mail
        archived since would be missing. Each hit's `store_id` is what
        `get_message` and `get_attachment` take.
        """
        # Refusals leave as ToolError throughout this module: only its text
        # reaches the model, everything else is treated as a crash and reported
        # without a word -- and these messages name the state and the move.
        state = jobs.freshness(archive, db_path)
        complaint = state.complaint(db_path.name)
        if complaint and not state.is_usable:
            raise ToolError(complaint)
        query = jobs.SearchQuery(
            sender=sender,
            recipient=recipient,
            subject=subject,
            mailbox=mailbox,
            folder=folder,
            since=since,
            until=until,
            limit=limit,
        )
        try:
            hits = jobs.search(db_path, query)
        except jobs.JobError as exc:
            raise ToolError(str(exc)) from exc
        note = None
        if len(hits) == limit:
            note = (
                f"stopped at limit {limit:,}; there may be more -- narrow the"
                f" search or raise the limit"
            )
        return SearchResult(hits=hits, count=len(hits), note=note, stale=complaint)

    @server.tool()
    def get_message(message_id: str) -> MessageResult:
        """Read one archived message: headers, body text, attachment list.

        Takes a `store_id` as `search` returns it, whole or just its beginning
        -- as much of one as names a single message is enough. The body is
        text, plain where the message offers it, HTML where that is all there
        is; `body_truncated` and `body_size` say whether and how much was cut.
        Attachments are listed with index, filename, type and size, never
        inlined -- fetch one with `get_attachment` when its content is needed.
        """
        try:
            full_id, raw = _read_message(archive, message_id)
        except jobs.JobError as exc:
            raise ToolError(str(exc)) from exc
        read = content.overview(raw)
        return MessageResult(
            store_id=full_id,
            subject=read.subject,
            sender=read.sender,
            to=read.to,
            cc=read.cc,
            date=read.date,
            message_id=read.message_id,
            body=read.body,
            body_type=read.body_type,
            body_size=read.body_size,
            body_truncated=read.body_truncated,
            attachments=read.attachments,
        )

    @server.tool()
    def get_attachment(
        message_id: str,
        index: int | None = None,
        filename: str | None = None,
    ) -> mcp.types.EmbeddedResource:
        """Fetch one attachment of a message, as the binary it is.

        Name the message by its `store_id` and the attachment by the `index` or
        `filename` that `get_message` listed -- one of the two. The attachment
        comes back whole, at the size that listing named, so check it there
        before fetching something enormous.
        """
        try:
            full_id, raw = _read_message(archive, message_id)
            info, data = content.attachment(raw, index=index, filename=filename)
        except (jobs.JobError, ValueError) as exc:
            raise ToolError(str(exc)) from exc
        return mcp.types.EmbeddedResource(
            type="resource",
            resource=mcp.types.BlobResourceContents(
                uri=f"mailvault://{full_id}/attachment/{info.index}",
                mime_type=info.content_type,
                blob=base64.b64encode(data).decode("ascii"),
            ),
        )

    @server.tool()
    def places() -> PlacesResult:
        """List every place the archive has mail from, with counts.

        One entry per mailbox and folder a backup has seen mail in, plus every
        import -- an entry with no mailbox is one. These are the names the
        `search` filters `mailbox` and `folder` match against. A message can
        lie in several places, so the counts add up to more than `messages`,
        which counts each message once.
        """
        summary = metalog.summarize(archive / metalog.DEFAULT_LOG_DIR)
        note = None
        if sum(place.messages for place in summary.places) != summary.messages:
            note = "the counts add up to more: a message can be in several places"
        return PlacesResult(places=summary.places, messages=summary.messages, note=note)

    return server


def serve(archive: pathlib.Path, listen: tuple[str, int] | None) -> None:
    """Run the server until the client hangs up or the process is stopped.

    Without `listen` it speaks MCP over stdin/stdout -- stdout belongs to the
    protocol then, which every log line already respects by going to stderr.
    With `listen` it serves streamable HTTP on that address, statelessly: no
    tool here has anything to remember between two calls.
    """
    server = build_server(archive)
    if listen is None:
        server.run(transport="stdio")
    else:
        host, port = listen
        server.run(transport="streamable-http", host=host, port=port, stateless_http=True)
