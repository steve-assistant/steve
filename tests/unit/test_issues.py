"""The failure ledger: what went wrong is written down where it happens.

Until 28 September 2026 nothing in the agent noticed its own failures. The
300-second stall and "both complete" read as the week's gym reminders were
found by a person reading logs. These pin each place that now records one,
the ledger's own counting, their thumbs on a reply, and the checks on what
it depends on.
"""

from __future__ import annotations

import json

import pytest

from agent import actions, issues, runtime
from agent.loop import TurnFailed
from core import db
from tests.unit.test_turn_failures import GAME_THEORY, talk  # noqa: F401 - the fixture


def _rows(conn, kind=None):
    query = "SELECT kind, summary, excerpt, count FROM issues"
    rows = conn.execute(query + (" WHERE kind = ?" if kind else ""),
                        (kind,) if kind else ()).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------
# The ledger
# --------------------------------------------------------------------------


def test_the_same_failure_counts_up_rather_than_piling_up(store):
    """"HTTP 502 after 41s" and "after 7s" are one issue, seen twice."""
    first = issues.record(store, "tool_error", "calendar_agenda: HTTP 502 after 41s", "a")
    again = issues.record(store, "tool_error", "calendar_agenda: HTTP 502 after 7s", "b")
    other = issues.record(store, "tool_error", "search_email: signed out", "c")

    assert first == again != other
    assert [r["count"] for r in _rows(store)] == [2, 1]
    assert store.execute("SELECT COUNT(*) FROM issue_events").fetchone()[0] == 3


def test_an_unknown_kind_is_refused_and_a_broken_ledger_breaks_nothing(store, tmp_path):
    assert issues.record(store, "tool_eror", "x", "a typo") is None
    closed = db.open_store(tmp_path / "closed.db")
    closed.close()
    assert issues.record(closed, "tool_error", "x", "y") is None, "it raised into the turn"


# --------------------------------------------------------------------------
# What the night works on
# --------------------------------------------------------------------------


def test_what_the_user_noticed_is_worked_on_before_what_happened_more(store):
    """A thumbs-down counts three times: the user saw it, and said so. Their
    thumbs-up is not work at all."""
    for _ in range(2):
        slow = issues.record(store, "slow_turn", "slow reply", "A reply took 133s")
    down = issues.record(store, "feedback_down", "the drafts", "Maya gave a thumbs-down")
    issues.record(store, "feedback_up", "good call", "Maya gave a thumbs-up")

    assert [c["id"] for c in issues.candidates(store)] == [down, slow]


def test_an_issue_tried_often_or_long_quiet_is_not_offered(store):
    """After three tries the pipeline cannot do it and a person should
    look; an issue not seen in two weeks has gone away."""
    tried = issues.record(store, "tool_error", "tried", "tried")
    quiet = issues.record(store, "tool_error", "quiet", "quiet")
    with store:
        for _ in range(issues.MAX_TRIES):
            store.execute("INSERT INTO issue_attempts (issue_id, status, at)"
                          " VALUES (?, 'rejected', datetime('now', '-10 days'))", (tried,))
        store.execute("UPDATE issues SET last_seen = datetime('now', '-15 days') WHERE id = ?",
                      (quiet,))

    assert issues.candidates(store) == []


def test_an_issue_tried_lately_waits_and_then_comes_back(store):
    issue = issues.record(store, "tool_error", "x", "x")
    issues.attempted(store, issue, 60, "rejected")
    assert issues.candidates(store) == []

    with store:
        store.execute("UPDATE issue_attempts SET at = datetime('now', ?)",
                      (f"-{issues.RETRY_AFTER_DAYS + 1} days",))
    assert [c["id"] for c in issues.candidates(store)] == [issue]
    assert issues.candidates(store)[0]["tries"] == 1


def test_an_attempt_however_it_ended_leaves_its_issue_open(store):
    """Until step 6 a change that merged marked its issue fixed. A fix is a
    module now, and nothing is fixed until the user turns it on."""
    issue = issues.record(store, "tool_error", "x", "x")
    issues.attempted(store, issue, 0, "approved")

    assert store.execute("SELECT status FROM issues").fetchone()[0] == "open"


