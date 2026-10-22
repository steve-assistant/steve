"""When the mail servers and Jev misbehave: a send refused on the yes, a
connection dropped mid-read, a password changed on the server, and the
screen's model down or slow.

The world's mailbox is test_mail.py's: IMAP and SMTP servers on this
computer, added as setup adds them. Each case sets one trouble on the
world's servers mid-run (`refuse` on SMTP, `drop_fetches` and `password`
on IMAP, `down` and `slow_s` on Jev) and checks that the user is told
plainly, that nothing is sent twice or lost, and that the next ask is
answered as before.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.e2e.backend import Backend, running, until
from tests.e2e.test_mail import ASK_READ, CHOIR, ROTA, SUMMARY, drafts, first_id, sent
from tests.fakes.jev import FakeJev
from tests.fakes.openai import FakeOpenAI, Rule
from tests.fakes.world import world

ASK_FEES = "Email Iris that I'll pay the plot fees on Saturday."
FEES = {"to": "iris@example.com", "subject": "Plot fees",
        "body": "Hi Iris, I'll pay the plot fees on Saturday.\n\nMaya"}
COULD_NOT = "I couldn't read your mail just now."
# One email for each of the screen's troubles, never read before it.
NOTES = {
    "down": {"sender": "Iris Moreau <iris@example.com>", "subject": "Greenhouse key",
             "body": "The greenhouse key is under the blue pot now.",
             "message_id": "<key-1@example.com>"},
    "slow": {"sender": "Iris Moreau <iris@example.com>", "subject": "Compost delivery",
             "body": "The compost comes on Friday morning, by the gate.",
             "message_id": "<compost-1@example.com>"},
}


def _about(note: dict) -> tuple[Rule, ...]:
    """Asking about one of NOTES: found, read, and said back whole. Its
    extract, written when the screen cannot clear it (core/screen.py
    `Extraction`), holds it whole too."""
    subject, body = note["subject"], note["body"]
    extract = {"summary": f"Iris writes about the {subject.lower()}.", "facts": [body],
               "asks": [], "dates": [], "links": [], "aimed_at_ai": []}
    return (Rule(said=body, say=json.dumps(extract)),
            Rule(said=f"about the {subject.lower()}", call="search_email",
                 arguments={"query": subject.lower()}),
            Rule(after="search_email", result=subject, call="read_email", arguments=first_id),
            Rule(after="read_email", result=body, say=f"Iris says: {body}"))


RULES = (
    *_about(NOTES["down"]), *_about(NOTES["slow"]),
    Rule(said=ASK_READ, call="search_email", arguments={"query": "allotment rota"}),
    # Before the next: a search that failed still names what was asked.
    Rule(after="search_email", result="password", say=COULD_NOT),
    Rule(after="search_email", result="Allotment rota", call="read_email", arguments=first_id),
    Rule(after="read_email", result="Saturday watering", say=SUMMARY),
    Rule(after="search_email", result="", say=COULD_NOT),
    Rule(after="read_email", result="", say=COULD_NOT),
    Rule(said=ASK_FEES, call="send_email", arguments=FEES),
    Rule(after="send_email", result="", say="It waits in your Drafts for your yes."),
)
REFUSED = "554 5.7.1 Message rejected as spam"


@contextlib.contextmanager
def _started(folder: Path) -> Iterator[Backend]:
    """A backend on `folder`, with the rota and the choir in its mailbox."""
    jev = FakeJev(otherwise=0.1, handling="as_is")
    with (world(folder / "world", openai=FakeOpenAI(RULES), jev=jev, mail=True) as made,
          running(made, folder / "data") as backend):
        made.mail.seed("mail", [ROTA, CHOIR])
        added = backend.post("/control/accounts", made.mailbox()).json()
        assert added.get("ok") and added["test"]["ok"], (added, backend.logs())
        yield backend


@pytest.fixture(scope="module")
def steve(fenced):
    with _started(fenced) as backend:
        yield backend


@pytest.fixture
def own_steve(tmp_path):
    """A backend for one test alone, on the test's own fenced folder. Jev's
    breaker is the worker's (`core/decide.py`): after three failed asks it
    asks no more for 30 s. Where the screen's local models are missing, as
    on CI, the down case failed enough asks to open it, and the slow case
    after it, on the same backend, never asked Jev at all (CI's first
    nightly run, 22 October 2026)."""
    with _started(tmp_path / "fenced") as backend:
        yield backend


def told(steve: Backend, words: str) -> list[str]:
    """What the day shows with these words: where the user hears news."""
    return [e.get("text") or "" for e in steve.get("/control/day").json()["entries"]
            if words in (e.get("text") or "")]


def results(steve: Backend, tool: str) -> list[str]:
    """What the tool handed the model, each time, in order."""
    return [text for name, text in steve.world.openai.results if name == tool]


async def test_a_send_the_server_refuses_on_the_yes_is_said_plainly_and_kept_in_drafts(steve):
    """The yes was given; the user must hear that it did not go, and why,
    and the email must not be lost or sent twice when they try again."""
    made = steve.world
    said = await steve.chat(ASK_FEES)
    assert said["type"] == "reply", steve.logs()
    note = steve.card("To: iris@example.com")
    made.smtp.refuse = REFUSED
    try:
        assert steve.decide(note, True).json()["ok"]
        assert until(lambda: steve.action(note) == "failed"), (
            f"{steve.action(note)}\n{steve.logs()}")
    finally:
        made.smtp.refuse = ""

    assert not sent(made, "iris@example.com")
    assert len(drafts(made, "Plot fees")) == 1
    assert until(lambda: told(steve, "Couldn't do it")), steve.logs()
    [news] = told(steve, "Couldn't do it")
    assert "Plot fees" in news or "Mailbox" in news, news
    assert "rejected as spam" in news and "b'" not in news, news


async def test_a_connection_dropped_mid_read_is_read_again_and_answered(steve):
    """Servers drop connections when they restart or the network blips; a
    read changes nothing, so it is tried again on a new connection rather
    than the user being told the mail can't be reached."""
    steve.world.mail.drop_reads = 1

    said = await steve.chat(ASK_READ)

    assert steve.world.mail.drop_reads == 0
    assert said["type"] == "reply", steve.logs()
    assert said["text"] == SUMMARY, (said, results(steve, "read_email")[-1:])


