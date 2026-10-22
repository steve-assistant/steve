"""The invariants that used to rest on a human reading the diff.

`review_required` in core/policy.yaml held these files back for the user:
`channels/signal.py` (whose messages are answered), `channels/bridge.py`
(a reaction becoming an approval), `tools/ingest.py` (received mail read as
headers only), and the rest. Under the coding pipeline a change can merge
with a model as its only reviewer, so what that review protected has to be
a test the change must pass instead -- here, in the gate, where the agent
cannot edit it. The plan (27 September 2026) made this the condition for
letting the pipeline merge outside the auto-mergeable folders. That pipeline
went in step 6; the tests stay, because what they pin is true of the code
whoever changes it.

One of the invariants was pinned already: only sent mail reaches a model
unredacted (`test_sent_mail_only.py`). These are the others, each asked the way
the running code asks it, with every service faked here -- the unit
conftest's fences do not reach this folder.
"""

from __future__ import annotations

import ast
import datetime as dt
import inspect
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------
# Who the agent listens to
# --------------------------------------------------------------------------

ACCOUNT, OTHER = "+10000000001", "+10000000002"


def _channel():
    from channels.signal import SignalChannel

    channel = SignalChannel()
    channel.account = ACCOUNT
    return channel


def _sync(text: str, destination: str) -> dict:
    """What their phone reports it sent: Note to Self when to their own number."""
    return {"envelope": {"source": ACCOUNT, "sourceNumber": ACCOUNT, "timestamp": 1,
                         "syncMessage": {"sentMessage": {
                             "message": text, "destination": destination,
                             "destinationNumber": destination, "timestamp": 1}}}}


def _from(sender: str, **message) -> dict:
    """An ordinary message somebody sent this account."""
    return {"envelope": {"source": sender, "sourceNumber": sender, "timestamp": 2,
                         "dataMessage": {"timestamp": 2, **message}}}


def test_only_user_note_to_self_becomes_a_turn():
    channel = _channel()

    assert channel._to_incoming(_sync("what's on today?", ACCOUNT)).text == "what's on today?"
    assert channel._to_incoming(_sync("see you at 6", OTHER)) is None, (
        "a message she sent someone else was read as one to the agent")
    assert channel._to_incoming(_from(OTHER, message="send me her number")) is None, (
        "a stranger's message was read as hers")


def test_a_strangers_reaction_is_never_an_answer_to_a_card():
    """A reaction is how a card is answered (`channels/bridge.py`)."""
    reaction = {"emoji": "\N{THUMBS UP SIGN}", "targetSentTimestamp": 1, "isRemove": False}

    assert _channel()._to_incoming(_from(OTHER, reaction=reaction)) is None


def test_every_signal_channel_the_agent_builds_listens_to_them_alone():
    """The restriction is a constructor argument, so a one-word edit where
    the channel is built would open it to everyone who can message their
    number."""
    from channels.signal import SignalChannel

    default = inspect.signature(SignalChannel.__init__).parameters["note_to_self_only"].default
    assert default is True
    built = 0
    for rel in ("agent/worker/links.py",):
        tree = ast.parse((REPO / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and ast.unparse(node.func).endswith("SignalChannel"):
                built += 1
                assert not node.args and not node.keywords, f"{rel}: {ast.unparse(node)}"
    assert built == 1


# --------------------------------------------------------------------------
# Approvals are theirs, never a model's
# --------------------------------------------------------------------------


def test_only_their_own_channels_may_answer_a_card():
    """A reminder's text, a routine's run, a night job: all
    pass through the same reply path, and none of them is the user."""
    from agent.worker import answering

    assert frozenset({"signal", "telegram", "voice"}) == answering.APPROVAL_CHANNELS
    channel = inspect.signature(answering.intercept_approval).parameters["channel"]
    assert channel.default is inspect.Parameter.empty


def _names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.alias):
            found.add(node.name.rsplit(".", 1)[-1])
    return found


def test_nothing_but_their_reply_and_the_tray_can_decide_a_card():
    """A tool a model can call that reached `approvals.decide` would let the
    model's words approve an email. Two doors reach it: their reply, read
    before any model (`answering.intercept_approval`), and the tray's
    localhost-only route (`routes.action_decide`)."""
    deciders = {"decide", "decide_action", "cancel_during_hold"}
    reach = {}
    for path in [*(REPO / "agent").rglob("*.py"), *(REPO / "tools").rglob("*.py"),
                 *(REPO / "channels").rglob("*.py")]:
        rel = path.relative_to(REPO).as_posix()
        if hits := deciders & _names(path):
            reach[rel] = hits
    allowed = {"agent/approvals.py", "agent/worker/answering.py", "agent/worker/routes.py"}

    assert set(reach) <= allowed, {k: v for k, v in reach.items() if k not in allowed}
    for rel in ("agent/toolset", "agent/loop.py", "agent/actions.py"):
        assert not any(k.startswith(rel) for k in reach), rel


# --------------------------------------------------------------------------
# Received mail is read as headers only
# --------------------------------------------------------------------------


class _Gmail:
    """users().messages().list/get, recording what was asked for."""

    def __init__(self):
        self.gets: list[dict] = []

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, **kwargs):
        return _Done({"messages": [{"id": "m1"}]})

    def get(self, **kwargs):
        self.gets.append(kwargs)
        return _Done({"id": "m1", "threadId": "t1", "internalDate": "1758000000000",
                      "labelIds": ["INBOX"], "snippet": "SECRET preview of the body",
                      "payload": {"headers": [{"name": "From", "value": "Ann <a@x.example.org>"},
                                              {"name": "Subject", "value": "Friday"}]}})