def test_a_fixed_issue_that_happens_again_is_opened_again(store):
    """The fix did not hold, and the ledger should say so rather than keep
    believing it."""
    issue = issues.record(store, "tool_error", "x", "x")
    with store:
        store.execute("UPDATE issues SET status = 'fixed' WHERE id = ?", (issue,))

    issues.record(store, "tool_error", "x", "x again")

    assert store.execute("SELECT status FROM issues").fetchone()[0] == "open"


def test_an_ignored_issue_stays_ignored_when_it_happens_again(store):
    issue = issues.record(store, "source_failed", "home dns", "x")
    with store:
        store.execute("UPDATE issues SET status = 'ignored' WHERE id = ?", (issue,))

    issues.record(store, "source_failed", "home dns", "x")

    assert store.execute("SELECT status FROM issues").fetchone()[0] == "ignored"


def test_the_morning_line_counts_the_last_day_by_kind(store):
    for _ in range(2):
        issues.record(store, "slow_turn", "converse", "slow")
    issues.record(store, "feedback_down", "that reply", "down")
    issues.record(store, "feedback_up", "a good one", "up")
    with store:
        old = issues.record(store, "tool_error", "old", "old")
        store.execute("UPDATE issue_events SET at = datetime('now', '-3 days') WHERE issue_id = ?",
                      (old,))

    line = issues.briefing_line(store)

    assert line == "noticed about itself: 2 slow replies, 1 thumbs-down"
    assert [i["lately"] for i in issues.recent(store)][:1] == [2]


def test_a_clean_day_says_nothing(store):
    assert issues.briefing_line(store) is None


@pytest.mark.parametrize("text", [
    "wait why did you cancel gym and runs?", "Why did you send that?",
    "no, I meant the Friday one", "that's not what I asked", "you misread it",
])
def test_their_corrections_are_recognised(text):
    assert issues.is_correction(text)


# Corrections as the user types them -- curly apostrophes, no capitals, a
# name and an employer run together. Twelve like these, typed between 28
# September and 7 October 2026, went past the detector, none caught.
REAL_CORRECTIONS = [
    "That’s who Alex is, not Alexandra Wren",
    "It’s in the work inbox",
    "tomorrow isn't actually light i'm heading out hiking",
    "that's literally your github login",
    "i just had you write the thank you note for it, but thanks for asking",
    "not my calendar or anything. Tomas Varga is someone who works at Fabrikam's Denver office",
    "you also used the wrong email addresses for everyone, the tailspin format is "
    "firstname_lastname@tailspin.example",
    "you shouldn't be on away mode right now, i'm messaging you on laptop",
    "Asha Verma Fabrikam is actually hosting the workshop",
    "not meeting with lena for that event",
    "Ok please don’t do that in the future",
]


@pytest.mark.parametrize("text", REAL_CORRECTIONS)
def test_their_real_corrections_are_recognised_curly_apostrophes_and_all(text):
    """Only one correction had ever been recorded (issue 65) when these ten
    went by; a correction the ledger never hears of is one the night never
    works on, and a skill was kept from a corrected turn."""
    assert issues.is_correction(text)


@pytest.mark.parametrize("text", [
    "no", "both complete", "yes recreate", "what's on today?", "noted, thanks",
    "good, no thank you note needed", "not right now", "not yet", "that's what I wanted!",
    "Thanks, that's great.",
])
def test_ordinary_replies_are_not_corrections(text):
    assert not issues.is_correction(text)


# --------------------------------------------------------------------------
# Where the conversation records them
# --------------------------------------------------------------------------


async def test_a_turn_that_breaks_is_an_issue(talk):  # noqa: F811
    talk["script"] = ["boom"]

    with pytest.raises(TurnFailed):
        await talk["open"]().send(GAME_THEORY)

    [row] = _rows(talk["conn"], "turn_broke")
    assert "RuntimeError: the provider gave up" in row["summary"]
    assert "game theory class" in row["excerpt"]


async def test_a_tool_that_errors_is_an_issue(talk, monkeypatch):  # noqa: F811
    async def raises(**kwargs):
        raise ValueError("the calendar is down")

    monkeypatch.setitem(actions.EXECUTORS, "create_event", raises)
    talk["script"] = ["add"]

    await talk["open"]().send(GAME_THEORY)

    [row] = _rows(talk["conn"], "tool_error")
    assert row["summary"].startswith("create_event reported: ValueError: the calendar is down")


