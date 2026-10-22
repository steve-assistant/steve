"""The end-to-end tests: Steve's backend, as shipped, in a fake world.

Each test file starts one backend (tests/e2e/backend.py) in one world
(tests/fakes/world.py), for all its tests: a start is about ten seconds,
and each test uses its own conversation, card and account. The doors are
shut as for every other test, by tests/gate/fences.py: once for the file,
for the backend and everything it starts, and again for each test's own
process, as tests/unit and tests/gate do.

After each test the canaries are read: a secret seen out of its one place,
in the data folder, the backend's log or any fake's requests, fails the
test that put it there, with the secret and the place named.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.e2e.backend import Backend
from tests.gate import fences

WHO, WHERE = "an end-to-end test", "tests/e2e/conftest.py"


def pytest_configure(config):
    # Registered here as well as in tests/unit/conftest.py, so running this
    # folder alone does not warn about its own markers.
    config.addinivalue_line(
        "markers", "processes: starts real processes, Docker or an AppContainer; "
                   "the developer's fast tier deselects these, the gate never does")
    config.addinivalue_line(
        "markers", "appcontainer: may make AppContainer profiles and run processes in them")


def pytest_collection_modifyitems(items):
    # A group of their own, on one worker, beside the zero-downtime gate's.
    # They shared its group from 19 October 2026: a backend's supervisor,
    # starting, reaped the orphan that gate makes on purpose. That was the
    # gate's orphan naming a data folder never made, which marks an orphan
    # anyone's to reap, and the real app a screen test opens did the same;
    # the gate now makes its folder. Together they were one worker's
    # 550-620 s, the floor under the whole suite's time.
    for item in items:
        if Path(item.fspath).parent.name == "e2e":
            item.add_marker(pytest.mark.processes)
            item.add_marker(pytest.mark.xdist_group("e2e"))


def _containers(request) -> bool:
    """A file marked `appcontainer` (its backend runs modules in the real
    sandbox) may reach the profiles from the test too, to delete what its
    backend made, as the walls' own tests do."""
    return bool(request.node.get_closest_marker("appcontainer"))


@pytest.fixture(scope="module")
def fenced(tmp_path_factory, request) -> Path:
    """The file's folder, with the doors shut for the backend it starts:
    its data folder is `fenced / "data"`."""
    scratch = tmp_path_factory.mktemp("e2e") / "fenced"
    scratch.mkdir()
    with pytest.MonkeyPatch.context() as patched:
        fences.live_services(patched, scratch, WHO, WHERE, containers=_containers(request))
        yield scratch


@pytest.fixture(autouse=True)
def _fenced_test(monkeypatch, tmp_path, request):
    scratch = tmp_path / "fenced"
    scratch.mkdir(exist_ok=True)
    fences.live_services(monkeypatch, scratch, WHO, WHERE, containers=_containers(request))


@pytest.fixture(autouse=True)
def _canaries(request):
    yield
    for value in request.node.funcargs.values():
        if isinstance(value, Backend):
            leaks = value.leaks()
            assert not leaks, f"secrets out of their place: {leaks}"
