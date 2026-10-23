"""Setup's backend answers the first-run pages, and resumes where it was left.

A stranger's first minutes: a key checked before it is kept, each page done
when what it set is there, setup done only on its last page, a preset in
force at once, and the drive's encryption said plainly
(agent/worker/setup.py). The keys go to a fake OpenAI on this computer
(tests/fakes/openai.py), named in `STEVE_OPENAI_URL`.
"""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path

import httpx
import pytest
from starlette.applications import Starlette

from agent import facts, runtime
from agent.attention import settings
from agent.worker import control, envfile, setup
from core import db, knobs, passphrase, secrets
from tests.fakes import jev
from tests.fakes.jev import FakeJev
from tests.fakes.openai import BROKE, GOOD, FakeOpenAI


class _Keychain:
    """The Credential Manager, for one test: what setup keeps, it keeps here."""

    def __init__(self) -> None:
        self.held: dict[str, str] = {}

    def get_password(self, service: str, key: str) -> str | None:
        return self.held.get(key)

    def set_password(self, service: str, key: str, value: str) -> None:
        self.held[key] = value


class _Client:
    """Setup's routes called on this thread, as the worker's loop calls them."""

    def __init__(self) -> None:
        self.app = Starlette(routes=setup.ROUTES)

    def _call(self, method: str, path: str, **kw) -> httpx.Response:
        async def go():
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
                return await c.request(method, path, **kw)

        return asyncio.run(go())

    def get(self, path: str) -> dict:
        return self._call("GET", path).json()

    def post(self, path: str, **kw) -> httpx.Response:
        return self._call("POST", path, **kw)


@pytest.fixture
def conn(monkeypatch):
    c = db.open_store()
    monkeypatch.setattr(runtime, "conn", c)
    for name in ("MAX_DAY_PINGS", "MAX_NIGHT_PINGS", "FOLLOW_UP_AFTER"):
        monkeypatch.setattr(settings, name, getattr(settings, name))
    control.defaults()
    yield c
    c.close()


@pytest.fixture
def keychain(monkeypatch):
    held = _Keychain()
    monkeypatch.setattr(secrets.keyring, "get_password", held.get_password)
    monkeypatch.setattr(secrets.keyring, "set_password", held.set_password)
    return held


@pytest.fixture
def openai(monkeypatch):
    fake = FakeOpenAI()
    with fake.running() as url, FakeJev().running() as jev_url:
        monkeypatch.setenv("STEVE_OPENAI_URL", url)
        monkeypatch.setenv("STEVE_OPENROUTER_URL", jev_url)
        yield fake


@pytest.fixture
def client(conn, keychain):
    return _Client()


def _done(client) -> dict[str, bool]:
    return {p["page"]: p["done"] for p in client.get("/setup/state")["pages"]}


def test_a_fresh_install_starts_at_hello_with_nothing_done(client):
    """Nothing is assumed: an empty keychain and data folder are a first run."""
    state = client.get("/setup/state")
    assert state["next"] == "hello" and not state["done"]
    assert not any(_done(client).values())
    assert [p["page"] for p in state["pages"] if p["required"]] == [
        "openai", "cap", "name", "passphrase", "preset", "login"]


def test_a_key_that_works_is_kept_and_its_page_is_done(client, keychain, openai):
    """A key checked with OpenAI before it is kept is a key the first answer
    can use."""
    said = client.post("/setup/key", json={"service": "openai", "key": f"  {GOOD}\n"})
    assert said.status_code == 200 and said.json()["ok"]
    assert keychain.held == {"OPENAI_API_KEY": GOOD}
    assert openai.asked == [("/v1/models", GOOD)]
    assert _done(client)["openai"]

    said = client.post("/setup/key", json={"service": "openrouter", "key": jev.GOOD}).json()
    assert said["ok"] and said["credit"] == 7.5
    assert keychain.held["OPENROUTER_API_KEY"] == jev.GOOD and _done(client)["jev"]


