"""A decision is asked one way, under the caps, and no answer is never a guess.

`core/decide.py` is the one call to Jev, through OpenRouter. What it must hold:
- the request and the reply are the service's own, as recorded
  (tests/gate/fixtures/decisions/captured.json): a shape drifted from the
  real one would be "no answer" at every site, forever, and every test of a
  site would still pass against its fake;
- an answer is read by question name, and one out of shape is no answer to
  that question;
- a busy service is asked once more, and nothing else is;
- after three failures in a row it answers "no answer" at once, so an outage
  does not stall every screened read for seconds a part;
- it asks the cap before a call and bills each answer in the caller's unit,
  the parts of one item included, though they are asked from other threads;
- and no test can reach the real service (tests/unit/test_live_guards.py).
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx2
import pytest

from core import decide, spend
from tests.gate.decisions import Decisions, install

CAPTURED = json.loads((Path(__file__).parent / "fixtures" / "decisions" / "captured.json")
                      .read_text(encoding="utf-8"))["exchanges"]


def _questions(request: dict) -> list[decide.Question]:
    """The questions a captured request asked, as `core.decide` asks them."""
    made: list[decide.Question] = []
    for name, q in request["questions"].items():
        if q["type"] == "noul":
            made.append(decide.Predicate(name, q["instructions"]))
        elif q["type"] == "choice":
            made.append(decide.Choice(name, q["instructions"], q["criteria"]))
    return made


class Recorded:
    """The service as it answered one captured request."""

    def __init__(self, exchange: dict) -> None:
        self.reply = decide.Reply(exchange["status"], exchange["response"])
        self.asked: list[dict] = []

    def __call__(self, payload: dict, key: str) -> decide.Reply:
        self.asked.append(payload)
        return self.reply


def _bills(conn) -> list[tuple]:
    return [tuple(row) for row in conn.execute("SELECT category, part, model, input_tokens,"
                                               " cost_usd FROM usage_events ORDER BY id")]


@pytest.mark.parametrize("name", ["predicate", "choice_screen", "choice_screen_benign",
                                  "attention"])
def test_a_question_goes_out_exactly_as_the_service_was_recorded_taking_it(conn, monkeypatch,
                                                                           name):
    """The wire shape, field for field, from the questions alone -- zero data
    retention asked for in each."""
    exchange = CAPTURED[name]
    service = Recorded(exchange)
    monkeypatch.setattr(decide, "_post", service)

    decide.ask(conn, exchange["request"]["state"], _questions(exchange["request"]))

    assert service.asked == [exchange["request"]]
    assert service.asked[0]["provider"] == {"zdr": True, "data_collection": "deny"}


def test_the_answers_are_read_and_billed_as_the_service_gave_them(conn, monkeypatch):
    """A planted instruction came back `sanitize`, sure, and the bill is the
    cost the reply gave."""
    exchange = CAPTURED["choice_screen"]
    monkeypatch.setattr(decide, "_post", Recorded(exchange))

    with spend.spending("background", part="triage", conn=conn):
        said = decide.ask(conn, exchange["request"]["state"], _questions(exchange["request"]))

    assert said.error == ""
    assert said.answers["handling"] == decide.Answer(
        choice="sanitize", probabilities={"as_is": 0.0, "sanitize": 1.0, "ignore": 0.0},
        confidence=1.0)
    assert said.answers["targeting"] == decide.Answer(probability=0.99)
    assert said.cost_usd == pytest.approx(2.268e-05)
    assert _bills(conn) == [("background", "triage", "typesafe/jev-1.13", 540,
                             pytest.approx(2.268e-05))]


def test_the_price_a_reply_without_a_cost_is_billed_at_is_what_jev_charges():
    """Every recorded reply cost its input tokens at `PRICE_PER_M_INPUT`, and
    nothing for its output: a bill made from tokens matches one Jev gave."""
    answered = [ex["response"]["usage"] for ex in CAPTURED.values() if ex["status"] == 200]

    assert len(answered) >= 4
    for usage in answered:
        assert usage["cost"] == pytest.approx(
            usage["input_tokens"] * decide.PRICE_PER_M_INPUT / 1_000_000)


def test_several_questions_about_one_input_are_one_call_answered_by_name(conn, monkeypatch):
    """The recorded reply answers a choice and a predicate; asked in the other
    order, each gets its own."""
    exchange = CAPTURED["attention"]
    service = Recorded(exchange)
    monkeypatch.setattr(decide, "_post", service)
    asked = list(reversed(_questions(exchange["request"])))

    said = decide.ask(conn, exchange["request"]["state"], asked)

    assert len(service.asked) == 1
    assert said.answers["critical"] == decide.Answer(probability=0.06)
    assert said.answers["say"].choice == "now"
    assert said.answers["say"].probabilities == {"briefing": 0.16, "before": 0.28, "now": 0.55,
                                                 "never": 0.0}
    assert set(said.answers) == {"critical", "say"} and said.error == ""


@pytest.mark.parametrize(("name", "said"), [
    ("error_bad_model", "HTTP 400: Model typesafe/jev-0 does not exist"),
    ("error_no_questions", "HTTP 400: ["),
])
def test_a_request_the_service_refuses_is_no_answer_asked_once_and_billed_nothing(
        conn, monkeypatch, name, said):
    """Asking again would be refused again."""
    service = Recorded(CAPTURED[name])
    monkeypatch.setattr(decide, "_post", service)

    decision = decide.ask(conn, "text", [decide.Predicate("q", "?")])

    assert decision.answers == {} and decision.error.startswith(said)
    assert len(service.asked) == 1 and _bills(conn) == []


def test_a_busy_service_is_asked_once_more_after_the_wait_it_asks_for(conn, monkeypatch):
    """A 429 or a 5xx is often over in a moment, so it is asked once more, and
    only once. The wait it asks for is honoured, up to two seconds: the
    caller is someone waiting for a reply."""
    waits: list[float] = []
    monkeypatch.setattr(decide, "_sleep", waits.append)
    busy = decide.Reply(429, {"error": {"code": 429, "message": "slow down"}}, retry_after=1.5)
    down = decide.Reply(503, {"error": {"message": "overloaded"}}, retry_after=10)
    fake = install(monkeypatch, failures=[busy], q=0.25)

    first = decide.ask(conn, "text", [decide.Predicate("q", "?")])
    fake.failures = [down]
    second = decide.ask(conn, "text", [decide.Predicate("q", "?")])
    fake.failures = [down, down]
    third = decide.ask(conn, "text", [decide.Predicate("q", "?")])

    assert waits == [1.5, 2.0, 2.0]
    assert first.error == "" and first.answers["q"].probability == 0.25
    assert second.answers["q"].probability == 0.25
    assert third.answers == {} and third.error == "HTTP 503: overloaded"
    assert len(fake.asked) == 6


@pytest.mark.parametrize(("failure", "said"), [
    (decide.Reply(402, {"error": {"code": 402, "message": "Insufficient credits"}}),
     "HTTP 402: Insufficient credits"),
    (httpx2.ReadTimeout("slow"), "no answer in 3s"),
    (httpx2.ConnectError("refused"), "ConnectError: refused"),
])
def test_an_account_out_of_credit_a_timeout_or_no_connection_is_not_asked_again(
        conn, monkeypatch, failure, said):
    """Credit does not come back in a second, and a timeout has already made
    its caller wait as long as it should."""
    monkeypatch.setattr(decide, "_sleep", lambda s: pytest.fail("waited to ask again"))
    fake = install(monkeypatch, failures=[failure], q=0.9)

    decision = decide.ask(conn, "text", [decide.Predicate("q", "?")])

    assert decision.answers == {} and decision.error == said
    assert len(fake.asked) == 1


def test_after_three_failures_in_a_row_it_is_no_answer_at_once_for_thirty_seconds(
        conn, monkeypatch):
    """During an outage, a forty-part email would wait out forty failed calls
    before its fallback. After three, nothing is sent until thirty seconds
    have passed; then one ask goes through, and its answer closes the breaker."""
    now = [1000.0]
    monkeypatch.setattr(decide, "_breaker", decide.Breaker(clock=lambda: now[0]))
    gone = decide.Reply(400, {"error": {"code": 400, "message": "gone"}})
    fake = install(monkeypatch, failures=[gone, gone, gone], q=0.5)
    question = [decide.Predicate("q", "?")]

    for _ in range(3):
        assert decide.ask(conn, "text", question).answers == {}
    held = decide.ask(conn, "text", question)
    now[0] += 29
    still = decide.ask(conn, "text", question)

    assert len(fake.asked) == 3
    assert held.error == "Jev is not answering; asked again in 30s"
    assert still.answers == {}
    now[0] += 1
    assert decide.ask(conn, "text", question).answers["q"].probability == 0.5
    assert decide.ask(conn, "text", question).answers["q"].probability == 0.5
    assert len(fake.asked) == 5


def test_a_success_between_failures_starts_the_count_again(conn, monkeypatch):
    """Three failures in a row, not three in all: a service that fails now
    and then is still worth asking."""
    monkeypatch.setattr(decide, "_breaker", decide.Breaker(clock=lambda: 0.0))
    gone = decide.Reply(500, {})
    monkeypatch.setattr(decide, "_sleep", lambda s: None)
    question = [decide.Predicate("q", "?")]
    fake = install(monkeypatch, failures=[gone, gone, gone, gone], q=0.5)
    decide.ask(conn, "text", question)  # 500, and 500 again: one failure
    decide.ask(conn, "text", question)  # one more
    assert decide.ask(conn, "text", question).answers["q"].probability == 0.5
    fake.failures = [gone] * 4
    decide.ask(conn, "text", question)
    decide.ask(conn, "text", question)

    assert decide.ask(conn, "text", question).answers["q"].probability == 0.5


def test_a_cap_reached_or_no_key_asks_nothing_and_says_why(conn, monkeypatch):
    """Spending refused is no answer, and each site falls back as it would in
    an outage: the screen to its local readers or the extract, the critic to
    a card."""
    fake = install(monkeypatch, q=0.5)
    spend.bill(conn, 2.60, model="m", unit=spend.Unit("background"))

    with spend.spending("background", conn=conn):
        capped = decide.ask(conn, "text", [decide.Predicate("q", "?")])
    keyless = decide.ask(conn, "text", [decide.Predicate("q", "?")], key="")

    assert capped.answers == {} and capped.error.startswith(
        "a spending cap is reached: BACKGROUND_DAY_USD")
    assert keyless.error == "no OpenRouter key"
    assert fake.asked == []


def test_jev_is_asked_on_the_openrouter_key_and_no_other(conn, monkeypatch):
    """The OpenAI key sent to OpenRouter would be refused, and a secret
    handed to a service it was never meant for."""
    keys = {"OPENAI_API_KEY": "openai-key"}
    monkeypatch.setattr("core.secrets.get", keys.get)
    fake = install(monkeypatch, q=0.5)
    sent = []
    monkeypatch.setattr(decide, "_post",
                        lambda payload, key: sent.append(key) or fake(payload, key))

    without = decide.ask(conn, "text", [decide.Predicate("q", "?")])
    keys["OPENROUTER_API_KEY"] = "openrouter-key"
    with_it = decide.ask(conn, "text", [decide.Predicate("q", "?")])

    assert without.error == "no OpenRouter key"
    assert with_it.answers["q"].probability == 0.5
    assert sent == ["openrouter-key"]


def test_two_questions_with_one_name_are_not_asked(conn, monkeypatch):
    """Answers come back keyed by name, so the second would overwrite the
    first, and one site would act on another's answer."""
    fake = install(monkeypatch, q=0.5)

    said = decide.ask(conn, "text", [decide.Predicate("q", "?"), decide.Predicate("q", "!")])

    assert said.answers == {} and said.error == "ValueError: two questions share a name"
    assert fake.asked == []


