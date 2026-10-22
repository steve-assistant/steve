"""The rig the app's screen tests stand on: Edge, the shell's stand-in, the real app.

Each guarantee here is one a screen test would otherwise take on trust: that
the page it drives is the one the app ships, talking to the test's backend;
that the browser reaches nothing past this computer; and that the real app,
started by a test, runs this checkout's backend behind the fences and keeps
out of the developer's own folders.
"""

from __future__ import annotations

import contextlib
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest
from playwright.sync_api import Error as BrowserError
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from tests.unit.ui import rig, walk

pytestmark = [pytest.mark.processes, pytest.mark.xdist_group("ui")]


def _recording():
    seen: list[tuple[str, str | None]] = []

    async def anything(request):
        seen.append((request.url.path, request.headers.get("authorization")))
        return JSONResponse({"detail": "not in this test"}, status_code=404)

    app = Starlette(routes=[Route("/{path:path}", anything, methods=["GET", "POST"])])
    return app, seen


def _until(check, within: float = 15.0):
    deadline = time.monotonic() + within
    while not check():
        if time.monotonic() > deadline:
            raise TimeoutError("not within the time")
        time.sleep(0.05)


def test_the_shipped_screens_open_in_edge_and_ask_the_tests_backend_with_its_token(screen):
    """A screen test is only worth its name if the page is the one the app
    ships, and its requests reach the backend the test set up, as the app
    would send them."""
    backend, seen = _recording()

    page = screen(backend, token="tok-1")
    page.wait_for_function("document.getElementById('root').innerText.length > 0")
    _until(lambda: seen)

    assert {auth for _, auth in seen} == {"Bearer tok-1"}
    asked = page.evaluate("window.__STEVE_SHELL__.calls.map(c => c.cmd)")
    assert "backend" in asked


def test_edge_reaches_nothing_past_this_computer(edge):
    """Edge is no Python, so the fences don't hold it. A name must resolve to
    nothing, and an address given as numbers must go to the refusing proxy:
    192.0.2.1 is TEST-NET-1, so if that fence were down this would time out
    rather than reach anything."""
    context = edge.new_context()
    try:
        page = context.new_page()
        failures = {}
        for url in ("http://steve-tests.example.com/", "http://192.0.2.1/"):
            with pytest.raises(BrowserError) as refused:
                page.goto(url, timeout=10_000)
            failures[url] = str(refused.value).split("\n")[0]
    finally:
        context.close()

    assert "ERR_PROXY_CONNECTION_FAILED" in failures["http://192.0.2.1/"]
    assert any(code in failures["http://steve-tests.example.com/"]
               for code in ("ERR_NAME_NOT_RESOLVED", "ERR_PROXY_CONNECTION_FAILED"))


def _kept(folder: Path) -> tuple[list[str], dict[str, float]]:
    """What a test's app could add to a developer's folder: an entry at the
    top, or a line in its logs. Not the webview's own files, which a Steve
    the developer is running changes all the time."""
    if not folder.exists():
        return [], {}
    logs = folder / "logs"
    return (sorted(p.name for p in folder.iterdir()),
            {p.name: p.stat().st_mtime for p in logs.glob("*")} if logs.exists() else {})


def _supervisor(process: psutil.Process) -> list[str] | None:
    """The command line of the app's backend, as the fenced Python started it."""
    with contextlib.suppress(psutil.Error):
        if "core.supervisor" in process.cmdline() and Path(process.exe()) == Path(sys.executable):
            return process.cmdline()
    return None


def test_the_real_app_runs_this_checkouts_backend_fenced_on_the_tests_own_folders(
        playwright, tmp_path):
    """The exit walk drives the real app. Started from a test, it must run
    its backend in the fenced Python (`-I` and `-E` dropped the old fence),
    with the checkout's code (`-I` ran the main checkout's from a worktree),
    on the test's data folder, and write nothing into the developer's
    `dev.steve.desktop` folders (its log went there)."""
    own = [Path(os.environ[v]) / "dev.steve.desktop" for v in ("LOCALAPPDATA", "APPDATA")
           if os.environ.get(v)]
    before = {str(f): _kept(f) for f in own}

    with rig.desktop_app(tmp_path, playwright) as app:
        page = app.page()
        # A locator, not wait_for_function: once that has to poll, Playwright
        # evaluates its string in the page, and the app's CSP refuses eval,
        # so under load it failed where alone it passed.
        page.locator("#root", has_text=re.compile(r"\S")).wait_for(timeout=30_000)
        _until((app.data / "run" / "backend.json").exists, rig.STARTS_WITHIN_S)
        children = psutil.Process(app.process.pid).children(recursive=True)
        supervisors = [line for line in map(_supervisor, children) if line]

    assert len(supervisors) == 1
    assert supervisors[0][1:4] == ["-E", "-s", "-B"]
    assert "backend started" in (app.data / "logs" / "app.log").read_text(encoding="utf-8")
    assert {str(f): _kept(f) for f in own} == before


