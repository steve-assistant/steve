"""The desk's rehearsal rig, in its parts that need no model and no time.

The rig (devtools/rehearse/) runs the real desk in a room that is software,
so the sound tests the user runs by ear stop being the first place a voice bug
is found (10 October 2026: "could you create some sort of sandbox where you
can test speaking and playback without running stuff through speakers?").
Its runs load Whisper and Kokoro and take minutes, so they are not part of
this suite; what is checked here is what its verdicts rest on -- the
scenario language, the room's arithmetic, what counts as two voices at
once -- and that it can never reach a real microphone or speaker.
"""

from __future__ import annotations

import sys

import numpy as np
import pytest

import tests.gate.voice_extra  # noqa: F401 - skips this module without the voice extra
from devtools.rehearse import __main__ as cli
from devtools.rehearse import keys, script
from devtools.rehearse import rig as rigs
from devtools.rehearse import room as rooms
from devtools.rehearse import speaker as speakers

STOP = """\
scenario stop-long-answer
about "hey Steve, stop" over a long answer
length 45
reply /history of contoso/ 3.0 research: Contoso was founded in 1802,
  and moved to Pittsburgh in 1868.
0.5      user: Hey Steve, tell me about the history of Contoso.
voice+6  user!: Hey Steve, stop.   # the line the checks measure from
expect cut-in <= 1.0
expect no-live
"""


# ---------------------------------------------------------------------------
# The scenario language
# ---------------------------------------------------------------------------


def test_a_scenario_reads_as_their_sound_test_is_described():
    scenario = script.parse(STOP)

    assert (scenario.name, scenario.length) == ("stop-long-answer", 45.0)
    [reply] = scenario.replies
    assert reply.tool == "research" and reply.text.endswith("moved to Pittsburgh in 1868.")
    first, stop = scenario.lines
    assert first.when == script.When(None, 0.5) and not first.mark
    assert stop.when == script.When("voice", 6.0) and stop.mark
    assert stop.text == "Hey Steve, stop.", "a comment is not part of what she says"
    assert [(e.check, e.args) for e in scenario.expects] == [
        ("cut-in", ("<=", 1.0)), ("no-live", ())]


@pytest.mark.parametrize(("text", "says"), [
    ("scenario x\nwhen+3 user: hi", "no anchor"),
    ("scenario x\n1 stranger: hi", "who is one of"),
    ("scenario x\nexpect cut-in 1.0", "wants <op> <number>"),
    ("scenario x\nexpect telepathy", "no check"),
    ("scenario x\nroom loudness 3", "no room knob"),
    ("about nothing", "needs a name"),
    ("scenario x\n1 user!: a\n2 user!: b", "only one line"),
])
def test_a_script_that_cannot_be_rehearsed_is_refused_naming_its_line(text, says):
    """A mistyped script that ran anyway would pass or fail on something
    nobody wrote."""
    with pytest.raises(script.ScriptError, match=says):
        script.parse(text)


PUSHES = sorted(name for name, s in cli.available().items() if keys.pushes(s))


@pytest.mark.parametrize("name", PUSHES)
async def test_every_push_to_talk_scenario_passes_on_core_with_openai_faked(name, tmp_path):
    """Push-to-talk is the turn every v0.1 user takes, and the room rig
    cannot rehearse it: its scenarios run here, through the worker's own
    socket as the app sends the keys, with OpenAI faked at its two calls
    and nothing else loaded."""
    result = await keys.rehearse(cli.available()[name], tmp=tmp_path, pace=0.25)

    assert result.passed, keys.report(result)


def test_push_to_talk_is_rehearsed_from_the_press_to_the_reply_cut_by_a_press():
    """`ptt-press-while-writing` is finding 1 of the review of steps 7 to 9:
    a release while the last reply was still being written left the
    microphone open. Losing the scenario would lose its guard."""
    assert {"ptt-ask-and-answer", "ptt-first-word-at-press", "ptt-press-over-a-reply",
            "ptt-empty-press", "ptt-press-while-writing"} <= set(PUSHES)


@pytest.mark.parametrize(("text", "says"), [
    ("scenario x\n1 key: hold", "the key is press or release"),
])
def test_a_key_that_is_neither_pressed_nor_let_go_is_refused(text, says):
    with pytest.raises(script.ScriptError, match=says):
        script.parse(text)


async def test_a_push_to_talk_scenario_with_a_hands_free_check_is_refused(tmp_path):
    """The room's checks read a room this rehearsal has none of: run anyway,
    they would fail, or pass, on nothing."""
    scenario = script.parse("scenario x\n0.5 key: press\nexpect cut-in <= 1.0")

    with pytest.raises(ValueError, match="push-to-talk has no check"):
        await keys.rehearse(scenario, tmp=tmp_path)


