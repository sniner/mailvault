"""Runs that are cut short and power that goes, against a real server.

Messages are written without a flush (`cas.Flush`), so a power loss can leave any
message stored since the last flush empty, cut short, or without its name.
`_power_loss` does that to the entries a test names: what the disk may have kept
is all a test can play, because the page cache itself cannot be dropped from
here. The log and the heads are flushed as they are written and survive.

Every test ends on the same two questions: is each message the server held in
the archive, intact (`_lost` is empty), and does `archive check` find the
archive sound.
"""

from __future__ import annotations

import multiprocessing
import os
import pathlib
import signal
import socket
import time
from typing import Any

import imapclient
import pytest

from mailvault import conf, jobs
from mailvault.jobs import readback
from mailvault.store import cas, marker, metalog
from tests.integration import corpus

pytestmark = pytest.mark.integration


def _archive(tmp_path: pathlib.Path) -> pathlib.Path:
    marker.write(tmp_path)
    return tmp_path


def _fill(server, user: str, folder: str, messages: list[bytes]) -> None:
    client = server.client(user)
    try:
        if folder != "INBOX" and not client.folder_exists(folder):
            client.create_folder(folder)
        for message in messages:
            client.append(folder, message)
    finally:
        client.logout()


def _on_server(server, user: str, folder: str = "INBOX") -> set[bytes]:
    """The messages a folder holds, as the server sends them."""
    client = server.client(user)
    try:
        client.select_folder(folder, readonly=True)
        uids = client.search(["NOT", "DELETED"])
        if not uids:
            return set()
        return {data[b"BODY[]"] for data in client.fetch(uids, ["BODY.PEEK[]"]).values()}
    finally:
        client.logout()


def _lost(original: set[bytes], archive: pathlib.Path) -> set[bytes]:
    """What of `original` the archive does not hold intact."""
    store = cas.mail_store(archive)
    intact = {store.read(path) for path in store.walk() if store.verify(path)}
    return original - intact


def _entries(archive: pathlib.Path) -> list[pathlib.Path]:
    return sorted(cas.mail_store(archive).walk())


