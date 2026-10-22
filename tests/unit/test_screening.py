"""Outside text reaches a model only as the injection screen allows.

Each email, page, shared document and attention preview goes through the
screen (agent/screening.py, core/screen.py) before a model reads it, and the
model reads what the screen gives back: the item, an extract, or a note
that it was withheld. These pin where the screen is fed from, what a tool
returns when it does not pass an item, and how the wrapper runs it -- off
the event loop, never round it. What the screen decides is the gate's
(tests/gate/test_injection_screen.py, tests/gate/test_screen_enforces.py).
"""

from __future__ import annotations

import threading

import pytest

from agent import screening
from agent.turn import Deps
from core import screen
from tests import persona
from tests.fakes import google as fake_google

# Before the conftest lets every item through for every test.
REAL_READ = screening._read


@pytest.fixture
def fed(monkeypatch):
    """What reaches the screen, and what it answers: as it came, unless a
    test sets `fed.answer` to what the screen allows instead."""

    class Fed(list):
        answer: screen.Shown | None = None

    seen = Fed()

    def read(conn, artifact):
        seen.append(artifact)
        return seen.answer or screen.Shown(artifact.text, "text")

    monkeypatch.setattr(screening, "_read", read)
    return seen


EXTRACT = screen.Shown("[Screened: an extract]", "extract", "sanitize")


class _Ctx:
    def __init__(self, deps):
        self.deps = deps


def _tools(module) -> dict:
    registered: dict = {}
    module.register(lambda fn: registered.setdefault(fn.__name__, fn))
    return registered


EMAIL = {"id": "m1", "thread_id": "t1", "from": "Iris Novak <iris_novak@tailspin.example>",
         "to": persona.EMAIL, "subject": "Great chatting",
         "body": "Could you confirm Tuesday?", "snippet": "Could you confirm",
         "internal_date": 1_700_000_000_000, "unread": True}


async def test_an_email_read_in_full_is_screened_and_one_passed_reaches_the_model_unchanged(
        fed, store, monkeypatch):
    from agent.toolset import mail

    monkeypatch.setattr(mail, "read_any", lambda message_id, upto: dict(EMAIL))
    said = await _tools(mail)["read_email"](_Ctx(Deps(conn=store, session_id=1)), "m1")

    assert said["body"] == "Could you confirm Tuesday?"
    assert [(a.source, a.source_id, a.origin) for a in fed] == [
        ("email_body", "m1", "Iris Novak <iris_novak@tailspin.example>")]
    assert "Could you confirm Tuesday?" in fed[0].text and "Great chatting" in fed[0].text


async def test_an_email_the_screen_did_not_pass_keeps_nothing_its_sender_wrote(fed, store):
    """Its name, subject and preview are the sender's to write as much as
    its body is; where it sits -- its account -- and when it came are not."""
    fed.answer = EXTRACT

    said = await screening.mail(store, dict(EMAIL, account="gmail"))

    assert said == {"id": "m1", "thread_id": "t1", "internal_date": 1_700_000_000_000,
                    "account": "gmail", "unread": True, "screened": "extract",
                    "body": EXTRACT.text}


async def test_every_message_of_a_thread_is_screened(fed, store, monkeypatch):
    from agent.toolset import mail
    from steve_sdk.classes import Message
    from tests.gate.mailfake import Mailbox, connect

    box = Mailbox(persona.EMAIL, *(
        Message(native_id=native, thread="t1", sender=EMAIL["from"], subject=EMAIL["subject"],
                body=body, when=when)
        for native, body, when in (("m1", EMAIL["body"], 1), ("m2", "Second.", 2))))
    gmail = connect(monkeypatch, gmail=box)["gmail"]
    fed.answer = EXTRACT
    said = await _tools(mail)["email_thread"](_Ctx(Deps(conn=store, session_id=1)),
                                             gmail.id("t1"))

    # Sorted: the messages are screened at once, each on a thread of its
    # own, so which reaches the screen first is the scheduler's choice.
    assert sorted(a.source_id for a in fed) == sorted([gmail.id("m1"), gmail.id("m2")])
    assert [m["body"] for m in said["messages"]] == [EXTRACT.text, EXTRACT.text]


