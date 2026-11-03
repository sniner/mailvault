"""A server that stops answering, over a real socket (issue #19).

A mocked connection cannot show what these tests are about: whether a socket
that receives nothing raises after the timeout, and whether the commands after
it fail at once instead of waiting once more each. The server here is a thread
that speaks just enough IMAP to log in, and then falls silent where a test asks
it to.
"""

from __future__ import annotations

import socket
import threading
import time

import pytest

from mailvault import conf
from mailvault.backend import base, imap

# Short enough for a test, long enough that a run on a slow CI machine does not
# time out where it should not.
TIMEOUT = 1.0


BODY = b"From: sender@example.com\r\nSubject: Answered\r\n\r\nbody\r\n"


class _QuietServer:
    """Answers until `silent_on`, then says nothing.

    Silent only on the first `quiet_connections` connections, or on all of them
    when that is None. A FETCH it answers comes back with `BODY`.
    """

    def __init__(
        self,
        silent_on: frozenset[bytes] = frozenset(),
        greet: bool = True,
        quiet_connections: int | None = None,
    ):
        self._silent_on = silent_on
        self._greet = greet
        self._quiet_connections = quiet_connections
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.port = self._listener.getsockname()[1]
        self.connections: list[socket.socket] = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        try:
            while True:
                conn, _addr = self._listener.accept()
                self.connections.append(conn)
                quiet = (
                    self._quiet_connections is None
                    or len(self.connections) <= self._quiet_connections
                )
                threading.Thread(target=self._serve, args=(conn, quiet), daemon=True).start()
        except OSError:
            # The test closed the listener.
            return

    def _serve(self, conn: socket.socket, quiet: bool) -> None:
        try:
            if not self._greet:
                return
            conn.sendall(b"* OK ready\r\n")
            for line in conn.makefile("rb"):
                tag, command = line.split(b" ", 2)[:2]
                command = command.strip().upper()
                if quiet and command in self._silent_on:
                    continue
                if command == b"CAPABILITY":
                    conn.sendall(b"* CAPABILITY IMAP4rev1 UNSELECT\r\n")
                elif command in (b"EXAMINE", b"SELECT"):
                    conn.sendall(b"* 2 EXISTS\r\n* OK [UIDVALIDITY 1] UIDs valid\r\n")
                elif line.split()[1:3] == [b"UID", b"FETCH"]:
                    uid = int(line.split()[3])
                    conn.sendall(
                        b"* 1 FETCH (UID %d BODY[] {%d}\r\n%s)\r\n" % (uid, len(BODY), BODY)
                    )
                conn.sendall(tag + b" OK done\r\n")
        except OSError:
            # The test closed the sockets; nothing is left to answer.
            return

    def close(self) -> None:
        for sock in [*self.connections, self._listener]:
            sock.close()


@pytest.fixture
def short_timeouts(monkeypatch):
    monkeypatch.setattr(imap, "CONNECT_TIMEOUT", TIMEOUT)
    monkeypatch.setattr(imap, "READ_TIMEOUT", TIMEOUT)


@pytest.fixture
def server(request):
    servers: list[_QuietServer] = []

    def start(**kwargs) -> _QuietServer:
        servers.append(_QuietServer(**kwargs))
        return servers[-1]

    yield start
    for s in servers:
        s.close()


def _job(port: int, tls: bool = False) -> conf.JobConfig:
    return conf.JobConfig(
        name="test-job",
        server="127.0.0.1",
        port=port,
        username="user",
        password="pass",
        tls=tls,
    )


def _timed(fn):
    """Call `fn`, return what it raised and how long it took."""
    start = time.perf_counter()
    try:
        fn()
    except Exception as exc:
        return exc, time.perf_counter() - start
    pytest.fail("expected an exception")


@pytest.mark.usefixtures("short_timeouts")
class TestAServerThatStopsAnswering:
    def test_a_silent_server_ends_the_fetch_as_a_lost_connection(self, server):
        quiet = server(silent_on=frozenset({b"EXAMINE", b"SELECT"}))
        client = imap.ImapClient.connect(_job(quiet.port))

        exc, took = _timed(lambda: list(client.fetch_messages([1, 2], "INBOX")))

        assert isinstance(exc, base.ConnectionLost)
        assert TIMEOUT * 0.9 <= took < TIMEOUT * 3

    def test_every_command_after_it_fails_at_once(self, server):
        """Not one more timeout each: a run over many folders would wait for every one.

        Python 3.13 gets this from `socket.makefile`, which refuses every read
        after a timeout. 3.14's imaplib reads on after one, and failed here
        until the connection was closed on the first error.
        """
        quiet = server(silent_on=frozenset({b"EXAMINE", b"SELECT"}))
        client = imap.ImapClient.connect(_job(quiet.port))
        _timed(lambda: list(client.fetch_messages([1], "INBOX")))

        exc, took = _timed(lambda: list(client.fetch_messages([1], "INBOX")))

        assert isinstance(exc, base.ConnectionLost)
        assert took < TIMEOUT / 2

        start = time.perf_counter()
        client.close()
        assert time.perf_counter() - start < TIMEOUT / 2

    def test_a_login_that_is_not_answered_is_reported(self, server):
        quiet = server(silent_on=frozenset({b"LOGIN"}))

        exc, took = _timed(lambda: imap.ImapClient.connect(_job(quiet.port)))

        assert isinstance(exc, imap.MailboxError)
        assert str(exc).startswith(f"cannot log in to 127.0.0.1:{quiet.port}: ")
        assert took < TIMEOUT * 3

    def test_a_server_that_never_greets_is_reported(self, server):
        quiet = server(greet=False)

        exc, took = _timed(lambda: imap.ImapClient.connect(_job(quiet.port)))

        assert isinstance(exc, imap.MailboxError)
        assert "cannot connect to 127.0.0.1" in str(exc)
        assert took < TIMEOUT * 3

    def test_a_tls_handshake_that_never_completes_is_reported(self, server):
        """imapclient before 4.1 ignored the connect timeout for TLS and hung here.

        Run in a thread of its own, so that a regression fails the test rather
        than hanging the suite.
        """
        quiet = server(greet=False)
        outcome: list[BaseException] = []

        def connect() -> None:
            try:
                imap.ImapClient.connect(_job(quiet.port, tls=True))
            except BaseException as exc:
                outcome.append(exc)

        worker = threading.Thread(target=connect, daemon=True)
        worker.start()
        worker.join(TIMEOUT * 5)

        assert not worker.is_alive(), "the TLS handshake blocked past the connect timeout"
        assert len(outcome) == 1
        assert isinstance(outcome[0], imap.MailboxError)


@pytest.mark.usefixtures("short_timeouts")
class TestAFetchThatIsNotAnswered:
    def test_is_set_aside_on_a_new_connection(self, server, monkeypatch):
        """That the timeout arrives as a TimeoutError without an errno is what this rests on."""
        monkeypatch.setattr(imap, "FETCH_CHUNK_SIZE", 1)
        quiet = server(silent_on=frozenset({b"UID"}), quiet_connections=1)
        client = imap.ImapClient.connect(_job(quiet.port))

        outcomes = dict(client.fetch_messages([7, 8], "INBOX"))

        assert len(quiet.connections) == 2
        assert str(outcomes[7]) == "INBOX[7]: no answer from the server"
        assert outcomes[8] == base.Fetched(BODY, ["INBOX"])
