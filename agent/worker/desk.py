"""Hands-free at the desk: the worker's side of "hey Steve".

The maintainer, 5 October 2026, wanted an always-on assistant at the laptop that they
can switch on and off, cheap, and without everything the user says going to a
model. The desk itself -- its states, who may speak, barge-in, follow-ups,
a live conversation's life -- is `voice/engine.py`; the ear is
`voice/listen.py`. This is what the engine calls on in the worker
(`DeskShell`), and the switch:

1. Whisper transcribes a capture, locally (`DeskShell.transcribe`).
2. `core/desk.wake_phrase` must find "hey Steve" at its start, or the wake
   was false and the capture is dropped -- unread by any model, the audio
   gone. The phrase alone gets a chime and a moment to ask.
3. Some things are answered by code, with no model (`DeskShell.decide`):
   "stop listening", "never mind", notes, and -- said over the desk's own
   voice -- any plain order to be quiet ("stop", "that's enough"), and the
   desk's own words heard back (`core/desk.own_words_removed`).
4. A yes to a card counts only with the laptop quiet
   (`core/desk.approval_counts`); otherwise the user is told to approve on screen.
5. "Let's talk live" opens a live conversation (voice/desk_call.py) --
   asked for by name, never because a reply asked them something: the
   follow-up window answers that, in the local voice, for nothing.
6. The rest is a turn of their own conversation on the desk's channel, `voice`
   -- the same thread, rights and approval intercept as the hotkey
   (`answering.voice_reply`), and the same local voice for the reply.

Only text is kept: the turn in the conversation, like a typed one. The
switch is `data/listening.json`, off until the user turns it on, and it is a
worker duty -- one worker holds the microphone through a swap.

Hands-free is not offered in v0.1 (`offered`). The app shows no control for
it, and the switch refuses unless a developer sets STEVE_HANDS_FREE=1.
Push-to-talk (`hotkey_turn`) needs none of it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import threading
import time
from pathlib import Path

from agent import runtime
from core import db, paths, settingsfile, spend
from core import desk as rules

log = logging.getLogger("desk")


def state_file() -> Path:
    return paths.data_dir() / "listening.json"


# Each change of the file's shape, oldest first (core/settingsfile.py).
STEPS = (settingsfile.stamped,)


_STOP = re.compile(r"^\W*(stop|quit) listening\b|^\W*(go to sleep|turn (yourself )?off)\W*$",
                   re.IGNORECASE)
_NEVERMIND = re.compile(r"^\W*(never ?mind|cancel|nothing|forget it|no thanks?)\W*$",
                        re.IGNORECASE)
# What "hey Steve, ..." means when it is said over the desk's own voice, and
# the whole of it is an order to be quiet. 9 October 2026, their first sound
# test: "hey Steve, stop" over an 82-second answer went to the model like any
# request and either restarted the answer from its first sentence or, once
# Whisper heard "I'll talk.", opened a billed live conversation. Said over
# the desk's voice it is code's to answer, and the answer is nothing: the
# wake has already stopped the desk, and a model asked "stop" has only
# things to say. The words are matched whole, after the polite words around
# them are taken off ("okay, stop it, thanks"), so "stop reading and tell me
# the time" still reaches the model. Not "yes" or "no": a card may be waiting.
_CUT_IN_STOPS = frozenset({
    "stop", "stop it", "stop that", "stop talking", "stop reading", "stop there",
    "stop right there", "quiet", "be quiet", "quiet down", "hush", "shush", "shh", "shhh",
    "shut up", "never mind", "nevermind", "forget it", "forget that", "cancel", "cancel that",
    "enough", "enough already", "thats enough", "that is enough", "thats all", "that is all",
    "thats it", "thats fine", "thats ok", "thats okay", "thats good", "thanks", "thank you",
    "thanks a lot", "thanks a bunch", "thanks so much", "thank you very much", "wait",
    "hold on", "hang on", "pause", "skip it", "skip that", "no thanks", "no thank you",
})
_CUT_IN_LEAD = re.compile(r"^(?:(?:okay|ok|alright|all right|yeah|yep|yes|please|just|um|uh|oh|"
                          r"well|so|hey|steve)\b\s*)+")
_CUT_IN_TRAIL = re.compile(r"(?:\s+(?:please|now|steve|okay|ok|thanks|thank you))+$")
# Sounds a person makes that are not words, and so not a request.
_FILLERS = frozenset({"uh", "um", "er", "erm", "ah", "oh", "mm", "mhm", "hm", "hmm", "huh"})
# A cut-in's request of this many words or fewer, with no speech after the
# wake (`Heard.speech_s`), is what the pre-roll left behind, not something the user
# said to the desk: the words are in the capture's first second and a half,
# mixed with the desk's own voice, which is what Whisper then guesses at.
CUT_IN_SHORT_WORDS = 2
CUT_IN_MIN_SPEECH_S = 0.15
# Notes (agent/notes.py), caught here so that taking one costs no model call.
_DICTATE = re.compile(r"^\W*(please\s+)?(take a note|start (a )?(note|dictation)|start dictating|"
                      r"dictate|start note[- ]?taking( what i say)?)\W*$", re.IGNORECASE)
# Asking for notes to start, in the words the user uses. It was "take notes", only:
# on 7 October 2026 "I'm going to need you to turn on the meeting
# transcription" missed, went to the model -- which had no way to start
# notes -- and it queued a change to its own code instead. Whatever these
# still miss, the model can now start notes itself (`take_notes`).
_STARTING = re.compile(
    r"\b(take|taking|start|begin|turn on|switch on|keep|do|record)\s+(\w+\s+){0,3}?"
    r"(notes?|minutes|transcription|transcript)\b|\btranscrib(e|ing)\b", re.IGNORECASE)
# "Did you take notes in the meeting?" asks about notes, and "stop
# transcribing the meeting" ends them; neither starts any.
_ASKING_ABOUT = re.compile(r"\b(did|have|has|were|was)\s+(you|i|we|it)\b|"
                           r"\b(stop|end|finish|turn off|switch off|pause)\b", re.IGNORECASE)
# Words that name notes without asking for them now: a reminder, a plan of
# their own, a time that is not this one, a question about how or whether.
# The audit of 8 October 2026 ran phrasings through `_asked`: "remind me to
# take notes on the lecture", "what's the best way to transcribe a zoom
# call", "will you take notes on the call tomorrow" and "I'm going to take
# notes on the lecture myself" all began recording -- the room, or what the
# laptop plays -- for up to three or four hours. They reach their assistant
# instead, which can start notes through `take_notes` if the user meant it.
# "can you ..." and "I'm going to need you to ..." are requests, and stay.
_NOT_NOW = re.compile(
    r"\bremind\w*\b"
    r"|\b(tomorrow|tonight|later|afterwards?|next\s+\w+|this\s+(morning|afternoon|evening)|"
    r"(on|every)\s+(mon|tues|wednes|thurs|fri|satur|sun)\w*|at\s+\d|\d\s?[ap]\.?m\b|"
    r"in\s+(an?|\d+|a\s+few|\w+)\s+(hours?|minutes?|days?)|whenever|if)\b"
    r"|\b(i'?ll|i\s+will|we'?ll|i'?m\s+going\s+to|i\s+am\s+going\s+to|we'?re\s+going\s+to|"
    r"gonna)\s+(\w+\s+)?(take|taking|start|begin|keep|do|record|transcribe)\b"
    r"|\b(myself|my\s+own|by\s+hand)\b"
    r"|^\W*(how|what|which|why|where|who|when)\b"
    r"|\b(best|better|easiest|good)\s+(way|app|tool)\b|\bis\s+there\s+a\s+way\b"
    r"|\b(do|does|can|could|is|are|will|would|should)\s+(it|that|this|i|zoom|teams)\b",
    re.IGNORECASE)
# Whatever way the user asks for notes of the room, their words name them -- the one
# thing `room_refusal` asks of the turn's words before the model may open their
# microphone on the room.
_NOTES_WORDS = re.compile(r"\b(notes?|minutes|transcri\w*|record\w*|dictat\w*|capture|jot\w*|"
                          r"summar\w*|write\W+(\w+\W+){0,3}down)\b", re.IGNORECASE)
_LECTURE = re.compile(r"\b(?P<what>lecture|class|meeting|talk|seminar|presentation|"
                      r"discussion)\b", re.IGNORECASE)
# What the laptop plays: heard on its way out (voice/listen.Tap), not through
# the microphone. Checked before _LECTURE; "meeting" goes here too when the
# laptop is playing speech as the user asks.
_CALL = re.compile(r"\b(?P<what>zoom|call|video|youtube|webinar|teams|meet|podcast|recording|"
                   r"stream|what'?s playing|the laptop)\b", re.IGNORECASE)
_QUICK = re.compile(r"^\W*(please\s+)?(make a note|take a note|note|jot down|write down)"
                    r"(\s+that)?\s*[:,-]?\s+(?P<text>\S.*)$", re.IGNORECASE)
_STOP_NOTES = re.compile(r"\b(stop|end|finish|turn off|switch off)\s+(\w+\s+){0,3}?(notes?|"
                         r"minutes|dictation|dictating|transcri(bing|ption|pt))\b", re.IGNORECASE)
_END_DICTATION = re.compile(r"[\s,.]*\b(end|stop|finish)\s+(the\s+)?(note|dictation|dictating)"
                            r"\W*$", re.IGNORECASE)

# A live conversation (voice/desk_call.py), asked for by name: the user's
# choice, 9 October 2026 -- "let's talk live", and the few ways of saying it
# that cannot mean anything else. Never a bare "let's talk" or "talk me
# through my week": those are a conversation the local voice carries, in
# the follow-up window, for nothing.
_LIVE = re.compile(
    r"\b(?:(?:let'?s|let us|can we|could we|i want to|i'?d like to)\s+)?"
    r"(?:talk|chat|speak|go)\s+live\b"
    r"|\bswitch(?:ing)?\s+(?:over\s+)?to\s+live\b"
    r"|\blive\s+(?:conversation|mode|chat)\b",
    re.IGNORECASE)
# GPT-Live minutes a day at the desk ($0.05 a minute): the DESK_LIVE_MIN
# knob, applied by `control.apply_knobs`.
LIVE_DAY_MIN = 30
# How often a note being taken is checked for its limits.
WATCH_S = 30.0
# How often a live conversation's row is brought up to date while it runs.
LIVE_BEAT_S = 30.0

_state: dict = {"ears": None, "listener": None, "engine": None, "ticker": None, "error": None,
                "last": None, "false_wakes": 0, "dropped_follow_ups": 0, "echo": None,
                "last_call": None, "last_switch": None, "warming": None}
_segment_lock: asyncio.Lock | None = None


def _words(request: str) -> list[str]:
    """The request's words, lowercase, with no punctuation or apostrophes."""
    return re.findall(r"[a-z]+", re.sub(r"['’]", "", request.lower()))