def test_what_the_real_app_leaves_on_disk_is_scanned_for_the_passphrase_and_its_key(tmp_path):
    """Each test of the real app ends with this scan (`rig.desktop_app`).
    The shell's own log and WebView2's folder, where a page's cache or a
    form's saved entries would keep what was typed, were read by no scan
    until step 11's audit. Shown finding what was planted in each, in the
    encoding a program there writes (a file it cannot read is said too:
    tests/unit/test_fake_world.py)."""
    data, webview = tmp_path / "data", tmp_path / "webview"
    (data / "logs").mkdir(parents=True)
    (data / "logs" / "app.log").write_text(f"key {rig.STAND_IN_KEY}\n", encoding="utf-8")
    (webview / "EBWebView" / "Default").mkdir(parents=True)
    (webview / "EBWebView" / "Default" / "Web Data").write_bytes(
        b"SQLite format 3\x00" + walk.PASSPHRASE.encode("utf-16-le"))
    (data / "clean.txt").write_text("nothing here", encoding="utf-8")

    assert sorted(rig.left_behind(data, webview, within=0)) == [
        "the passphrase in the webview's folder (EBWebView/Default/Web Data)",
        "the stand-in key in the data folder (logs/app.log)",
    ]


HOLD = ("import msvcrt, sys, time; f = open(sys.argv[1], 'r+b'); "
        "msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1); print('held', flush=True); "
        "time.sleep(60)")


@contextlib.contextmanager
def _held_open(path: Path):
    """A child that holds a byte of `path` locked, as a browser holds its
    profile's lock files, until the block ends."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    child = subprocess.Popen([sys.executable, "-c", HOLD, str(path)], stdout=subprocess.PIPE)
    try:
        assert child.stdout.readline().strip() == b"held"
        yield child
    finally:
        child.kill()
        child.wait(10)
        child.stdout.close()


def test_a_webview_file_still_held_names_each_process_holding_it_and_its_parent(tmp_path):
    """On CI the scan found the webview's lock files held 10 s after the
    app's job had ended, and said only that it could not read them; then,
    once, that no process named the folder. Which process holds them, asked
    of Windows itself, and who started it, is the fix's first question."""
    folder = tmp_path / "webview"
    lock = folder / "EBWebView" / "LOCK"
    assert rig._holding(folder, []) == ["no process holds a file there or names the folder"]

    with _held_open(lock) as child:
        said = rig._holding(folder, [lock])
    assert any(line.startswith("holds a file: python.exe ") for line in said), said
    assert any(f" {child.pid} (no --type), " in line and f"from python.exe {os.getpid()}" in line
               for line in said), said
    # Why it could not be read, as Windows said it: a locked range.
    assert said[-1] == "LOCK now: PermissionError errno 13 Permission denied; 1 bytes", said


def test_a_webview_process_left_holding_its_folder_is_ended_and_the_scan_made_again(tmp_path):
    """Once on CI a WebView2 process outlived the job that ended the app,
    its parent gone, and the folder was still held past the scan's wait.
    The rig ends such a process, as the job should have, and scans again;
    a warning says which, so the escape is seen though the test passes."""
    data, webview = tmp_path / "data", tmp_path / "webview"
    data.mkdir()
    with _held_open(webview / "EBWebView" / "LOCK") as child:
        with pytest.warns(UserWarning, match=r"outlived its job; the rig ended .*python\.exe"):
            assert rig.left_behind(data, webview, within=0.5, stray="python.exe") == []
        assert child.wait(10) is not None


def test_a_contained_process_is_in_its_job_before_it_runs_and_runs_once_it_is(monkeypatch):
    """Started running and put in its job after, the app could start a
    WebView2 browser in between that no job held: on CI's busy runner
    browsers and their helpers outlived the app's job, holding its webview
    folder. Suspended until the job holds it, it starts nothing outside.

    Each thread's suspend count is read as Windows keeps it: psutil's
    status said "running" on CI for a process this test read as "stopped"
    here (22 October 2026)."""
    import ctypes
    from ctypes import wintypes

    from core.sandbox import job

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetProcessId.argtypes = [wintypes.HANDLE]
    kernel32.GetProcessId.restype = wintypes.DWORD
    kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenThread.restype = wintypes.HANDLE
    for name in ("SuspendThread", "ResumeThread"):
        getattr(kernel32, name).argtypes = [wintypes.HANDLE]
        getattr(kernel32, name).restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    def suspends(pid: int) -> list[int]:
        """Each thread's suspend count: suspended once more and back."""
        counts = []
        for thread in psutil.Process(pid).threads():
            handle = kernel32.OpenThread(0x0002, False, thread.id)  # THREAD_SUSPEND_RESUME
            counts.append(kernel32.SuspendThread(handle))
            kernel32.ResumeThread(handle)
            kernel32.CloseHandle(handle)
        return counts

    seen, contain = [], job.contain

    def watched(handle: int, *limits):
        seen.append(suspends(kernel32.GetProcessId(handle)))
        return contain(handle, *limits)

    monkeypatch.setattr(job, "contain", watched)
    with rig.contained([sys.executable, "-c", "print('ran')"], stdout=subprocess.PIPE) as child:
        said = child.stdout.readline()
        child.stdout.close()
    [counts] = seen
    assert counts and min(counts) >= 1, f"its threads' suspend counts as it joined: {counts}"
    assert said.strip() == b"ran"


