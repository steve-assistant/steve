"""The app's screens under test: Microsoft Edge, the real app, and a stand-in for the shell.

Playwright drives both browsers over CDP, and never starts one itself. Its
own `launch` of Edge took 30 s to close every time: Edge's main process
outlived `Browser.close`, Playwright waited out its timeout, and the kill
it sent then reached the main process alone. 71 of Edge's helpers from the
spikes were still running afterwards, each holding its profile
(docs/plan/spikes/s9-app.md). Here the test starts the browser, puts it in
a kill-on-close job straight away (core/sandbox/job.py), attaches, and ends
the job: up in under a second, gone at once, nothing left.

Neither browser may reach past this computer. The fences
(tests/gate/fences.py) hold the test and every Python it starts, the
app's backend included, but Edge and WebView2 are not Python. So both
start with every host name resolving to nothing and every other address
sent to a proxy nobody runs, loopback excepted (`LOOPBACK_ONLY`).

**Edge** runs the bundle the app ships (`build_ui`), served with a backend
of the test's own on one origin (`serve`). The page believes it is in the
app: the stand-in (`SHELL`) answers the shell's `backend` command with that
origin and a token, and records every other command it is given (a toast,
the waiting count), so a test can read what the page asked of the shell.

**The real app** (`desktop_app`) is the developer's debug build, on the
test's own data folder, webview folder and fenced Python. It is skipped
where it was never built, as in CI, and fails where it is older than its
sources: a stale build would test code that no longer exists.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import time
import warnings
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
UI = REPO / "ui"
EDGE = (Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
        / "Microsoft" / "Edge" / "Application" / "msedge.exe")

# Where the app's own pages are, in a release build and a developer's alike
# (Tauri's custom protocol, as WebView2 serves it).
APP_ORIGIN = "http://tauri.localhost/"

# For Edge and the app's WebView2 alike. Chromium never sends loopback
# (`localhost`, `*.localhost`, 127/8, [::1]) to a proxy, and the app's own
# pages are `tauri.localhost`.
LOOPBACK_ONLY = (
    # Every name resolves to nothing but this computer's own ...
    "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost, EXCLUDE *.localhost, "
    "EXCLUDE 127.0.0.1",
    # ... and an address given as numbers goes to a proxy nobody runs.
    "--proxy-server=http://127.0.0.1:9",
    # What the browser fetches for itself, unasked.
    "--disable-background-networking", "--disable-component-update", "--disable-sync",
    "--no-pings", "--disable-domain-reliability",
)

# The app's window as on the screen, whatever the desktop it runs on is
# doing. A page asks for nothing while Windows calls its window hidden
# (ui/src/desktop.ts `watchOnScreen`), and WebView2 calls a window hidden
# when another covers it or the screen is locked: a new window opens behind
# the one the developer is using, and the exit walk's Settings never asked
# for its knobs. The other three are what Tauri passes when nothing is
# given, which this variable replaces; Chromium reads one --disable-features.
ON_SCREEN = (
    "--disable-features=msWebOOUI,msPdfOOUI,msSmartScreenProtection,"
    "CalculateNativeWinOcclusion",
)

# The app's WebView2 in a test: a debugging port of the system's choosing,
# and the two above. The rig gives them as WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS,
# which WebView2 ignores in a process running elevated, as everything on CI's
# runner does: there every test that started the app waited for a port never
# asked for (22 October 2026). So CI builds the app with them as its windows'
# own (`build_config`).
WEBVIEW_ARGS = subprocess.list2cmdline(["--remote-debugging-port=0", *LOOPBACK_ONLY, *ON_SCREEN])


def build_config() -> str:
    """TAURI_CONFIG for CI's build of the app, which Tauri's build merges
    into tauri.conf.json: its windows as that file has them, each with the
    rig's arguments as its own. An elevated WebView2 takes an app's own
    arguments and nothing from outside: on the runner neither the variable
    nor WebView2's machine policy, under the app's name or `*`, reached the
    browser (22 October 2026). A window's own arguments replace wry's, so
    its autoplay policy is given again (`ON_SCREEN` keeps the rest)."""
    config = json.loads((UI / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))
    own = f"{WEBVIEW_ARGS} --autoplay-policy=no-user-gesture-required"
    windows = [dict(window, additionalBrowserArgs=own) for window in config["app"]["windows"]]
    return json.dumps({"app": {"windows": windows}})

STARTS_WITHIN_S = 60  # the app: its webview, then its backend


def forget_port(folder: Path) -> None:
    """Removes the port files a browser left in a profile used before. Read
    after the next start, a stale one names a port nobody listens on."""
    for marker in folder.rglob("DevToolsActivePort"):
        marker.unlink()


def devtools_port(folder: Path, within: float) -> int:
    """The debugging port a browser chose (port 0), from the file it writes
    in its profile: Edge's at the top, WebView2's under `EBWebView`. The
    caller forgets the old one first (`forget_port`)."""
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        for marker in folder.rglob("DevToolsActivePort"):
            with contextlib.suppress(OSError):
                first = marker.read_text(encoding="utf-8").split("\n")[0].strip()
                if first:
                    return int(first)
        time.sleep(0.05)
    raise TimeoutError(f"no browser wrote its debugging port under {folder} in {within:.0f} s")


@contextlib.contextmanager
def contained(command: list[str], **popen) -> Iterator[subprocess.Popen]:
    """A process in a kill-on-close job from its start, ended with the job
    (and so with everything it started) when the block ends."""
    from core.sandbox import job

    process = subprocess.Popen(command, **popen)
    # Popen's own handle, not one opened by pid (core/decider._contain).
    try:
        held = job.contain(int(process._handle))
    except OSError:
        process.kill()
        raise
    try:
        yield process
    finally:
        held.end()
        process.wait(10)


@contextlib.contextmanager
def edge(profile: Path, playwright) -> Iterator:
    """Microsoft Edge, as Windows ships it, headless, attached over CDP."""
    if not EDGE.exists():
        pytest.skip(f"Microsoft Edge is not at {EDGE}")
    command = [str(EDGE), "--headless", "--remote-debugging-port=0",
               f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
               *LOOPBACK_ONLY, "about:blank"]
    forget_port(profile)
    with contained(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL):
        browser = playwright.chromium.connect_over_cdp(
            f"http://127.0.0.1:{devtools_port(profile, 30)}")
        try:
            yield browser
        finally:
            # Over CDP this only lets go; the job ends the browser.
            browser.close()


def build_ui(out: Path, mode: str = "production") -> Path:
    """The app's screens as `npm run build` makes them, into `out`, never
    into `ui/dist`, which the app's own build embeds. Vite only: the types
    are the lint's (`tsc -b`), and a test of the screens should not fail on
    them. The "specimen" mode adds the mark's specimen sheet at
    `/specimen.html` (ui/vite.config.ts), and changes nothing else."""
    node = shutil.which("node")
    vite = UI / "node_modules" / "vite" / "bin" / "vite.js"
    if node is None or not vite.exists():
        pytest.skip("the UI's packages are not installed: `npm ci` in ui/")
    subprocess.run([node, str(vite), "build", "--mode", mode, "--outDir", str(out),
                    "--emptyOutDir", "--logLevel", "warn"], cwd=UI, check=True, timeout=300,
                   stdout=subprocess.DEVNULL)
    return out


# The shell's side of `invoke` and events, as @tauri-apps/api reaches it.
# `window.__STEVE_SHELL__` is the test's view: the commands asked for, and
# `emit` for an event the shell would send (a tray click, a hotkey).
SHELL = """
(() => {
  const backend = %(backend)s;
  const label = %(label)s;
  const calls = [];
  // What a test has the shell answer: a command's name to its answer.
  const answers = {};
  const callbacks = {};
  const listeners = {};
  let next = 1;
  // Each channel's next message's index, which @tauri-apps/api's Channel
  // reads to keep its messages in order.
  const sent = {};
  // Accelerators another app holds: registering one fails, as Windows'
  // refusal reaches the global-shortcut plugin. Kept across a reload, as
  // the other app would keep them.
  const taken = JSON.parse(sessionStorage.getItem("shell.taken") || "[]");
  // The app's session, held as signin.rs holds it: for every window, and
  // across a reload, until it is ended.
  const held = () => sessionStorage.getItem("shell.session");
  const signedOut = (why) => {
    const was = held();
    sessionStorage.removeItem("shell.session");
    if (was) window.__STEVE_SHELL__.emit("session", { live: false, why });
  };
  const registered = (accelerator) => calls.filter((c) =>
    c.cmd === "plugin:global-shortcut|register" && c.args.shortcuts.includes(accelerator)
    && !taken.includes(accelerator)).pop();
  window.__STEVE_SHELL__ = {
    calls,
    answers,
    // What the `backend` command proves and hands over: a test gives it a
    // new token, as a backend that restarted has.
    backend,
    take(accelerator) {
      taken.push(accelerator);
      sessionStorage.setItem("shell.taken", JSON.stringify(taken));
    },
    // The session ended by the shell: "locked", "slept", "idle".
    signOut(why) {
      signedOut(why);
    },
    emit(event, payload) {
      for (const id of listeners[event] || []) {
        if (callbacks[id]) callbacks[id]({ event, id, payload });
      }
    },
    // A hotkey's press or release ("Pressed", "Released"), to the handler
    // it was last registered with.
    hotkey(accelerator, state) {
      const call = registered(accelerator);
      if (!call) throw new Error(`${accelerator} is not registered`);
      const id = Number(String(call.args.handler).split(":")[1]);
      sent[id] = (sent[id] ?? -1) + 1;
      callbacks[id]({ index: sent[id], message: { shortcut: accelerator, id: 0, state } });
    },
  };
  window.__TAURI_EVENT_PLUGIN_INTERNALS__ = {
    unregisterListener(event, id) {
      listeners[event] = (listeners[event] || []).filter((x) => x !== id);
    },
  };
  window.__TAURI_INTERNALS__ = {
    metadata: {
      currentWindow: { label },
      currentWebview: { windowLabel: label, label },
    },
    transformCallback(callback) {
      const id = next++;
      callbacks[id] = callback;
      return id;
    },
    unregisterCallback(id) {
      delete callbacks[id];
    },
    convertFileSrc(path) {
      return path;
    },
    async invoke(cmd, args) {
      calls.push({ cmd, args: JSON.parse(JSON.stringify(args ?? {})) });
      if (cmd === "backend") return { url: location.origin, token: backend.token };
      if (cmd === "plugin:global-shortcut|register"
          && args.shortcuts.some((s) => taken.includes(s))) {
        throw "HotKey already registered";
      }
      // A test's answers, set after the page loads, or before it in
      // `__STEVE_SHELL_PRESET__` (an init script of the test's own); a
      // function answers from the command's arguments.
      const preset = window.__STEVE_SHELL_PRESET__ || {};
      const given = cmd in answers ? answers[cmd] : preset[cmd];
      if (cmd in answers || cmd in preset) {
        return typeof given === "function" ? await given(args) : given;
      }
      if (cmd === "session_keep") {
        sessionStorage.setItem("shell.session", args.secret);
        window.__STEVE_SHELL__.emit("session", { live: true });
        return null;
      }
      if (cmd === "session_get") return held();
      if (cmd === "session_end") {
        signedOut("ended");
        return null;
      }
      // A window on screen, as the app is while a test drives it: answered
      // null, the app took itself for hidden in the tray and slowed its polls.
      if (cmd === "plugin:window|is_visible") return true;
      if (cmd === "plugin:window|is_minimized") return false;
      // The user's own Steve, which can start with Windows; one on a data
      // folder of its own has no autostart plugin, and gets null.
      if (cmd === "plugin:autostart|is_enabled") return false;
      if (cmd === "plugin:event|listen") {
        (listeners[args.event] = listeners[args.event] || []).push(args.handler);
        return args.handler;
      }
      // What a page emits reaches its own listeners too, later, as Tauri
      // sends an event to every window, the sender's included. Kept to the
      // test's own `emit`, the main window reloading after its own saves
      // raced only in the real app, in the exit walk under load.
      if (cmd === "plugin:event|emit") {
        setTimeout(() => window.__STEVE_SHELL__.emit(args.event, args.payload), 0);
        return null;
      }
      return null;
    },
  };
})();
"""


def shell(token: str, label: str = "main") -> str:
    """The stand-in's script for one page, run before the page's own."""
    return SHELL % {"backend": json.dumps({"token": token}), "label": json.dumps(label)}


class _Screens:
    """The built screens at `/` and its files, everything else to the backend."""

    def __init__(self, dist: Path, backend):
        from starlette.staticfiles import StaticFiles

        self.files = StaticFiles(directory=dist, html=True)
        self.tops = {"", *(p.name for p in dist.iterdir())}
        self.backend = backend

    async def __call__(self, scope, receive, send):
        top = scope.get("path", "/").lstrip("/").split("/", 1)[0]
        if scope["type"] == "http" and top in self.tops:
            await self.files(scope, receive, send)
        else:
            await self.backend(scope, receive, send)


@dataclass
class Served:
    url: str
    server: object = field(repr=False)


@contextlib.contextmanager
def serve(dist: Path, backend) -> Iterator[Served]:
    """The screens and an ASGI backend on one origin, on a port of the
    system's choosing on loopback, in a thread, for the length of the block.
    It stops when the test ends, as the fakes' servers do."""
    from tests.fakes.served import in_thread

    with in_thread(_Screens(dist, backend), "the test's server") as (port, server):
        yield Served(f"http://127.0.0.1:{port}", server)