def cut_in_stop(request: str) -> bool:
    """Whether a cut-in's whole request is an order to be quiet (`_CUT_IN_STOPS`),
    however politely it is put."""
    words = re.sub(r"\b(\w+)(?:\s+\1\b)+", r"\1", " ".join(_words(request)))  # "stop, stop"
    words = _CUT_IN_LEAD.sub("", words)
    return words in _CUT_IN_STOPS or _CUT_IN_TRAIL.sub("", words) in _CUT_IN_STOPS


def cut_in_noise(request: str, heard) -> bool:
    """Whether what a cut-in's capture came to is not a request: no word in it
    but a "hmm", or a word or two with nothing said after the wake -- what the
    pre-roll's mix of their phrase and the desk's voice leaves for Whisper to guess
    at (`CUT_IN_SHORT_WORDS`)."""
    real = [word for word in _words(request) if word not in _FILLERS]
    return not real or (len(real) <= CUT_IN_SHORT_WORDS and heard.speech_s < CUT_IN_MIN_SPEECH_S)


def live_asked(request: str) -> str | None:
    """What the user wants to talk about, if they asked for a live conversation by
    name ("let's talk live about my week": "about my week"); "" for just
    asking; None if the user did not."""
    match = _LIVE.search(request or "")
    if match is None:
        return None
    rest = (request[:match.start()] + " " + request[match.end():]).strip()
    rest = _TOPIC_LEAD.sub("", rest)
    return rest.strip(" ,.!?;:-")


# Words before a topic that are not the topic: "okay, so, let's talk live,
# actually about my week" is about "about my week".
_TOPIC_LEAD = re.compile(r"^\W*(?:(?:okay|ok|so|please|now|then|and|actually|um|uh|er|"
                         r"well|alright|all right|right|hey|just|like|maybe)\b\W*)*",
                         re.IGNORECASE)


def _asked(kind: re.Pattern, request: str) -> re.Match | None:
    """`kind`'s word in a request to start taking notes, or None."""
    if (not _STARTING.search(request) or _ASKING_ABOUT.search(request)
            or _NOT_NOW.search(request)):
        return None
    return kind.search(request)


# ---------------------------------------------------------------------------
# The switch
# ---------------------------------------------------------------------------


# Hands-free is not offered in v0.1 (docs/plan/07-voice.md, item 15). On the
# maintainer's recordings its front end cut 24 of 60 requests early, and no
# wake check fits a CPU, so the app shows no switch for it. Hiding the
# switch alone was not enough: a `listening.json` left on from a developer's
# run, or a POST to /voice/listen, would still open the microphone. Hands-free
# stays reachable for the step that rebuilds it, behind this setting, which
# no screen shows.
OFFERED_ENV = "STEVE_HANDS_FREE"


class NotOffered(RuntimeError):
    """Hands-free asked for where it is not offered."""


NOT_OFFERED = ("Hands-free listening is not offered in this version: hold the push-to-talk "
               "key to talk.")


def offered() -> bool:
    """Whether hands-free may be switched on at all: a developer's setting."""
    return os.environ.get("STEVE_HANDS_FREE", "").strip() == "1"


def switched_on() -> bool:
    if not offered():
        return False
    try:
        return bool((settingsfile.load(state_file(), STEPS) or {}).get("on"))
    except (OSError, ValueError):
        return False