def test_a_tests_app_shows_its_window_and_page_behind_the_one_in_use(playwright, tmp_path):
    """Started by a process in the background, the app took the foreground
    anyway: Windows refuses it, so tao presses and releases Alt to get it
    (`force_window_active`), a keystroke into whatever the developer was
    using, and their next keys and clicks went to the test's app. An app on
    a data folder of its own now only shows its window (lib.rs
    `focus_main`). The page must still count itself on screen, or it asks
    the backend for nothing (ui/src/desktop.ts `watchOnScreen`).

    Shown so, without the foreground, the window still came up over the
    one in use, in the developer's way until they minimised it; minimised,
    its page asks for nothing, and two walks failed (19 October). It now
    goes behind (lib.rs `behind_unless_in_front`).

    On a locked screen tao's Alt reaches nothing, and no window is in use to
    be behind, so this can pass on the old app; at the desk it fails there."""
    import ctypes

    user32 = ctypes.WinDLL("user32")
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    with rig.desktop_app(tmp_path, playwright) as app:
        page = app.page()
        page.locator("#root", has_text=re.compile(r"\S")).wait_for(timeout=30_000)
        _until(lambda: rig.window(app.process.pid) != 0, rig.STARTS_WITHIN_S)
        hwnd = rig.window(app.process.pid)
        assert not user32.IsIconic(hwnd)
        assert page.evaluate("document.visibilityState") == "visible"
        in_use = user32.GetForegroundWindow() or 0
        assert in_use != hwnd
        if in_use:
            assert _above(in_use, hwnd), "the app's window came up over the one in use"


def _above(upper: int, lower: int) -> bool:
    """Whether one window is above another in Windows' order of windows.

    Windows orders its windows in bands, and an app's are in the lowest;
    the start menu's, the system's tools' and accessibility windows' bands
    are above it, and walking an app's band never meets theirs. With one of
    those in use (19 October), the walk up found nothing and the test said
    the app's window was above it. So a window met neither above nor below
    is in a band above."""
    import ctypes

    user32 = ctypes.WinDLL("user32")
    user32.GetWindow.restype = ctypes.c_void_p
    user32.GetWindow.argtypes = [ctypes.c_void_p, ctypes.c_uint]

    def meets(step: int) -> bool:
        hwnd = lower
        while hwnd := user32.GetWindow(hwnd, step):
            if hwnd == upper:
                return True
        return False

    return meets(3) or not meets(2)  # GW_HWNDPREV, the one above; GW_HWNDNEXT, below


# What the card's page sends when its "on top" setting changes (desktop.ts
# `deskcard.want`), with it turned off.
NOT_ON_TOP = """() => window.__TAURI_INTERNALS__.invoke(
  "deskcard_want", {on: true, onTop: false})"""


def test_the_desk_card_stays_on_screen_when_its_page_turns_on_top_off(
        playwright, tmp_path, monkeypatch):
    """The card is shown by a raw ShowWindow, so as never to take the focus,
    and tao never hears of it. At tao's next change to the window, the page
    turning "on top" off, tao rewrote the window's style from its own
    record and hid it, and nothing showed it again until the app restarted
    (deskcard.rs `settled`). On a screen that isn't free the card is hidden
    by design, so there is nothing to see."""
    import ctypes

    if not rig.screen_free():
        pytest.skip("the desk card shows only on a free screen")
    user32 = ctypes.WinDLL("user32")
    walk.set_up_already()
    with walk.fake_openai(monkeypatch), rig.desktop_app(tmp_path, playwright) as app:
        walk.sign_in(app.page())
        card = app.page("deskcard")
        _until(lambda: rig.window(app.process.pid, "Steve desk card") != 0, rig.STARTS_WITHIN_S)
        hwnd = rig.window(app.process.pid, "Steve desk card")

        card.evaluate(NOT_ON_TOP)
        # Shown again within the card's poll (2 s), and still shown after
        # two more: the old card was hidden for good.
        _until(lambda: user32.IsWindowVisible(hwnd), 5)
        held = time.monotonic() + 4
        while time.monotonic() < held:
            assert user32.IsWindowVisible(hwnd), "the card left the screen"
            time.sleep(0.1)