# What the app's build embeds or compiles: a build older than any of them
# would test code that no longer exists.
APP_SOURCES = ("ui/src", "ui/index.html", "ui/src-tauri/src", "ui/src-tauri/Cargo.toml",
               "ui/src-tauri/tauri.conf.json", "ui/src-tauri/capabilities")


def built_app() -> Path:
    """The developer's debug build of the app, built with its screens in it."""
    target = Path(os.environ.get("CARGO_TARGET_DIR") or UI / "src-tauri" / "target")
    exe = target / "debug" / "app.exe"
    if not exe.exists():
        pytest.skip(f"the app is not built at {exe}: in ui/, `npm run build`; then in "
                    "ui/src-tauri, `cargo build -j 3 --features tauri/custom-protocol`")
    built = exe.stat().st_mtime
    for source in APP_SOURCES:
        path = REPO / source
        for file in ([path] if path.is_file() else path.rglob("*")):
            if file.is_file() and file.stat().st_mtime > built:
                pytest.fail(f"the app at {exe} is older than {file.relative_to(REPO)}: "
                            "rebuild it (`npm run build` in ui/, then `cargo build -j 3 "
                            "--features tauri/custom-protocol` in ui/src-tauri)")
    return exe


def window(pid: int, title: str = "Steve", *, visible: bool = True) -> int:
    """A top-level window of that process with that title: the app's main
    window by default; with `visible` False, shown or not. 0 if none."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        text = ctypes.create_unicode_buffer(64)
        user32.GetWindowTextW(hwnd, text, 64)
        if (owner.value == pid and text.value == title
                and (user32.IsWindowVisible(hwnd) or not visible)):
            found.append(hwnd)
        return True

    user32.EnumWindows(each, 0)
    return found[0] if found else 0


def screen_free() -> bool:
    """Whether Windows says the user is free for the desk card: not locked,
    no screen saver, no full-screen app, game or presentation in front
    (QUNS 1 to 4). The test runs on the developer's own desktop, which may
    be any of these."""
    import ctypes

    state = ctypes.c_int(0)
    if ctypes.WinDLL("shell32").SHQueryUserNotificationState(ctypes.byref(state)) != 0:
        return True
    return state.value not in (1, 2, 3, 4)


# What `watchOnScreen` asks, asked the same way.
ON_SCREEN_ASKED = """async () => {
  const ask = (cmd) => window.__TAURI_INTERNALS__.invoke(`plugin:window|${cmd}`, {label: "main"})
    .catch((e) => String(e));
  return {visibility: document.visibilityState, visible: await ask("is_visible"),
          minimized: await ask("is_minimized")};
}"""


@dataclass
class Desktop:
    process: subprocess.Popen
    browser: object
    data: Path
    webview: Path
    # What the app's pages did while the test drove them, for its failure.
    seen: list[str] = field(default_factory=list)

    def story(self) -> str:
        """What a failure should say about the app: whether it is still
        running, its pages now, what happened to them, whether its main
        window counts itself on screen, and the tail of each log in its data
        folder, which goes with the test."""
        code = self.process.poll()
        lines = ["the app is " + ("running" if code is None else f"gone, exit code {code}")]
        with contextlib.suppress(Exception):
            lines.append("its pages now: " + ", ".join(
                page.url for context in self.browser.contexts for page in context.pages))
        lines.extend(self.seen)
        with contextlib.suppress(Exception):
            lines.append(self.on_screen())
        for log in sorted((self.data / "logs").glob("*.log")):
            tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-25:]
            lines.append(f"--- {log.name}\n" + "\n".join(tail))
        return "\n".join(lines)

    def on_screen(self) -> str:
        """What the main page reads to decide it is on screen, which it
        must be to ask for more than the tray shows (ui/src/desktop.ts
        `watchOnScreen`), and what Windows says of the window and the
        session."""
        import ctypes

        user32 = ctypes.WinDLL("user32")
        shell32 = ctypes.WinDLL("shell32")
        hwnd = window(self.process.pid, visible=False)
        session = ctypes.c_int(0)
        shell32.SHQueryUserNotificationState(ctypes.byref(session))
        main = [page for context in self.browser.contexts for page in context.pages
                if page.url.startswith(APP_ORIGIN) and "#" not in page.url]
        asked = ""
        with contextlib.suppress(Exception):
            asked = main[0].evaluate(ON_SCREEN_ASKED)
        return (f"on screen? the page says {asked or 'nothing'}; Windows: visible"
                f" {bool(user32.IsWindowVisible(hwnd))}, minimised {bool(user32.IsIconic(hwnd))},"
                f" session state {session.value} (1 locked, 5 free)")

    def page(self, label: str = "main"):
        """A window's page: the quick bar is `#quickbar`, the desk card
        `#deskcard`, the main window the rest of the app's own pages.

        Only once a page is at the app's address: each window's page opens
        at about:blank first, and the main window's was "the rest", so a
        walk started under load could be given the quick bar's or the desk
        card's, and wait two minutes on it for a sign-in that never came."""
        deadline = time.monotonic() + STARTS_WITHIN_S
        while time.monotonic() < deadline:
            for context in self.browser.contexts:
                for page in context.pages:
                    if not page.url.startswith(APP_ORIGIN):
                        continue
                    hash_ = page.url.rpartition("#")[2] if "#" in page.url else ""
                    if hash_ in ("quickbar", "deskcard"):
                        if hash_ == label:
                            return page
                    elif label == "main":
                        return page
            self._wait(0.1)
        raise TimeoutError(f"the app showed no {label} window")

    def _wait(self, seconds: float) -> None:
        """A pause in which the browser's news comes in. Playwright's sync
        API takes in a page's navigation and a new page only inside one of
        its own calls: around `time.sleep`, a page's address read again and
        again stayed about:blank, and no other page ever appeared."""
        for context in self.browser.contexts:
            for page in context.pages:
                with contextlib.suppress(Exception):
                    page.wait_for_timeout(seconds * 1000)
                    return
        time.sleep(seconds)


