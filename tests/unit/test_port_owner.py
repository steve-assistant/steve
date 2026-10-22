"""Whose a listening port is (`core/localonly.served_by`).

Steve tells signal-cli and the local decision model which port to bind, and
they bind it seconds later. A program of another Windows account that took
the port first could answer as either one, and be sent the user's messages
(step 11's audit). Each asks this before it sends anything that matters, so
it must say yes only for the process that was started and its own children,
and no when anything else listens on the port too.
"""

from __future__ import annotations

import contextlib
import os
import socket
import subprocess
import sys

import pytest

from core import localonly

pytestmark = pytest.mark.processes

# A child that listens on 127.0.0.1, on the port it is given (0 for any),
# sharing it if asked, and says which port it holds.
LISTENS = """
import socket, sys
sock = socket.socket()
if sys.argv[2] == "share":
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.bind(("127.0.0.1", int(sys.argv[1])))
sock.listen()
print(sock.getsockname()[1], flush=True)
sys.stdin.read()
"""


@contextlib.contextmanager
def _child(port: int = 0, how: str = "alone"):
    """A listening child: its process and the port it holds."""
    child = subprocess.Popen([sys.executable, "-I", "-c", LISTENS, str(port), how],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        yield child, int(child.stdout.readline())
    finally:
        child.kill()
        child.wait()


def _listening(share: bool = False) -> socket.socket:
    sock = socket.socket()
    if share:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    return sock


def test_a_port_this_process_listens_on_is_its_and_not_another_programs():
    with _listening() as sock, _child() as (other, _):
        port = sock.getsockname()[1]
        assert localonly.served_by(port, os.getpid())
        assert not localonly.served_by(port, other.pid)


def test_a_port_a_child_listens_on_is_its_and_its_parents():
    """signal-cli started through a launcher is the launcher's child."""
    with _child() as (child, port):
        assert localonly.served_by(port, child.pid)
        assert localonly.served_by(port, os.getpid())


def test_a_port_nothing_listens_on_is_no_ones():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    assert not localonly.served_by(port, os.getpid())


@pytest.mark.skipif(sys.platform != "win32", reason="Windows lets two listeners share a port")
def test_a_port_another_program_listens_on_as_well_is_not_the_programs():
    """Windows lets a second socket of the same user bind a port the first
    let it share; which one a connection reaches is then not ours to say."""
    with _listening(share=True) as sock:
        port = sock.getsockname()[1]
        with _child(port, "share") as (child, held):
            assert held == port
            assert not localonly.served_by(port, child.pid)
            # Both listeners are this process's or its child's.
            assert localonly.served_by(port, os.getpid())
