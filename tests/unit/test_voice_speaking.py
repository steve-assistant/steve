"""The desk speaks a reply as it is written, says a word about the lookup it is
doing, and keeps only what the user heard when the user cuts in (voice/speaking.py).

From how voice assistants are built (7 October 2026): stream the reply into
speech, preface a slow lookup with a few specific words, and truncate an
interrupted reply to what was played.
"""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

import tests.gate.voice_extra  # noqa: F401 - skips this module without the voice extra
from voice import pipeline, speaking
from voice.stt import Transcript
from voice.tts import split_for_streaming

# ---------------------------------------------------------------------------
# The pieces
# ---------------------------------------------------------------------------

REPLY = ("Around 6:30 tonight it'll be about 60 degrees and clear. A light long-sleeve "
         "top should do for a run. Bring a thin layer if you cool down fast after.")


def test_streamed_text_is_cut_where_a_whole_reply_would_be():
    cut = speaking.Sentences()
    out = []
    for i in range(0, len(REPLY), 7):  # in pieces, as a model writes
        out += cut.feed(REPLY[i:i + 7])
    out.append(cut.rest())
    assert out == split_for_streaming(REPLY)


@pytest.mark.parametrize(("tool", "line"), [
    ("calendar_agenda", "Checking your calendar."), ("search_email", "Looking through your mail."),
    ("weather", "Checking the forecast."), ("research", "Looking that up."),
    ("create_event", None), ("draft_email", None),
])
def test_the_preamble_is_about_the_lookup_and_none_for_an_action(tool, line):
    assert speaking.preamble_for(tool) == line


@pytest.mark.parametrize(("sentence", "claims"), [
    ("I've added the review session to your calendar for Friday.", True),
    ("Reminder set for 7:15 tomorrow.", True),
    ("Want me to add it to your calendar?", False),
    ("It's 60 degrees and clear.", False),
])
def test_a_sentence_that_says_something_was_done_is_known(sentence, claims):
    assert speaking.claims_an_effect(sentence) is claims


def test_what_is_left_to_say_follows_what_was_said():
    spoken = ["Around 6:30 tonight it'll be about 60 degrees and clear."]
    assert speaking.rest_of(REPLY, spoken).startswith("A light long-sleeve top")
    assert speaking.rest_of(REPLY, split_for_streaming(REPLY)) == ""


def test_a_reply_the_check_changed_says_only_what_was_not_said():
    spoken = ["Around 6:30 tonight it'll be about 60 degrees and clear."]
    final = "I haven't added it yet. Around 6:30 tonight it'll be about 60 degrees and clear."
    assert speaking.rest_of(final, spoken) == "I haven't added it yet."


def test_what_the_user_heard_is_what_played_before_the_user_cut_in():
    said = [(None, 0.8), ("First sentence.", 2.0), ("Second sentence.", 2.0),
            ("Third sentence.", 2.0)]
    assert speaking.heard(said, unplayed_s=3.5) == "First sentence."
    assert speaking.heard(said, unplayed_s=2.5) == "First sentence. Second sentence."
    assert speaking.heard(said, unplayed_s=0.0) == (
        "First sentence. Second sentence. Third sentence.")


# ---------------------------------------------------------------------------
# A turn
# ---------------------------------------------------------------------------


class _Speaker:
    def __init__(self):
        self.said: list[str] = []

    def say(self, text):
        self.said.append(text)
        return type("S", (), {"audio": np.zeros(10, np.float32), "sample_rate": 24_000,
                              "duration_s": 1.0})()


class _Player:
    def __init__(self):
        self.lines = 0
        self.queued_seconds = 0.0

    def enqueue(self, audio, rate):
        if audio[0] == np.float32(0.25):
            self.lines += 1

    def wait(self, *a):
        return True

    def stop(self):
        return True


def _session(reply_fn, *, streams=True):
    session = object.__new__(pipeline.VoiceSession)
    session.reply_fn, session.speak, session.streams = reply_fn, True, streams
    session.speaker, session.player = _Speaker(), _Player()
    session._listening = session._cut = False
    stall = (np.full(10, 0.25, np.float32), 24_000, 0.6)
    preamble = (np.full(10, 0.25, np.float32), 24_000, 0.7)
    session._stalls = [stall] * len(pipeline.STALLS)
    session._preambles = dict.fromkeys(speaking.PREAMBLE_LINES, preamble)
    session._last_stall = -1
    session.cut = []
    session.on_cut = session.cut.append
    return session


def _heard():
    return Transcript(text="what's the weather for a run tonight", language="en",
                      duration_s=1.0, transcribe_ms=1, device="cpu")