@contextlib.contextmanager
def desktop_app(folder: Path, playwright) -> Iterator[Desktop]:
    """The real app, on the test's data folder (`STEVE_DATA_DIR`, set by the
    fences), a webview folder in `folder`, and the fenced Python
    (`sys.executable` in a test) for its backend."""
    exe = built_app()
    data, webview = Path(os.environ["STEVE_DATA_DIR"]), folder / "webview"
    env = dict(os.environ, STEVE_PYTHON=sys.executable, WEBVIEW2_USER_DATA_FOLDER=str(webview),
               WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=WEBVIEW_ARGS)
    forget_port(webview)
    began = time.monotonic()
    printed = folder / "app.out"
    with (printed.open("wb") as out,
          contained([str(exe)], env=env, cwd=folder, stdout=out,
                    stderr=subprocess.STDOUT) as process):
        try:
            port = devtools_port(webview, STARTS_WITHIN_S)
        except TimeoutError as late:
            # On CI's first nightly run (22 October 2026) five walks said
            # only that no port came: not whether the app had ended, nor
            # what it said as it did, with its output thrown away.
            raise TimeoutError(f"{late}\n{_unstarted(process, printed, data, webview)}") from late
        browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        app = Desktop(process, browser, data, webview)

        def noted(what: str, page) -> None:
            app.seen.append(f"{time.monotonic() - began:6.1f} s  {what}: {page.url}")

        def watch(page) -> None:
            page.on("close", lambda p: noted("closed", p))
            page.on("crash", lambda p: noted("crashed", p))

        for context in browser.contexts:
            context.on("page", lambda p: (noted("opened", p), watch(p)))
            for page in context.pages:
                watch(page)
        browser.on("disconnected", lambda _: app.seen.append(
            f"{time.monotonic() - began:6.1f} s  the browser's connection closed"))
        try:
            yield app
        except Exception as failed:
            # A failure on the real app says what happened to it: the
            # backend's reason is in its logs, and the data folder is gone
            # once the test ends.
            raise AssertionError(f"{failed}\n{app.story()}") from failed
        finally:
            browser.close()
    left = left_behind(data, webview)
    assert not left, f"secrets the app left on disk: {left}"


