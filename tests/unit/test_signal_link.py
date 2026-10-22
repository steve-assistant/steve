"""Linking this machine to Signal, against a signal-cli that runs.

`channels/signal.start_link` reads the link to scan while signal-cli is
still running, and `finish_link` waits for the phone. Faked at the process
boundary (tests/fakes/signal_cli.py), because the
failures worth pinning are the process's: a link that must be read before
the program ends, a program that hangs and must be killed, and the two
ways a scan can fail that `listAccounts` cannot tell apart.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from channels import signal
from core import paths
from tests.fakes import signal_cli
from tests.fakes.signal_cli import LINK, NUMBER

pytestmark = [pytest.mark.processes,
              pytest.mark.skipif(sys.platform != "win32", reason="the launcher is a .bat")]

FAKE = Path(signal_cli.__file__)


@pytest.fixture
def fake(tmp_path, monkeypatch):
    """The fake as `STEVE_SIGNAL_CLI`, run as the real one is: a launcher
    .bat. Call it with the mode it should play."""
    launcher = tmp_path / "signal-cli.bat"
    launcher.write_text(f'@"{sys.executable}" "{FAKE}" %*\r\n', encoding="utf-8")
    monkeypatch.setenv(signal.CLI_ENV, str(launcher))

    def playing(mode: str) -> None:
        signal.signal_data().mkdir(parents=True, exist_ok=True)
        (signal.signal_data() / "fake-mode").write_text(mode, encoding="utf-8")

    return playing


def _holding(text: str) -> list[Path]:
    return [path for path in paths.data_dir().rglob("*")
            if path.is_file() and text.encode() in path.read_bytes()]


def test_the_link_is_read_while_signal_cli_waits_and_written_nowhere(fake):
    """The link is a credential for the account until it is scanned or
    expires. The personal edition saved it as a PNG and a text file in the
    data folder, and deleted them only if the attempt ran to its end."""
    fake("waits")

    link, proc = signal.start_link()
    try:
        assert link == LINK and proc.poll() is None
        assert _holding(LINK) == []
    finally:
        signal.end_link(proc)

    assert proc.poll() is not None


def test_a_completed_scan_links_the_account(fake):
    fake("links")

    _, proc = signal.start_link()

    assert signal.finish_link(proc, timeout=30) == {"linked": True, "account": NUMBER}


@pytest.mark.parametrize("mode, error", [
    ("half", "The phone scanned the code, but linking did not finish. Remove the unfinished"
             " device on the phone (Signal, Settings, Linked devices), then show a new code."),
    ("fails", "Linking did not finish. Show a new code to try again."),
], ids=["scanned-not-finished", "not-scanned"])
def test_a_scan_that_did_not_finish_says_which_fix_it_needs(fake, mode, error):
    """`listAccounts` shows neither, and the fixes differ: a half-linked
    device has to be removed on the phone first."""
    fake(mode)

    _, proc = signal.start_link()

    assert signal.finish_link(proc, timeout=30) == {"linked": False, "error": error}


def test_a_code_not_scanned_in_time_ends_signal_cli(fake):
    """Left running, it holds the account folder's lock, and the next
    attempt, or the channel, waits on it forever."""
    fake("waits")

    _, proc = signal.start_link()
    outcome = signal.finish_link(proc, timeout=1)

    assert outcome == {"linked": False,
                       "error": "The code was not scanned in time. Show a new one to try again."}
    assert proc.poll() is not None


def test_signal_cli_giving_no_link_says_what_it_said(fake):
    fake("refuses")

    with pytest.raises(signal.LinkFailed, match="Error: Config file is in use by another"):
        signal.start_link()


def test_signal_cli_hanging_before_the_link_is_ended_at_the_deadline(fake, monkeypatch):
    """Read line by line, a silent signal-cli would block the read, and the
    app's "Show code" would wait for ever."""
    fake("silent")
    monkeypatch.setattr(signal, "LINK_URI_WAIT_S", 2.0)

    with pytest.raises(signal.LinkFailed, match="gave no link to scan within 2 seconds$"):
        signal.start_link()