async def switch(on: bool | None, *, by: str) -> dict:
    """Turn hands-free listening on or off -- or, with `on` None, the other
    way from how it is, decided here: the app's hotkey toggled from its own
    copy of the state, which can be stale (9 October 2026: no switch reached
    the worker when Alt+Home was pressed). Remembered across restarts.
    Switching it on raises `NotOffered` unless a developer offered it."""
    if on is None:
        on = not switched_on()
    if on and not offered():
        log.info("hands-free refused: not offered (%s)", by)
        raise NotOffered(NOT_OFFERED)
    settingsfile.save(state_file(), {"on": bool(on), "by": by, "at": time.time()}, STEPS)
    _state["last_switch"] = {"on": bool(on), "by": by, "at": time.time()}
    if on:
        await start()
    else:
        stop()
        eng = _state.get("engine")
        if eng is not None and runtime.voice is not None:
            from voice.engine import OFF

            eng.play("chime", *OFF)
    log.info("hands-free listening %s (%s)", "on" if on else "off", by)
    return status()


def engine():
    """The desk's engine (voice/engine.py), made once the speech models are
    warm; None until then. It runs whether or not hands-free is on: a
    push-to-talk turn goes through it too, so it owns the speakers."""
    eng = _state.get("engine")
    if eng is None and runtime.voice is not None:
        from voice.engine import Engine
        from voice.floor import Floor

        eng = Engine(DeskShell(runtime.voice), Floor(runtime.voice.player))
        _state["engine"] = eng
        with contextlib.suppress(RuntimeError):  # no loop: a CLI or a test ticks by hand
            _state["ticker"] = asyncio.get_running_loop().create_task(eng.run())
    return eng


async def start() -> None:
    """Open the ear, if it is switched on and the speech models are warm.
    The models load in a thread: never on the event loop."""
    if _state["ears"] is not None or not switched_on() or runtime.voice is None:
        return
    from voice import wake

    head = wake.model_dir() / wake.HEAD_FILE
    if not head.exists():
        _state["error"] = f"no wake model at {head}: run python -m voice.wake_train"
        return
    eng = engine()
    loop = asyncio.get_running_loop()
    try:
        ear, ears = await asyncio.to_thread(_build_ear, eng, loop)
    except Exception as exc:  # noqa: BLE001 - no ear is a state, not a crash
        _state["error"] = f"{type(exc).__name__}: {exc}"[:300]
        log.exception("could not open the desk's ear")
        return
    if _state["ears"] is not None or not switched_on():  # switched again meanwhile
        ears.stop()
        return
    _state.update(ears=ears, listener=ear, error=None)
    eng.attach(ear)
    ears.start()
    _state["warming"] = loop.create_task(_warm_whisper())
    # A dictation or a lecture under way when the last worker gave up the
    # microphone carries on here: the note is open in the database.
    from agent import notes

    if (note := notes.open_note(runtime.conn)) is not None and note["kind"] != "call":
        eng.transcribe(True)
        _watch(int(note["id"]))


async def _warm_whisper() -> None:
    """Whisper run once as hands-free opens, in a thread: the first "hey
    Steve" of the 9 October 2026 retest took 2,433 ms to transcribe, cold,
    and what the user asked meanwhile was lost. A GPU left idle a while is slow
    to its first run too, so this is done at every switch-on, not only at
    load (voice/stt.Transcriber.warm)."""
    transcriber = getattr(runtime.voice, "transcriber", None)
    warm = getattr(transcriber, "warm", None)
    if warm is None:
        return
    try:
        took = await asyncio.to_thread(warm)
    except Exception:  # noqa: BLE001 - a cold first turn is slow, not broken
        log.exception("desk: could not warm Whisper")
        return
    log.info("desk: whisper warmed in %d ms", took)


def _build_ear(eng, loop: asyncio.AbstractEventLoop):
    """The ear and its hardware, loaded off the event loop. What it hears it
    hands to the engine on the loop."""
    from agent.worker import signin
    from voice import listen, wake
    from voice.turns import TurnJudge

    frontend = wake.Frontend()
    model = wake.Head.load(wake.model_dir() / wake.HEAD_FILE)
    loopback = listen.Loopback(lambda: wake.Detector(frontend, model), listen.Vad())
    judge = TurnJudge()

    def heard(event) -> None:  # the ear's thread
        loop.call_soon_threadsafe(eng.ear_event, event)

    def state(opened: bool, why: str) -> None:  # the ears' thread
        loop.call_soon_threadsafe(eng.opened if opened else (lambda: eng.closed(why)))

    ear = listen.Ear(detector=wake.Detector(frontend, model), loopback=loopback,
                     vad=listen.Vad(), on_event=heard,
                     judge=judge if judge.available else None,
                     raw_detector=wake.Detector(frontend, model))
    ears = listen.Ears(ear, signed_in=lambda: not signin.required() or signin.held(),
                       canceller=_echo_canceller(), on_state=state)
    return ear, ears


def stop(reason: str = "off") -> None:
    """Close the ear: the engine stops at once -- a live conversation ends,
    handing over what the user said; nothing more is heard -- and the threads are
    joined in a thread of their own, never on the event loop (9 October
    2026: switching off from the app joined four of them there)."""
    ears, _state["ears"], _state["listener"] = _state["ears"], None, None
    eng = _state.get("engine")
    if eng is not None:
        eng.switch_off(reason)
        eng.detach()
    if ears is not None:
        ears.halt()
        threading.Thread(target=ears.stop, name="ears-stop", daemon=True).start()
    if reason == "swap":
        ticker, _state["ticker"] = _state.get("ticker"), None
        if ticker is not None:
            ticker.cancel()
        _state["engine"] = None
    if runtime.voice is not None:
        runtime.voice.player.on_play = None
    _state["echo"] = None


def _echo_canceller():
    """The canceller for the ear (voice/echo.py), fed by the desk's player;
    None if there is none to have."""
    from voice.echo import EchoCanceller

    canceller = EchoCanceller()
    if not canceller.available or runtime.voice is None:
        _state["echo"] = None
        return None
    runtime.voice.player.on_play = canceller.far
    _state["echo"] = canceller
    return canceller


def echo_cancelled() -> bool:
    """Whether the desk's own voice is being taken out of the microphone:
    then a live conversation can hear them while it talks, on the speakers."""
    canceller = _state.get("echo")
    return canceller is not None and canceller.available


def status() -> dict:
    ears, listener, eng = _state["ears"], _state["listener"], _state.get("engine")
    if ears is None:
        state = "off" if not switched_on() else ("warming" if runtime.voice is None
                                                  else "unavailable")
    elif ears.paused_for:
        state = f"paused ({ears.paused_for})"
    elif not ears.open:
        state = "opening"
    else:
        state = eng.mode if eng is not None else "listening"
    counts = listener.counts if listener else None
    return {
        "on": switched_on(),
        "offered": offered(),
        "state": state,
        "error": _state["error"] or (ears.error if ears else None),
        "mic": ears.device if ears else None,
        "wakes": counts.wakes if counts else 0,
        "near_wakes": counts.near_wakes if counts else 0,
        "laptop_wakes_ignored": counts.laptop_wakes_ignored if counts else 0,
        "false_wakes": _state["false_wakes"],
        "dropped_follow_ups": _state["dropped_follow_ups"],
        "barge_ins": counts.barge_ins if counts else 0,
        "last": _state["last"],
        "last_switch": _state["last_switch"],
        "note": _open_note(),
        "tap": tap_status(),
        # The engine: its mode, who holds the speakers, whether anything is
        # audible (measured, voice/floor.py), and its last transitions.
        "engine": eng.status() if eng is not None else None,
        "live": {"on": bool(eng is not None and eng.call is not None),
                 "last": _state["last_call"],
                 "minutes_today": round(live_minutes_today(), 1) if runtime.conn else 0,
                 "minutes_a_day": LIVE_DAY_MIN},
        # Its own voice out of the microphone (voice/echo.py), and whether the
        # ear is judging when the user has finished (voice/turns.py).
        "echo": {"on": echo_cancelled(),
                 "frames": _state["echo"].cancelled if _state.get("echo") else 0},
        "turn_judge": bool(listener is not None and listener.judge is not None),
    }


