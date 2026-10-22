"""Setup's backend: what the first-run pages ask, and what each has been told.

Before step 9 a stranger had no way in: the key went into the Credential
Manager by `python -m core.secrets set`, and the rest was found by trial.
Setup's pages (ui/src, item 7) ask one question each, and this answers them.

What it prevents:

- **A key typed wrong, or for an account with no credit, kept and found
  out at the first answer,** which says only that something failed. Each
  key is checked with one request before it is kept (OpenAI's model list,
  OpenRouter's credit), and the page says what came back: a wrong key, no
  credit, no network. A refused key is not stored.
- **Setup closed halfway, and counted as done or started over.** A page
  that sets something is done when that thing is there: the cap among the
  knobs, the name among the facts, the passphrase. The rest are done when
  answered, in `setup.json`. Setup is done only on its last page, and only
  once every required page is.
- **A key kept from before, never shown.** The key page was done when the
  Credential Manager held a key, so a key left there by an earlier install
  skipped it: the user was never asked for a key, and never learned where
  theirs was kept or that it could be changed. The page is done when a key
  is kept *and* it was checked or kept on the page (`ANSWERS["openai"]`).
- **A preset reaching past attention.** The three presets move how often
  Steve speaks up unasked, nothing else: never an approval, a switch, a cap
  (tests/gate/test_setup_bounds.py).
- **A stolen laptop read like an open book, unsaid.** The last page says
  whether the data folder's drive is encrypted, read without administrator
  rights (docs/plan/spikes/s9-app.md, the drive's encryption), and names
  what is there. Until step 11 it checked the system drive, whatever drive
  the data folder was on, and named only the conversations, not the Signal
  link or the sign-ins kept beside them. It also says when another account
  may read the data folder (`shared`), which a folder chosen outside the
  user's profile can allow.
- **The browser switched on with nothing to run it.** The browser and
  running code need Docker Desktop, installed and running. Turned on
  without it, the switch said nothing until Steve first tried, and failed.
  The Docker page (UI pass, 18 October) says whether Docker Desktop is
  installed and running, and offers the two switches only when it is. It
  reads that without asking Docker anything (`docker`): asking wakes
  Docker Desktop from its resource saver, and Steve never installs Docker
  or changes its settings.

Every route is the app's, behind the local-only check and this start's
token, like pairing.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from agent import facts, offloop, runtime, sqltime
from agent.attention.sources import health
from agent.cut import cut
from agent.worker import bodies
from core import endpoints, knobs, passphrase, paths, secrets, settingsfile

log = logging.getLogger(__name__)

# The pages, in the order setup shows them (docs/plan/09-app-and-setup.md).
PAGES = ("hello", "openai", "cap", "jev", "name", "passphrase", "channel", "accounts", "docker",
         "preset", "login", "privacy")
REQUIRED = ("openai", "cap", "name", "passphrase", "preset", "login")
# The pages done by being answered here, and the answers each takes. A
# channel is linked and an account added on their own routes; the answer
# says the page was left, linked or skipped.
ANSWERS = {"hello": ("seen",), "openai": ("kept",), "jev": ("skipped",),
           "channel": ("signal", "telegram", "skipped"), "accounts": ("added", "skipped"),
           "docker": ("on", "skipped"), "login": ("on", "off")}

# How much Steve does unasked: attention's knobs only. None is the code's
# default, so Balanced is today's Steve, and choosing it after another puts
# back everything that one moved. Meeting research is left out: it sends the
# guests' names to a search engine, a choice of its own (core/knobs.py).
PRESET_KNOBS = ("MAX_DAY_PINGS", "MAX_NIGHT_PINGS", "FOLLOW_UP_AFTER")
PRESETS: dict[str, dict[str, int | str | None]] = {
    "quiet": {"MAX_DAY_PINGS": 4, "MAX_NIGHT_PINGS": 1, "FOLLOW_UP_AFTER": "off"},
    "balanced": dict.fromkeys(PRESET_KNOBS),
    "more": {"MAX_DAY_PINGS": 12, "MAX_NIGHT_PINGS": None, "FOLLOW_UP_AFTER": "every"},
}

# The services a key is checked with, and the secret each is kept under.
KEYS = {"openai": ("OpenAI", "OPENAI_API_KEY"), "openrouter": ("OpenRouter", "OPENROUTER_API_KEY")}
CHECK_SECONDS = 15
SAYS = {
    "wrong-key": "{service} did not accept this key. Check it was copied whole, with nothing"
                 " before or after it.",
    "no-credit": "The key is right, but the account has no credit left. Add some in"
                 " {service}'s billing settings, then try again.",
    "refused": "{service} refused this key: {message}",
    "unreachable": "Steve couldn't reach {service}. Check the internet connection, then try"
                   " again.",
    "error": "{service} answered with an error ({status}). Try again in a minute.",
}

# The shell's property for a volume's BitLocker state, which Device
# Encryption sets too, readable without administrator rights. 1 is on and 6
# on and locked; 2 off, 3 encrypting, 4 decrypting, 5 suspended, 8 waiting
# to be turned on. Nothing at all: Windows can't say, and setup says nothing.
BITLOCKER = ("(New-Object -ComObject Shell.Application).NameSpace('{drive}').Self"
             ".ExtendedProperty('System.Volume.BitLockerProtection')")
PROTECTED = (1, 6)
# Who may read the data folder without its being another account's
# business: SYSTEM, the Administrators, and the owner's placeholders
# (CREATOR OWNER, OWNER RIGHTS); app containers, Steve's sandboxes among
# them, are no account (S-1-15-). Reading is the data bit or a generic grant.
OWN_SIDS = ("S-1-5-18", "S-1-5-32-544", "S-1-3-0", "S-1-3-4")
APP_SIDS = "S-1-15-"
READS = 0x1 | 0x80000000 | 0x10000000

STEPS = (settingsfile.stamped,)


def path() -> Path:
    return paths.data_dir() / "setup.json"


def _held() -> dict:
    try:
        return settingsfile.load(path(), STEPS) or {}
    except (OSError, ValueError):
        log.warning("%s is unreadable; setup starts from what the pages set", path())
        return {}


def _answered(held: dict) -> dict[str, str]:
    given = held.get("answered")
    return {page: answer for page, answer in (given if isinstance(given, dict) else {}).items()
            if answer in ANSWERS.get(page, PRESETS if page == "preset" else ())}


def answered(page: str) -> str | None:
    """The answer a page was given, as kept: the channel chosen, say."""
    return _answered(_held()).get(page)


def preset_now() -> str | None:
    """The preset the attention knobs match now, or None if moved by hand."""
    chosen = knobs.read()
    for name, moves in PRESETS.items():
        if all(chosen.get(knob) == value for knob, value in moves.items()):
            return name
    return None


def state(conn) -> dict:
    """Each page done or not, the first not done, and whether setup is."""
    held = _held()
    answered = _answered(held)
    chosen = knobs.read()
    done = {
        "hello": "hello" in answered,
        "openai": bool(secrets.get("OPENAI_API_KEY")) and "openai" in answered,
        "cap": "MONTH_USD" in chosen,
        "jev": bool(secrets.get("OPENROUTER_API_KEY")) or "jev" in answered,
        "name": bool(facts.identity(conn).get("name")),
        "passphrase": passphrase.is_set(),
        "channel": "channel" in answered,
        "accounts": "accounts" in answered,
        "docker": "docker" in answered,
        "preset": "preset" in answered,
        "login": "login" in answered,
        "privacy": bool(held.get("done_at")),
    }
    return {
        "pages": [{"page": page, "done": done[page], "required": page in REQUIRED}
                  for page in PAGES],
        # Where setup resumes; nowhere once it is done, though an optional
        # page skipped by closing the window stays not done.
        "next": None if done["privacy"] else next((p for p in PAGES if not done[p]), None),
        "done": done["privacy"],
        "answered": answered,
        # A key in the Credential Manager, from this setup or an earlier
        # install: the key page offers to keep it.
        "key_kept": bool(secrets.get("OPENAI_API_KEY")),
        "preset": preset_now(),
        # The cap kept, so its page shows it on the way back: the page showed
        # the suggestion, and Back then Continue wrote that over their own.
        "cap": chosen.get("MONTH_USD"),
    }


def _answer(conn, page: str, answer: str) -> dict:
    """Keep a page's answer; the last page finishes setup, once every
    required page is done. ValueError, and nothing kept, otherwise."""
    held = _held()
    if page == "privacy":
        missing = [p["page"] for p in state(conn)["pages"] if p["required"] and not p["done"]]
        if missing:
            raise ValueError(f"setup isn't finished: {', '.join(missing)} still to do")
        held["done_at"] = sqltime.stamp(datetime.now(UTC))
    else:
        if answer not in ANSWERS.get(page, PRESETS if page == "preset" else ()):
            raise ValueError(f"{page!r} doesn't take the answer {answer!r}")
        if page == "openai" and not secrets.get("OPENAI_API_KEY"):
            raise ValueError("there is no key kept to keep")
        held["answered"] = {**_answered(held), page: answer}
    settingsfile.save(path(), held, STEPS)
    return state(conn)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


async def get_state(_: Request) -> JSONResponse:
    return JSONResponse({"ok": True, **await offloop.with_own_conn(runtime.conn, state)})


async def answer(request: Request) -> JSONResponse:
    """`{"page": ..., "answer": ...}`; the last page takes no answer."""
    body = await bodies.body(request)
    page, given = body.get("page"), body.get("answer")
    if page not in ANSWERS and page != "privacy":
        return bodies.bad(f"{page!r} is not a page answered here")
    try:
        now = await offloop.with_own_conn(runtime.conn, _answer, page, str(given or ""))
    except (ValueError, settingsfile.Newer) as exc:
        return bodies.bad(str(exc))
    return JSONResponse({"ok": True, **now})


async def check_key(service: str, key: str) -> dict:
    """One request with the key, and what it says: `{"ok": True}`, with the
    credit left for OpenRouter, or `{"ok": False, "why": ..., "error": ...}`."""
    name = KEYS[service][0]
    # Jev's credit, read where the health check reads it: the one place besides
    # Jev's call that reaches OpenRouter (tests/unit/test_no_vendor_names.py).
    url = (f"{endpoints.openai()}/models" if service == "openai"
           else health.openrouter_credits())
    try:
        async with httpx.AsyncClient(timeout=CHECK_SECONDS) as client:
            got = await client.get(url, headers={"Authorization": f"Bearer {key}"})
    except httpx.HTTPError as exc:
        log.info("checking a %s key: %s", name, type(exc).__name__)
        return {"ok": False, "why": "unreachable",
                "error": SAYS["unreachable"].format(service=name)}
    try:
        body = got.json()
    except ValueError:
        body = {}
    error = (body.get("error") or {}) if isinstance(body, dict) else {}
    if not isinstance(error, dict):
        error = {}
    if got.status_code == 200 and service == "openai":
        return {"ok": True}
    if got.status_code == 200:
        data = (body.get("data") or {}) if isinstance(body, dict) else {}
        try:
            left = float(data.get("total_credits") or 0) - float(data.get("total_usage") or 0)
        except (TypeError, ValueError, AttributeError):
            left = 0.0
        if left <= 0:
            return {"ok": False, "why": "no-credit",
                    "error": SAYS["no-credit"].format(service=name)}
        return {"ok": True, "credit": round(left, 2)}
    if got.status_code == 401:
        why = "wrong-key"
    elif got.status_code == 429 and error.get("code") == "insufficient_quota":
        why = "no-credit"
    elif got.status_code == 403:
        why = "refused"
    else:
        why = "error"
    return {"ok": False, "why": why, "error": SAYS[why].format(
        service=name, status=got.status_code, message=cut(str(error.get("message") or ""), 200))}


async def set_key(request: Request) -> JSONResponse:
    """`{"service": "openai" | "openrouter", "key": ...}`: checked, then kept."""
    body = await bodies.body(request)
    service, key = body.get("service"), body.get("key")
    if service not in KEYS:
        return bodies.bad(f"a key is for one of {', '.join(KEYS)}")
    key = key.strip() if isinstance(key, str) else ""
    if not key or len(key) > 512 or any(c.isspace() or not c.isprintable() for c in key):
        return bodies.bad("That doesn't look like a key: paste it whole, on its own.", why="shape")
    checked = await check_key(service, key)
    if not checked["ok"]:
        return JSONResponse(checked, status_code=400)
    await asyncio.to_thread(secrets.set_secret, KEYS[service][1], key)
    if service == "openai":
        # Checked and kept on the page: the page is done.
        await offloop.with_own_conn(runtime.conn, _answer, "openai", "kept")
    return JSONResponse({**checked, "says": f"{KEYS[service][0]} accepted the key."})


async def set_preset(request: Request) -> JSONResponse:
    """`{"preset": "quiet" | "balanced" | "more"}`: attention's knobs, in force at once."""
    from agent.worker import control

    name = (await bodies.body(request)).get("preset")
    if name not in PRESETS:
        return bodies.bad(f"a preset is one of {', '.join(PRESETS)}")
    try:
        knobs.write(dict(PRESETS[name]))
        control.apply_knobs()
        now = await offloop.with_own_conn(runtime.conn, _answer, "preset", name)
    except (knobs.BadKnob, settingsfile.Newer) as exc:
        return bodies.bad(str(exc))
    return JSONResponse({"ok": True, **now})