def _unstarted(process: subprocess.Popen, printed: Path, data: Path, webview: Path) -> str:
    """What an app whose webview never came up left to go on: whether it is
    still running, what the webview wrote in its folder, how its browser
    was started, what the app printed, and the end of each log it wrote."""
    code = process.poll()
    lines = ["the app is " + ("running" if code is None else f"gone, exit code {code}")]
    # On CI's runner (22 October 2026) the app ran, its page reached the
    # backend, and still no port file came: an empty folder says the
    # webview kept its profile elsewhere, a profile without the file that
    # it never took the debugging port.
    written = sorted(path.relative_to(webview).as_posix()
                     for pattern in ("*", "*/*") for path in webview.glob(pattern))
    lines.append(f"--- the webview's folder, two levels down: {len(written)} entries\n"
                 + "\n".join(written[:40]))
    # And whether the debugging port reached the webview at all: its
    # browser process's command line names it if it did. On CI the folder
    # held a profile and no port file.
    browsers = []
    with contextlib.suppress(Exception):
        import psutil

        for child in psutil.Process(process.pid).children(recursive=True):
            with contextlib.suppress(Exception):
                line = subprocess.list2cmdline(child.cmdline())
                # The browser, not its renderers, nor the backend's Pythons.
                if "webview" in child.name().lower() and "--type=" not in line:
                    browsers.append(f"{child.name()}: {line[:2000]}")
    lines.append("--- the app's browser processes\n" + ("\n".join(browsers) or "none found"))
    logs = [printed, *sorted((data / "logs").glob("*.log"))]
    for log in logs:
        with contextlib.suppress(OSError):
            tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-25:]
            lines.append(f"--- {log.name}\n" + "\n".join(tail))
    return "\n".join(lines)


