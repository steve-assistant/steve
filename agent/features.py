"""What this user has set up, and so what the model is told about and offered.

The house prompt was one fixed text for one user: three mailboxes by name,
two calendars, a course feed, a phone, a home town. Given to someone with
none of them, it describes an assistant they do not have. The model offers
to search mail that is not there, and a tool that can only fail reads as a
broken one. Telling the model "ignore what you lack" would be a patch on a
prompt that should never have said it.

So a feature describes itself only when it is on. Each one here names its
tools and how to tell from stored configuration whether the user has it:
an account's token or credentials, the home they gave, an add-on
installed, a switch they turned (core/knobs.py). `enabled` asks each; `assemble` builds
the house prompt from the core text and the sections of those that are on,
and the toolset registers only their tools (`agent/toolset.register_tools`).

Asked at the start of each turn rather than once per worker, so a home
given in conversation, or an account connected in the app, counts from the
next turn. Every check reads stored state and nothing on the network, so
the answer, and the prompt built from it, stays the same byte for byte
from one turn to the next until the user changes something. That keeps it
in the cached prefix.

The assembly is here, not beside the text in `agent/prompts/`: that folder
is auto-mergeable (`core/policy.yaml`), right for wording the night may
sharpen and wrong for code that decides what the model is offered.
"""

from __future__ import annotations

import functools
import logging
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from core import knobs, paths

log = logging.getLogger("worker")

PROMPTS = Path(__file__).resolve().parent / "prompts"
CORE_PATH = PROMPTS / "system.md"
# Where in the core the enabled sections go: after the tools every user has,
# before the standing rules that end it.
SECTIONS_HERE = "{{features}}"
# Where in a section the lines its check found go: the accounts it lists.
LINES_HERE = "{lines}"

# The sections assembled, by feature name, with the lines each adds: the
# accounts a mail section lists, say. "" for a feature with nothing to add.
Enabled = Mapping[str, str]


@dataclass(frozen=True)
class Feature:
    name: str  # its section is agent/prompts/features/<name>.md
    tools: frozenset[str]
    # The lines its section adds when the user has it, or None when they do
    # not. Reads stored state only; never raises for a missing account.
    look: Callable[[sqlite3.Connection], str | None]
    # The lines with every account connected, for `everything`.
    every: str = ""


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------

# Two accounts the user added, as the whole agent's lines show them
# (`everything`, for the evaluations): by names the user might give them.
# These lines named Gmail, "the Google account" and "the Outlook account on
# this computer" until each became an account the user adds (step 5, items 9
# and 10), and a section's line says what `_accounts` says of an account.
ADDED = ("personal", "an account the user added")
ADDED_TOO = ("work", "another account the user added")


def _listed(*pairs: tuple[str, str]) -> str:
    return "\n".join(f"- {name}: {about}" for name, about in pairs)


def accounts(lines: str) -> set[str]:
    """The accounts a section's lines list, by name: {"personal", "outlook"}
    from `_listed`'s lines. What the attention loop watches is what the
    model is told about, read from one answer."""
    return {line[2:].split(":", 1)[0] for line in lines.splitlines() if line.startswith("- ")}


# Where the user turns a switch on: the desktop app's page and group of
# switches (ui/src/App.tsx, `MENU`; ui/src/Autonomy.tsx, `GROUP_NAMES`).
# Said in one place, so that a tool's answer sends the user to the switch,
# and moves with it.
SWITCHES_PAGE = "What Steve may do"
SWITCHES_GROUP = "What Steve may use on this computer"


def switched_off(switch: str) -> str:
    """What a tool answers when the user's switch for it is off: which
    switch, and where it is. "Turn it on in Steve's settings" sent the user
    to the Settings tab, where there is no switch."""
    return (f"off: {knobs.KNOBS[switch].says}, a switch the user has not turned on. "
            f"It is in Steve's app: the menu at the user's name, {SWITCHES_PAGE},"
            f" under {SWITCHES_GROUP}.")