def test_which_keys_are_kept_is_said_and_a_key_given_again_replaces_the_one_kept(
        client, keychain, openai):
    """Setup was the only place to give a key: one skipped there, or one to
    change, took a PowerShell command, and the first install found nowhere
    in the app to see or change one (22 October 2026). Settings: Accounts
    reads which are kept here, and keeps a new one as setup does."""
    keychain.held["OPENAI_API_KEY"] = "sk-old"  # pragma: allowlist secret
    assert client.get("/setup/keys")["keys"] == {"openai": True, "openrouter": False}

    said = client.post("/setup/key", json={"service": "openai", "key": GOOD}).json()
    assert said["ok"] and keychain.held["OPENAI_API_KEY"] == GOOD
    assert client.post("/setup/key", json={"service": "openrouter", "key": jev.GOOD}).json()["ok"]
    assert client.get("/setup/keys")["keys"] == {"openai": True, "openrouter": True}


def test_a_refused_key_says_why_and_is_not_kept(client, keychain, openai, monkeypatch):
    """Kept, a wrong key would be found out at the first answer, which says
    only that something failed."""
    for service, key, why in (("openai", "sk-proj-typed-wrong", "wrong-key"),
                              ("openai", BROKE, "no-credit"),
                              ("openrouter", "sk-or-typed-wrong", "wrong-key"),
                              ("openrouter", jev.BROKE, "no-credit"),
                              ("openai", "sk proj with spaces", "shape"),
                              ("openai", "", "shape")):
        said = client.post("/setup/key", json={"service": service, "key": key})
        assert said.status_code == 400 and said.json()["why"] == why, (service, key)
    assert "copied whole" in client.post("/setup/key", json={
        "service": "openai", "key": "sk-proj-typed-wrong"}).json()["error"]

    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    monkeypatch.setenv("STEVE_OPENAI_URL", f"http://127.0.0.1:{closed.getsockname()[1]}/v1")
    closed.close()
    away = client.post("/setup/key", json={"service": "openai", "key": GOOD}).json()
    assert away["why"] == "unreachable" and "internet connection" in away["error"]
    assert keychain.held == {}


def test_a_key_kept_from_before_leaves_its_page_to_be_seen_and_kept_there(client, keychain):
    """A key an earlier install left in the Credential Manager skipped the
    key page: the user was never asked for a key, and never learned where
    theirs was kept or that it could be changed. The page offers to keep
    it, and is done once it has been kept there."""
    keychain.held["OPENAI_API_KEY"] = GOOD
    state = client.get("/setup/state")
    assert state["key_kept"] and not _done(client)["openai"]
    assert client.post("/setup/answer", json={"page": "hello", "answer": "seen"}).json()["ok"]
    assert client.get("/setup/state")["next"] == "openai"

    kept = client.post("/setup/answer", json={"page": "openai", "answer": "kept"}).json()
    assert kept["ok"] and _done(client)["openai"]
    assert keychain.held == {"OPENAI_API_KEY": GOOD}


def test_keeping_a_key_when_none_is_kept_is_refused(client, keychain):
    """"Keep this key" with nothing kept would mark the page done with no
    key behind it, and the first answer would fail."""
    said = client.post("/setup/answer", json={"page": "openai", "answer": "kept"})
    assert said.status_code == 400 and "no key" in said.json()["error"]
    assert not _done(client)["openai"]


def test_a_credits_answer_of_another_shape_is_said_not_raised(client, keychain, monkeypatch):
    """OpenRouter's answer was read as an object: a list in its place raised,
    and the page could say only that something failed."""

    class _AnswersAList(httpx.AsyncClient):
        def __init__(self, **kw):
            kw.setdefault("transport", httpx.MockTransport(
                lambda request: httpx.Response(200, json=[{"total_credits": 10.0}])))
            super().__init__(**kw)

    monkeypatch.setattr(httpx, "AsyncClient", _AnswersAList)
    said = client.post("/setup/key", json={"service": "openrouter", "key": jev.GOOD})
    assert said.status_code == 400 and said.json()["why"] == "no-credit"
    assert keychain.held == {}


