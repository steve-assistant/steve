"""The desk, run whole against a room, a laptop and GPT-Live (tests/unit/desk_world.py).

The user's two sound tests of 9 October 2026 are replayed here as the user lived
them, and what went wrong in each is a scenario of its own:

- the local voice and GPT-Live both said the opening;
- a live conversation hung up nine seconds in, as the user finished their first
  sentence, and their words were lost;
- GPT-Live's stream of silent frames held a call open for minutes;
- "hey Steve, stop" over a long answer opened a live conversation;
- inside a live conversation, "hey Steve" could not end it.

Every test drives the real engine, ear, floor, routing and live client;
only the microphone, the speakers, Whisper, their assistant and GPT-Live are
fakes, and the fakes behave as the real ones did that day.
"""

from __future__ import annotations

import collections
import random
import re

import pytest

import tests.gate.voice_extra  # noqa: F401 - skips this module without the voice extra
from agent import approvals, runtime
from core import db
from tests.unit.desk_world import Live as Live_
from tests.unit.desk_world import Reply, Say, Video, World, run
from voice import engine as engine_mod

WEATHER = Reply(r"weather", "Pittsburgh today is 45 to 68 degrees and clear, with no rain.")
PITTSBURGH = Reply(r"history of pittsburgh", " ".join(
    f"Part {n} of the story of Pittsburgh goes on at some length about the town." for n in
    range(1, 13)), delay=3.0, tool="research")
CONTOSO = Reply(r"history of contoso", " ".join(
    f"Chapter {n} of Contoso's history is long and full of detail about the college." for n in
    range(1, 13)), delay=3.0, tool="research")
WEEK = Reply(r"talk through my week", "Let's start with what's coming up: do you want to go "
                                      "day by day, or focus on events outside class?")
DAY_BY_DAY = Reply(r"day by day", "Today you have the ropes course. Monday is the case team "
                                  "kickoff.", delay=2.0, tool="calendar_agenda")
CALENDAR = Reply(r"coming up|my week|calendar", "Monday is the case team kickoff, and Thursday "
                                                "has two networking events.", delay=2.0,
                 tool="calendar_agenda")
THERE = Reply(r"are you there", "Yes, I'm here. What would you like to look at first?",
              delay=1.0)
GO_THROUGH = Reply(r"go through it", "Outside class: the ropes course today, the kickoff on "
                                     "Monday, and two events on Thursday.", delay=2.0)


@pytest.fixture
def store(tmp_path, monkeypatch):
    from agent.worker import desk as handler

    conn = db.open_store(tmp_path / "desk.db")
    monkeypatch.setattr(runtime, "conn", conn)
    monkeypatch.setattr(approvals, "_notify", None)
    monkeypatch.setattr(handler, "state_file", lambda: tmp_path / "listening.json")
    yield conn
    conn.close()


def scenario(world: World, monkeypatch, store, until: float) -> World:
    async def main():
        await world.build(monkeypatch, store)
        await world.run_until(until)
        return world

    return run(main)


def _local_sentences(world: World) -> list[str]:
    return [text for owner, text, _, _ in world.floor._said
            if type(owner).__name__ == "Turn"]