async def test_a_page_read_is_screened_and_one_not_passed_loses_its_title(
        fed, store, monkeypatch):
    from agent.toolset import web as web_tools
    from tools import web

    async def fetch(url, **kwargs):
        return web.PageContent(url=url, title="Story", text="the page")

    monkeypatch.setattr(web, "fetch", fetch)
    deps = Deps(conn=store, session_id=1, channel="signal")
    read_page = _tools(web_tools)["read_page"]

    passed = await read_page(_Ctx(deps), "https://news.example/a")
    fed.answer = EXTRACT
    screened = await read_page(_Ctx(deps), "https://news.example/a")

    assert [(a.source, a.origin) for a in fed] == [("web_page", "news.example")] * 2
    assert "Story" in fed[0].text and "the page" in fed[0].text
    assert passed["title"] == "Story" and passed["text"] == "the page"
    assert screened["title"] is None and screened["text"] == EXTRACT.text


async def test_a_shared_document_is_screened(fed, store, monkeypatch):
    from agent.toolset import drive
    from steve_sdk.classes import FileItem
    from tests.gate import slotfake

    shared = FileItem(native_id="d1", name="Shared notes", text="Action items")
    accounts = slotfake.plug(monkeypatch, "files", google=slotfake.Drive(shared))
    fed.answer = EXTRACT
    said = await _tools(drive)["read_drive_file"](
        _Ctx(Deps(conn=store, session_id=1)), accounts["google"].id("d1"))

    assert [(a.source, a.origin) for a in fed] == [("drive_doc", "Shared notes")]
    assert said["text"] == EXTRACT.text and "name" not in said


@pytest.fixture
def google(store):
    """Their Google account, added and signed in as the app does it; its
    connection of a class, as the slot builds it."""
    from agent import connectors

    fake_google.signed_in(store)
    return lambda klass: next(a for a in connectors.accounts(store, klass)
                              if a.key == fake_google.KEY)


def test_the_attention_judges_preview_is_screened(fed, store, google, monkeypatch):
    from agent.attention.sources import mail as mail_source
    from modules.google_cloud import gmail as google_gmail

    monkeypatch.setattr(google_gmail, "message_ids", lambda q, n: {"ids": ["g1"]})
    monkeypatch.setattr(google_gmail, "summary", lambda i: {
        "id": i, "from": "Site News <news@site.example>", "subject": "Hi",
        "snippet": "a preview", "internal_date": 0})
    fed.answer = EXTRACT

    account = google("mail")
    [seen] = mail_source.watch(store, account)

    assert [(a.source, a.source_id) for a in fed] == [("email_preview", account.id("g1"))]
    assert "a preview" in fed[0].text
    assert "Hi" not in seen.title and "a preview" not in seen.detail
    assert EXTRACT.text in seen.detail
    assert seen.ref["from"] == "news@site.example" and seen.ref["subject"] is None


def _attention(name: str):
    """One of the attention loop's sources, as the loop runs it."""
    from agent.attention import sources

    return next(source for source in sources.all_sources() if source.name == name)


def test_a_headline_is_screened_and_one_not_passed_keeps_nothing_its_outlet_wrote(
        fed, store, monkeypatch):
    """The outlet names itself, so a headline the screen did not pass loses
    the outlet's name with its words. One it passed is as it was."""
    from agent.attention import fetch, interests, sources

    interests.follow(store, "world news")
    monkeypatch.setattr(fetch, "get_text", lambda url, params=None: (
        "<rss><channel><item><title>Rates rise - Daily Planet</title>"
        "<link>https://news.example/a</link>"
        "<source url='https://dailyplanet.example'>Daily Planet</source>"
        "</item></channel></rss>"))

    [passed] = sources.collect(store, _attention("news"))
    fed.answer = EXTRACT
    [seen] = sources.collect(store, _attention("news"))

    assert [(a.source, a.source_id, a.origin, a.text) for a in fed] == [
        ("news_headline", passed.key, "Daily Planet", "Rates rise (Daily Planet)")] * 2
    assert passed.title == "Rates rise (Daily Planet)" and passed.ref["outlet"] == "Daily Planet"
    assert seen.title == "A news headline" and EXTRACT.text in seen.detail
    assert "Rates rise" not in str(seen) and "Daily Planet" not in str(seen)
    assert seen.ref == {"link": "https://news.example/a"} and seen.key == passed.key


