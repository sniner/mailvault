"""A Dovecot in a container, and the means to put mail into it.

The server is started once per session and torn down afterwards; each test gets
a mailbox of its own inside it, named after the test, so nothing has to be
cleaned up between them and a failure leaves its own mailbox behind to look at.

Mail is put in over IMAP rather than by writing Maildir files. It is slower and
it is the point: what the tests then read back has been through the server's own
parsing, naming and UID assignment, which is the half a mock cannot have.
"""

from __future__ import annotations

import shutil
import socket
import ssl
import subprocess
import time
from typing import Any

import pytest

from mailvault import conf

IMAGE = "dovecot/dovecot:latest"
PASSWORD = "secret"

# Dovecot's own ports inside the image; the host side is assigned by Docker so
# that a run does not collide with anything already listening.
PLAIN_PORT = 31143
TLS_PORT = 31993

# The image refuses cleartext authentication out of the box, which is right for
# a mail server and wrong for a test that wants to exercise the `tls = false`
# path -- the one a Proton Bridge on 127.0.0.1 actually uses.
EXTRA_CONFIG = "auth_allow_cleartext = yes\n"

STARTUP_TIMEOUT = 30.0


class Dovecot:
    """Where the server is, and how to put mail into it."""

    def __init__(self, host: str, plain_port: int, tls_port: int) -> None:
        self.host = host
        self.plain_port = plain_port
        self.tls_port = tls_port

    def job(self, name: str, folders: list[str] | None = None, **overrides: Any):
        """A job configuration pointed at this server, cleartext by default."""
        settings: dict[str, Any] = dict(
            name=name,
            server=self.host,
            port=self.plain_port,
            username=f"{name}@example.com",
            password=PASSWORD,
            tls=False,
            folders=folders or [],
        )
        settings.update(overrides)
        return conf.JobConfig(**settings)

    def tls_job(self, name: str, folders: list[str] | None = None, **overrides: Any):
        """The same over TLS, with the certificate unverified.

        The container's certificate is self-signed, which is exactly the case
        `tls_verify_cert = false` exists for -- and the branch of `connect` that
        builds its own SSL context is otherwise only ever run against a mock.
        """
        return self.job(
            name,
            folders,
            port=self.tls_port,
            tls=True,
            tls_verify_cert=False,
            tls_check_hostname=False,
            **overrides,
        )

    def client(self, user: str, tls: bool = False):
        """A raw imapclient connection, for setting a test up and checking on it.

        Deliberately not mailvault's own client: what puts the mail in must not
        be the thing under test, or a fault in it would hide itself.
        """
        import imapclient

        if tls:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            client = imapclient.IMAPClient(
                self.host, port=self.tls_port, ssl=True, ssl_context=context
            )
        else:
            client = imapclient.IMAPClient(self.host, port=self.plain_port, ssl=False)
        client.login(f"{user}@example.com", PASSWORD)
        return client


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


def _published_port(container: str, inside: int) -> int:
    out = subprocess.run(
        ["docker", "port", container, f"{inside}/tcp"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return int(out.splitlines()[0].rsplit(":", 1)[1])


def _wait_for(host: str, port: int, deadline: float) -> None:
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError(f"{host}:{port} did not come up in time")


@pytest.fixture(scope="session")
def dovecot(tmp_path_factory) -> Any:
    """A running Dovecot, or a skip when there is no Docker to run it in."""
    if not _docker_available():
        pytest.skip("docker is not available")

    config = tmp_path_factory.mktemp("dovecot") / "zz-test.conf"
    config.write_text(EXTRA_CONFIG, encoding="utf-8")
    config.chmod(0o644)

    container = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--detach",
            "--env",
            f"USER_PASSWORD={PASSWORD}",
            "--volume",
            f"{config}:/etc/dovecot/conf.d/zz-test.conf:ro",
            "--publish",
            f"127.0.0.1::{PLAIN_PORT}",
            "--publish",
            f"127.0.0.1::{TLS_PORT}",
            IMAGE,
        ],
        capture_output=True,
        text=True,
    )
    if container.returncode != 0:
        pytest.skip(f"could not start {IMAGE}: {container.stderr.strip()}")
    name = container.stdout.strip()

    try:
        plain = _published_port(name, PLAIN_PORT)
        tls = _published_port(name, TLS_PORT)
        deadline = time.monotonic() + STARTUP_TIMEOUT
        _wait_for("127.0.0.1", plain, deadline)
        _wait_for("127.0.0.1", tls, deadline)
        yield Dovecot("127.0.0.1", plain, tls)
    finally:
        subprocess.run(["docker", "rm", "--force", name], capture_output=True)