def _open_note() -> dict | None:
    from agent import notes

    if runtime.conn is None:
        return None
    note = notes.open_note(runtime.conn)
    return None if note is None else {"id": note["id"], "kind": note["kind"],
                                      "since": note["started_at"]}


# ---------------------------------------------------------------------------
# Push-to-talk, through the engine
# ---------------------------------------------------------------------------


def hotkey_down() -> None:
    """The user pressed the push-to-talk key: whatever the desk is saying stops."""
    eng = _state.get("engine")
    if eng is not None:
        eng.hotkey_down()


async def hotkey_turn(run):
    """A push-to-talk turn (`routes.ws_session`), run through the engine so
    it owns the speakers and what it asks lives on its turn; and its line in
    the log (`log_hotkey_turn`)."""
    eng = engine()
    turn = await (run(None) if eng is None else eng.hotkey_turn(run))
    try:
        log_hotkey_turn(turn)
    except Exception:  # noqa: BLE001 - the turn is answered; its line is not worth it
        # Never silently: session E scores these lines, and a line that was
        # not written read only as "no turn was logged".
        log.exception("could not log the push-to-talk turn")
    return turn


def log_hotkey_turn(turn) -> None:
    """One line per push-to-talk turn: where its time went, never its words.
    The engine logged hands-free turns only, so push-to-talk's waits -- the
    press to a live microphone, the release to the first word -- could not
    be measured on a real desk (docs/plan/07-voice.md, section 1)."""
    timing = turn.timing
    outcome = ("error" if turn.error else "interrupted" if turn.interrupted
               else "answered" if turn.reply_text else
               "empty" if not turn.transcript.ok else "unanswered")
    log.info(
        "push-to-talk turn: outcome=%s held_s=%.2f live_ms=%s stt_ms=%s first_token_ms=%s "
        "agent_ms=%s first_audio_ms=%s spoken_s=%.2f stall=%s preamble=%s total_ms=%s "
        "rejected=%s",
        outcome, timing.captured_s, "-" if timing.live_ms is None else timing.live_ms,
        timing.stt_ms, timing.first_token_ms or "-", timing.agent_ms,
        timing.first_audio_ms or "-", timing.spoken_s, timing.stalled, timing.preambled,
        timing.total_ms, "yes" if turn.transcript.rejected else "no")


# ---------------------------------------------------------------------------
# What the engine calls on in the worker
# ---------------------------------------------------------------------------


def _approval_shaped(text: str) -> bool:
    from agent import approvals

    waiting = approvals.pending(runtime.conn)
    return approvals.parse_reply(text, waiting, approvals.holding(runtime.conn),
                                 withdrawn=approvals.withdrawn(runtime.conn)) is not None


def _recent_turns(limit: int = 6) -> list[tuple[str, str]]:
    """The desk thread's last words, theirs and their assistant's, for the voice
    taking over: so "yes, the second one" means what it meant."""
    from agent.worker import answering

    session_id = answering.recent_session("voice")
    if session_id is None:
        return []
    rows = runtime.conn.execute(
        "SELECT role, content FROM messages WHERE session_id = ? AND role IN ('user','assistant')"
        " ORDER BY id DESC LIMIT ?", (session_id, limit)).fetchall()
    return [("user" if r["role"] == "user" else "assistant", r["content"][:600])
            for r in reversed(rows)]


async def hand_over(text: str, *, heard_at: float, own_voice_until: float, laptop_speech,
                    now: float, turn=None) -> str:
    """Their words from a live conversation, to their assistant on the desk's
    thread -- after the rules of the desk: a yes said while the laptop
    played something other than this conversation's own voice is not their
    (`core/desk.approval_counts`), and a yes counts only if the user finished
    saying it after the conversation's own voice and its echo
    (`core/desk.heard_user`). `heard_at` is when the stretch of speech it
    came from ended, by the microphone; `laptop_speech(start, end)` is the
    loopback's seconds of speech."""
    from agent.worker import answering

    if _approval_shaped(text):
        since = max(now - rules.HEARD_USER_S, own_voice_until)
        if not rules.approval_counts(laptop_speech_s=laptop_speech(since, now)):
            return ("That sounded like an approval, but the laptop was playing audio as the "
                    "user said it. Tell them to approve it on screen instead.")
        if not rules.heard_user(mic_speech_at=heard_at, own_voice_until=own_voice_until, now=now):
            log.info("an approval-shaped handoff dropped: no speech heard outside its own voice")
            return ("That sounded like an approval, but the microphone heard nobody say it "
                    "-- it may have been this conversation's own voice coming back. Ask "
                    "the user to say it again.")
    token = runtime.desk_turn.set(turn)
    try:
        reply, _ = await answering.voice_reply(text, channel="voice")
    finally:
        runtime.desk_turn.reset(token)
    return reply