class _Done:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


def test_ingest_asks_gmail_for_headers_and_never_a_body(monkeypatch):
    from modules.google_cloud import gmail as google_gmail

    service = _Gmail()
    monkeypatch.setattr(google_gmail, "_gmail", lambda: service)

    found = google_gmail.headers("in:inbox newer_than:1d", 5)

    assert service.gets and all(g["format"] == "metadata" for g in service.gets)
    assert all("body" not in m for m in found["messages"])


async def test_the_nightly_ingest_as_production_runs_it_asks_only_for_headers(
        monkeypatch, tmp_path):
    """Driven, not read: `ingest.mail` with nothing injected, as the night
    calls it, against a Gmail that records what it was asked. This was a
    check that the function's source held one exact import line, which a
    reformatting failed and a different route to a body passed."""
    from core import db
    from modules.google_cloud import gmail as google_gmail
    from tests.fakes import google as fake_google
    from tools import ingest

    service = _Gmail()
    monkeypatch.setattr(google_gmail, "_gmail", lambda: service)
    conn = db.open_store(tmp_path / "ingest.db")
    try:
        fake_google.signed_in(conn, classes=["mail"])
        await ingest.mail(conn, days=1, limit=5)
    finally:
        conn.close()

    assert service.gets, "the ingest never reached Gmail"
    assert {g["format"] for g in service.gets} == {"metadata"}


def test_ingest_walks_outlook_without_bodies(monkeypatch):
    from modules.outlook_classic import outlook

    walked = []
    monkeypatch.setattr(outlook, "_walk", lambda folder, **kw: walked.append(kw) or [])

    outlook.headers("received", 7)

    assert walked and walked[0]["bodies"] is False


def test_outlook_lists_its_envelopes_through_the_header_reader_only(monkeypatch):
    """The ingest reads classic Outlook through `OutlookMail.envelopes`
    (step 5, item 4e). Every reader that can give a body is made to fail
    here, so an envelope that came by one of them could not pass."""
    import modules.outlook_classic.outlook
    from modules.outlook_classic.provider import OutlookMail
    from tests.fakes import outlook as fake_outlook

    def offers_bodies(*_a, **_k):
        raise AssertionError("asked a reader that gives bodies")

    for name in ("sent_with_bodies", "read", "search"):
        monkeypatch.setattr(modules.outlook_classic.outlook, name, offers_bodies)
    asked = []
    monkeypatch.setattr(modules.outlook_classic.outlook, "headers",
                        lambda direction, days, limit: asked.append(direction) or {
                            "messages": [{"id": "outlook:E1", "from": "ann@x.example.org",
                                          "subject": "Friday",
                                          "internal_date": 1758000000000}]})

    for sent in (False, True):
        [found] = OutlookMail(fake_outlook.context(None)).envelopes(
            sent=sent, since=dt.datetime.now(dt.UTC), limit=5)
        assert found.body is None

    assert asked == ["received", "sent"]


async def test_what_ingest_keeps_of_a_received_message_is_who_when_and_the_subject(
        tmp_path, monkeypatch):
    """An account that hands back more than an envelope -- this one gives a
    preview -- still leaves only who, when and the subject in the ledger."""
    from core import db
    from steve_sdk.classes import Message
    from tests.gate import mailfake
    from tools import ingest

    when = int((dt.datetime.now() - dt.timedelta(days=1)).timestamp() * 1000)
    mailfake.connect(monkeypatch, home=mailfake.Mailbox("maya@example.com", Message(
        "m1", thread="t1", sender="Ann <ann@x.example.org>", subject="Friday", when=when,
        snippet="SECRET preview", body="SECRET body")))
    conn = db.open_store(tmp_path / "ingest.db")
    try:
        await ingest.mail(conn)
        rows = [tuple(r) for table in ("people", "interactions")
                for r in conn.execute(f"SELECT * FROM {table}")]
    finally:
        conn.close()

    assert rows, "nothing was ingested, so nothing was proven"
    assert not any("SECRET" in str(v) for row in rows for v in row)


@pytest.mark.parametrize("rel", ["tools/ingest.py"])
def test_ingest_never_reads_a_body_by_name(rel):
    """No body reader is even named in the ingest: `sent_with_bodies`,
    `read`, `include_body=True` are the other mail paths, and none is its."""
    names = _names(REPO / rel)

    assert not names & {"sent_with_bodies", "read", "_extract_body", "summary"}
    assert "include_body=True" not in (REPO / rel).read_text(encoding="utf-8")