async def test_the_first_sentence_is_spoken_before_the_reply_is_finished():
    finish = asyncio.Event()
    holder: dict = {}

    async def reply(text, *, on_text=None, on_tool=None, on_restart=None):
        on_text("Around 6:30 tonight it'll be about 60 degrees and clear. ")
        await asyncio.sleep(0.05)
        holder["before_done"] = list(holder["session"].speaker.said)
        on_text("A light long-sleeve top should do for a run.")
        finish.set()
        return ("Around 6:30 tonight it'll be about 60 degrees and clear. "
                "A light long-sleeve top should do for a run."), {}

    session = _session(reply)
    holder["session"] = session
    await session.respond(_heard(), released=0.0)

    assert holder["before_done"] == ["Around 6:30 tonight it'll be about 60 degrees and clear."]
    assert session.speaker.said == ["Around 6:30 tonight it'll be about 60 degrees and clear.",
                                    "A light long-sleeve top should do for a run."]


async def test_a_claim_waits_for_the_checked_reply():
    """The check after a reply (agent/verify.py) may correct "I've added it";
    spoken first, the correction would contradict it."""
    async def reply(text, *, on_text=None, on_tool=None, on_restart=None):
        on_text("I've added the review session to your calendar for Friday. ")
        await asyncio.sleep(0.05)
        return "I haven't added it yet. Want me to put it on your calendar?", {}

    session = _session(reply)
    await session.respond(_heard(), released=0.0)

    assert session.speaker.said == split_for_streaming(
        "I haven't added it yet. Want me to put it on your calendar?")
    assert not any("I've added" in s for s in session.speaker.said)


async def test_a_lookup_is_announced_and_the_generic_line_is_not_added(monkeypatch):
    monkeypatch.setattr(pipeline, "STALL_S", 0.01)

    async def reply(text, *, on_text=None, on_tool=None, on_restart=None):
        on_tool("calendar_agenda")
        await asyncio.sleep(0.1)
        return "You have two meetings tomorrow, at ten and at two.", {}

    session = _session(reply)
    await session.respond(_heard(), released=0.0)

    assert session.player.lines == 1, "one line before the answer, not two"
    assert session._said[0] == (None, 0.7), "the calendar's line, not a generic one"


async def test_the_turns_timing_says_whether_a_preamble_or_a_stall_was_said(monkeypatch):
    """For the turn's log line (agent/worker/desk.py): with the stall at the
    model's latency floor, whether "On it." fired is what the sound test
    needs to know."""
    async def lookup(text, *, on_text=None, on_tool=None, on_restart=None):
        on_tool("calendar_agenda")
        await asyncio.sleep(0.1)
        return "You have two meetings tomorrow, at ten and at two.", {}

    async def slow(text, *, on_text=None, on_tool=None, on_restart=None):
        await asyncio.sleep(0.1)
        return "You have two meetings tomorrow, at ten and at two.", {}

    async def quick(text, *, on_text=None, on_tool=None, on_restart=None):
        return "You have two meetings tomorrow, at ten and at two.", {}

    monkeypatch.setattr(pipeline, "STALL_S", 0.03)
    asked = (await _session(lookup).respond(_heard(), released=0.0)).timing
    stalled = (await _session(slow).respond(_heard(), released=0.0)).timing
    plain = (await _session(quick).respond(_heard(), released=0.0)).timing

    assert (asked.preambled, asked.stalled) == (True, False)
    assert (stalled.preambled, stalled.stalled) == (False, True)
    assert (plain.preambled, plain.stalled) == (False, False)


async def test_cut_off_the_user_is_recorded_as_hearing_only_what_played():
    async def reply(text, *, on_text=None, on_tool=None, on_restart=None):
        return ("First she heard this sentence clearly. Then this one, which she cut "
                "off halfway through."), {}

    session = _session(reply, streams=False)
    real_speak = session._speak

    async def speak_then_cut(text, released, timing):
        cut = await real_speak(text, released, timing)
        session.player.queued_seconds = 1.0  # the second sentence, all of it, unplayed
        session.interrupt()
        return cut

    session._speak = speak_then_cut
    turn = await session.respond(_heard(), released=0.0)

    assert turn.interrupted
    assert session.cut == ["First she heard this sentence clearly."]


async def test_without_streaming_the_reply_is_spoken_whole():
    async def reply(text):
        return REPLY, {}

    session = _session(reply, streams=False)
    await session.respond(_heard(), released=0.0)
    assert session.speaker.said == split_for_streaming(REPLY)


# ---------------------------------------------------------------------------
# A cut-in, and a run that starts over (audit of 8 October 2026)
# ---------------------------------------------------------------------------


class _SlowSpeaker:
    """A voice whose second sentence takes as long as the test likes."""

    def __init__(self):
        import threading

        self.calls = 0
        self.second_started = threading.Event()
        self.release_second = threading.Event()

    def say(self, text):
        self.calls += 1
        if self.calls == 2:
            self.second_started.set()
            self.release_second.wait(5)
        return type("S", (), {"audio": np.zeros(10, np.float32), "sample_rate": 24_000,
                              "duration_s": 1.0})()