# What each optional part adds, and what it takes to set up, said beside it
# where it is turned on (`adds`, `takes`). The switches said only "(needs
# Docker)", and nothing said what one was for, so whether to turn it on was
# a guess. Every switch has its line here (tests/unit/test_features.py).
OFFERS: dict[str, tuple[str, str]] = {
    "BROWSER": (
        "Steve can open pages a plain read can't, ones that need their scripts, and sites "
        "you've saved a login for, in a browser sealed off from this computer.",
        "It takes Docker Desktop, installed and running."),
    "BACKGROUND_BROWSING": (
        "A routine or a goal can browse too, each time in a fresh browser.",
        "It takes the sandboxed browser, turned on."),
    "RUN_CODE": (
        "Steve can run Python to work through a spreadsheet or a file of figures you give it.",
        "It takes Docker Desktop, installed and running, and an image with pandas in it, "
        "built when you turn this on."),
    "WAKE_TIMER": (
        "A reminder due while the computer sleeps wakes it, so it comes on time.",
        "It takes a task in Windows' Task Scheduler, made when you turn this on."),
    "KEEP_AWAKE": (
        "The computer doesn't sleep while it's plugged in, so Steve stays reachable.",
        "It takes nothing to set up, and some power while the computer is idle."),
    "SIGN_OUT_IDLE": (
        "Steve signs out when nobody has used the computer for 30 minutes, though Windows "
        "stays unlocked. Locking Windows, sleep and quitting sign it out either way.",
        "It takes your passphrase or Windows Hello each time you come back."),
    # Not a switch: the local readers (agent/worker/readers.py).
    "READERS": (
        "When Jev can't answer (no key for it, an outage, a cap reached), two models on this "
        "computer read new mail and pages for instructions planted in them. Without them, "
        "such an item is cut to an extract, or withheld.",
        "It takes about 7 GB of disk, fetched once: PIGuard, 0.7 GB, and Tev1 with its "
        "runtime, 6 GB. Tev1 uses about 4.6 GB of the graphics card's memory while loaded."),
}


def _accounts(conn: sqlite3.Connection, klass: str) -> list[tuple[str, str]]:
    """Each account of `klass`, by name, with its address. Read from the
    registry, so an account or module switched off drops out, and its tools
    with it. Outlook on this computer was listed only once asked whether it
    held an account, which started Outlook, until step 5's item 10 made it
    an account the user adds."""
    from agent import connectors

    return [(found.name, found.address or found.name)
            for found in connectors.accounts(conn, klass)]


def _mail(conn: sqlite3.Connection) -> str | None:
    found = _accounts(conn, "mail")
    return _listed(*found) if found else None


def _calendar(conn: sqlite3.Connection) -> str | None:
    found = _accounts(conn, "calendar")
    return _listed(*found) if found else None


def _drive(conn: sqlite3.Connection) -> str | None:
    return "" if _accounts(conn, "files") else None


def _tasks(conn: sqlite3.Connection) -> str | None:
    """A tasks account, where `add_task` mirrors each commitment."""
    return "" if _accounts(conn, "tasks") else None


def _mailbox(conn: sqlite3.Connection) -> str | None:
    """The agent's own address, when the user has set one up."""
    from agent import mailbox

    return mailbox.address(conn) or None


def _deadlines(conn: sqlite3.Connection) -> str | None:
    """A calendar the user marked as due dates (a course site's feed)."""
    from agent import connectors
    from steve_sdk import classes

    return "" if any(found.provider.has(classes.DUE_DATES)
                     for found in connectors.accounts(conn, "calendar")) else None


def _browser(conn: sqlite3.Connection) -> str | None:
    import core.browser

    return "" if core.browser.enabled() else None


def _code(conn: sqlite3.Connection) -> str | None:
    """`run_code`, in Docker: the user's RUN_CODE switch. Offered to every
    user until October 2026, and told to use it for arithmetic, so on a
    computer without Docker the model's first sum failed."""
    return "" if knobs.on("RUN_CODE") else None


@functools.cache
def _voice_installed() -> bool:
    """Voice, by the microphone's library: core's since step 7, so missing
    only from a broken install. Asked once a process: installing it means a
    restart anyway."""
    import importlib.util

    try:
        return importlib.util.find_spec("sounddevice") is not None
    except (ImportError, ValueError):
        return False


def _desk(conn: sqlite3.Connection) -> str | None:
    return "" if _voice_installed() else None


def _weather(conn: sqlite3.Connection) -> str | None:
    """Every user: the forecast needs no account and no key.

    It was on only with a home (the `home_city` fact), on the reasoning that
    without one the tool could only ask where. It also answers for a town
    named ("the weather in Boston this weekend"), and with it hidden that
    question went to a web search -- the slow, costly answer the tool was
    built to replace -- and "will it rain tomorrow?" from a user with no
    home went to a web search before Steve asked where (the persona eval,
    12 October 2026). With no home the tool says to ask where
    (`toolset.weather.lookup`). The attention loop's forecast still needs
    a home (`sources.weather.wanted`)."""
    return ""


def _self_upgrade(conn: sqlite3.Connection) -> str | None:
    """A module Steve builds has its tests run in the sandbox
    (agent/modbuild.py): on a computer that cannot hold a module's code
    (D6), or before the sandbox was checked at this start, nothing offers
    a build that could only fail."""
    from core.sandbox import probe

    support = probe.verdict()
    return "" if support is not None and support.ok else None


def can_build(conn: sqlite3.Connection) -> bool:
    """Whether Steve can build an extension here: the app offers one only
    where `build_module` is in the turn's tools."""
    return _self_upgrade(conn) is not None