def bitlocker(drive: str) -> int | None:
    """The drive's BitLocker state as the shell gives it; None if it can't."""
    try:
        done = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
             BITLOCKER.format(drive=drive)],
            capture_output=True, text=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired):
        return None
    said = done.stdout.strip()
    return int(said) if said.isdigit() else None


def shared(folder: Path) -> bool | None:
    """Whether an account other than this user's may read what `folder`
    holds; None when Windows can't say. In the user's profile only the
    user, SYSTEM and the Administrators may; a folder made at a drive's
    root inherits reading by every signed-in account."""
    if sys.platform != "win32":
        return None
    import pywintypes
    import win32security

    from core.sandbox import win32

    try:
        dacl = win32security.GetFileSecurity(
            str(folder), win32security.DACL_SECURITY_INFORMATION).GetSecurityDescriptorDacl()
        me = win32.user_sid()
    except (OSError, pywintypes.error):
        return None
    if dacl is None:  # no access list at all: anyone may
        return True
    for index in range(dacl.GetAceCount()):
        ace = dacl.GetAce(index)
        if ace[0][0] != win32security.ACCESS_ALLOWED_ACE_TYPE or not ace[1] & READS:
            continue
        who = win32security.ConvertSidToStringSid(ace[-1])
        if who != me and who not in OWN_SIDS and not who.startswith(APP_SIDS):
            return True
    return False