async def test_their_correction_is_an_issue_with_what_it_had_said(talk):  # noqa: F811
    """27 September: "wait why did you cancel gym and runs?"."""
    convo = talk["open"]()
    talk["script"] = ["Nice work on both of them.", "I misread you, sorry."]

    await convo.send("both complete")
    await convo.send("wait why did you cancel gym and runs?")

    [row] = _rows(talk["conn"], "correction")
    assert row["excerpt"].startswith("It said: Nice work on both of them.")
    assert "The user said: wait why did you cancel" in row["excerpt"]


async def test_words_the_agent_hands_itself_are_never_their_correction(talk):  # noqa: F811
    """On 10 October the brief for something prepared unasked ("She is not at
    her phone and is not waiting for an answer") was filed as the user
    correcting it, and the checks before a ping read the ledger's
    corrections as their words."""
    from agent.loop import Conversation

    talk["script"] = ["Found it."]

    await Conversation(talk["conn"], channel="initiative").send(
        "wait why did you cancel gym and runs?")

    assert _rows(talk["conn"], "correction") == []


async def test_a_slow_reply_is_an_issue(talk, monkeypatch):  # noqa: F811
    monkeypatch.setattr(issues, "SLOW_TURN_MS", -1)
    talk["script"] = ["Here you are."]

    await talk["open"]().send("what's on today?")

    assert _rows(talk["conn"], "slow_turn")[0]["summary"].startswith("A reply took")


async def test_a_claim_no_tool_backs_is_an_issue(talk):  # noqa: F811
    talk["script"] = ["I've added it to your calendar.", "I haven't added it yet."]

    await talk["open"]().send(GAME_THEORY)

    [row] = _rows(talk["conn"], "unbacked_claim")
    assert "added it to your calendar" in row["excerpt"]


def test_a_source_that_fails_is_an_issue(store):
    from agent.attention import sources

    def broken(conn):
        raise ConnectionError("the feed is down")

    sources.collect(store, sources.Source("weather", broken))

    [row] = _rows(store, "source_failed")
    assert row["summary"] == "The weather source failed: ConnectionError: the feed is down"


# --------------------------------------------------------------------------
# Their thumbs on a reply
# --------------------------------------------------------------------------


class _Channel:
    """A Signal channel whose sent messages are known by timestamp."""

    name = "signal"

    def __init__(self, sent):
        self.sent = sent

    def sent_text(self, timestamp):
        return self.sent.get(timestamp)

    def sent_card(self, timestamp):
        return None  # a reply, never a card

    async def mark_read(self, message):
        pass


async def test_a_thumbs_down_on_a_reply_is_feedback_not_an_answer():
    """Before 28 September a reaction on anything but a card was dropped."""
    from channels.base import Incoming
    from channels.bridge import ChannelBridge

    heard, answered = [], []

    async def reply_fn(text):
        answered.append(text)
        return "", {}

    async def feedback(verdict, on):
        heard.append((verdict, on))

    bridge = ChannelBridge(_Channel({7: "Done: I cancelled six reminders."}), reply_fn,
                           feedback=feedback)
    for emoji in ("👎", "❤️"):
        await bridge._handle(Incoming(text="", channel="signal", sender="+1", message_id="1",
                                      timestamp=1, reaction=emoji, reacted_to=7))

    assert heard == [("down", "Done: I cancelled six reminders.")]
    assert answered == [], "a reaction went to the model"


def test_their_thumbs_go_in_the_ledger(store, monkeypatch):
    """A thumb on a reply is their verdict on it; anything but up or down,
    or on nothing, is not one."""
    from agent.worker import answering

    monkeypatch.setattr(runtime, "conn", store)

    answering.note_feedback("down", "Here's your agenda.")
    answering.note_feedback("up", "Moved it to Friday.")
    answering.note_feedback("sideways", "x")
    answering.note_feedback("down", "  ")

    assert _rows(store, "feedback_down")[0]["excerpt"] == "Here's your agenda."
    assert _rows(store, "feedback_up")[0]["excerpt"] == "Moved it to Friday."
    assert len(_rows(store)) == 2