# The key the backend is given in a test: the child fence's stand-in
# (tests/gate/childfence/sitecustomize.py `dummy_or_unset`).
STAND_IN_KEY = "test-openai_api_key"


def left_behind(data: Path, webview: Path, within: float = 10.0,
                stray: str = "msedgewebview2.exe") -> list[str]:
    """The passphrase typed into the app and the key its backend was given,
    in what they left on disk once they had gone: the data folder, with the
    shell's own log, and WebView2's folder, its cache, storage and form
    data. The end-to-end scan read neither the shell's log nor WebView2's
    folder until step 11's audit. A file the webview's last processes still
    hold as they end is read again, for `within` seconds.

    On CI (22 October 2026) a walk's webview files were still held after
    that, once by a WebView2 process whose parent had gone: one that had
    outlived the job that ended the app. A process named `stray` that holds
    a file there, or names the folder, is the app's own: it is ended, as the
    job should have, the scan made again, and a warning says which."""
    left = _scanned(data, webview, within)
    held = _held(webview, left)
    if not held:
        return left
    ended = _ended(webview, held, stray)
    if ended:
        left = _scanned(data, webview, within)
        held = _held(webview, left)
        if not held:
            warnings.warn(f"the app's webview outlived its job; the rig ended {ended}",
                          stacklevel=2)
            return left
    return [*left, *(f"ended {process}" for process in ended), *_holding(webview, held)]