def drive() -> dict:
    """Whether the data folder's drive is encrypted, and whether another
    account may read the folder; and what setup says of both."""
    folder = paths.data_dir()
    letter = folder.drive or os.environ.get("SYSTEMDRIVE") or "C:"
    found = bitlocker(letter)
    protected = None if found is None else found in PROTECTED
    others = shared(folder)
    said = []
    if protected:
        said.append(f"The drive Steve keeps its data on ({letter}) is encrypted: if this laptop"
                    " is lost, what Steve keeps can't be read from it.")
    elif protected is False:
        said.append(f"The drive Steve keeps its data on ({letter}) isn't encrypted. Anyone who"
                    " has this laptop can read what Steve keeps: your conversations, notes and"
                    " what it has learned about you, its link to your phone's Signal, and the"
                    " sign-ins of services you add. Windows' device encryption, or BitLocker,"
                    " protects it.")
    if others:
        said.append(f"Other accounts on this computer can read Steve's data folder ({folder})."
                    " Remove their access in its Properties, under Security, or keep Steve's"
                    " data in your own profile.")
    return {"drive": letter, "state": found, "protected": protected, "shared": others,
            "says": " ".join(said)}


async def get_drive(_: Request) -> JSONResponse:
    return JSONResponse({"ok": True, **await asyncio.to_thread(drive)})