async def test_a_password_changed_on_the_server_is_named_as_the_thing_to_fix(steve):
    """Said as the account's state, the same sentence the app shows: the
    user learns what to do, not that "something failed"."""
    made = steve.world
    kept = made.mail.password
    made.mail.password = "changed-on-the-server"
    try:
        said = await steve.chat(ASK_READ)
    finally:
        made.mail.password = kept

    assert said["type"] == "reply", steve.logs()
    assert said["text"] == COULD_NOT
    told = results(steve, "search_email")[-1]
    assert "password" in told.lower(), told

    again = await steve.chat(ASK_READ)
    assert again["text"] == SUMMARY, steve.logs()


@pytest.mark.parametrize("trouble", ["down", "slow"])
async def test_the_screen_down_or_slow_still_lets_an_email_be_read(own_steve, trouble):
    """Jev screens what an email says before the model reads it. Down, or
    slower than its time limit, it falls back to the screen on this
    computer, then to an extract; the email is read either way, and the
    turn is not held up. Each case has its own backend, whose Jev has not
    failed yet, and reads an email never read before: a verdict kept from
    an earlier read would not ask Jev at all."""
    steve = own_steve
    made, jev = steve.world, steve.world.jev
    note = NOTES[trouble]
    made.mail.seed("mail", [note])
    asked = len(jev.asked)
    if trouble == "down":
        jev.down = 100
    else:
        jev.slow_s = 5.0
    try:
        said = await steve.chat(f"What did Iris say about the {note['subject'].lower()}?",
                                within=90)
    finally:
        jev.down, jev.slow_s = 0, 0.0

    assert len(jev.asked) > asked
    assert said["type"] == "reply", steve.logs()
    assert said["text"] == f"Iris says: {note['body']}", (said, results(steve, "read_email")[-1:])