class DeskShell:
    """The worker's side of the engine: Whisper, the rules, their assistant,
    notes, a live conversation's parts and the record. `voice` is the warm
    `VoiceSession` (or the simulator's)."""

    def __init__(self, voice, *, clock=time.monotonic, sleep=asyncio.sleep, blocking=None,
                 connect=None) -> None:
        self.voice = voice
        self.engine = None
        self.clock = clock
        self.sleep = sleep
        self.blocking = blocking or asyncio.to_thread
        self.connect = connect
        self._row: dict = {"billed": 0.0, "beat": None}

    # -- a capture ------------------------------------------------------------

    async def transcribe(self, heard):
        audio, heard.audio = heard.audio, None  # the only copy, gone after this
        try:
            return await self.blocking(self.voice.transcriber.transcribe, audio)
        finally:
            del audio

    @contextlib.contextmanager
    def turn_context(self, turn):
        token = runtime.desk_turn.set(turn)
        try:
            yield
        finally:
            runtime.desk_turn.reset(token)

    async def decide(self, turn, transcript, heard):
        """What code makes of a capture, before any model: the old turn's
        rules, in order. Code-answered ones are answered here."""
        from voice.engine import GO_ON, Decision

        eng = self.engine
        turn.facts["stt_ms"] = transcript.transcribe_ms
        turn.released = time.perf_counter()
        if not transcript.ok:
            return Decision("unreadable")
        request = rules.wake_phrase(transcript.text)
        if heard.woke and request is None:
            _state["false_wakes"] += 1
            log.info("a wake without the phrase: dropped unread")
            return Decision("false_wake")
        if heard.kind == "barge":
            # Speech over its voice with no phrase (D2, core/desk.py): their
            # words without its own, and no other laptop speech through them.
            said = eng.floor.said_between(heard.ended - rules.OWN_WORDS_S, heard.ended)
            theirs = rules.own_words_removed(transcript.text, said)
            other = self.laptop_speech(max(heard.started, eng.floor.audible_until()),
                                       heard.ended)
            if not rules.barge_in_counts(other_laptop_speech_s=other, user_words=theirs):
                return Decision("barge_dropped")
            request = theirs
        elif not heard.woke:
            if not rules.capture_counts(woke=False, laptop_speech_s=heard.laptop_speech_s):
                _state["dropped_follow_ups"] += 1
                # Said in the log, never what the user said: "it misses half of what I
                # say" (7 October 2026) could not be traced to anything.
                log.info("a follow-up dropped: the laptop played %.1f s of speech through it",
                         heard.laptop_speech_s)
                return Decision("follow_up_dropped")
            request = transcript.text if request is None else request
        if heard.barge_in and request:
            # Said over the desk's own voice: what of it is the desk's own
            # words, come back through the microphone, is not theirs.
            said = eng.floor.said_between(heard.ended - rules.OWN_WORDS_S, heard.ended)
            left = rules.own_words_removed(request, said)
            if request.strip() and not left.strip():
                log.info("a cut-in that was the desk's own words: dropped")
                return Decision("own_words")
            request = left
        turn.request = request
        if not request:
            # Not over them: if the user has begun asking while Whisper checked the
            # phrase (voice/engine.py holds it), "go on" is already true.
            if not eng.asking():
                eng.floor.claim(turn)
                eng.floor.play(turn, *GO_ON)
                await eng.floor.drained(turn, 5.0)
            return Decision("phrase_alone", then="follow")
        if _STOP.search(request):
            await eng.say(turn, "Okay. I've stopped listening.")
            await switch(False, by="voice")
            return Decision("stop", then="off")
        if _NEVERMIND.match(request):
            return Decision("never_mind")
        if heard.barge_in and cut_in_stop(request):
            # Said over its voice, which the wake stopped: nothing is left to
            # do, and nothing more is said.
            return Decision("cut_in_stop")
        if (then := await self._notes(request, turn)) is not None:
            return Decision("notes", then=then)
        if _approval_shaped(request) and not rules.approval_counts(
                laptop_speech_s=heard.laptop_speech_s):
            await eng.say(turn, "The laptop was playing something as you said that, so "
                                "please approve it on screen.")
            return Decision("approval_to_screen", then="follow")
        if (topic := live_asked(request)) is not None:
            return Decision("live_asked", then="live", request=topic)
        if heard.barge_in and cut_in_noise(request, heard):
            log.info("a cut-in that was not a request: dropped unanswered")
            return Decision("cut_in_noise")
        return Decision("agent", then="agent", request=request)

    async def answer(self, turn):
        """Their assistant's answer, spoken as it is written, through the
        turn's hold on the speakers."""
        from voice.stt import Transcript

        transcript = turn.transcript or Transcript(text=turn.request, language="en",
                                                   duration_s=0.0, transcribe_ms=0,
                                                   device="-")
        with self.turn_context(turn):
            result = await self.voice.respond(transcript, released=turn.released or
                                              time.perf_counter(), text=turn.request,
                                              out=turn.voice)
        turn.outcome = ("cut" if result.interrupted else
                        "failed_turn" if result.error else "answered")
        _state["last"] = {"at": time.time(), "woke": turn.heard.get("woke"),
                          "heard_s": round(turn.heard.get("captured_s", 0.0), 2),
                          **result.timing.as_dict(), "error": result.error}
        turn.facts.update(agent_ms=result.timing.agent_ms,
                          first_audio_ms=result.timing.first_audio_ms,
                          spoken_s=round(result.timing.spoken_s, 1), stall=result.timing.stalled,
                          preamble=result.timing.preambled, cut=result.interrupted,
                          error=(result.error or "-").split(":")[0])
        return result

    def interrupt(self) -> None:
        self.voice.interrupt()

    async def speak(self, text: str, out) -> None:
        await self.voice.say(text, out=out)

    async def segment(self, heard) -> None:
        await _segment(heard, self.engine, self.voice)

    def approval_shaped(self, text: str) -> bool:
        return _approval_shaped(text)

    def laptop_speech(self, start: float, end: float) -> float:
        ear = self.engine.ear if self.engine is not None else None
        return ear.loopback.speech_between(start, end) if ear is not None else 0.0

    def start_room(self, title: str) -> None:
        from agent import notes

        if notes.open_note(runtime.conn) is None:
            _start_room(title, self.engine)

    async def _notes(self, request: str, turn) -> str | None:
        """A note's words, answered by code: the mode to go to, or None if
        it was not one."""
        from agent import notes

        eng, conn = self.engine, runtime.conn
        if _STOP_NOTES.search(request) and notes.open_note(conn) is not None:
            await _finish_note(eng, self.voice, owner=turn)
            return "idle"
        lecture = _asked(_LECTURE, request)
        if (match := _asked(_CALL, request)) or (
                lecture and lecture.group("what").lower() == "meeting"
                and self._laptop_talking()):
            what = (match or lecture).group("what").lower()
            kind = "Video" if what in ("video", "youtube", "stream", "podcast", "recording") \
                else "Call"
            said = tap(True, title=f"{kind}, {time.strftime('%b %d, %I:%M %p')}")
            if said.get("error"):
                await eng.say(turn, f"I can't take notes on it: {said['error']}.")
            else:
                await eng.say(turn, "Taking notes on what the laptop plays. Say hey Steve, stop "
                                    "taking notes, when it's over.")
            return "idle"
        if match := lecture:
            what = match.group("what").lower()
            await eng.say(turn, f"Taking notes on this {what}. Say hey Steve, stop taking "
                                "notes, when it's over.")
            # Only after saying so: its own words would be the transcript's first.
            # Titled by what they called it, which the summary is told
            # (`notes.ROOMS`).
            _start_room(f"{what.capitalize()}, {time.strftime('%b %d, %I:%M %p')}", eng)
            return "idle"
        if _DICTATE.match(request):
            note_id = notes.start(conn, "dictation")
            _state["note_heard"] = time.time()
            # Its idle watch (`notes.DICTATION_IDLE_S`): a forgotten "take a
            # note" left the ear transcribing everything said in the room into
            # the note until the user said "end note" (audit of 8 October 2026).
            _watch(note_id)
            # A chime, not words: words it said would be heard into the note.
            from voice.engine import GO_ON

            eng.floor.claim(turn)
            eng.floor.play(turn, *GO_ON)
            await eng.floor.drained(turn, 5.0)
            eng.transcribe(True)
            return "idle"
        if match := _QUICK.match(request):
            notes.quick(conn, match.group("text"))
            await eng.say(turn, "Noted.")
            return "follow"
        return None

    def _laptop_talking(self) -> bool:
        """Whether the laptop has been playing speech these last seconds:
        "take notes on this meeting" then means the one on the screen."""
        now = self.clock()
        return self.laptop_speech(now - 15.0, now) >= 2.0

    # -- a live conversation ----------------------------------------------

    def place(self) -> str:
        """The town the user said they live in, for the live voice's sense of
        where it is, or "" (`agent/home.py`)."""
        from agent.home import home_city

        return (home_city(runtime.conn) or "") if runtime.conn is not None else ""

    def history(self, turn) -> list[tuple[str, str]]:
        """What GPT-Live starts with: the desk thread's recent turns, and their
        request if it never reached their assistant."""
        rows = _recent_turns(limit=8)
        last = [("user", turn.request)] if turn.request else []
        if turn.result is not None and getattr(turn.result, "reply_text", ""):
            last.append(("assistant", turn.result.reply_text))
        # This turn's words are in the thread already when their assistant
        # answered them; when it did not (the user asked for a live conversation),
        # or the thread could not say, they are added here.
        for row in last:
            if row not in rows[-len(last):]:
                rows.append(row)
        return rows

    def live_minutes_left(self) -> float:
        """The desk's own day of minutes, and none once a spending cap the
        live voice counts against is reached. The voice is billed as it
        passes (`_write_live`), so the caps are asked here, before it
        opens: it came from the phone, whose calls were billed on another
        machine, and nothing asked them."""
        if spend.may_spend(runtime.conn, unit=spend.Unit("chat", part="live")) is not None:
            return 0.0
        return LIVE_DAY_MIN - live_minutes_today()

    def live_call(self):
        from voice import desk_call

        headphones = desk_call.duplex_default()
        eng = self.engine
        holder: dict = {}

        async def ask(text: str, *, heard_at: float) -> str:
            from voice.engine import Turn

            turn = Turn(id=0, source="live", started=self.clock())
            reply = await hand_over(
                text, heard_at=heard_at, own_voice_until=eng.floor.audible_until(holder["call"]),
                laptop_speech=self.laptop_speech, now=self.clock(), turn=turn)
            if (title := turn.asks.get("notes")) is not None:
                eng.end_live_soon("notes", title)
            return reply

        call = desk_call.LiveCall(
            floor=eng.floor, words=desk_call.Words(clock=self.clock), ask=ask, clock=self.clock,
            sleep=self.sleep, duplex=lambda: headphones or echo_cancelled(),
            connect=self.connect)
        holder["call"] = call
        return call

    def live_started(self, call) -> None:
        """Its minutes are billed while it runs, a beat at a time: a swap or
        a crash then leaves what was spent."""
        self._row = {"billed": 0.0, "beat": None, "call": call}

        async def beat() -> None:
            while True:
                await self.sleep(LIVE_BEAT_S)
                self._row["billed"] = _write_live(self._row["billed"],
                                                  call.seconds or call.elapsed())

        with contextlib.suppress(RuntimeError):
            self._row["beat"] = asyncio.get_running_loop().create_task(beat())

    def live_ended(self, call, reason: str, *, unanswered: str) -> None:
        """The record of a live conversation: its minutes, its transcript,
        and one line of numbers, never words."""
        row = self._row if self._row.get("call") is call else {"billed": 0.0, "beat": None}
        if row.get("beat") is not None:
            row["beat"].cancel()
        seconds = call.seconds or (call.elapsed() if call.live is not None else 0.0)
        _write_live(row.get("billed", 0.0), seconds)
        _state["last_call"] = {"at": time.time(), "reason": reason, "seconds": round(seconds),
                               "handed_over": len(call.session.turns)}
        log.info("a live conversation ended: reason=%s seconds=%.0f handed_over=%d "
                 "unanswered=%s talking_s=%.0f %s", reason, seconds, len(call.session.turns),
                 "yes" if unanswered else "no",
                 self.engine.floor.audible_s.get("livecall", 0.0), call.summary())
        if call.audio_s - call.loud_s > 10 and call.loud_s < 1:
            log.info("live: audio without words: %.0f s of frames, none of them audible",
                     call.audio_s)
        _keep_transcript(call.session)

    def to_thread(self, words: str) -> None:
        """What the user said in a live conversation that ended before it was
        answered -- the desk going off, or their next request taking over --
        into the desk's thread as theirs, so the next turn knows it was asked."""
        if not words or runtime.conn is None:
            return
        from agent.history import LIVE
        from agent.worker import answering

        convo = answering.conversation(answering.recent_session("voice"), channel="voice")
        convo.note_exchange(f"{words}\n\n[said in a live conversation at the desk that ended "
                            "before it was answered]", "", by=LIVE)

    # -- the record --------------------------------------------------------

    def log_turn(self, turn) -> None:
        """One line per capture: how it ended, what each step took, what
        became of it -- numbers and outcomes, never words. The 7 October 2026
        hands-free logs had no turn lines at all (audit of 8 October 2026)."""
        heard, facts = turn.heard, turn.facts
        asks = ",".join(f"{quiet}:{chance}" for quiet, chance in heard.get("asks", ())) or "-"
        log.info(
            "hands-free turn: outcome=%s source=%s woke=%s barge_in=%s captured_s=%.2f "
            "speech_after_wake_s=%.2f ended_by=%s asks=%s laptop_s=%.1f "
            "stt_ms=%s agent_ms=%s first_audio_ms=%s spoken_s=%s stall=%s preamble=%s cut=%s "
            "error=%s took_ms=%d",
            turn.outcome or "-", turn.source, heard.get("woke", "-"), heard.get("barge_in", "-"),
            heard.get("captured_s", 0.0), heard.get("speech_s", 0.0),
            heard.get("ended_by") or "-", asks, heard.get("laptop_s", 0.0),
            facts.get("stt_ms", "-"), facts.get("agent_ms", "-"),
            facts.get("first_audio_ms", "-"), facts.get("spoken_s", "-"),
            facts.get("stall", "-"), facts.get("preamble", "-"), facts.get("cut", "-"),
            facts.get("error", "-"), (time.perf_counter() - turn.began) * 1000)