async def test_what_the_user_says_about_how_it_did_is_written_down_by_the_tool(store):
    from types import SimpleNamespace

    from agent import loop
    from agent.models import Role

    tools = loop.build_agent(Role.CONVERSE)._function_toolset.tools
    ctx = SimpleNamespace(deps=SimpleNamespace(tools_used=[], conn=store, session_id=None,
                                               performed={}))

    assert (await tools["note_feedback"].function(
        ctx, "don't ping me about newsletters", "the 9am ping"))["ok"]

    [row] = _rows(store, "feedback_note")
    assert row["summary"] == "The user: don't ping me about newsletters"


# --------------------------------------------------------------------------
# What it depends on
# --------------------------------------------------------------------------

# The shape OpenRouter's /api/v1/credits returned on 28 September 2026, the
# morning the balance was found below zero (protocols §11).
CREDITS = {"data": {"total_credits": 10, "total_usage": 10.095969669}}


@pytest.mark.parametrize("credits, said", [
    (CREDITS, "OpenRouter credit is used up ($-0.10)"),
    ({"data": {"total_credits": 10, "total_usage": 9.4}}, "OpenRouter credit is low: $0.60 left"),
    ({"data": {"total_credits": 10, "total_usage": 1.0}}, None),
])
def test_the_openrouter_balance_is_watched(store, monkeypatch, credits, said):
    from agent.attention import fetch
    from agent.attention.sources import health

    monkeypatch.setattr(fetch, "get_json", lambda url, params=None, headers=None: credits)
    for check in ("_decisions", "_module_sandbox", "_docker", "_disk", "_failing_jobs"):
        monkeypatch.setattr(health, check, lambda conn: None)

    found = health.health(store)

    assert [o.title for o in found] == ([said] if said else [])
    if said:
        assert found[0].key.startswith("health:openrouter:") and found[0].source == "health"


def test_a_condition_is_said_once_a_day_and_a_broken_check_skips_itself(store, monkeypatch):
    from agent.attention.sources import health

    def broken(conn):
        raise OSError("no")

    monkeypatch.setattr(health, "_openrouter", broken)
    monkeypatch.setattr(health, "_decisions", lambda conn: None)
    monkeypatch.setattr(health, "_module_sandbox", lambda conn: None)
    monkeypatch.setattr(health, "_docker", lambda conn: ("docker", "Docker is not running", ""))
    monkeypatch.setattr(health, "_disk", lambda conn: None)
    monkeypatch.setattr(health, "_failing_jobs", lambda conn: None)

    once, twice = health.health(store), health.health(store)

    assert [o.key for o in once] == [o.key for o in twice]
    assert once[0].title == "Docker is not running"


def test_failing_jobs_are_said_with_each_kinds_count_and_latest_error(store):
    """The kinds alone left the user asking which jobs had failed and why,
    and Steve, with no tool to read the queue, unable to say (22 October
    2026). The latest error is what answers both."""
    from agent import jobs
    from agent.attention.sources import health

    def failed(kind: str, error: str) -> None:
        jobs.fail(store, jobs.enqueue(store, kind=kind, prompt="1"), error, retry=False)

    failed("reflect", "ModuleNotFoundError: an older error")
    failed("reflect", "ModuleNotFoundError: No module named 'presidio_analyzer'")
    assert health._failing_jobs(store) is None, "two is below the day's threshold"
    failed("build", "TimeoutError: " + "x" * 400)

    _, title, detail = health._failing_jobs(store)

    assert title == "3 background jobs failed in the last day"
    assert detail.startswith("2 reflect, the last with: ModuleNotFoundError: No module named "
                             "'presidio_analyzer'; 1 build, the last with: TimeoutError: ")
    assert "an older error" not in detail
    assert len(detail) < 400, "an error is cut to its head"