def test_the_parts_of_one_item_are_asked_together_and_each_billed_in_the_callers_unit(
        conn, monkeypatch):
    """The parts go out from a pool's threads, which carry neither the
    caller's connection nor its unit of work; their bills are written back
    here, under the caller's."""
    install(monkeypatch, q=0.1, tokens=300)
    texts = [f"part {n}" for n in range(1, 6)]

    with spend.spending("chat", conn=conn):
        said = decide.ask_many(conn, texts, [decide.Predicate("q", "?")], parallel=3)

    assert [d.answers["q"].probability for d in said] == [0.1] * 5
    assert _bills(conn) == [("chat", None, "typesafe/jev-1.13", 300,
                             pytest.approx(300 * 0.042 / 1e6))] * 5


def test_an_answer_out_of_shape_is_no_answer_to_that_question_only(conn, monkeypatch):
    """A probability of 1.5, a choice not offered, a probability for a value
    not offered, an answer of the other kind (even one carrying a number
    where a predicate's would be): none is a guess at the safe side, so
    each is no answer, and the site's fallback decides. The other questions
    in the call keep theirs."""
    reply = decide.Reply(200, {"answers": {
        "high": {"type": "noul", "noul": 1.5},
        "offered": {"type": "choice", "choice": "maybe", "probabilities": {"maybe": 1.0}},
        "chosen": {"type": "choice", "choice": "yes", "probabilities": {"maybe": 1.0}},
        "listed": {"type": "choice", "choice": "yes",
                   "probabilities": [{"value": "yes", "probability": 1.0}]},
        "kind": {"type": "choice", "choice": "yes", "probabilities": {"yes": 1.0},
                 "noul": 0.9},
        "true": {"type": "noul", "noul": True},
        "fine": {"type": "noul", "noul": 0.75},
    }, "usage": {"input_tokens": 100, "cost": 0.0000042}})
    monkeypatch.setattr(decide, "_post", lambda payload, key: reply)
    questions = [decide.Predicate("high", "?"), decide.Choice("offered", "?", {"yes": "y"}),
                 decide.Choice("chosen", "?", {"yes": "y"}),
                 decide.Choice("listed", "?", {"yes": "y"}), decide.Predicate("kind", "?"),
                 decide.Predicate("true", "?"), decide.Predicate("fine", "?"),
                 decide.Predicate("forgotten", "?")]

    said = decide.ask(conn, "text", questions)

    assert said.answers == {"fine": decide.Answer(probability=0.75)}
    assert said.error == ("high: answered in a shape not asked for; offered: answered in a"
                          " shape not asked for; chosen: answered in a shape not asked for;"
                          " listed: answered in a shape not asked for; kind: answered in a"
                          " shape not asked for; true: answered in a shape not asked for;"
                          " forgotten: not answered")


