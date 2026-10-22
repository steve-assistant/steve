"""Files in and out of the sandboxed browser: a download is quarantined and
read only in a sealed container; an upload comes only from where the user allows,
as the bytes the user approved, and always by card.

The maintainer, 1 October 2026: downloads into a quarantine, uploads only of files the user
has granted. Frozen. No Docker, no network, no real folder of theirs: the
roots are the test's own, the browser and the reader are fakes. The design
is the browser add-on in docs/security-model.md.
"""

from __future__ import annotations

import base64
import os

import pytest

import core.browser
from agent.browser import session as sessions
from agent.toolset import browser
from agent.turn import Deps
from core.browser import driver, files


@pytest.fixture
def roots(tmp_path, monkeypatch):
    for name in ("outbox", "quarantine", "artifacts"):
        (tmp_path / name).mkdir()
    monkeypatch.setattr(files, "outbox_dir", lambda: tmp_path / "outbox")
    monkeypatch.setattr(files, "quarantine_dir", lambda: tmp_path / "quarantine")
    monkeypatch.setattr(files.paths, "artifacts_dir", lambda: tmp_path / "artifacts")
    (tmp_path / "outbox" / "cv.pdf").write_bytes(b"%PDF-1.7 her cv")
    (tmp_path / "secret.txt").write_bytes(b"not for uploading")
    return tmp_path


# ---------------------------------------------------------------------------
# Where an upload may come from
# ---------------------------------------------------------------------------


def test_a_file_in_their_outbox_may_be_uploaded(roots):
    assert files.upload_source("outbox/cv.pdf") == (roots / "outbox" / "cv.pdf").resolve()


@pytest.mark.parametrize("spec", [
    "outbox/../secret.txt", "../secret.txt", "secret.txt", "documents/secret.txt",
    "data/secret.txt",
    "outbox/missing.pdf", "outbox", "outbox/", "C:/Windows/win.ini", "/etc/passwd",
    "outbox\\..\\secret.txt",
])
def test_nothing_else_can_be_uploaded(roots, spec):
    assert isinstance(files.upload_source(spec), str), spec


def test_a_file_too_large_is_not_uploaded(roots):
    (roots / "outbox" / "big.bin").write_bytes(b"x" * (files.MAX_FILE_BYTES + 1))

    assert "larger than" in files.upload_source("outbox/big.bin")


def test_a_link_out_of_the_outbox_is_not_followed(roots):
    try:
        os.symlink(roots / "secret.txt", roots / "outbox" / "link.txt")
    except (OSError, NotImplementedError):
        pytest.skip("this account may not make symlinks")

    assert "not inside" in files.upload_source("outbox/link.txt")


# ---------------------------------------------------------------------------
# What a download is called
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("suggested, kept", [
    ("../../evil.exe", "evil.exe"), ("a/b\\c.pdf", "c.pdf"), ("", "download"),
    (".bashrc", "bashrc"), ("ticket <1>.pdf", "ticket _1_.pdf"), ("x" * 300, "x" * 100),
])
def test_a_download_is_kept_under_a_name_with_no_path_in_it(suggested, kept):
    assert files.quarantine_name(suggested) == kept
    assert driver.download_name(suggested) == kept


# ---------------------------------------------------------------------------
# The file field
# ---------------------------------------------------------------------------

FIELD = {"ref": "e5", "tag": "input", "type": "file", "label": "Resume"}


def test_a_file_goes_only_into_the_file_field_the_user_was_shown():
    assert driver.upload_refusal(FIELD, "Resume") is None
    assert driver.upload_refusal({**FIELD, "type": "text"}, "Resume")
    assert driver.upload_refusal({**FIELD, "label": "Profile photo"}, "Resume")
    assert driver.upload_refusal(None, "Resume")


# ---------------------------------------------------------------------------
# The tools
# ---------------------------------------------------------------------------


class _Page:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []
        self.box = type("B", (), {"session": "s9"})()
        self.site, self.steps = "", 1

    async def ask(self, cmd, **args):
        self.sent.append((cmd, args))
        if cmd == "describe":
            return {"ok": True, "url": "https://jobs.example/apply", "title": "Apply",
                    "fields": [], "button": FIELD, "payment": None}
        if cmd == "fetch_download":
            return {"ok": True, "name": args["name"],
                    "b64": base64.b64encode(b"%PDF ticket").decode()}
        return {"ok": True, "url": "https://jobs.example/apply"}


class _Ctx:
    def __init__(self, deps) -> None:
        self.deps = deps


def _tools() -> dict:
    found: dict = {}
    browser.register(lambda fn: found.setdefault(fn.__name__, fn))
    return found