def _scanned(data: Path, webview: Path, within: float) -> list[str]:
    from tests.fakes import world
    from tests.unit.ui import walk

    watched = {"the passphrase": walk.PASSPHRASE, "the stand-in key": STAND_IN_KEY}
    deadline = time.monotonic() + within
    while True:
        left = [*world.found_in(data, watched, "the data folder"),
                *world.found_in(webview, watched, "the webview's folder")]
        if time.monotonic() > deadline or not any(
                line.startswith("could not read") for line in left):
            return left
        time.sleep(0.2)


UNREAD = "could not read the webview's folder ("


def _held(webview: Path, left: list[str]) -> list[Path]:
    return [webview / line[len(UNREAD):-1] for line in left if line.startswith(UNREAD)]


def _ended(folder: Path, held: list[Path], stray: str) -> list[str]:
    """Ends each process named `stray` that holds one of `held` or names
    `folder`, and says each as it was."""
    import psutil

    ended = []
    for pid in _holders(folder, held):
        with contextlib.suppress(psutil.Error):
            process = psutil.Process(pid)
            if process.name().lower() == stray:
                ended.append(_described(pid))
                process.kill()
                process.wait(10)
    return ended


def _holding(folder: Path, held: list[Path]) -> list[str]:
    """Each process that holds one of `held` open or names `folder` on its
    command line, as WebView2's name their user data folder: who kept the
    files the scan could not read, which a failure said nothing of."""
    said = [f"{how}: {_described(pid)}" for pid, how in _holders(folder, held).items()]
    return said or ["no process holds a file there or names the folder"]