def _norm(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9']+", text.lower()))


def _said_twice(world: World) -> list[str]:
    """Sentences the desk said more often than their assistant wrote them,
    whichever voice said them -- the local voice's sentences and a live
    conversation's transcript, joined into its lines."""
    said = [_norm(s) for s in _local_sentences(world)]
    for live in world.lives:
        said += [_norm(s) for s in re.split(r"(?<=[.?!])\s+", "".join(live.said)) if s.strip()]
    counts = collections.Counter(s for s in said if len(s.split()) >= 4)
    written = [_norm(r) for r in world.replied]
    return [s for s, n in counts.items() if n > max(1, sum(s in r for r in written))]


def _live_ends(world: World) -> list[tuple[float, str]]:
    """When each live conversation was ended, and why: the moment the desk
    decided it, and the reason it recorded."""
    return [(when, why) for when, why, _ in world.live_ends]


# ---------------------------------------------------------------------------
# The two sound tests of 9 October 2026, replayed
# ---------------------------------------------------------------------------


def test_the_morning_sound_test_replayed_opens_nothing_on_a_stop_and_closes_its_live_call(
        tmp_path, monkeypatch, store):
    """08:03-08:11. The weather; the history of Pittsburgh, 82 seconds of it;
    "hey Steve, stop" over it, which Whisper heard as "I'll talk." and which
    opened a live conversation that never closed. Now: the stop stops the
    answer and opens nothing; a live conversation opens only when the user asks
    for one by name, and closes when it goes quiet."""
    world = World(
        says=[Say(1, "Hey Steve, what's the weather?", wake=True),
              Say(11, "can you tell me about the history of Pittsburgh?"),
              Say(28, "Hey Steve, stop", wake=True, heard_as="Hey Steve. I'll talk."),
              Say(45, "Hey Steve, let's talk live about what I have coming up this week",
                  wake=True)],
        replies=[WEATHER, PITTSBURGH, CALENDAR])
    scenario(world, monkeypatch, store, until=110)

    assert [r for r in world.asked if "history" in r.lower()], "the follow-up reached her assistant"
    # The stop: nothing of the long answer after it, and no live call before
    # the user asked for one by name.
    assert world.sounded("Turn", 28.9, 30.0) == 0 or "I'll talk" in " ".join(world.asked)
    history_said = [s for s in _local_sentences(world) if "Pittsburgh goes on" in s]
    assert 0 < len(history_said) < 12, "the answer was cut, not finished nor restarted"
    assert len(world.lives) == 1 and world.live_opened >= 45, "only her own words opened one"
    # The live conversation answered them, then went quiet, and closed.
    assert any("coming up" in r for r in world.asked)
    ends = _live_ends(world)
    assert ends and ends[0][1] in ("quiet", "silent"), ends
    last_voice = max(at - world.t0 for at, owner, rms in world.player.played
                     if type(owner).__name__ == "LiveCall" and rms >= 0.003)
    assert ends[0][0] - last_voice <= engine_mod.IDLE_S + 1.0, "silent frames held it open"
    assert not _said_twice(world)
    assert world.engine.mode == "idle"


def test_the_afternoon_sound_test_replayed_keeps_one_voice_and_loses_nothing(tmp_path,
                                                                             monkeypatch, store):
    """15:25-15:33. The history of Contoso, stopped; "talk through my week",
    which opened GPT-Live after the local voice had said the opening; their
    answer, lost when it hung up; two "hey Steve"s that left no trace; "go
    through it", said twice. Now, as the user chose: the back-and-forth is the
    local voice's -- their answer is a follow-up, answered -- and the two
    missed wakes are two lines in the log."""
    world = World(
        says=[Say(1, "Hey Steve", wake=True, secs=0.9),
              Say(4, "Can you tell me about the history of Contoso?"),
              Say(17, "Hey Steve, stop", wake=True, heard_as="I'll top"),
              Say(25, "Hey Steve, talk through my week.", wake=True),
              Say(37, "Let's go day by day, but you can skip classes and assignments. "
                      "Look at my email too, for context.", secs=6),
              Say(60, "Hey Steve", near=True, secs=0.9),
              Say(70, "Hey Steve", near=True, secs=0.9),
              Say(80, "Hey Steve, are you there?", wake=True),
              Say(88, "Yeah, go through it.")],
        replies=[CONTOSO, WEEK, DAY_BY_DAY, THERE, GO_THROUGH])
    scenario(world, monkeypatch, store, until=125)

    assert not world.lives, "no live conversation: she never asked for one by name"
    assert any("day by day" in r for r in world.asked), "her answer reached her assistant"
    assert any("go through it" in r.lower() for r in world.asked)
    contoso = [s for s in _local_sentences(world) if "Contoso's history" in s]
    assert 0 < len(contoso) < 12
    assert world.ear.counts.near_wakes == 2, "two missed wakes, each a line in the log"
    assert not _said_twice(world)
    assert world.engine.mode == "idle"


# ---------------------------------------------------------------------------
# Each of the day's failures, on its own
# ---------------------------------------------------------------------------


def test_an_opening_their_assistant_said_is_never_said_again_by_the_live_voice(
        tmp_path, monkeypatch, store):
    """15:26 and 15:29: their assistant spoke its reply and asked for a live
    conversation with the same words as its opening; GPT-Live was told to
    say them, and did. Under decision A -- their assistant may open one -- the
    reply goes to GPT-Live as history, and nothing tells it to say it."""
    monkeypatch.setattr(engine_mod, "LIVE_OPENED_BY", "user and assistant")
    opening = ("Let's start with what's coming up: do you want to go day by day, or focus on "
               "events outside class?")
    world = World(
        says=[Say(1, "Hey Steve, talk through my week.", wake=True),
              Say(14, "Let's go day by day, look at my email too, for context.", secs=7)],
        replies=[Reply(r"talk through my week", opening, live=True), DAY_BY_DAY],
        live_behaviour={"first_response_s": 6.0})
    scenario(world, monkeypatch, store, until=70)

    assert len(world.lives) == 1
    live = world.lives[0]
    start = next(e for e in live.sent if e["type"] == "session.start")["session"]
    assert any(i["role"] == "assistant" and "day by day" in i["content"][0]["text"]
               for i in start["input"]), "the reply it said is the live voice's history"
    told = " ".join([start["instructions"]] + [e.get("content", "") for e in live.sent])
    assert "Say now" not in told and "Open by saying" not in told
    assert "day by day, or focus" not in "".join(live.said), "the live voice said it again"
    assert not _said_twice(world)
    assert any("day by day, look at my email" in r for r in world.asked)


def test_a_live_call_the_user_talks_in_first_is_not_hung_up_on_and_user_words_are_answered(
        tmp_path, monkeypatch, store):
    """15:27:04: the user answered at once and for eight seconds; GPT-Live waited
    for them to finish, and the phone's watchdog hung up at nine, their words
    lost. Now a call the user is talking in is never given up on for silence, and
    a slow first answer (six seconds after the user stops) still comes."""
    world = World(
        says=[Say(1, "Hey Steve, let's talk live", wake=True, secs=1.5),
              Say(4, "Let's go day by day, but you can skip classes, and look at my email "
                     "too for context.", secs=8)],
        replies=[DAY_BY_DAY], live_behaviour={"first_response_s": 6.0})
    scenario(world, monkeypatch, store, until=60)

    assert any("day by day" in r for r in world.asked), "her words were handed over"
    ends = _live_ends(world)
    assert ends and ends[0][0] > 4 + 8 + 6, f"it hung up before it could answer: {ends}"


def test_a_live_call_that_never_answers_hands_user_words_to_the_local_voice(
        tmp_path, monkeypatch, store):
    """However a live conversation ends, what the user said in it that nobody
    answered is answered by the local voice."""
    world = World(
        says=[Say(1, "Hey Steve, let's talk live", wake=True, secs=1.5),
              Say(4, "Let's go day by day and look at my email too.", secs=4)],
        replies=[DAY_BY_DAY], live_behaviour={"first_response_s": None})
    scenario(world, monkeypatch, store, until=60)

    assert any("day by day" in r for r in world.asked), "her words were lost"
    ends = _live_ends(world)
    assert ends and ends[0][1] in ("quiet", "silent")
    assert world.sounded("Turn", ends[0][0], 60) > 0, "the local voice answered them"


@pytest.mark.parametrize("noise", [0.0, 0.001])
def test_a_live_voice_streaming_silent_frames_does_not_hold_a_call_open(
        tmp_path, monkeypatch, store, noise):
    """GPT-Live sends a frame every 100 ms, silence included -- zeros, or a
    faint noise floor. On 9 October 2026 every frame was taken for speech,
    and a call held 150 s of nothing open. Silent frames are not its voice."""
    world = World(says=[Say(1, "Hey Steve, let's talk live about my week", wake=True)],
                  replies=[CALENDAR], live_behaviour={"noise": noise})
    scenario(world, monkeypatch, store, until=90)

    ends = _live_ends(world)
    assert ends and ends[0][1] == "quiet", ends
    last_voice = max(at - world.t0 for at, owner, rms in world.player.played
                     if type(owner).__name__ == "LiveCall" and rms >= 0.003)
    assert ends[0][0] - last_voice <= engine_mod.IDLE_S + 1.0
    assert world.engine.mode == "idle"


def test_hey_steve_inside_a_live_call_ends_it_at_once(tmp_path, monkeypatch, store):
    """08:06: "hey Steve, stop" inside the call only paused it for a second,
    and it ran on until hands-free was switched off. The phrase reaches the
    desk in every state, and in a live conversation it ends it."""
    long = Reply(r"coming up", " ".join(f"Item {n} on your calendar is worth a mention." for n
                                         in range(1, 15)), delay=1.0)
    world = World(says=[Say(1, "Hey Steve, let's talk live about what's coming up", wake=True),
                        Say(14, "Hey Steve, stop", wake=True)],
                  replies=[long])
    scenario(world, monkeypatch, store, until=40)

    ends = _live_ends(world)
    assert ends and ends[0][1] == "wake" and ends[0][0] <= 14.9, ends
    assert world.sounded("LiveCall", 15.0, 40) == 0, "its voice went on after the phrase"
    assert "cut_in_stop" in world.turn_lines
    assert world.engine.mode == "idle"


def test_hey_steve_while_a_live_call_is_connecting_ends_it_and_is_answered(
        tmp_path, monkeypatch, store):
    """A live conversation ended before its connection was up is closed then
    and there, not carried: there was no socket yet to close, and the call
    would have waited on it for ever, the desk stuck in it."""
    world = World(says=[Say(1, "Hey Steve, let's talk live", wake=True, secs=1.5),
                        Say(3.5, "Hey Steve, what's the weather?", wake=True)],
                  replies=[WEATHER], connect_delay=5.0)
    scenario(world, monkeypatch, store, until=40)

    assert _live_ends(world) and _live_ends(world)[0][1] == "wake"
    assert any("weather" in r for r in world.asked)
    assert world.engine.call is None and world.engine.mode == "idle"


@pytest.mark.parametrize("heard_as", ["Hey Steve. I'll talk.", "I'll top", "Hey Steve, stop."])
def test_hey_steve_stop_over_a_long_answer_stops_it_and_opens_nothing(
        tmp_path, monkeypatch, store, heard_as):
    """However Whisper hears "hey Steve, stop" mixed with the desk's own
    voice, the answer stops at the phrase, is not restarted, and no live
    conversation opens."""
    world = World(says=[Say(1, "Hey Steve, can you tell me about the history of Pittsburgh?",
                            wake=True),
                        Say(12, "Hey Steve, stop", wake=True, heard_as=heard_as)],
                  replies=[PITTSBURGH])
    scenario(world, monkeypatch, store, until=40)

    said = [s for s in _local_sentences(world) if "Pittsburgh goes on" in s]
    assert 0 < len(said) < 12 and not _said_twice(world)
    assert not world.lives
    assert sum("history" in r for r in world.asked) == 1, "the answer was asked for again"


def test_the_desks_own_words_heard_back_over_its_voice_are_not_a_request(
        tmp_path, monkeypatch, store):
    """A cut-in whose words are the desk's own sentence, come back through a
    canceller that leaked: dropped, not answered (core/desk.own_words_removed)."""
    world = World(says=[Say(1, "Hey Steve, can you tell me about the history of Pittsburgh?",
                            wake=True),
                        Say(12, "Hey Steve", wake=True, heard_as="Hey Steve, Part 1 of the "
                                "story of Pittsburgh goes on")],
                  replies=[PITTSBURGH])
    scenario(world, monkeypatch, store, until=30)

    assert sum("history" in r.lower() or "story" in r.lower() for r in world.asked) == 1
    assert "own_words" in world.turn_lines


@pytest.mark.parametrize("asked, reply", [
    ("Hey Steve, take notes on this lecture", None),
    # Not caught by code: their assistant starts them (`take_notes`).
    ("Hey Steve, could you write down what we say in here",
     Reply(r"write down", "Starting notes of the meeting now.", notes="Meeting")),
])
def test_once_notes_of_the_room_start_what_is_said_goes_to_the_note_and_no_model(
        tmp_path, monkeypatch, store, asked, reply):
    """Found in the port of the desk's tests, 9 October 2026: notes of the
    room started from a turn, and the turn then opened its follow-up window,
    so the lecturer's next sentence, said within ten seconds, became a
    request to their assistant. A lecture never reaches a model: with a note
    open there is no follow-up window, and what the room says is the note's."""
    from agent import notes

    monkeypatch.setattr(notes, "notes_dir", lambda: tmp_path / "notes")
    world = World(says=[Say(1, asked, wake=True),
                        Say(9, "and so the supply chain grew all through the nineties", secs=3),
                        Say(15, "which brings us to the second case for today", secs=3)],
                  replies=[reply] if reply else [])
    scenario(world, monkeypatch, store, until=35)

    assert len(world.asked) == (1 if reply else 0), world.asked
    assert world.engine.notes_on and world.engine.mode == "idle"
    note = notes.open_note(store)
    assert note is not None and note["kind"] == "lecture"
    assert "supply chain" in notes.transcript(store, int(note["id"]))
    from agent.worker import desk as handler

    if handler._state.get("watch") is not None:
        handler._state["watch"].cancel()


def test_a_video_saying_the_phrase_wakes_nothing(tmp_path, monkeypatch, store):
    world = World(videos=[Video(2, 4, wake=True)])
    scenario(world, monkeypatch, store, until=12)

    assert world.ear.counts.laptop_wakes_ignored == 1 and not world.asked
    assert world.engine.mode == "idle"


def test_every_wake_is_heard_with_a_tick(tmp_path, monkeypatch, store):
    """The maintainer, 9 October 2026: the user could not tell whether a "hey Steve" had
    been heard. Each counted wake plays a soft tick at once."""
    world = World(says=[Say(1, "Hey Steve, what's the weather?", wake=True)],
                  replies=[WEATHER])
    scenario(world, monkeypatch, store, until=4)

    ticks = [at - world.t0 for at, owner, rms in world.player.played if owner == "tick"]
    assert ticks and 1.5 <= ticks[0] <= 1.9


# ---------------------------------------------------------------------------
# The retest of 9 October 2026, 17:25-17:31, on the rebuilt desk
# ---------------------------------------------------------------------------


def _finished(audio):
    """The end-of-turn judge on "hey Steve" alone, as it was at 17:26:08:
    a phrase that sounds finished (0.99)."""
    return 0.99


def test_hey_steve_a_pause_for_the_tick_then_the_question_is_answered(
        tmp_path, monkeypatch, store):
    """17:26:08: "hey Steve", a pause for the tick, then "what's the
    weather?". The judge closed the capture 0.48 s after the wake --
    nothing had been said after it, and the phrase alone sounds finished --
    Whisper, cold, took 2.4 s over it, and the question, asked meanwhile,
    was heard by nothing; the follow-up window then closed empty. A wake
    now waits for their request to begin."""
    world = World(says=[Say(1, "Hey Steve", wake=True, secs=0.7),
                        Say(2.8, "what's the weather?", secs=1.2)],
                  replies=[WEATHER], stt_s=(2.4, 0.45), judge=_finished)
    scenario(world, monkeypatch, store, until=25)

    assert any("weather" in r for r in world.asked), world.turn_lines
    assert "phrase_alone" not in world.turn_lines, "the question was its own capture"
    assert world.engine.mode == "idle"


def test_a_question_begun_while_a_bare_hey_steve_is_checked_is_held_and_answered(
        tmp_path, monkeypatch, store):
    """The same, with a longer pause and a slower Whisper: the wake's
    capture has ended with nothing in it, and Whisper is still on the phrase
    when the user asks. What the user says is gathered from its first word, held until
    Whisper has heard "hey Steve", then answered -- and the "go on" chime
    does not play over them."""
    world = World(says=[Say(1, "Hey Steve", wake=True, secs=0.7),
                        Say(5.0, "what's the weather going to be like this afternoon in "
                                 "Pittsburgh, and should I take a coat?", secs=4.5)],
                  replies=[WEATHER], stt_s=(4.0, 0.45), judge=_finished)
    scenario(world, monkeypatch, store, until=25)

    assert "phrase_alone" in world.turn_lines
    assert any("weather" in r for r in world.asked), world.turn_lines
    follow = [start for start, _, kind in world.captures.values() if kind == "follow"]
    assert follow and follow[0] <= 5.0, "its first words were not gathered"
    # Whisper's verdict on the phrase comes at 8.6 s, as the user is still talking.
    assert world.sounded("Turn", 5.0, 9.5) == 0, "the chime talked over her"


@pytest.mark.parametrize("secs", [2.0, 4.5])
def test_what_follows_a_wake_whisper_does_not_confirm_reaches_nothing(
        tmp_path, monkeypatch, store, secs):
    """The other side of holding what the user says while a bare wake is checked:
    if Whisper does not hear the phrase, the wake was false -- somebody
    else in the room -- and what was said after it is dropped unread,
    whether it was finished before the verdict (held) or after (still
    being gathered). A capture is handed on without the phrase only in a
    window a confirmed wake opened."""
    world = World(says=[Say(1, "Hey Steph", wake=True, secs=0.7, heard_as="Hey Steph."),
                        Say(5.0, "send the slides to everyone on the list", secs=secs)],
                  replies=[WEATHER], stt_s=(2.4, 0.45), judge=_finished)
    scenario(world, monkeypatch, store, until=25)

    assert world.asked == []
    assert world.transcribed == ["Hey Steph."], "Whisper heard what followed a false wake"
    assert world.engine.dropped_unread == 1 and world.engine.mode == "idle"


def test_a_speaker_stream_that_falls_behind_mid_reply_loses_none_of_it(
        tmp_path, monkeypatch, store):
    """17:27:14: the history of Contoso. The user heard "Contoso began in...", about
    three seconds of glitches, then "that's the short version": 36.7 s of
    reply synthesised, a few seconds heard. The speaker stream went more
    than a second without a callback, and the floor's watchdog took that
    for a stopped stream and dropped everything queued -- twice in 250 ms.
    A stream that is late is left to catch up, and the reply plays whole."""
    world = World(says=[Say(1, "Hey Steve, can you tell me about the history of Contoso?",
                            wake=True)],
                  replies=[CONTOSO], stalls=[(12.0, 1.5)])
    scenario(world, monkeypatch, store, until=100)

    queued = sum(end - start for owner, _, start, end in world.floor._said
                 if type(owner).__name__ == "Turn")
    assert queued > 50, "the reply was not all written"
    assert world.sounded("Turn", 0, 100) >= queued - 0.5, "some of the reply never played"
    assert world.floor.late == 1 and world.floor.reopened == 0


def test_hey_steve_stop_ducked_by_the_canceller_under_its_voice_still_stops_it(
        tmp_path, monkeypatch, store):
    """17:27-17:29: "hey Steve, stop" over its own answers was missed three
    times. The echo canceller ducks their voice under the desk's (by 8 dB and
    more on laptop speakers, measured on its AEC3), so the phrase scores
    under the usual bar. While the desk's voice is audible the bar is lower
    (voice/listen.WAKE_OVER_VOICE), and the stop is heard."""
    world = World(says=[Say(1, "Hey Steve, can you tell me about the history of Contoso?",
                            wake=True),
                        Say(14, "Hey Steve, stop", muffled=True, heard_as="Hey Steve, stop.")],
                  replies=[CONTOSO])
    scenario(world, monkeypatch, store, until=40)

    assert "cut_in_stop" in world.turn_lines, world.turn_lines
    contoso = [s for s in _local_sentences(world) if "Contoso's history" in s]
    assert 0 < len(contoso) < 12
    assert world.sounded("Turn", 15.5, 40) == 0, "it talked on"


def test_the_same_muffled_phrase_with_the_desk_quiet_wakes_nothing(tmp_path, monkeypatch, store):
    """The lower bar is for over its own voice only: in quiet, a "hey
    Steve" that scores as low is a near miss, logged, and nothing more."""
    world = World(says=[Say(2, "Hey Steve, what's the weather?", muffled=True)],
                  replies=[WEATHER])
    scenario(world, monkeypatch, store, until=15)

    assert world.asked == [] and world.engine.counts["wakes"] == 0
    assert world.ear.counts.near_wakes == 1


def test_lets_talk_live_said_as_a_follow_up_is_heard_at_once_and_answered_by_its_voice(
        tmp_path, monkeypatch, store):
    """17:29:22: after an answer, "let's talk live", with no wake word. The
    live conversation opened, and GPT-Live -- told only to answer their last
    message, which was "let's talk live" -- sent eight seconds of silent
    frames, a nudge included (closed=1 appended=2 delta=76, audible_s=0).
    The user heard nothing, and said "hey Steve" again. Now a chime says at once
    that the user was heard, and it is told to say it is listening."""
    world = World(says=[Say(1, "Hey Steve, what's the weather?", wake=True),
                        Say(12, "let's talk live", secs=1.0)],
                  replies=[WEATHER], connect_delay=1.4)
    scenario(world, monkeypatch, store, until=40)

    assert len(world.lives) == 1 and "live_asked" in world.turn_lines
    chime = [at - world.t0 for at, owner, rms in world.player.played
             if type(owner).__name__ == "LiveCall" and rms >= 0.003]
    # As soon as the request is decided -- before the connection is made.
    assert chime and chime[0] < world.live_opened, "nothing said she had been heard"
    voiced = [at - world.t0 for at, owner, rms in world.player.played
              if type(owner).__name__ == "LiveCall" and rms >= 0.003
              and at - world.t0 > world.live_opened]
    assert voiced and voiced[0] <= 13.0 + 4.0, "its voice was not heard within 4 s of asking"
    assert "listening" in "".join(world.lives[0].said)
    assert all(why != "never spoke" for _, why in _live_ends(world))


def test_lets_talk_live_again_while_the_last_one_closes_opens_one_that_runs(
        tmp_path, monkeypatch, store):
    """17:29:32-17:29:57: "hey Steve" ended a live conversation, and "let's
    talk live" asked for another while the first was still closing. The
    first's end set the reason on the engine -- after the second had
    cleared it -- so the second took itself to have been ended by a wake
    while it connected, abandoned itself two seconds in, and left the desk
    in "live" with no conversation, deaf to all but the phrase, for 23 s.
    Each conversation's end is its own now."""
    world = World(says=[Say(1, "Hey Steve, let's talk live", wake=True, secs=1.5),
                        Say(9, "Hey Steve, let's talk live", wake=True, secs=1.5)],
                  connect_delay=1.4, live_behaviour={"close_delay": 2.5})
    scenario(world, monkeypatch, store, until=60)

    assert len(world.lives) == 2
    ends = _live_ends(world)
    assert ends[0][1] == "wake" and all(why != "wake" for _, why in ends[1:]), ends
    second = [at - world.t0 for at, owner, rms in world.player.played
              if owner is not None and type(owner).__name__ == "LiveCall" and rms >= 0.003
              and at - world.t0 > 13.5]
    assert second, "the second conversation never spoke"
    stuck = [h for h in world.engine.history if h["to"] == "live"]
    assert world.engine.counts["stuck"] == 0 and stuck
    assert world.engine.mode == "idle"


def test_a_live_voice_that_misses_its_opener_is_nudged_within_four_seconds_of_their_asking(
        tmp_path, monkeypatch, store):
    """If it lets the first instruction pass, it is told again, firmly, four
    seconds after the user asked (it was six; at 17:29:30 the user had all but given up)."""
    first = {"seen": False}
    obey = Live_._obey

    def obey_after_the_first(self, text):
        if "Say now" in (text or "") and not first["seen"]:
            first["seen"] = True
            return
        obey(self, text)

    monkeypatch.setattr(Live_, "_obey", obey_after_the_first)
    world = World(says=[Say(1, "Hey Steve, let's talk live", wake=True, secs=1.5)],
                  connect_delay=1.4)
    scenario(world, monkeypatch, store, until=30)

    voiced = [at - world.t0 for at, owner, rms in world.player.played
              if type(owner).__name__ == "LiveCall" and rms >= 0.003
              and at - world.t0 > world.live_opened]
    asked = world.live_opened - 1.4  # the request was decided as it began connecting
    # Nudged at 4 s, and it speaks a moment later.
    assert voiced and voiced[0] <= asked + 4.0 + 1.5, voiced[:1]
    assert first["seen"] and all(why != "never spoke" for _, why in _live_ends(world))


def test_the_hey_steve_that_ends_a_live_call_is_not_left_as_their_unanswered_words(
        tmp_path, monkeypatch, store):
    """17:29:32: the "hey Steve" that ended a live conversation was cut into
    the stretch of their speech before it, transcribed, and -- the call ended
    by a wake -- written into the desk's thread as words of their it had left
    unanswered ("unanswered=yes"). The phrase, and what follows it, are the
    wake's own capture's."""
    world = World(says=[Say(1, "Hey Steve, let's talk live about what's coming up", wake=True),
                        Say(14, "Hey Steve, stop", wake=True)],
                  replies=[CALENDAR])
    scenario(world, monkeypatch, store, until=40)

    assert world.live_ends and world.live_ends[0][1] == "wake"
    assert world.live_ends[0][2] == "", world.live_ends


def test_a_live_voice_that_never_speaks_is_said_to_have_failed(tmp_path, monkeypatch, store):
    """Asked for by name with nothing more, and it never says a word: the
    local voice says so, rather than leave them in a silence the user cannot tell
    from not having been heard."""
    world = World(says=[Say(1, "Hey Steve, let's talk live", wake=True, secs=1.5)],
                  live_behaviour={"first_response_s": None})
    monkeypatch.setattr(Live_, "_obey", lambda self, text: None)
    scenario(world, monkeypatch, store, until=30)

    assert _live_ends(world)[0][1] == "never spoke"
    assert any("live voice" in s for s in _local_sentences(world))


# ---------------------------------------------------------------------------
# Properties, over many rooms
# ---------------------------------------------------------------------------


def _random_room(seed: int) -> World:
    rng = random.Random(seed)
    says, t = [], 1.0
    requests = ["what's the weather", "can you tell me about the history of Contoso",
                "talk through my week", "are you there", "what's on today"]
    while t < 70:
        kind = rng.random()
        text = rng.choice(requests)
        if kind < 0.5:
            says.append(Say(t, f"Hey Steve, {text}?", wake=True))
        elif kind < 0.65:
            says.append(Say(t, "Hey Steve, stop", wake=True,
                            heard_as=rng.choice(["Hey Steve, stop.", "I'll talk.", "stop"])))
        elif kind < 0.8:
            says.append(Say(t, f"{text}?"))  # a follow-up, or talk to someone else
        else:
            says.append(Say(t, "Hey Steve", near=True, secs=0.9))
        t = says[-1].end + rng.uniform(1.5, 12.0)
    videos = [Video(rng.uniform(5, 60), rng.uniform(2, 8), wake=rng.random() < 0.3)
              for _ in range(rng.randint(0, 2))]
    return World(says=says, videos=videos, replies=[WEATHER, CONTOSO, WEEK, THERE],
                 residual=rng.choice([0.0, 0.0, 0.1]))


@pytest.mark.parametrize("seed", range(12))
def test_in_any_room_the_desk_keeps_one_voice_answers_each_wake_and_comes_back_to_rest(
        tmp_path, monkeypatch, store, seed):
    world = _random_room(seed)
    # Long enough after their last word for the longest answer (about a
    # minute) and the follow-up window to finish.
    scenario(world, monkeypatch, store, until=170)

    assert not _said_twice(world), "a sentence was said twice"
    assert len(world.turn_lines) >= world.engine.counts["wakes"], "a wake left no turn"
    assert world.engine.counts["stuck"] == 0
    assert world.engine.mode == "idle", "it did not come back to waiting for the phrase"
    assert not world.lives, "a live conversation opened that she never asked for by name"
    handed = sum(1 for e in world.events if e in ("captured", "utterance", "segment"))
    assert len(world.transcribed) == handed - world.engine.dropped_unread, \
        "Whisper heard something the ear never handed on, or a capture was lost unlogged"