def test_setup_closed_halfway_resumes_at_the_first_page_not_done(client, keychain, conn):
    """Closing the window mid-setup must neither start it over nor count it
    as done: each page is read from what it set."""
    assert client.post("/setup/answer", json={"page": "hello", "answer": "seen"}).json()["ok"]
    keychain.held["OPENAI_API_KEY"] = GOOD
    assert client.post("/setup/answer", json={"page": "openai", "answer": "kept"}).json()["ok"]
    knobs.write({"MONTH_USD": 30})
    facts.add(conn, facts.USER, "name", "Maya Lindqvist", source="confirmed")

    state = client.get("/setup/state")
    assert state["next"] == "jev" and not state["done"]
    assert client.post("/setup/answer", json={"page": "jev", "answer": "skipped"}).json()["ok"]
    assert client.get("/setup/state")["next"] == "passphrase"


def test_setup_is_done_only_on_its_last_page_once_every_required_page_is(client, keychain,
                                                                           conn):
    """Done too early, the journal would open on a Steve with no key or no
    passphrase for a high-risk yes."""
    keychain.held["OPENAI_API_KEY"] = GOOD
    assert client.post("/setup/answer", json={"page": "openai", "answer": "kept"}).json()["ok"]
    early = client.post("/setup/answer", json={"page": "privacy"})
    assert early.status_code == 400
    assert "cap, name, passphrase, preset, login" in early.json()["error"]

    knobs.write({"MONTH_USD": 30})
    facts.add(conn, facts.USER, "name", "Maya Lindqvist", source="confirmed")
    assert passphrase.choose("correct horse battery") is None
    assert client.post("/setup/preset", json={"preset": "balanced"}).json()["ok"]
    assert client.post("/setup/answer", json={"page": "login", "answer": "off"}).json()["ok"]
    assert client.get("/setup/state")["next"] == "hello"

    finished = client.post("/setup/answer", json={"page": "privacy"}).json()
    assert finished["ok"] and finished["done"] and finished["next"] is None
    # The optional pages left unvisited stay not done, and say so.
    assert not _done(client)["channel"]


def test_the_docker_page_comes_after_accounts_and_is_done_once_answered_never_required(
        client, monkeypatch):
    """Docker Desktop is a large install that needs an administrator, so
    setup goes on without it; the page says what this laptop has."""
    monkeypatch.setattr(setup, "docker", lambda: {"installed": True, "running": False})
    assert client.get("/setup/docker") == {"ok": True, "installed": True, "running": False}

    pages = client.get("/setup/state")["pages"]
    names = [p["page"] for p in pages]
    assert names.index("docker") == names.index("accounts") + 1
    assert not any(p["required"] for p in pages if p["page"] == "docker")
    assert not _done(client)["docker"]
    assert client.post("/setup/answer", json={"page": "docker", "answer": "on"}).json()["ok"]
    assert _done(client)["docker"]
    assert client.post("/setup/answer",
                       json={"page": "docker", "answer": "installed"}).status_code == 400


def test_docker_desktop_is_read_by_its_program_and_its_pipe_and_never_asked(
        tmp_path, monkeypatch):
    """The page asks every few seconds while it is open, and asking Docker
    anything wakes Docker Desktop from its resource saver. Its program on
    disk, or its command on PATH, says it is installed; its engine's pipe
    says it runs."""
    pipes = tmp_path / "pipes"
    pipes.mkdir()
    desktop = tmp_path / "Docker Desktop.exe"
    monkeypatch.setattr(setup.shutil, "which", lambda name: None)
    monkeypatch.setattr(setup.subprocess, "run", lambda *a, **k: pytest.fail("ran a program"))

    assert setup.docker_in(str(pipes), desktop) == {"installed": False, "running": False}
    desktop.write_bytes(b"")
    assert setup.docker_in(str(pipes), desktop) == {"installed": True, "running": False}
    (pipes / "docker_engine").write_bytes(b"")
    assert setup.docker_in(str(pipes), desktop) == {"installed": True, "running": True}

    desktop.unlink()
    (pipes / "docker_engine").unlink()
    monkeypatch.setattr(setup.shutil, "which", lambda name: "C:/elsewhere/docker.exe")
    assert setup.docker_in(str(pipes), desktop) == {"installed": True, "running": False}
    assert setup.docker_in(str(tmp_path / "no pipes"), desktop)["running"] is False