@pytest.fixture
def page(roots, monkeypatch, tmp_path):
    from agent import actions, approvals, runtime
    from core import db

    the_page = _Page()
    conn = db.open_store(tmp_path / "files.db")
    with conn:
        conn.execute("INSERT INTO sessions (id, channel) VALUES (3, 'signal')")
    monkeypatch.setattr(core.browser, "enabled", lambda: True)
    monkeypatch.setattr(sessions, "current", lambda c: the_page)
    monkeypatch.setattr(sessions, "get", lambda c, site="": the_page)
    monkeypatch.setattr(sessions, "hold", lambda c, s: None)
    monkeypatch.setattr(sessions, "release", lambda c: None)
    monkeypatch.setattr(approvals, "_notify", None)
    monkeypatch.setattr(approvals, "_execute_tool", actions.perform)
    monkeypatch.setattr(approvals, "HOLD_SECONDS", 0)
    monkeypatch.setattr(runtime, "conn", conn)
    yield the_page, conn
    conn.close()


def _theirs(conn) -> _Ctx:
    return _Ctx(Deps(conn=conn, session_id=3, channel="signal"))


async def test_an_upload_is_a_card_naming_the_file_the_site_and_the_field(page):
    the_page, conn = page

    said = await _tools()["browse_upload"](_theirs(conn), ref="e5", file="outbox/cv.pdf")

    assert said["status"] == "awaiting_approval"
    assert [c for c, _ in the_page.sent] == ["describe"], "nothing sent before her yes"
    preview = conn.execute("SELECT preview FROM actions").fetchone()[0]
    for shown in ("cv.pdf", "jobs.example/apply", '"Resume"', "KB"):
        assert shown in preview


async def test_a_file_from_outside_their_grants_gets_no_card(page):
    the_page, conn = page

    said = await _tools()["browse_upload"](_theirs(conn), ref="e5", file="outbox/../secret.txt")

    assert said["ok"] is False and the_page.sent == []
    assert conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 0


async def test_their_yes_uploads_exactly_the_bytes_the_user_approved(page, roots):
    from agent import approvals

    the_page, conn = page
    said = await _tools()["browse_upload"](_theirs(conn), ref="e5", file="outbox/cv.pdf")
    approvals.decide(conn, said["action_id"], approve=True, by="tray")

    ran = await approvals.execute(conn, said["action_id"], hold=False)

    assert ran["ok"]
    cmd, args = the_page.sent[-1]
    assert cmd == "upload" and base64.b64decode(args["b64"]) == b"%PDF-1.7 her cv"
    assert (args["ref"], args["label"]) == ("e5", "Resume")


async def test_a_file_swapped_after_their_yes_is_not_uploaded(page, roots):
    from agent import approvals

    the_page, conn = page
    said = await _tools()["browse_upload"](_theirs(conn), ref="e5", file="outbox/cv.pdf")
    approvals.decide(conn, said["action_id"], approve=True, by="tray")
    (roots / "outbox" / "cv.pdf").write_bytes(b"something else entirely")

    ran = await approvals.execute(conn, said["action_id"], hold=False)

    assert not ran["ok"] and "changed" in ran["error"]
    assert "upload" not in [c for c, _ in the_page.sent]


async def test_a_download_is_quarantined_and_read_only_in_the_sealed_reader(page, roots,
                                                                         monkeypatch):
    """Parsed where it can reach nothing: the analysis container, no network,
    running the fixed reader -- never anything a model wrote."""
    from agent import screening
    from core import screen
    from tools import code_exec

    the_page, conn = page
    ran, watched = [], []

    def run_python(code, **kwargs):
        ran.append((code, kwargs))
        return code_exec.ExecResult(ok=True, stdout="Ticket: Boston 9:15", stderr="",
                                    exit_code=0, duration_s=0.1)

    monkeypatch.setattr(code_exec, "run_python", run_python)
    monkeypatch.setattr(screening, "_read", lambda conn, a: watched.append(
        (conn, a.source, a.source_id, a.origin, a.text)) or screen.Shown(a.text, "text"))

    said = await _tools()["read_download"](_theirs(conn), name="1-ticket.pdf")

    assert said["ok"] and said["text"] == "Ticket: Boston 9:15"
    assert said["kept_as"] == "quarantine/s9/1-ticket.pdf"
    assert (roots / "quarantine" / "s9" / "1-ticket.pdf").read_bytes() == b"%PDF ticket"
    [(code, kwargs)] = ran
    assert code == browser.EXTRACT and kwargs["network"] is False
    assert watched and watched[0][1] == "browser_download"