def test_a_reply_that_does_not_say_what_it_used_is_billed_high_not_nothing(conn, monkeypatch):
    """A bill of nothing is spend no cap sees."""
    reply = decide.Reply(200, {"answers": {"q": {"type": "noul", "noul": 0.5}}})
    monkeypatch.setattr(decide, "_post", lambda payload, key: reply)

    decide.ask(conn, "some text", [decide.Predicate("q", "is it?")])

    [(_, _, _, tokens, cost)] = _bills(conn)
    assert tokens >= len("some text") + len("is it?")
    assert cost == pytest.approx(tokens * decide.PRICE_PER_M_INPUT / 1e6)


def test_the_cost_openrouter_reports_is_the_bill_not_the_price_written_here(conn,
                                                                           monkeypatch):
    """Jev's price can change, and each reply says what its call cost. A bill
    worked out from the price frozen here would undercount once it rose."""
    reply = decide.Reply(200, {"answers": {"q": {"type": "noul", "noul": 0.5}},
                               "usage": {"input_tokens": 1_000, "cost": 0.0003}})
    monkeypatch.setattr(decide, "_post", lambda payload, key: reply)

    decide.ask(conn, "text", [decide.Predicate("q", "?")])

    assert _bills(conn) == [("background", "unattributed", "typesafe/jev-1.13", 1_000,
                             pytest.approx(0.0003))]