def test_a_preset_is_in_force_at_once_and_balanced_puts_back_what_it_moved(client):
    """Balanced is today's Steve: chosen after Quiet, it must not leave
    Quiet's limits behind."""
    assert client.post("/setup/preset", json={"preset": "quiet"}).json()["preset"] == "quiet"
    assert (settings.MAX_DAY_PINGS, settings.MAX_NIGHT_PINGS, settings.FOLLOW_UP_AFTER) == (
        4, 1, "off")

    back = client.post("/setup/preset", json={"preset": "balanced"}).json()
    assert back["preset"] == "balanced" and back["answered"]["preset"] == "balanced"
    assert (settings.MAX_DAY_PINGS, settings.MAX_NIGHT_PINGS, settings.FOLLOW_UP_AFTER) == (
        8, 2, "judged")
    assert not set(knobs.read()) & set(setup.PRESET_KNOBS)

    knobs.write({"MAX_DAY_PINGS": 6})
    assert client.get("/setup/state")["preset"] is None, "moved by hand, no preset"
    assert client.post("/setup/preset", json={"preset": "loud"}).status_code == 400


def test_a_page_takes_only_its_own_answers_here(client):
    """A page whose state is set elsewhere can't be marked done by saying so."""
    for body in ({"page": "login", "answer": "maybe"}, {"page": "openai", "answer": "seen"},
                 {"page": "preset", "answer": "quiet"}, {"page": "name", "answer": "Maya"},
                 {}):
        assert client.post("/setup/answer", json=body).status_code == 400, body
    assert not any(_done(client).values())


@pytest.mark.parametrize(("found", "protected", "says"), [
    (1, True, "is encrypted"), (6, True, "is encrypted"), (2, False, "isn't encrypted"),
    (3, False, "isn't encrypted"), (5, False, "isn't encrypted"), (None, None, "")])
def test_the_last_page_says_plainly_whether_the_system_drive_is_encrypted(
        client, monkeypatch, found, protected, says):
    """Unsaid, a stolen laptop gives up every conversation Steve keeps; and
    when Windows can't tell, setup says nothing rather than guess."""
    monkeypatch.setattr(setup, "bitlocker", lambda drive: found)
    monkeypatch.setattr(setup, "shared", lambda folder: False)
    drive = client.get("/setup/drive")
    assert drive["protected"] is protected and says in drive["says"]
    assert bool(drive["says"]) == (found is not None)


READ = 0x1200A9  # read and execute, as core/sandbox/grants.READ
WRITE_ONLY = 0x2  # FILE_WRITE_DATA


def test_the_drive_checked_is_the_data_folders_and_its_warning_names_the_phone_link_and_sign_ins(
        monkeypatch, tmp_path):
    """Checking the system drive passed a data folder kept on an unencrypted
    second drive, and the warning named the conversations alone, though the
    Signal link and the sign-ins lie beside them (the step 11 audit, E7)."""
    asked = []
    monkeypatch.setattr(setup.paths, "data_dir", lambda: Path("D:/Steve data"))
    monkeypatch.setattr(setup, "bitlocker", lambda drive: asked.append(drive) or 2)
    monkeypatch.setattr(setup, "shared", lambda folder: False)
    said = setup.drive()
    assert asked == ["D:"] and said["drive"] == "D:"
    assert "(D:) isn't encrypted" in said["says"]
    assert "Signal" in said["says"] and "sign-ins" in said["says"]