# ---------------------------------------------------------------------------
# A live conversation's record
# ---------------------------------------------------------------------------


def live_minutes_today() -> float:
    """GPT-Live minutes spent at the desk since the user's midnight
    (`core/spend.windows`), counted on the desk's own thread."""
    from agent.models import LIVE_VOICE

    row = runtime.conn.execute(
        "SELECT COALESCE(SUM(u.cost_usd), 0) FROM usage_events u JOIN sessions s"
        " ON s.id = u.session_id WHERE s.channel = 'voice' AND u.model = ? AND u.created_at >= ?",
        (LIVE_VOICE.model_id, spend.windows().day.since)).fetchone()
    return float(row[0]) / LIVE_VOICE.usd_per_minute


def _voice_session() -> int:
    """The desk's thread, or a new one if it has no message in the history
    window -- which is what "let's talk live" with nothing yet handed over
    looks like. Its minutes and transcript need a session of their own kind
    to count against (`live_minutes_today` joins on it): with none, they
    were dropped, and the day's cap and the spend report both missed them
    (audit of 8 October 2026)."""
    from agent.worker import answering

    if (found := answering.recent_session("voice")) is not None:
        return found
    with db.transaction(runtime.conn):
        cur = runtime.conn.execute("INSERT INTO sessions (channel) VALUES ('voice')")
    return int(cur.lastrowid)


def _write_live(billed: float, seconds: float) -> float:
    """The voice's minutes, on the desk's thread, billed as they pass: each
    beat bills the seconds since the last as a bill of its own, chat's
    `live` part. Billed only at the end, a worker that died mid-call -- or a
    swap -- left a billed conversation uncounted. One row brought up to date
    did that too, but the ledger has one writer (`core/spend.bill`), which
    only adds. Returns the seconds billed so far.

    A call that reports fewer seconds at its end than its own clock counted
    stays billed by the clock: a bill is never taken back, and the
    difference is under a beat."""
    from agent.models import LIVE_VOICE

    more = seconds - billed
    if more <= 0:
        return billed
    try:
        thread = _voice_session()
        with (spend.spending("chat", part="live", conn=runtime.conn, session_id=thread),
              db.transaction(runtime.conn)):
            spend.bill(runtime.conn, LIVE_VOICE.cost_usd(more), model=LIVE_VOICE.model_id,
                       latency_ms=int(more * 1000))
    except Exception:  # noqa: BLE001 - a missed bill is better than a dropped call
        log.exception("could not record the live conversation's minutes")
        return billed
    return seconds