def _missed(store, table: str, error: str, ago: str = "-5 minutes") -> None:
    """One ask of Jev, at the site that keeps `table`, with no answer."""
    at = f"datetime('now', '{ago}')"
    store.execute({
        "screened": "INSERT INTO screened (source, source_id, origin, content_sha, error, at)"
                    f" VALUES ('gmail', 'm1', 'mail', hex(randomblob(8)), ?, {at})",
        "critic_reviews": "INSERT INTO critic_reviews (channel, tool, gate, decision, mode,"
                          f" error, at) VALUES ('job', 'send_email', 'card', 'card', 'enforce',"
                          f" ?, {at})",
        "browser_judgements": "INSERT INTO browser_judgements (site, label, error, at)"
                              f" VALUES ('contoso', 'Delete', ?, {at})",
        "crews": f"INSERT INTO crews (offered, jev_error, at) VALUES (0, ?, {at})",
    }[table], (error,))
    store.commit()


def test_jev_not_answering_at_any_of_its_sites_is_said_with_what_that_means(store):
    """Each site falls back safely on its own, so without this an outage is
    silence: mail read from extracts and actions held, and no word of why."""
    from agent.attention.sources import health

    for table in ("screened", "critic_reviews", "browser_judgements"):
        _missed(store, table, "RuntimeError: HTTP 503")
    _missed(store, "crews", "Jev is not answering; asked again in 28s", "-1 minutes")

    name, title, detail = health._decisions(store)

    assert (name, title) == ("decisions", "Jev, the decision model, is not answering")
    assert detail.startswith("4 of its answers were missing in the last hour")
    assert "Jev is not answering; asked again in 28s" in detail
    assert detail.endswith(health.WITHOUT_JEV)


@pytest.mark.parametrize("missed", [
    [("screened", "RuntimeError: HTTP 503")] * 2,
    [("critic_reviews", "a spending cap is reached: background, $2.00 today")] * 3,
    [("screened", "RuntimeError: HTTP 503", "-2 hours")] * 3,
], ids=["two, a slow reply", "a cap, not Jev", "over an hour ago"])
def test_a_slow_reply_a_spending_cap_or_an_old_outage_is_not_jev_down(store, missed):
    from agent.attention.sources import health

    for one in missed:
        _missed(store, *one)

    assert health._decisions(store) is None


def test_an_answer_jev_gave_is_never_counted_as_missed(store):
    """A screened item Jev answered for keeps an error only when one of its
    parts went unread; a critic's or a click's verdict, none."""
    from agent.attention.sources import health

    for _ in range(3):
        store.execute("INSERT INTO screened (source, source_id, origin, content_sha, verdict,"
                      " classifier, error) VALUES ('gmail', 'm1', 'mail', hex(randomblob(8)),"
                      " 'as_is', 'jev', '1 of 2 parts unread: no answer in 3s')")
        store.execute("INSERT INTO browser_judgements (site, label, p) VALUES ('c', 'Open', 0.1)")
    store.commit()

    assert health._decisions(store) is None


def test_jev_with_no_key_is_said_by_the_health_source_even_when_nothing_was_asked(
        store, monkeypatch):
    from agent.attention.sources import health
    from core import secrets

    monkeypatch.setattr(secrets, "get", lambda name: None)

    [said] = [o for o in health.health(store) if o.key.startswith("health:decisions")]

    assert said.key.startswith("health:decisions:nokey:")
    assert said.title == "Jev, the decision model, has no key"
    assert "OpenRouter key" in said.detail and said.detail.endswith(health.WITHOUT_JEV)


def test_it_runs_with_the_other_sources_and_the_judge_knows_it():
    from agent.attention import settings, triage
    from agent.attention.sources import all_sources

    assert "health" in [s.name for s in all_sources()]
    assert settings.CADENCE_S["health"] >= 30 * 60
    assert "**Health** items" in triage.judge_prompt({"health"})


# --------------------------------------------------------------------------
# A worker that died
# --------------------------------------------------------------------------

# A note as the supervisor writes one (core/supervisor._fault): the lines
# before the fault, its header, and the stack of the thread that faulted.
FAULTED_TAIL = (
    "2026-09-30 00:07:31,310 INFO httpx2: HTTP Request: POST https://api.openai.com/v1\n"
    "Windows fatal exception: access violation\n\n"
    "Current thread 0x000062dc (most recent call first):\n"
    r'  File "C:\Python313\Lib\ssl.py", line 1140 in read' "\n"
    r'  File "C:\Steve\.venv\Lib\site-packages\httpx2\_api.py", line 303 in post' "\n"
    r'  File "C:\Steve\agent\attention\jev.py", line 89 in _post' "\n"
    r'  File "C:\Steve\agent\attention\jev.py", line 127 in ask' "\n"
    r'  File "C:\Python313\Lib\threading.py", line 1015 in _bootstrap' "\n"
)


