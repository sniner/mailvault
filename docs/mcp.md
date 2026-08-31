# The MCP server -- asking the archive from an AI client

`mailvault mcp` serves the archive over the Model Context Protocol, so an AI
client -- Claude Desktop, Claude Code, anything that speaks MCP -- can search
the mail and read what it finds. It is read-only: nothing a client can say
changes the archive or reaches a mailbox, and no mailbox credential is ever in
the server's hands.

Searches are answered from [the query
database](deep-dive.md#the-query-database-in-detail), so the server refuses to
start until `mailvault db create` has built one. Keep it in step the way you
already do -- `index_db = true` in the configuration, or `mailvault db update`
-- and when it falls behind anyway, every search result says so rather than
quietly missing the newest mail.

* [Installing](#installing)
* [Connecting a client](#connecting-a-client)
* [Serving HTTP instead](#serving-http-instead)
* [What the client can do](#what-the-client-can-do)

## Installing

The server sits behind an extra, because it carries a protocol stack most
installations never start:

```console
$ uv tool install 'mailvault[mcp]'
```

`pipx install 'mailvault[mcp]'` and `pip install 'mailvault[mcp]'` work just as
well, and the Windows `mailvault.exe` has the server built in. An install
without the extra refuses `mailvault mcp` with the line to run.

## Connecting a client

Without options the server speaks MCP over stdin/stdout, which is how a
desktop AI client starts it: the client runs the command itself and keeps it
running as long as the conversation needs it. What every client wants to know
is the same one line:

```console
$ mailvault --archive /srv/archive/private mcp
```

For **Claude Desktop** that goes into `claude_desktop_config.json` (on macOS
under `~/Library/Application Support/Claude/`, on Windows under `%APPDATA%\Claude\`):

```json
{
  "mcpServers": {
    "mailvault": {
      "command": "mailvault",
      "args": ["--archive", "/srv/archive/private", "mcp"]
    }
  }
}
```

For **Claude Code** it is one command:

```console
$ claude mcp add mailvault -- mailvault --archive /srv/archive/private mcp
```

Any other MCP client takes the same command under whatever its configuration
calls it. `--archive` is spelled out because the client starts the server
wherever it pleases -- standing in the archive is not something a desktop app
does for you.

## Serving HTTP instead

`--listen HOST:PORT` serves streamable HTTP instead of stdin/stdout, for
clients that connect to a URL rather than start a process. The endpoint is
`http://HOST:PORT/mcp`:

```console
$ mailvault --archive /srv/archive/private mcp --listen 127.0.0.1:56789
```

The server itself asks for no authentication -- whoever reaches the port reads
the mail. On `127.0.0.1` that is nobody but this machine, and that is the
address to use. An address other machines can reach is refused, unless
`--allow-remote` says that something in front of the server -- a reverse proxy
that authenticates, a firewall that limits who connects -- guards it. That
something is yours to put there; the flag only records that you said it
exists.

## What the client can do

Four tools, and the model on the other end reads their descriptions itself --
this is what they amount to:

| Tool | What it answers |
|------|-----------------|
| `search` | The filters `db search` takes: from, to, subject, mailbox, folder, since, until |
| `get_message` | One message as text: headers, body, attachments listed with name, type, size |
| `get_attachment` | One attachment, as the file it is |
| `places` | The mailbox and folder names the search filters match against |

Three seams worth knowing about, because they are where this differs from the
command line:

**A search returns at most 100 hits unless the client asks for more.** A
terminal scrolls; a model pays for every line it is handed. The result says
when the cap cut the list short, so nothing goes missing quietly -- the model
narrows the search or raises the limit itself.

**A message arrives as text, attachments as a list.** The body is the plain
text where the message offers it, the HTML where that is all there is; a very
long body is cut and says so. Attachments are named with their size and
fetched one at a time on request, because an attachment nobody asked about
would be paid for in context all the same.

**A stale database is said in the answer, not only in a log.** When mail has
been archived since `index.db` was last brought up to date, every search
result carries the same notice `db search` prints, naming `mailvault db
update` -- the model sees it and can say so, instead of presenting an
incomplete answer as the whole truth.