@pytest.mark.parametrize(("given", "mask", "others"), [
    (None, 0, False), ("S-1-5-32-544", READ, False), ("S-1-15-2-1-2-3-4-5-6-7", READ, False),
    ("S-1-5-32-545", WRITE_ONLY, False), ("S-1-5-32-545", READ, True), ("S-1-5-11", READ, True)],
    ids=["the user alone", "administrators", "an app container", "users writing only", "users",
         "authenticated users"])
def test_a_data_folder_another_account_can_read_is_said_and_one_kept_to_the_user_is_not(
        monkeypatch, tmp_path, given, mask, others):
    """A folder chosen at a drive's root lets every signed-in account read
    what Steve keeps; inside the profile, no other account can (the step 11
    audit, C5). SYSTEM, the Administrators and app containers are no other
    account's, and a right that does not read reads nothing."""
    from core import localapi
    from core.sandbox import grants

    folder = tmp_path / "chosen"
    folder.mkdir()
    localapi.owner_only(folder)
    if given:
        grants.grant(folder, given, mask)
    assert setup.shared(folder) is others
    monkeypatch.setattr(setup.paths, "data_dir", lambda: folder)
    monkeypatch.setattr(setup, "bitlocker", lambda drive: None)
    said = setup.drive()["says"]
    assert ("Other accounts on this computer can read" in said) is others
    assert (str(folder) in said) is others


@pytest.mark.processes
def test_the_drives_state_is_read_from_the_shell_without_administrator_rights():
    """The spike's way (docs/plan/spikes/s9-app.md): a count Windows gives
    any user, or nothing; never an error that stops the page."""
    assert setup.bitlocker("C:") in (None, 0, 1, 2, 3, 4, 5, 6, 7, 8)


# ---------------------------------------------------------------------------
# A .env near Steve (agent/worker/envfile.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def near(tmp_path, monkeypatch):
    """Steve's own folder, the test's own, so no real `.env` is read."""
    own = tmp_path / "above" / "steve"
    own.mkdir(parents=True)
    monkeypatch.setattr(envfile, "own_folder", lambda: own)
    return own


def _env(folder: Path, text: str) -> str:
    (folder / ".env").write_text(text, encoding="utf-8", newline="\n")
    return str(folder / ".env")


def test_keys_from_a_env_are_checked_and_kept_as_typed_ones_are(client, keychain, openai, near):
    """A file is not a way round the check: each key goes to its service
    first, and the OpenAI page is done as if it had been pasted."""
    file = _env(near, f"OPENAI_API_KEY={GOOD}\nOPENROUTER_API_KEY={jev.GOOD}\n"
                      "BRAVE_API_KEY=bsa-x\n")
    assert client.get("/setup/envfile")["files"] == [
        {"file": file, "keys": ["OPENAI_API_KEY", "OPENROUTER_API_KEY", "BRAVE_API_KEY"],
         "empty": []}]
    said = client.post("/setup/envfile", json={"file": file}).json()
    assert said["ok"] and all(k["ok"] for k in said["keys"].values())
    assert said["keys"]["OPENROUTER_API_KEY"]["credit"] == 7.5
    assert openai.asked == [("/v1/models", GOOD)]
    assert keychain.held == {"OPENAI_API_KEY": GOOD, "OPENROUTER_API_KEY": jev.GOOD,
                             "BRAVE_API_KEY": "bsa-x"}  # pragma: allowlist secret
    assert _done(client)["openai"] and _done(client)["jev"]


def test_a_refused_key_from_a_env_is_not_kept_and_its_page_is_not_done(
        client, keychain, openai, near):
    """The file's key is refused in the paste's words, so the page can
    say why and ask for one by hand."""
    file = _env(near, f"OPENAI_API_KEY=sk-proj-filed-wrong\nOPENROUTER_API_KEY={jev.GOOD}\n")
    said = client.post("/setup/envfile", json={"file": file}).json()
    assert said["keys"]["OPENAI_API_KEY"]["why"] == "wrong-key"
    assert "copied whole" in said["keys"]["OPENAI_API_KEY"]["error"]
    assert said["keys"]["OPENROUTER_API_KEY"]["ok"]
    assert keychain.held == {"OPENROUTER_API_KEY": jev.GOOD} and not _done(client)["openai"]