def test_every_shipped_scenario_parses_and_names_are_unique():
    found = cli.available()

    assert {"stop-long-answer", "stop-inside-live", "quiet-closes-live", "talk-live",
            "weather-after-tick", "video-says-hey-steve"} <= set(found)
    assert all(s.expects for s in found.values()), "a scenario that checks nothing passes"


def test_a_value_that_never_came_fails_its_check():
    """No cut-in measured means the desk never stopped: that is not a pass."""
    assert script.compare(None, "<=", 1.0) is False
    assert script.compare(0.4, "<=", 1.0) and not script.compare(1.4, "<=", 1.0)


def test_an_unknown_scenario_is_refused_and_all_means_every_one():
    found = cli.available()

    assert cli.chosen(["all"], found) == list(found.values())
    with pytest.raises(SystemExit, match="no scenario"):
        cli.chosen(["stop-everything"], found)


def test_a_live_run_says_its_most_before_it_starts_and_each_scenario_is_capped():
    long = script.parse("scenario long\nlength 600\n1 user: Hey Steve, let's talk live.")
    short = script.parse("scenario short\nlength 30\n1 user: Hey Steve, let's talk live.")

    capped = rigs.LIVE_CAP_S / 60 * rigs.LIVE_USD_PER_MIN
    assert cli.live_cost([long]) == round(capped, 2)
    assert cli.live_cost([short]) == round(0.5 * rigs.LIVE_USD_PER_MIN, 2)


# ---------------------------------------------------------------------------
# The room
# ---------------------------------------------------------------------------


def test_the_echo_arrives_late_by_the_rooms_delay():
    delay = rooms.Delay(160)  # 10 ms at 16 kHz
    click = np.zeros(400, np.float32)
    click[0] = 1.0

    out = np.concatenate([delay.push(click[:200]), delay.push(click[200:])])

    assert int(np.argmax(out)) == 160


def test_a_small_speaker_squashes_loud_sound_and_leaves_quiet_sound_alone():
    quiet, loud = np.full(4, 0.01, np.float32), np.full(4, 0.9, np.float32)

    assert np.allclose(rooms.soft_clip(quiet, 3.0), quiet, rtol=1e-3)
    assert rooms.soft_clip(loud, 3.0).max() < 1 / 3 + 1e-6
    assert np.array_equal(rooms.soft_clip(loud, 0.0), loud), "drive 0 is a clean speaker"


def test_levels_in_db_are_what_they_say():
    tone = np.sin(np.linspace(0, 200 * np.pi, 16_000)).astype(np.float32)

    assert rooms.dbfs(tone * rooms.gain(-20.0)) == pytest.approx(-23.0, abs=0.1)


# ---------------------------------------------------------------------------
# What it calls two voices at once
# ---------------------------------------------------------------------------


class Turn:
    pass


class LiveCall:
    pass


def test_a_live_voice_cutting_straight_into_the_desks_is_two_voices_at_once():
    """9 October 2026: the local voice said their week, then GPT-Live said it
    again over the end of it."""
    turn, call = Turn(), LiveCall()
    played = [(0.0, turn, 0.1, 0.1), (0.1, turn, 0.1, 0.1), (0.25, call, 0.1, 0.1)]

    runs = speakers.audible_runs(played, floor=0.003)

    assert [speakers.kind(r["owner"]) for r in runs] == ["turn", "livecall"]
    assert speakers.voices_at_once(runs)


def test_one_voice_after_another_with_a_pause_and_a_tick_between_is_not():
    runs = [{"owner": "turn", "start": 0.0, "end": 2.0, "peak": 0.1},
            {"owner": "tick", "start": 2.05, "end": 2.1, "peak": 0.1},
            {"owner": "livecall", "start": 3.0, "end": 5.0, "peak": 0.1}]

    assert speakers.voices_at_once(runs) == []


def test_silence_below_the_floor_is_not_a_voice():
    owner = Turn()

    assert speakers.audible_runs([(0.0, owner, 0.0005, 1.0)], floor=0.003) == []


# ---------------------------------------------------------------------------
# How long it took to stop
# ---------------------------------------------------------------------------


def _stop_record(played, *, start=10.0, wake_end=10.6):
    line = script.Line(when=script.When(None, 10.0), who="user", text="Hey Steve, stop.",
                       mark=True)
    record = rigs.Record(scenario=script.Scenario(name="s", lines=[line]))
    record.lines = [rigs.Placed(line=line, start=start, end=start + 1.2, voice="am_michael",
                                wake_end=wake_end)]
    record.played = played
    return record