def _power_loss(paths: list[pathlib.Path]) -> None:
    """What a disk may have kept of files nobody flushed: cut short, empty, or no name."""
    for number, path in enumerate(paths):
        path.chmod(0o644)
        if number % 3 == 0:
            path.write_bytes(path.read_bytes()[: path.stat().st_size // 2])
        elif number % 3 == 1:
            path.write_bytes(b"")
        else:
            path.unlink()


def _an_hour_later(archive: pathlib.Path) -> None:
    """Age the log as a run an hour later finds it; reading back waits that long."""
    past = time.time() - readback.MIN_AGE - 60
    for path in metalog.log_files(archive / metalog.DEFAULT_LOG_DIR):
        os.utime(path, (past, past))


def _backup(job: conf.JobConfig, archive: pathlib.Path) -> jobs.BackupReport:
    """`mailvault backup` as the command runs it: read back first, then the job."""
    jobs.read_back(archive)
    return jobs.backup(job, archive)


def _cut_at_the_second_fetch(monkeypatch, then: Any) -> None:
    """Do `then` to the connection when the second chunk of bodies is asked for.

    The backup fetches ten bodies at a time, so ten messages are stored by then.
    """
    real_fetch = imapclient.IMAPClient.fetch
    calls = 0

    def fetch(self, messages, data, modifiers=None):
        nonlocal calls
        calls += 1
        if calls == 2:
            then(self)
        return real_fetch(self, messages, data, modifiers)

    monkeypatch.setattr(imapclient.IMAPClient, "fetch", fetch)


def _drop(client: imapclient.IMAPClient) -> None:
    client.socket().shutdown(socket.SHUT_RDWR)


def _interrupt(client: imapclient.IMAPClient) -> None:
    raise KeyboardInterrupt


def _sound(archive: pathlib.Path) -> bool:
    result = jobs.check(archive)
    return result.sound and not result.orphans


class TestAPowerLoss:
    def test_after_a_first_backup(self, dovecot, tmp_path):
        """A run within the hour leaves it; the first one after it fetches it all again."""
        user = "pl-after"
        _fill(dovecot, user, "INBOX", corpus.everything())
        original = _on_server(dovecot, user)
        archive = _archive(tmp_path)
        job = dovecot.job(user, ["INBOX"])
        assert _backup(job, archive).stored == len(original)

        _power_loss(_entries(archive))
        soon = _backup(job, archive)
        _an_hour_later(archive)
        later = _backup(job, archive)

        assert soon.stored == 0, "too soon to read back, and nothing new"
        assert later.stored > 0
        assert _lost(original, archive) == set()
        assert _sound(archive)
        assert _backup(job, archive).stored == 0, "and incremental again"

    def test_in_the_middle_of_a_first_backup(self, dovecot, tmp_path, monkeypatch):
        user = "pl-middle"
        _fill(dovecot, user, "INBOX", corpus.numbered(25))
        original = _on_server(dovecot, user)
        archive = _archive(tmp_path)
        job = dovecot.job(user, ["INBOX"])
        _cut_at_the_second_fetch(monkeypatch, _drop)
        first = _backup(job, archive)
        monkeypatch.undo()
        assert first.not_fetched == 15

        _power_loss(_entries(archive))
        carried_on = _backup(job, archive)
        _an_hour_later(archive)
        _backup(job, archive)

        assert carried_on.stored == 15, "the rest, from where the first run broke off"
        assert _lost(original, archive) == set()
        assert _sound(archive)

    def test_after_ctrl_c_in_the_middle_of_a_backup(self, dovecot, tmp_path, monkeypatch):
        user = "pl-ctrlc"
        _fill(dovecot, user, "INBOX", corpus.numbered(25))
        original = _on_server(dovecot, user)
        archive = _archive(tmp_path)
        job = dovecot.job(user, ["INBOX"])
        _cut_at_the_second_fetch(monkeypatch, _interrupt)
        with pytest.raises(KeyboardInterrupt):
            _backup(job, archive)
        monkeypatch.undo()
        assert len(_entries(archive)) == 10

        _power_loss(_entries(archive))
        _backup(job, archive)
        _an_hour_later(archive)
        _backup(job, archive)

        assert _lost(original, archive) == set()
        assert _sound(archive)

    def test_of_a_message_filed_in_two_folders(self, dovecot, tmp_path):
        user = "pl-twice"
        _fill(dovecot, user, "INBOX", [*corpus.PLAIN, corpus.IN_TWO_PLACES])
        _fill(dovecot, user, "Archive", [corpus.IN_TWO_PLACES])
        original = _on_server(dovecot, user) | _on_server(dovecot, user, "Archive")
        archive = _archive(tmp_path)
        job = dovecot.job(user, ["INBOX", "Archive"])
        _backup(job, archive)
        store = cas.mail_store(archive)
        twice = store.locate(store.hashval(corpus.IN_TWO_PLACES), exists=True)
        assert twice is not None

        _power_loss([twice])
        _an_hour_later(archive)
        _backup(job, archive)

        assert _lost(original, archive) == set()
        assert _sound(archive)

    def test_and_then_a_repair(self, dovecot, tmp_path):
        """`verify --repair` reads back first as well, and stores what it set aside."""
        user = "pl-repair"
        _fill(dovecot, user, "INBOX", corpus.everything())
        original = _on_server(dovecot, user)
        archive = _archive(tmp_path)
        job = dovecot.job(user, ["INBOX"])
        _backup(job, archive)

        _power_loss(_entries(archive))
        _an_hour_later(archive)
        jobs.read_back(archive)
        (result,) = jobs.verify(job, archive, repair=True)

        assert result.restored > 0
        assert _lost(original, archive) == set()
        assert _sound(archive)


class TestAPowerLossWhileDeleting:
    """`delete_after_export`: here a mistake costs the only copy there is."""

    def test_before_the_flush_the_server_keeps_everything(self, dovecot, tmp_path, monkeypatch):
        """The flush goes, the power with it, and the next run comes straight after.

        Too soon to read back: what saves the mail is that the next run reads each
        message it finds in the archive before the purge, and replaces a damaged one.
        """
        user = "pld-flush"
        _fill(dovecot, user, "INBOX", corpus.numbered(12))
        original = _on_server(dovecot, user)
        archive = _archive(tmp_path)
        job = dovecot.job(user, ["INBOX"], delete_after_export=True)

        def power_goes(paths):
            raise OSError("the power went")

        monkeypatch.setattr(cas.atomic, "sync_together", power_goes)
        _backup(job, archive)
        monkeypatch.undo()
        assert _on_server(dovecot, user) == original, "nothing deleted"

        _power_loss(_entries(archive))
        _backup(job, archive)

        assert _on_server(dovecot, user) == set(), "deleted now"
        assert _lost(original, archive) == set()
        assert _sound(archive)

    def test_a_connection_lost_in_the_middle(self, dovecot, tmp_path, monkeypatch):
        user = "pld-drop"
        _fill(dovecot, user, "INBOX", corpus.numbered(25))
        original = _on_server(dovecot, user)
        archive = _archive(tmp_path)
        job = dovecot.job(user, ["INBOX"], delete_after_export=True)
        _cut_at_the_second_fetch(monkeypatch, _drop)
        _backup(job, archive)
        monkeypatch.undo()

        _backup(job, archive)

        assert _on_server(dovecot, user) == set()
        assert _lost(original, archive) == set()
        assert _sound(archive)


def _killed_backup(job: conf.JobConfig, archive: pathlib.Path, waiting: Any) -> None:
    """A backup that stops dead at its second chunk, to be killed there."""

    def wait_there(client: imapclient.IMAPClient) -> None:
        waiting.set()
        time.sleep(600)

    pytest.MonkeyPatch().setattr(imapclient.IMAPClient, "fetch", _fetch_with(wait_there))
    jobs.backup(job, archive)


def _fetch_with(then: Any) -> Any:
    real_fetch = imapclient.IMAPClient.fetch
    calls = 0

    def fetch(self, messages, data, modifiers=None):
        nonlocal calls
        calls += 1
        if calls == 2:
            then(self)
        return real_fetch(self, messages, data, modifiers)

    return fetch


class TestAProcessThatIsKilled:
    @pytest.mark.parametrize("deleting", [False, True], ids=["keeping", "deleting"])
    def test_kill_9_in_the_middle_of_a_first_backup(self, dovecot, tmp_path, deleting):
        """No `finally` runs: nothing written down, ten messages lying in the store."""
        user = f"kill-{'del' if deleting else 'keep'}"
        _fill(dovecot, user, "INBOX", corpus.numbered(25))
        original = _on_server(dovecot, user)
        archive = _archive(tmp_path)
        job = dovecot.job(user, ["INBOX"], delete_after_export=deleting)
        context = multiprocessing.get_context("fork")
        waiting = context.Event()
        child = context.Process(target=_killed_backup, args=(job, archive, waiting))
        child.start()
        try:
            assert waiting.wait(30), "the backup never reached its second chunk"
        finally:
            os.kill(child.pid or 0, signal.SIGKILL)
            child.join()
        assert len(_entries(archive)) == 10
        assert _on_server(dovecot, user) == original, "nothing deleted"

        _backup(job, archive)

        assert _lost(original, archive) == set()
        assert _sound(archive)
        if deleting:
            assert _on_server(dovecot, user) == set()