def _holders(folder: Path, held: list[Path]) -> dict[int, str]:
    import psutil

    holders = dict.fromkeys(_lockers(held), "holds a file")
    named = str(folder).lower()
    for process in psutil.process_iter(["cmdline"]):
        if named in " ".join(process.info["cmdline"] or []).lower():
            holders.setdefault(process.pid, "names the folder")
    return holders


def _described(pid: int) -> str:
    """A process by name, pid, WebView2's kind of process (`--type`; none
    is the browser's own), age and parent, or that its command line could
    not be read, as a process still ending has none."""
    import psutil

    try:
        process = psutil.Process(pid)
        name, ppid, born = process.name(), process.ppid(), process.create_time()
    except psutil.Error:
        return f"pid {pid}, gone"
    try:
        kind = next((arg.split("=", 1)[1] for arg in process.cmdline()
                     if arg.startswith("--type=")), "no --type")
    except psutil.Error:
        kind = "command line unreadable"
    try:
        parent = f"{psutil.Process(ppid).name()} {ppid}"
    except psutil.Error:
        parent = f"gone {ppid}"
    return f"{name} {pid} ({kind}), {time.time() - born:.0f} s old, from {parent}"


def _lockers(paths: list[Path]) -> list[int]:
    """The processes Windows' Restart Manager says hold any of `paths` open,
    whatever their command line says."""
    import ctypes
    from ctypes import wintypes

    class Unique(ctypes.Structure):
        _fields_ = [("pid", wintypes.DWORD), ("started", wintypes.FILETIME)]

    class Info(ctypes.Structure):
        _fields_ = [("process", Unique), ("app", wintypes.WCHAR * 256),
                    ("service", wintypes.WCHAR * 64), ("kind", ctypes.c_int),
                    ("status", wintypes.ULONG), ("session", wintypes.DWORD),
                    ("restartable", wintypes.BOOL)]

    if not paths:
        return []
    rm = ctypes.WinDLL("rstrtmgr")
    session, key = wintypes.DWORD(), ctypes.create_unicode_buffer(33)
    if rm.RmStartSession(ctypes.byref(session), 0, key):
        return []
    try:
        names = (wintypes.LPCWSTR * len(paths))(*(str(p) for p in paths))
        if rm.RmRegisterResources(session, len(paths), names, 0, None, 0, None):
            return []
        needed, count, reasons = wintypes.UINT(), wintypes.UINT(0), wintypes.DWORD()
        rm.RmGetList(session, ctypes.byref(needed), ctypes.byref(count), None,
                     ctypes.byref(reasons))
        found = (Info * needed.value)()
        count = wintypes.UINT(needed.value)
        if needed.value and rm.RmGetList(session, ctypes.byref(needed), ctypes.byref(count),
                                         found, ctypes.byref(reasons)):
            return []
        return [info.process.pid for info in found[:count.value]]
    finally:
        rm.RmEndSession(session)