@pytest.mark.parametrize("cost", ["free", -0.01, True, None])
def test_a_reply_with_tokens_but_no_cost_is_billed_at_jevs_price(conn, monkeypatch, cost):
    """A cost OpenRouter left out, or sent as something other than a number,
    is not a free call: the caps would never see it."""
    reply = decide.Reply(200, {"answers": {"q": {"type": "noul", "noul": 0.5}},
                               "usage": {"input_tokens": 1_000, "cost": cost}})
    monkeypatch.setattr(decide, "_post", lambda payload, key: reply)

    decide.ask(conn, "text", [decide.Predicate("q", "?")])

    assert _bills(conn) == [("background", "unattributed", "typesafe/jev-1.13", 1_000,
                             pytest.approx(1_000 * 0.042 / 1e6))]


@pytest.mark.parametrize("answers", [{}, {"answers": [{"name": "q", "probability": 0.9}]},
                                     {"answers": "q: 0.9"}])
def test_a_reply_with_no_answers_at_all_is_a_failure_and_still_billed(conn, monkeypatch,
                                                                      answers):
    """None, or answers in another service's shape -- a list, as OpenAI's
    Decisions API sends -- is a failure of the service, counted towards the
    breaker, and the call it cost is still billed."""
    monkeypatch.setattr(decide, "_breaker", decide.Breaker(clock=lambda: 0.0))
    reply = decide.Reply(200, {**answers, "usage": {"input_tokens": 50, "cost": 0.0000021}})
    monkeypatch.setattr(decide, "_post", lambda payload, key: reply)

    for _ in range(3):
        said = decide.ask(conn, "text", [decide.Predicate("q", "?")])

    assert said.error == "a reply with no answers"
    assert len(_bills(conn)) == 3
    assert decide._breaker.open_for() == decide.BREAK_S


def test_a_response_is_read_with_its_wait_in_seconds_and_a_body_that_is_not_json():
    said = decide.reply_of(httpx2.Response(429, headers={"retry-after": "1.5"},
                                           json={"error": {"code": 429}}))
    dated = decide.reply_of(httpx2.Response(503, headers={"retry-after":
                                                          "Wed, 21 Oct 2026 07:28:00 GMT"},
                                            text="<html>busy</html>"))

    assert said == decide.Reply(429, {"error": {"code": 429}}, 1.5)
    assert dated == decide.Reply(503, {"error": {"message": "<html>busy</html>"}}, None)


def test_the_fake_answers_as_the_recorded_service_does():
    """The fake is what every site's tests ask; built from a recorded request,
    its reply has the recorded reply's shape, field for field."""
    exchange = CAPTURED["choice_screen"]

    reply = Decisions(handling={"as_is": 0, "sanitize": 1, "ignore": 0}, targeting=0.99)(
        exchange["request"], "k")

    assert reply.body["answers"] == exchange["response"]["answers"]
    assert set(reply.body["usage"]) == set(exchange["response"]["usage"])