def _notes(path, *notes):
    path.write_text("".join(json.dumps(n) + "\n" for n in notes), encoding="utf-8")
    return path


def test_each_death_the_supervisor_noted_becomes_an_issue_once(store, tmp_path):
    """Two crashes in the same place are one issue seen twice; a worker that
    stopped answering is another. The notes are consumed."""
    notes = _notes(tmp_path / "worker_deaths.jsonl",
                   {"at": "2026-09-29 12:00:59", "worker": "w3-120059", "how": "gone",
                    "exit": "0xC0000005", "tail": FAULTED_TAIL},
                   {"at": "2026-09-30 09:10:00", "worker": "w3-120059", "how": "gone",
                    "exit": "0xC0000005", "tail": FAULTED_TAIL},
                   {"at": "2026-09-30 10:00:00", "worker": "w4-100000", "how": "unresponsive",
                    "exit": None, "tail": "INFO worker: took up duties\n"})

    assert issues.record_deaths(store, notes) == 3

    rows = {r["summary"]: r for r in _rows(store, "worker_crashed")}
    crash = rows["The worker (w3-120059) crashed: an access violation, "
                 "in read (ssl.py:1140) from _post (jev.py:89)"]
    assert crash["count"] == 2
    assert "post (_api.py:303) < _post (jev.py:89) < ask (jev.py:127)" in crash["excerpt"]
    assert "The worker (w4-100000) stopped answering its health checks" in rows
    assert not notes.exists() and not list(tmp_path.glob("*.taking"))


def test_a_death_before_faulthandler_was_on_is_named_by_its_exit_code(store, tmp_path):
    notes = _notes(tmp_path / "deaths.jsonl", {"worker": "w1-090000", "how": "gone",
                                                "exit": "0xC0000374", "tail": "INFO ...\n"})

    issues.record_deaths(store, notes)

    assert _rows(store, "worker_crashed")[0]["summary"] == (
        "The worker (w1-090000) crashed: heap corruption")


def test_a_crash_inside_the_repos_own_code_is_named_once(store, tmp_path):
    """No "x from x" when the frame that faulted is already ours."""
    tail = ("Current thread 0x1 (most recent call first):\n"
            r'  File "C:\Steve\agent\tools\outlook.py", line 40 in _walk' "\n")
    issues.record_deaths(store, _notes(tmp_path / "d.jsonl", {"worker": "w2-090000", "how": "gone",
                                                              "exit": "0xC0000005", "tail": tail}))

    assert _rows(store, "worker_crashed")[0]["summary"] == (
        "The worker (w2-090000) crashed: an access violation, in _walk (outlook.py:40)")


def test_with_no_deaths_there_is_nothing_to_record(store, tmp_path):
    assert issues.record_deaths(store, tmp_path / "none.jsonl") == 0


async def test_a_worker_taking_up_its_duties_records_the_deaths_before_it(store, monkeypatch):
    from agent.worker import background, duties, links

    recorded = []

    async def idle():
        pass

    for module, names in ((links, ("warm_voice",)),
                          (background, ("build_sandbox", "build_browser", "reembed",
                                        "reflection_sweep",
                                        "night_loop", "attention_loop", "briefing_loop",
                                        "reminder_loop", "action_sweep", "resend_loop",
                                        "wake_loop",
                                        "account_health_loop"))):
        for name in names:
            monkeypatch.setattr(module, name, idle)
    monkeypatch.setattr(runtime, "conn", store)
    monkeypatch.setattr(runtime, "jobs", type("J", (), {"start": lambda self: None})())
    monkeypatch.setattr(issues, "record_deaths", lambda conn: recorded.append(conn) or 1)
    monkeypatch.setattr(duties, "_duties", {"state": "standby"})
    monkeypatch.setattr(duties, "_duty_tasks", [])

    duties.take_up_duties()

    assert recorded == [store]


def test_the_morning_counts_crashes(store):
    issues.record(store, "worker_crashed", "access violation", "The worker crashed")

    assert issues.briefing_line(store) == "noticed about itself: 1 crash"