def _blocks(owner, start, end, step=0.1):
    return [(start + i * step, owner, 0.1, step) for i in range(int(round((end - start) / step)))]


def test_a_stop_that_was_never_heard_is_measured_to_where_the_answer_ended():
    """The rig's first run: the answer ran on 30 seconds past "hey Steve,
    stop", and the end of the sentence it was said over read as a cut-in of
    1.35 s -- a pass for a stop nobody heard."""
    turn = Turn()
    played = _blocks(turn, 8.0, 11.0) + _blocks(turn, 11.4, 40.0)  # a pause, then on

    assert rigs.cut_in(_stop_record(played)) == pytest.approx(40.0 - 10.6, abs=0.15)


def test_a_stop_that_was_heard_is_measured_to_the_silence():
    turn = Turn()

    assert rigs.cut_in(_stop_record(_blocks(turn, 8.0, 11.0))) == pytest.approx(0.4, abs=0.15)


def test_a_live_voice_that_yielded_as_the_user_spoke_was_still_spoken_over():
    """GPT-Live stops when it hears speech: by the end of "hey Steve" it may
    already be quiet. That is a stop in no time, not "never"."""
    call = LiveCall()

    assert rigs.cut_in(_stop_record(_blocks(call, 6.0, 10.3))) == 0.0


def test_a_voice_quiet_just_before_the_user_spoke_is_stopped_unless_it_carries_on():
    """In a loud room the fake GPT-Live stopped for its own echo before the user
    said a word: nothing left to cut. A local answer that only paused there,
    and went on, was not stopped at all."""
    call, turn = LiveCall(), Turn()

    assert rigs.cut_in(_stop_record(_blocks(call, 6.0, 9.0))) == 0.0
    assert rigs.cut_in(_stop_record(_blocks(turn, 6.0, 9.0) + _blocks(turn, 11.0, 30.0))) \
        == pytest.approx(30.0 - 10.6, abs=0.15)
    assert rigs.cut_in(_stop_record(_blocks(turn, 2.0, 5.0))) is None, "spoke over nothing"


# ---------------------------------------------------------------------------
# Never a real device
# ---------------------------------------------------------------------------


def test_the_rig_cannot_open_a_real_microphone_or_speaker_and_puts_everything_back():
    """It runs beside their live agent, often unattended: a rehearsal that
    reached the real speakers would play to their room, one that reached the
    microphone would hear it."""
    import sounddevice as sd

    before_stream, before_card = sd.OutputStream, sys.modules.get("soundcard")
    patches = rigs.Patches()
    rigs.no_devices(patches)
    try:
        with pytest.raises(RuntimeError, match="never opens a real"):
            sd.OutputStream(samplerate=16_000, channels=1)
        with pytest.raises(RuntimeError, match="never opens a real"):
            sd.InputStream(samplerate=16_000, channels=1)
        import soundcard

        with pytest.raises(RuntimeError, match="never opens a real"):
            soundcard.default_microphone()
        with pytest.raises(RuntimeError, match="never opens a real"):
            soundcard.default_speaker()
    finally:
        patches.undo()
        if before_card is None:
            sys.modules.pop("soundcard", None)
        else:
            sys.modules["soundcard"] = before_card
    assert sd.OutputStream is before_stream


def test_the_person_at_the_desk_is_the_user_not_him():
    """The product assumes no one's gender (docs/plan/02-depersonalisation.md):
    the scenarios, written for one person's desk, called the speaker "him".
    A script still written that way is refused, not read as someone else."""
    with pytest.raises(script.ScriptError, match="who is one of"):
        script.parse("scenario x\n1 him: Hey Steve.")
    assert {line.who for s in cli.available().values() for line in s.lines} <= {
        "user", "other", "video", "key"}


def test_the_fake_live_call_answers_the_prompts_the_desk_really_sends():
    """The fake matches the desk's prompts by their words. When the desk's
    nudge stopped calling the user "him", the fake still waited for "his last
    message", so a rehearsal of a silent opener heard nothing and blamed the
    product. It must answer the very text voice/desk_call.py sends."""
    from devtools.rehearse import fakes
    from voice import desk_call

    live = fakes.Live(lambda text: np.zeros(1, np.float32))
    heard: list[str] = []
    live._say_later = lambda text, delay: heard.append(text)
    live._obey(desk_call.nudge(desk_call.GREET))
    live._obey(desk_call.nudge("anything else"))
    live._obey(desk_call.WRAP_UP)
    assert heard == [fakes.GREETING, fakes.FILLER, fakes.GOODBYE]