def test_a_shared_files_name_is_screened_and_one_not_passed_is_left_to_its_owner(
        fed, store, google, monkeypatch):
    """Drive gives the owner's address, which stays; the name, which the
    owner wrote, goes when the screen does not pass it."""
    from agent.attention import sources
    from modules.google_cloud import drive as google_drive

    monkeypatch.setattr(google_drive, "recent", lambda limit: {"files": [
        {"id": "f1", "name": "Q3 numbers", "owner": "eli@contoso.example", "modified": "t1",
         "mime_type": "spreadsheet", "link": "https://drive.example/f1"}]})

    [passed] = sources.collect(store, _attention("drive"))
    fed.answer = EXTRACT
    [seen] = sources.collect(store, _attention("drive"))

    file_id = google("files").id("f1")
    where = f"their files ({fake_google.ADDRESS})"
    assert [(a.source, a.source_id, a.origin, a.text) for a in fed] == [
        ("drive_file_name", file_id, "eli@contoso.example",
         f"Q3 numbers -- changed in {where}")] * 2
    assert passed.title == f"Q3 numbers -- changed in {where}"
    assert seen.title == f"A file someone else changed in {where}"
    assert "Q3 numbers" not in str(seen) and EXTRACT.text in seen.detail
    assert "eli@contoso.example" in seen.detail and seen.ref["file_id"] == file_id


# ---------------------------------------------------------------------------
# How the wrapper runs the screen
# ---------------------------------------------------------------------------


@pytest.fixture
def ran(monkeypatch):
    """The real wrapper, over a screen that records the thread and the
    connection it ran on."""
    monkeypatch.setattr(screening, "_read", REAL_READ)
    calls: list[tuple] = []

    def read(conn, artifact, key=None):
        calls.append((threading.current_thread(), conn, artifact.source_id))
        return screen.Shown(artifact.text, "text")

    monkeypatch.setattr(screen, "read", read)
    return calls


async def test_on_the_event_loop_the_screen_runs_in_a_thread_on_its_own_connection(
        ran, store):
    """A connection belongs to the thread that made it, and the screen can
    take seconds: it never runs on the loop, nor on the loop's connection."""
    said = await screening.shown(store, "web_page", "u", "x.example", "text")

    [(thread, conn, _)] = ran
    assert said.text == "text"
    assert thread is not threading.current_thread()
    assert conn is not store and conn is not None


def test_in_a_thread_the_screen_runs_in_place_on_its_connection(ran, store):
    """The attention sources run in a thread with a connection of their own."""
    worker = threading.Thread(target=screening.shown_here,
                              args=(store, "email_preview", "g1", "x", "a preview"))
    worker.start()
    worker.join()

    assert ran == [(worker, store, "g1")]


async def test_with_no_connection_an_item_is_still_screened(ran):
    """`conn` None only means nothing is looked up or kept."""
    await screening.shown(None, "web_page", "u", "x.example", "text")

    assert [source_id for _, _, source_id in ran] == ["u"]


async def test_a_failed_read_and_an_empty_one_have_nothing_to_screen(fed, store):
    from tools import web

    failed = {"error": "Invalid id value (400)"}
    assert await screening.mail(store, failed) is failed
    assert (await screening.shown(store, "web_page", "u", "x", "   ")).how == "text"
    unread = web.PageContent(url="https://x.example", error="HTTP 404")
    assert await screening.page(store, unread) is unread

    assert fed == []


async def test_a_screen_that_breaks_withholds_the_item(monkeypatch, store):
    """A broken screen is loud, never a quiet way round it."""
    monkeypatch.setattr(screening, "_read", REAL_READ)

    def broken(conn, artifact, key=None):
        raise RuntimeError("the screen fell over")

    monkeypatch.setattr(screen, "read", broken)

    said = await screening.shown(store, "web_page", "u", "x.example", "PLANTED TEXT")

    assert said.how == "withheld" and "PLANTED TEXT" not in said.text
    assert "could not be screened" in said.text
