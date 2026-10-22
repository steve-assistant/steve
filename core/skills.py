"""What a skill may be written from, and what it may say.

A skill is a procedure the agent saved from a run of something the user asked
for, offered to it the next time the user asks something like it
(agent/skills.py). The leading self-improving agents write their own; the
research on them (skill misevolution, arXiv 2608.12851) found every evolved
configuration it tried wrote unsafe skills, because a run that succeeded
while doing something it should not have is saved as a recipe for doing it
again. A page that talks a run into emailing an address, saved as a skill,
is an attack that no longer needs the page.

So, frozen here, where the night cannot loosen it:

- **Written from their words and the steps, never from what came back.** The
  writer is shown what the user wrote and which tools the run called, in order,
  with argument values kept only where their own words hold them
  (`trusted_value`) -- never a tool's result, never the assistant's reply,
  either of which may carry what a page or a mail said.
- **Only from runs the user was in, or tasks they asked for** (`eligible_channel`):
  never a routine's run, a goal's session, a mail to the agent, the
  attention loop or the night.
- **It names nothing new** (`problems`): no tool the run did not call, no
  address, link or number their words did not contain, nothing that claims to
  set aside their approval or tells the agent to keep something from them, and
  nothing the injection screen's patterns flag.
- **It grants nothing.** A skill is text in the instructions of their own
  conversations and nowhere else; every action it suggests still passes the
  monitor, the critic and their cards, exactly as without it.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from urllib.parse import urlsplit

MAX_STEPS = 12
MAX_CHARS = 2_000
MIN_TOOL_CALLS = 3

# Where a skill may come from: their conversations, and background tasks the user
# asked for -- the caller says which a job was (`handlers.asked_by_user`).
FROM_CHANNELS = frozenset({"signal", "telegram", "tray", "voice"})
FROM_JOBS = "job"

_ADDRESS = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_URL = re.compile(r"\bhttps?://[^\s)>\]]+", re.IGNORECASE)
_HOST = re.compile(r"\b(?:[a-z0-9-]+\.)+(?:com|org|net|edu|gov|io|co|uk|example)\b",
                   re.IGNORECASE)
_PHONE = re.compile(r"\+?\d[\d\s().-]{7,}\d")
# A measure written out, such as a cup's 236.5882365 ml: two built tools'
# descriptions in the self-upgrade trial (19 October 2026) gave that one.
_DECIMAL = re.compile(r"\d+\.\d+")
# Phrasing that claims to set aside their say, or to hide something from them.
_AUTHORITY = re.compile(
    r"without (?:asking|(?:his|her|their|the user'?s) (?:approval|yes|ok|okay)|approval"
    r"|a card|telling)"
    r"|(?:don'?t|do not|no need to|never) (?:ask|tell|mention|show|card)"
    r"|skip (?:the )?(?:approval|card|confirmation)|bypass|auto-?approve"
    r"|silently|quietly|in secret|secretly",
    re.IGNORECASE)


def eligible_channel(channel: str | None, *, asked_by_user: bool = False) -> bool:
    """Whether a session's run may become a skill."""
    channel = (channel or "").strip().lower()
    return channel in FROM_CHANNELS or (channel == FROM_JOBS and asked_by_user)


def trusted_value(value, user_words: str) -> bool:
    """Whether a tool argument's value may be shown to the writer: a number
    or a yes/no, or text their own words contain. Anything else may have come
    from what the run read."""
    if isinstance(value, bool | int | float):
        return True
    if isinstance(value, str):
        text = value.strip().lower()
        return bool(text) and text in user_words.lower()
    return False


def problems(skill: dict, *, user_words: str, tools_used: set[str],
             known_tools: set[str], whose: str = "the user's words",
             hosts: Collection[str] = ()) -> list[str]:
    """Why a written skill may not be kept, or [] if it may. `skill` has
    `title`, `when`, `steps` (a list of strings) and `tools`. `user_words`
    are what its addresses, links, sites and numbers must come from, and
    `whose` says what they are; `hosts` are `text_problems`'."""
    found: list[str] = []
    title = str(skill.get("title") or "").strip()
    when = str(skill.get("when") or "").strip()
    steps = [str(s).strip() for s in (skill.get("steps") or []) if str(s).strip()]
    tools = {str(t).strip() for t in (skill.get("tools") or []) if str(t).strip()}
    if not (title and when and steps):
        found.append("a skill needs a title, when it applies, and steps")
    if len(steps) > MAX_STEPS:
        found.append(f"more than {MAX_STEPS} steps")
    text = "\n".join([title, when, *steps])
    if len(text) > MAX_CHARS:
        found.append(f"longer than {MAX_CHARS} characters")

    # Named in the text as well as listed: a tool's name has an underscore,
    # or is listed; a bare English word ("recall") is not taken for one.
    named = tools | {t for t in known_tools
                     if "_" in t and re.search(rf"\b{re.escape(t)}\b", text)}
    if new := sorted(named - tools_used):
        found.append(f"names tools the run did not call: {', '.join(new)}")
    if unknown := sorted(tools - known_tools):
        found.append(f"names tools that do not exist: {', '.join(unknown)}")
    return found + text_problems(text, user_words=user_words, whose=whose, hosts=hosts)


def text_problems(text: str, *, user_words: str, whose: str = "the user's words",
                  hosts: Collection[str] = ()) -> list[str]:
    """Why text that will sit in Steve's instructions may not, or []: an
    address, a link, a site or a number that `user_words` do not hold, a
    claim to set aside the user's say, or the screen's injection markers.
    A night's skill is held to the user's request; a module's prompt
    section and skills to its own manifest (core/lifecycle.py), which names
    the `hosts` it reaches, not each link on them: a link on one of them is
    its own."""
    from core import screen

    found: list[str] = []
    mine = user_words.lower()
    own = {host.lower() for host in hosts}
    for kind, pattern in (("address", _ADDRESS), ("link", _URL), ("site", _HOST),
                          ("number", _PHONE)):
        for hit in pattern.findall(text):
            if kind == "number" and (sum(c.isdigit() for c in hit) < 10
                                     or _DECIMAL.fullmatch(hit)):
                continue  # a date, a time or a measure, not a number anyone can be reached at
            if kind == "link" and _host_of(hit) in own:
                # Compared whole with the bare hosts, a built upload's
                # description naming its own address was refused at
                # admission (the self-upgrade trial, 20 October 2026).
                continue
            if hit.lower().strip(".,;:") not in mine:
                found.append(f"names a {kind} {whose} did not: {hit[:80]}")
    if match := _AUTHORITY.search(text):
        found.append(f"claims to set aside the user's say: {match.group(0)!r}")
    if hits := screen.pre_screen(text):
        found.append(f"reads like an injection: {', '.join(hits)}")
    return found


def _host_of(link: str) -> str:
    """The host a link reaches, by the URL's own rules, so the host in
    `https://own.example@other.example/` is the other one; "" if it has
    none."""
    try:
        return urlsplit(link.strip(".,;:")).hostname or ""
    except ValueError:
        return ""