def test_with_two_files_the_one_chosen_is_the_one_read(client, keychain, openai, near):
    """Two `.env` files may hold different keys; the user picks, and only
    the picked one's keys are kept."""
    _env(near, "OPENAI_API_KEY=sk-proj-the-nearer-one\n")
    above = _env(near.parent, f"OPENAI_API_KEY={GOOD}\n")
    files = client.get("/setup/envfile")["files"]
    assert [f["file"] for f in files] == [str(near / ".env"), above]
    assert client.post("/setup/envfile", json={"file": above}).json()["keys"][
        "OPENAI_API_KEY"]["ok"]
    assert keychain.held == {"OPENAI_API_KEY": GOOD}


def test_unknown_and_empty_names_in_a_env_are_never_kept_or_checked(
        client, keychain, openai, near):
    """Another program's key is not Steve's, and an empty one is not a key."""
    file = _env(near, "PORTKEY_API_KEY=pk-not-steves\nOPENAI_API_KEY=\nBRAVE_API_KEY=bsa-x\n")
    said = client.post("/setup/envfile", json={"file": file}).json()
    assert list(said["keys"]) == ["BRAVE_API_KEY"]
    assert openai.asked == []
    assert keychain.held == {"BRAVE_API_KEY": "bsa-x"}  # pragma: allowlist secret
    empty = _env(near, "OPENAI_API_KEY=\n")
    refused = client.post("/setup/envfile", json={"file": empty})
    assert refused.status_code == 400
    assert keychain.held == {"BRAVE_API_KEY": "bsa-x"}  # pragma: allowlist secret


def test_a_path_steve_did_not_offer_is_refused_and_nothing_kept(
        client, keychain, openai, near, tmp_path):
    """The app sends a path; any path but one offered is refused unread."""
    (tmp_path / "elsewhere").mkdir()
    elsewhere = _env(tmp_path / "elsewhere", f"OPENAI_API_KEY={GOOD}\n")
    for given in (elsewhere, str(near / ".env"), 7, None):
        said = client.post("/setup/envfile", json={"file": given})
        assert said.status_code == 400, given
    assert keychain.held == {} and openai.asked == []


def test_a_value_from_a_env_is_never_in_a_response_or_the_log(
        client, keychain, openai, near, caplog):
    """The page shows names; a value echoed back, even refused, would sit
    in the webview's memory and any log a user shares."""
    caplog.set_level("DEBUG")
    wrong = "sk-proj-filed-wrong-and-secret"
    file = _env(near, f"OPENAI_API_KEY={wrong}\nOPENROUTER_API_KEY={jev.GOOD}\n"
                      "BRAVE_API_KEY=bsa-also-secret\n")
    texts = [str(client.get("/setup/envfile")),
             client.post("/setup/envfile", json={"file": file}).text]
    for value in (wrong, jev.GOOD, "bsa-also-secret"):
        assert not any(value in text for text in texts), value
        assert value not in caplog.text, value
    assert "OPENAI_API_KEY" in texts[0]


def test_entering_keys_by_hand_still_works_with_a_env_beside_steve(client, keychain, openai, near):
    """Declining the file leaves the paste as it was: the typed key is
    kept, and nothing is taken from the file."""
    _env(near, f"OPENROUTER_API_KEY={jev.GOOD}\nBRAVE_API_KEY=bsa-x\n")
    said = client.post("/setup/key", json={"service": "openai", "key": GOOD})
    assert said.status_code == 200 and keychain.held == {"OPENAI_API_KEY": GOOD}
    assert _done(client)["openai"] and not _done(client)["jev"]
