"""Gate tests never run the real coding pipeline, and never reach a model.

Frozen, like the rest of tests/gate. Written after the first run of the
suite once self-upgrade v1's sandbox account existed (27 September 2026): a
gate test took the pipeline path, and -- the unit tests' fences did not
reach this folder -- the coder made a real model call with the user's
OpenRouter key before failing on the stub's missing worktree. The task text
was all it sent, but a test must not reach a live model at all.

So here, for every gate test:
- the doors to live services and their accounts are shut by the same
  function the unit tree uses (tests/gate/fences.py): their keychain,
  Signal's paths, Docker, every model client, their mailboxes. They were
  fenced for unit tests only until 8 October 2026, and a gate test read
  their real OpenRouter key on the laptop and nothing as the sandbox account
  -- which rejected shadow attempts #82-#87 and their request #88 for a
  failure none of them caused;
- AppContainer profiles are made only by the module sandbox's own walls
  tests (`appcontainer`), which delete them;
- nothing can ask the live supervisor to swap its worker.
"""

from __future__ import annotations

import pytest

from tests.gate import fences, storecopy

# The fixtures of a new test database, defined once for both trees.
store, conn = storecopy.store, storecopy.conn


def pytest_configure(config):
    # Registered here as well as in tests/unit/conftest.py, so running this
    # folder alone does not warn about its own markers.
    config.addinivalue_line(
        "markers", "processes: starts real processes, Docker or an AppContainer; "
                   "the developer's fast tier deselects these, the gate never does")
    config.addinivalue_line(
        "markers", "appcontainer: may make AppContainer profiles and run processes in them")
    config.addinivalue_line(
        "markers", "docker: runs the real Docker daemon on purpose; every other test is refused it")
    config.addinivalue_line(
        "markers", "real_migrations: migrates each new database, not copied from a template")


@pytest.fixture(autouse=True)
def _no_live_coder(monkeypatch, tmp_path, tmp_path_factory, request):
    scratch = tmp_path / "fenced"
    scratch.mkdir(exist_ok=True)
    fences.live_services(monkeypatch, scratch, "a gate test", "tests/gate/conftest.py",
                         containers=bool(request.node.get_closest_marker("appcontainer")),
                         docker=bool(request.node.get_closest_marker("docker")))
    if not request.node.get_closest_marker("real_migrations"):
        storecopy.copy_new_stores(monkeypatch, tmp_path_factory.getbasetemp())

    def refuse(what: str):
        def fail(*args, **kwargs):
            raise RuntimeError(f"a gate test tried to reach {what}; see tests/gate/conftest.py")

        return fail

    # The coder's clients are shut in fences.py too; shut again here in the
    # words test_coder_barriers.py asks for.
    monkeypatch.setattr("core.coder.models.client", refuse("a live model through the coder"))
    monkeypatch.setattr("core.coder.models._openai", refuse("OpenAI through the coder"))


@pytest.fixture(autouse=True)
def _critic_in_shadow(monkeypatch):
    """The action critic in shadow, unless a test asks for it enforcing.

    It has enforced since 5 October 2026 (`critic` in core/policy.yaml).
    Enforcing, a free action it could not review becomes a card -- and here
    it never can, its call out being refused above. The day it switched,
    six gate tests of what a routine's run or a goal's session may do inside
    its envelope were testing that refusal instead. test_critic.py sets the
    mode it tests."""
    import dataclasses

    import core.policy

    real = core.policy.load_policy

    def in_shadow(*args, **kwargs):
        return dataclasses.replace(
            real(*args, **kwargs),
            critic=core.policy.CriticConfig(mode="shadow", allow_at=0.6),
        )

    monkeypatch.setattr(core.policy, "load_policy", in_shadow)
