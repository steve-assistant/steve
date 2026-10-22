"""What Steve's end writes into a module's pipe reaches the other end, even
when it closes at once; and a reset reaches it as an error.

core/sandbox/relay.py writes a server's last chunk into the pipe and closes
the connection straight after, as a service that answered and hung up
does. Closing used to disconnect the pipe's instance, and Windows then
discards whatever the client has not read yet: a reply that ended with its
connection could lose its tail inside a module. CI's gate job met it on 22
October 2026, as a container's read failing with EINVAL (c170922).
"""

from __future__ import annotations

import threading
import time

import pytest

from core.sandbox import pipe, profiles

# A package SID's shape: the pipe's access list names one; no container
# opens it here.
PACKAGE = "S-1-15-2-1-2-3-4-5-6-7"


def _open(name: str):
    for _ in range(50):
        try:
            return open(name, "r+b", buffering=0)  # noqa: SIM115
        except OSError:
            time.sleep(0.05)
    raise AssertionError("the pipe never opened")


def test_what_was_written_before_a_close_is_read_by_the_client_and_then_the_end():
    """The client reads only after Steve's end has written and closed, as
    a busy container does."""
    held = pipe.Pipe(PACKAGE, profiles.install())
    accepted: list[pipe.Connection | None] = []
    waiting = threading.Thread(target=lambda: accepted.append(held.accept(5)))
    waiting.start()
    try:
        with _open(held.name) as client:
            waiting.join(5)
            [connection] = accepted
            assert connection is not None
            connection.stream.write(b"the last of the reply")
            connection.close()

            assert client.read(64) == b"the last of the reply"
            assert client.read(64) == b""
    finally:
        held.close()


def test_a_reset_is_an_error_to_the_client_not_a_clean_end():
    """The relay refuses a connection with a reset. A clean end would read
    as a service that answered nothing and hung up, and Node gives a plain
    socket no error for one: a server waiting for its refused connection's
    error waited for ever (tests/gate/test_local_servers.py)."""
    held = pipe.Pipe(PACKAGE, profiles.install())
    accepted: list[pipe.Connection | None] = []
    waiting = threading.Thread(target=lambda: accepted.append(held.accept(5)))
    waiting.start()
    try:
        with _open(held.name) as client:
            waiting.join(5)
            [connection] = accepted
            assert connection is not None
            connection.close(reset=True)

            with pytest.raises(OSError):
                client.read(64)
    finally:
        held.close()