def _keep_transcript(talked) -> None:
    """What was said in a live conversation, both sides -- as GPT-Live heard
    them and as it answered -- on the desk's thread.

    As a tool row, `live_conversation`: kept for reading back and grading,
    never replayed as turns, since the requests it handed over are there as
    turns already. Without it, "it misses half of what I say" (7 October
    2026) could not be checked: 7.6 minutes had left one request behind."""
    from agent.models import LIVE_VOICE

    text = getattr(talked, "transcript", "") or ""
    if not text.strip() or runtime.conn is None:
        return
    session_id = _voice_session()
    with db.transaction(runtime.conn):
        runtime.conn.execute(
            "INSERT INTO messages (session_id, role, content, tool_name, model)"
            " VALUES (?, 'tool', ?, 'live_conversation', ?)",
            (session_id, text[:20_000], LIVE_VOICE.model_id))


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------


def _start_room(title: str, eng=None) -> int:
    """Notes of the room, through their microphone, from now. Only once
    nothing of the assistant's own is still to be said: what the speakers
    say, the microphone hears."""
    from agent import notes

    note_id = notes.start(runtime.conn, "lecture", title)
    eng = eng if eng is not None else _state.get("engine")
    if eng is not None:
        eng.transcribe(True)
    _watch(note_id)
    return note_id


async def notes_asked(source: str, on: bool, *, title: str | None = None,
                      channel: str = "", asked: str = "") -> dict:
    """Notes started or stopped because the user asked their assistant, in whatever
    words (`agent/toolset/desk.take_notes`), from the desk or their phone.

    `source` is "laptop" (a call or a video, from what the laptop plays;
    no microphone needed) or "room" (their microphone: hands-free has to be
    on, and the turn has to be their own spoken one at the desk, with their words
    `asked` naming notes: `room_refusal`). Notes of the room start once the
    turn has finished speaking -- they are asked of the turn
    (`runtime.desk_turn`) -- and a live conversation ends for them."""
    from agent import notes
    from agent.worker import links

    open_ = notes.open_note(runtime.conn)
    if not on:
        if open_ is None:
            return {"ok": True, "stopped": None, "note": "no notes were being taken"}
        # Written up in the background, and the user is sent where it went: the
        # summary is a model call, and this turn need not wait for it.
        asyncio.get_running_loop().create_task(_finish_note(
            _state.get("engine"), runtime.voice, spoken=False, ended="Notes stopped.",
            notify=True))
        return {"ok": True, "stopped": open_["kind"],
                "note": "stopped; the write-up goes to a new document, and the user is sent "
                        "the link"}
    if open_ is not None:
        return {"ok": False, "error": f"notes are already being taken ({open_['kind']}, "
                                      f"{open_['title']}); stop those first"}
    stamp = time.strftime("%b %d, %I:%M %p")
    eng = _state.get("engine")
    if source == "laptop":
        await links.ensure_voice()
        said = tap(True, title=title or f"Call, {stamp}")
        if said.get("ok") and eng is not None and eng.call is not None:
            eng.end_live_soon("notes")  # its voice would be in the call's notes
        return said
    # Only the user starts notes of the room, aloud at their desk. `take_notes` is a
    # free tool offered on every channel the user is present on, so before this a
    # Signal turn that had read a mail could be told to open their microphone
    # on the room (audit of 8 October 2026, finding 10).
    if refusal := room_refusal(channel, asked):
        return {"ok": False, "error": refusal}
    turn = runtime.desk_turn.get()
    if _state["listener"] is None or turn is None:
        # v0.1 offers no switch (`offered`): told to turn it on in the app, the
        # model sent the user looking for a setting that is not there.
        closed = ("hands-free listening is off, so the microphone is closed." if offered()
                  else "this version does not offer hands-free listening, so the room's "
                       "microphone is never open.")
        return {"ok": False, "error": closed + " A call or video on the laptop needs no "
                                               "microphone (source='laptop')."}
    turn.asks["notes"] = title or f"Meeting, {stamp}"
    return {"ok": True, "note": "notes of the room start as soon as you have finished "
                                "speaking; keep your reply to one short sentence"}


def room_refusal(channel: str, asked: str) -> str | None:
    """Why notes of the room may not start from this turn, or None.

    They may only when the turn is their own spoken one at the desk -- channel
    `voice`: the hotkey, "hey Steve", or what a live conversation hands over
    -- and their words in it ask for notes. Anything else (Signal, the tray,
    a reminder, an unattended run) is words the microphone's owner did not
    say to this laptop."""
    if channel != "voice":
        return ("notes of the room start only when the user asks for them aloud at their "
                "desk, so not from here: tell them to say 'hey Steve, take notes on this' "
                "there")
    if not _NOTES_WORDS.search(asked or ""):
        return ("the user did not ask for notes of the room in this turn: do not start "
                "them unless they do, aloud")
    return None


async def _segment(heard, listener, session) -> None:
    """A stretch of a dictation, a lecture or a call: transcribed into the
    note, and never anywhere else. One at a time, so the note keeps the
    order -- the user's side and the other's. `listener` is what is told the note has
    ended (the engine), if anything."""
    from agent import notes

    global _segment_lock
    if _segment_lock is None:
        _segment_lock = asyncio.Lock()
    async with _segment_lock:
        audio, heard.audio = heard.audio, None
        transcript = await asyncio.to_thread(session.transcriber.transcribe, audio)
        del audio
        note = notes.open_note(runtime.conn)
        if note is None:
            if listener is not None:
                listener.transcribe(False)
            return
        text = transcript.text if transcript.ok else ""
        mostly_laptop = heard.laptop_speech_s > 0.5 * max(heard.ended - heard.started, 0.1)
        if note["kind"] == "call":
            if heard.source == "laptop":
                text = f"Them: {text}" if text else ""
            elif mostly_laptop:
                return  # the call itself, through the speakers: already heard as "Them"
            else:
                text = f"Me: {text}" if text else ""
        elif heard.source == "laptop":
            return  # a tap left over from a call that has ended
        if note["kind"] == "dictation":
            # What the laptop itself mostly played is not their dictation.
            if mostly_laptop:
                return
            if _END_DICTATION.search(text):
                notes.append(runtime.conn, int(note["id"]), _END_DICTATION.sub("", text))
                await _finish_note(listener, session)
                return
        if text:
            notes.append(runtime.conn, int(note["id"]), text)
            _state["note_heard"] = time.time()


async def _tell(session, text: str, *, spoken: bool, owner=None) -> None:
    """Said aloud when the user is talking to it (hands-free on); otherwise
    silent -- a call the user started from the app, perhaps in public, must not
    have the laptop start talking. The app shows the note either way."""
    eng = _state.get("engine")
    if not spoken or session is None or _state["ears"] is None:
        return
    if eng is not None:
        await eng.say(owner if owner is not None else "notes", text)
    else:
        await session.say(text)


