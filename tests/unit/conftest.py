"""Unit tests do not read the real mailboxes, or reach anything else live.

This exists because they did, twice, and the second time was after the fix.

Adding the nightly `learn()` block put a mail and calendar read inside
`run_night`, and eleven existing tests that call it started reaching into
the user's real Gmail -- noticed only because Google rate-limited them. A guard
went in for Google. Then the Outlook ingest landed, a test that stubbed
the Gmail and calendar ingests but not Outlook's walked straight into the
live Exchange cache, and quietly recorded three hundred messages.

That repetition is the argument for closing it by category rather than by
name. A unit test that quietly uses live data is slow, flaky and reads
private mail, and none of those announce themselves -- the suite goes green
either way. So every door to real data is shut here at once. A test that
needs a mailbox injects a fake; a test that reaches for a real one gets an
error naming the file that shut it.

The doors both trees need -- their keychain, Signal's paths, Docker,
every model client, their mailboxes -- are shut by one frozen
function, tests/gate/fences.py, which the gate conftest calls too. Until 8
October 2026 the keychain fence lived here alone, and a gate test read their
real key on the laptop and nothing in self-upgrade v1's sandbox: six nights'
attempts were rejected for it. What only unit tests need stays below.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from tests.gate import fences, storecopy

# The fixtures of a new test database, defined once for both trees.
store, conn = storecopy.store, storecopy.conn


def pytest_configure(config):
    # In a conftest rather than pyproject.toml, which is frozen; the gate
    # conftest registers the same, so either folder can run alone.
    config.addinivalue_line(
        "markers", "processes: starts real processes, Docker or an AppContainer; "
                   "the developer's fast tier deselects these, the gate never does")
    config.addinivalue_line(
        "markers", "docker: runs the real Docker daemon on purpose; every other test is refused it")
    config.addinivalue_line(
        "markers", "real_migrations: migrates each new database, not copied from a template")


@pytest.fixture(autouse=True)
def _no_live_mailboxes(monkeypatch, tmp_path, tmp_path_factory, request):
    # Scratch folders for the fences below, inside the test's own tmp_path,
    # which pytest removes after a few runs. They were `tempfile.mkdtemp()`
    # calls, five a test and never removed: on 6 October 2026 their %TEMP%
    # held 721,477 empty `tmp*` folders, ~11,000 more with every suite run.
    fenced = tmp_path / "fenced"
    fenced.mkdir()

    fences.live_services(monkeypatch, fenced, "a unit test", "tests/unit/conftest.py",
                         docker=bool(request.node.get_closest_marker("docker")))
    if not request.node.get_closest_marker("real_migrations"):
        storecopy.copy_new_stores(monkeypatch, tmp_path_factory.getbasetemp())

    def refuse(what: str):
        return fences.refusal("a unit test", what, "tests/unit/conftest.py")

    # Envelopes a search kept belong to the test that walked them.
    import modules.outlook_classic.outlook as outlook

    outlook._envelopes.clear()
    # The desk's rehearsal (agent/night/rehearsal.py): a child process that
    # loads Whisper and Kokoro on the GPU for minutes. A test that runs the
    # night must never start it; its verdicts are read from the test's own
    # data folder (tests/gate/fences.py).
    monkeypatch.setattr("agent.night.rehearsal._run", refuse("the desk rehearsal"))
    # The injection screen lets every item through as it came, so a test
    # reads what it planted; a test of the screening brings the real one
    # back (tests/unit/test_screening.py). Its calls out, Jev and the
    # extractor, are fenced with the other billed calls in
    # tests/gate/fences.py, and its local readers beside them -- which would
    # leave an item with a pattern in it withheld, and every other one
    # recorded unscreened.
    from core import screen as _screen

    monkeypatch.setattr("agent.screening._read",
                        lambda conn, artifact: _screen.Shown(artifact.text, "text"))
    # The web: search, and reading a page. The news summary
    # (`attention/newsdesk.py`) went looking for real articles from a
    # briefing test on 25 September. Here rather than in fences.py because
    # the gate's injection tests drive the real `fetch` over a fake network.
    monkeypatch.setattr("tools.web.search", refuse("the web"))
    monkeypatch.setattr("tools.web.fetch", refuse("the web"))
    # Their saved logins (core/browser/vault.py): the real one is their keychain,
    # DPAPI and data/browser. Every test gets a throwaway one instead.

    import core.browser.vault as vault_module

    class _Keychain(dict):
        def get(self, key, default=None):  # noqa: D401 - the keyring's shape
            return super().get(key, default)

        def set(self, key, value):
            self[key] = value

        def delete(self, key):
            self.pop(key, None)

    test_vault = vault_module.Vault(root=fenced / "browser",
                                    keyring=_Keychain(), protect=lambda b: b"enc:" + b,
                                    unprotect=lambda b: b.removeprefix(b"enc:"))
    monkeypatch.setattr(vault_module, "_default", [test_vault])


@pytest.fixture(autouse=True)
def _lineup_as_written(monkeypatch, tmp_path):
    """The worker's startup applies the model scout's switches from
    data/model_switches.json, and several tests run that startup. Pointed at
    the live file, they would run on whatever the scout last switched to --
    and a test that loads switches of its own would leave them for the next.
    Each test gets the lineup as agent/models.py writes it, from a data
    folder of its own (tests/gate/fences.py)."""
    import agent.models as models

    yield
    models.MODELS.clear()
    models.MODELS.update(models.BASE)


@pytest.fixture(autouse=True)
def _every_feature_set_up(monkeypatch):
    """The user of most tests has set up everything, each piece behind a
    fake: a turn's prompt and tools follow what is set up
    (agent/features.py), and in a test's own data folder nothing is, so a
    test driving the mail tools would find none. What a user without them
    is told and offered is tested through `features.enabled`, which this
    leaves alone (tests/unit/test_features.py)."""
    from agent import features

    async def everything(conn):
        return features.everything()

    monkeypatch.setattr(features, "now", everything)


@pytest.fixture(autouse=True)
def _critic_in_shadow(monkeypatch):
    """The action critic in shadow, unless a test asks for it enforcing.

    It has enforced since 5 October 2026 (`critic` in core/policy.yaml).
    Enforcing, a free action it could not review becomes a card, and here it
    can never review: its call out is fenced. A test of a goal's session
    acting inside its envelope would then be testing that fence, not the
    envelope it names -- one here did, the day it switched. The critic's
    own tests set the mode they test (test_critic_wiring.py,
    tests/gate/test_critic.py)."""
    import dataclasses

    import core.policy

    real = core.policy.load_policy

    def in_shadow(*args, **kwargs):
        return dataclasses.replace(
            real(*args, **kwargs),
            critic=core.policy.CriticConfig(mode="shadow", allow_at=0.6),
        )

    monkeypatch.setattr(core.policy, "load_policy", in_shadow)


# --------------------------------------------------------------------------
# Things a test needs built, built once
# --------------------------------------------------------------------------
#
# Measured on 8 October 2026: `git init` takes 66 ms and each further git
# command ~40 ms. Twelve unit files made the same empty memories repository
# by hand. A copy of what was built once per worker is a few milliseconds,
# and one place to read. A new database is a copy too, for every test, by
# the autouse fixture above (tests/gate/storecopy.py).
#
# Nothing here is autouse, and none of it replaces what a test is *about*.


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t",
                    *args], check=True, capture_output=True)


@pytest.fixture
def folder(tmp_path):
    """`folder(files, dest=None)`: these files ({relative path: text})
    written in a folder of their own, or in `dest`. What the coding pipeline
    works in since step 6: a folder, not a repository -- so nothing to build
    once and copy, as the git repositories it replaced were."""

    def make(files: dict[str, str], *, dest: Path | None = None) -> Path:
        target = dest if dest is not None else tmp_path / "folder"
        for rel, text in files.items():
            path = target / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            # LF: a CRLF file is a different file to everything that hashes it.
            path.write_text(text, encoding="utf-8", newline="\n")
        return target

    return make


@pytest.fixture(scope="session")
def _memories_template(tmp_path_factory):
    """An empty memories repository, with an identity of its own so a commit
    works where git has none."""
    template = tmp_path_factory.mktemp("memories-template") / "memories"
    template.mkdir()
    subprocess.run(["git", "init", "-q", str(template)], check=True, capture_output=True)
    _git(template, "config", "user.email", "t@example.com")
    _git(template, "config", "user.name", "t")
    return template


@pytest.fixture
def memories_repo(tmp_path, monkeypatch, _memories_template):
    """A throwaway memories repository, with `tools.memory` pointed at it, so
    a test that remembers something never touches the real store."""
    root = tmp_path / "memories"
    shutil.copytree(_memories_template, root)
    monkeypatch.setenv("STEVE_MEMORIES_DIR", str(root))
    return root