@functools.cache
def all_features() -> tuple[Feature, ...]:
    """Every feature, in the order their sections are assembled."""
    from agent.toolset import browser, desk, linkedin

    return (
        Feature("mail", frozenset({
            "search_email", "read_email", "email_thread", "modify_email", "writing_style",
            "draft_email", "delete_draft", "send_email", "send_draft"}), _mail,
            every=_listed(ADDED, ADDED_TOO)),
        Feature("mailbox", frozenset(), _mailbox,
                every="a plus-address of one of the user's mail accounts, chosen in the app"),
        Feature("calendar", frozenset({
            "calendar_agenda", "calendar_free", "calendar_search", "create_event",
            "update_event", "delete_event"}), _calendar,
            every=_listed(ADDED, ADDED_TOO)),
        Feature("deadlines", frozenset({"due_dates"}), _deadlines),
        Feature("drive", frozenset({
            "search_drive", "read_drive_file", "create_document", "create_spreadsheet",
            "read_spreadsheet", "update_spreadsheet"}), _drive),
        Feature("tasks", frozenset({"list_tasks"}), _tasks),
        # LinkedIn's tools ride on the browser: offered on top of it only
        # with a saved LinkedIn login (`toolset.linkedin.offered`), and
        # described in their own docstrings, so no section names LinkedIn to
        # a user who has not set it up.
        Feature("browser", frozenset({*browser.NAMES, *linkedin.NAMES, "start_browsing_worker"}),
                _browser),
        Feature("code", frozenset({"run_code"}), _code),
        Feature("desk", desk.NAMES | desk.NOTE_NAMES | {"find_notes"}, _desk),
        Feature("weather", frozenset({"weather"}), _weather),
        Feature("self_upgrade", frozenset({"build_module"}), _self_upgrade),
    )


def everything() -> dict[str, str]:
    """Every feature on and every account connected: the whole agent, for
    the evaluations and for the tests of what the prompt promises."""
    return {f.name: f.every for f in all_features()}


# Each feature's last answer, by data folder: what a check that fails
# stands on.
_last: dict[tuple[str, str], str | None] = {}


def enabled(conn: sqlite3.Connection) -> dict[str, str]:
    """The features this user has, each with the lines its section adds.
    Blocking (the registry and the keychain): from a coroutine,
    use `now`.

    A check that fails keeps its last answer: it dropped the feature for
    that turn, so a keychain that hiccupped took the mail tools away
    mid-conversation and changed the cached prompt twice. Never known, it
    is a feature not set up."""
    out = {}
    for feature in all_features():
        if (lines := _look(conn, feature)) is not None:
            out[feature.name] = lines
    return out


def has(conn: sqlite3.Connection, name: str) -> bool:
    """Whether this user has the feature `name`, asking only it. Blocking,
    like `enabled`."""
    [feature] = [f for f in all_features() if f.name == name]
    return _look(conn, feature) is not None


def _look(conn: sqlite3.Connection, feature: Feature) -> str | None:
    folder = str(paths.data_dir())
    try:
        lines = feature.look(conn)
    except Exception:  # noqa: BLE001 - a check that fails is answered as last time
        log.warning("could not tell whether %s is set up", feature.name, exc_info=True)
        return _last.get((folder, feature.name))
    _last[(folder, feature.name)] = lines
    return lines


async def now(conn: sqlite3.Connection) -> dict[str, str]:
    """`enabled`, off the event loop, on a connection of its own."""
    from agent import offloop

    return await offloop.with_own_conn(conn, enabled)


def tools_off(on: Enabled | None) -> frozenset[str]:
    """The tools of every feature not in `on`; none when `on` is None, the
    whole toolset."""
    if on is None:
        return frozenset()
    return frozenset().union(*(f.tools for f in all_features() if f.name not in on))


# ---------------------------------------------------------------------------
# The house prompt
# ---------------------------------------------------------------------------


def core() -> str:
    """`agent/prompts/system.md` with no feature in it: who the assistant
    is and the standing rules, which every user gets."""
    return CORE_PATH.read_text(encoding="utf-8").replace(SECTIONS_HERE + "\n\n", "")


def section(name: str, lines: str = "") -> str:
    """One feature's section, `agent/prompts/features/<name>.md`, with
    `lines` where it says `{lines}`."""
    text = (PROMPTS / "features" / f"{name}.md").read_text(encoding="utf-8").strip()
    return text.replace(LINES_HERE, lines.strip())


def assemble(on: Enabled) -> str:
    """The house prompt for a user with the features `on`: the core, with
    each one's section where it says `{{features}}`, in the order of
    `all_features`."""
    sections = [section(f.name, on[f.name]) for f in all_features() if f.name in on]
    house = CORE_PATH.read_text(encoding="utf-8")
    if not sections:
        return core()
    return house.replace(SECTIONS_HERE, "\n\n".join(sections))