async def _finish_note(listener, session, *, spoken: bool = True, ended: str = "",
                       notify: bool = False, owner=None) -> None:
    from agent import notes

    note = notes.open_note(runtime.conn)
    if listener is not None:
        listener.transcribe(False)
    _stop_tap()
    if note is None:
        return
    notes.finish(runtime.conn, int(note["id"]))
    if note["kind"] == "dictation":
        await _tell(session, "Saved your note.", spoken=spoken, owner=owner)
        return
    await _tell(session, "Stopped. I'll write up your notes.", spoken=spoken, owner=owner)
    try:
        made = await notes.summarize(runtime.conn, int(note["id"]))
    except Exception:  # noqa: BLE001 - the transcript is kept either way
        log.exception("summarising notes failed")
        await _tell(session, "I couldn't write up the summary, but the transcript is saved.",
                    spoken=spoken, owner=owner)
        return
    where = "in a new document" if made.get("doc_url") else "in your notes"
    said = f"Your notes are {where}, called {made.get('title') or 'your notes'}."
    await _tell(session, said, spoken=spoken, owner=owner)
    if ended and (notify or _state["ears"] is None):
        # It ended itself (the call went quiet) with nobody talking to it,
        # or the user stopped it by asking their assistant, which answered before
        # the write-up was done: tell them where they went, the one way that
        # reaches them.
        from agent.worker import links

        await links.notify_user(f"{ended} {said}")


def _watch(note_id: int) -> None:
    """End a lecture at LECTURE_MAX_S, a call at CALL_MAX_S or once nothing
    has been said in it for CALL_IDLE_S, and a dictation left idle."""
    import datetime as dt

    from agent import notes
    from agent.sqltime import moment

    # A note resumed after a swap has no "last heard" yet: the clock starts
    # now, or its idle time would be zero for ever.
    _state.setdefault("note_heard", time.time())

    async def watching() -> None:
        while True:
            await asyncio.sleep(WATCH_S)
            note = notes.open_note(runtime.conn)
            if note is None or int(note["id"]) != note_id:
                return
            ran = (dt.datetime.now(dt.UTC) - moment(note["started_at"])).total_seconds()
            idle = time.time() - _state.get("note_heard", time.time())
            tapped = _state.get("tap")
            quiet = time.monotonic() - tapped.last_speech if tapped is not None else 0.0
            ended = ""
            if note["kind"] == "lecture" and ran >= notes.LECTURE_MAX_S:
                ended = "The notes reached their time limit."
            elif note["kind"] == "call" and ran >= notes.CALL_MAX_S:
                ended = "The call notes reached their time limit."
            elif note["kind"] == "call" and quiet >= notes.CALL_IDLE_S:
                ended = "Nothing was said for ten minutes, so I stopped taking notes."
            elif note["kind"] == "dictation" and idle >= notes.DICTATION_IDLE_S:
                ended = "Your dictation went quiet, so I saved it."
            if ended:
                await _finish_note(_state.get("engine"), runtime.voice, spoken=True,
                                   ended=ended)
                return

    _state["watch"] = asyncio.get_running_loop().create_task(watching())


# ---------------------------------------------------------------------------
# Notes of what the laptop plays (voice/listen.Tap)
# ---------------------------------------------------------------------------

NO_ADD_ON = "Notes on a call need the voice add-on, which isn't installed."


def tap(on: bool, *, title: str | None = None) -> dict:
    """Start or stop taking notes on what the laptop plays -- a call, a
    video. Starting opens a "call" note; stopping finishes it and writes it
    up in the background. Needs the speech models, not the microphone."""
    from agent import notes

    if not on:
        note = notes.open_note(runtime.conn)
        if note is None or note["kind"] != "call":
            _stop_tap()
            return {"ok": True, "tapping": False}
        asyncio.get_running_loop().create_task(
            _finish_note(_state.get("engine"), runtime.voice, spoken=False))
        return {"ok": True, "tapping": False, "summarizing": True, "note": int(note["id"])}
    if runtime.voice is None:
        return {"ok": False, "error": "speech is still loading"}
    if (open_ := notes.open_note(runtime.conn)) is not None and open_["kind"] != "call":
        return {"ok": False, "error": f"a {open_['kind']} is being taken; end it first"}
    # The tap before the note: on an install without the voice add-on the
    # note was written first, the tap then failed on its import, and the
    # note read "still listening" for good.
    try:
        _tap_on()
    except ImportError:
        return {"ok": False, "error": NO_ADD_ON}
    if open_ is None:
        note_id = notes.start(runtime.conn, "call",
                              title or f"Call, {time.strftime('%b %d, %I:%M %p')}")
    else:
        note_id = int(open_["id"])
    _start_tap(note_id)
    return {"ok": True, "tapping": True, "note": note_id}


def _tap_on() -> None:
    """The tap, built and started unless it is. ImportError without the
    voice add-on: its detector is faster-whisper's model, run by onnxruntime."""
    from voice import listen

    if _state.get("tap") is None:
        loop = asyncio.get_running_loop()

        def handed(heard) -> None:  # the tap's thread
            asyncio.run_coroutine_threadsafe(_tap_segment(heard), loop)

        tapped = listen.Tap(handed)
        tapped.start()
        _state["tap"] = tapped


def _start_tap(note_id: int) -> None:
    _tap_on()
    eng = _state.get("engine")
    if eng is not None and _state["listener"] is not None:
        eng.transcribe(True)  # the user's side, as "Me:"
    _state["note_heard"] = time.time()
    _watch(note_id)


def _stop_tap() -> None:
    tapped, _state["tap"] = _state.get("tap"), None
    if tapped is not None:
        tapped.stop()


async def _tap_segment(heard) -> None:
    if runtime.voice is None:
        return
    await _segment(heard, _state.get("engine"), runtime.voice)


def resume_tap() -> None:
    """A call being taken when the last worker gave up its duties carries on
    here: the note is open in the database."""
    from agent import notes

    note = notes.open_note(runtime.conn) if runtime.conn is not None else None
    if note is not None and note["kind"] == "call" and runtime.voice is not None:
        try:
            _start_tap(int(note["id"]))
        except ImportError:
            # Retried, it failed at every worker's start, and the note went
            # on reading "still listening". What was heard is kept.
            log.warning("the voice add-on is gone; call note %s is closed as it is", note["id"])
            notes.finish(runtime.conn, int(note["id"]))


# What notes on a call need beyond Steve's own packages: the tap's detector
# is faster-whisper's model, run by onnxruntime (voice/listen.py). Both come
# with the voice add-on, which v0.1 doesn't ship.
TAP_NEEDS = ("faster_whisper", "onnxruntime")


def add_on() -> bool:
    """Whether the voice add-on that call notes need is installed. Looked
    for, not imported: importing loads the models' libraries into the worker.
    Without it the window offered the switch, and pressing it brought only a
    refusal (`NO_ADD_ON`)."""
    from importlib.util import find_spec

    return all(find_spec(name) is not None for name in TAP_NEEDS)


def tap_status() -> dict:
    tapped = _state.get("tap")
    return {"on": tapped is not None, "ok": bool(tapped and tapped.ok),
            "error": tapped.error if tapped else None, "add_on": add_on()}