# Where Docker Desktop installs itself. Its command is put on PATH too, but
# only programs started after the install see the new PATH.
DESKTOP = (Path(os.environ.get("PROGRAMFILES") or "C:\\Program Files")
           / "Docker" / "Docker" / "Docker Desktop.exe")
# Windows' named pipes, listed by name: listing opens none of them.
PIPES = "\\\\.\\pipe\\"


def docker() -> dict:
    """Docker Desktop on this laptop (`docker_in`)."""
    return docker_in(PIPES, DESKTOP)


def docker_in(pipes: str, desktop: Path) -> dict:
    """Whether Docker Desktop is installed, and whether its engine is
    running: its program on disk or its command on PATH, its engine's pipe
    among the pipes. Not `docker version`, which every other check uses: on
    this page it would be asked every few seconds, and each ask wakes Docker
    Desktop from its resource saver (agent/worker/routes.py,
    `_SANDBOX_PROBE`)."""
    try:
        running = "docker_engine" in os.listdir(pipes)
    except OSError:
        running = False
    installed = running or desktop.exists() or shutil.which("docker") is not None
    return {"installed": installed, "running": running}


async def get_docker(_: Request) -> JSONResponse:
    return JSONResponse({"ok": True, **await asyncio.to_thread(docker)})


ROUTES = [
    Route("/setup/state", get_state),
    Route("/setup/answer", answer, methods=["POST"]),
    Route("/setup/key", set_key, methods=["POST"]),
    Route("/setup/preset", set_preset, methods=["POST"]),
    Route("/setup/drive", get_drive),
    Route("/setup/docker", get_docker),
]