class _ClearingPlayer:
    """`Player.stop` as it is: the queue is cleared and nothing is flagged."""

    def __init__(self):
        self.queued, self.after_stop = 0, 0
        self.stopped = False
        self.queued_seconds = 0.0

    def enqueue(self, audio, rate):
        self.queued += 1
        if self.stopped:
            self.after_stop += 1

    def stop(self):
        self.queued, self.stopped = 0, True
        return True

    def wait(self, *a):
        return True


async def test_a_sentence_finished_across_the_cut_is_not_queued_after_it():
    """The audit's fake-player race: a later sentence is almost always being
    synthesised while an earlier one plays, so the barge-in cleared the queue
    and the sentence then arrived behind it."""
    session = _session(lambda text: None, streams=False)
    session.speaker, session.player = _SlowSpeaker(), _ClearingPlayer()
    speaking = asyncio.ensure_future(session._speak(
        "The first sentence is quite long enough. The second one is long enough as well.",
        0.0, pipeline.TurnTiming()))
    assert await asyncio.to_thread(session.speaker.second_started.wait, 5)

    session.interrupt()  # "hey Steve", while the second sentence is being made
    session.speaker.release_second.set()

    assert await asyncio.wait_for(speaking, 5) is True
    assert session.player.after_stop == 0 and session.player.queued == 0


@pytest.mark.parametrize(("cut_ago_s", "spoken"), [
    (0.0, False),  # "hey Steve" while their request was being transcribed: it stands
    (30.0, True),  # the cut of an earlier reply: over
])
async def test_a_hey_steve_over_the_transcription_is_not_forgotten(cut_ago_s, spoken):
    """`respond` cleared the cut at its start, so a barge-in in the ~0.3 s of
    Whisper was forgotten and the reply the user had talked over was spoken in
    full."""
    async def reply(text):
        return "Around 6:30 tonight it'll be about 60 degrees and clear.", {}

    session = _session(reply, streams=False)
    session.interrupt()
    session._cut_at -= cut_ago_s  # as if it had been that long ago
    heard = Transcript(text="what's the weather", language="en", duration_s=1.0,
                       transcribe_ms=300, device="cpu")

    turn = await session.respond(heard, released=0.0)

    assert bool(session.speaker.said) is spoken
    assert turn.interrupted is (not spoken)


async def test_a_run_that_starts_over_does_not_say_again_what_was_said():
    """`run_agent` retries a failed call and the escalation loop replays the
    turn on a stronger model: both stream again from the first word."""
    first = "Around 6:30 tonight it'll be about 60 degrees and clear."
    second = "A light long-sleeve top should do for a run."

    async def reply(text, *, on_text=None, on_tool=None, on_restart=None):
        on_text(first + " ")
        await asyncio.sleep(0.05)  # the first sentence is spoken
        on_restart()  # the call failed; the run begins again
        on_text(first + " ")
        on_text(second)
        return f"{first} {second}", {}

    session = _session(reply)
    await session.respond(_heard(), released=0.0)

    assert session.speaker.said == [first, second]


async def test_a_run_that_starts_over_before_anything_was_said_still_streams():
    first = "Around 6:30 tonight it'll be about 60 degrees and clear."
    during: list[list[str]] = []

    async def reply(text, *, on_text=None, on_tool=None, on_restart=None):
        on_text("Around 6:30 tonight")  # a partial sentence, from the failed run
        on_restart()
        on_text(first + " ")
        await asyncio.sleep(0.05)
        during.append(list(session.speaker.said))  # before the reply is finished
        return first, {}

    session = _session(reply)
    await session.respond(_heard(), released=0.0)

    assert during == [[first]], "held although nothing had been said, or the half sentence stayed"


async def test_streamed_sentences_lose_the_markers_the_final_reply_loses():
    """A streamed sentence is spoken before the final reply's cleanup (`clean`
    in `Conversation.send`): GPT's private citation markers -- on Signal in
    two replies in the two weeks to 5 October 2026 -- and the note a cut-off
    reply carries in the history were said aloud."""
    async def reply(text, *, on_text=None, on_tool=None, on_restart=None):
        on_text("It will be about 60 degrees and clear citeturn0search1. ")
        on_text("[the user cut in here; the rest was not said] Bring a thin layer for later. ")
        await asyncio.sleep(0.05)
        return "It will be about 60 degrees and clear. Bring a thin layer for later.", {}

    session = _session(reply)
    await session.respond(_heard(), released=0.0)

    assert session.speaker.said == ["It will be about 60 degrees and clear.",
                                    "Bring a thin layer for later."]
