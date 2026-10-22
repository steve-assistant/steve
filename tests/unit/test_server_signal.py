"""Bringing the Signal bridge up inside the worker.

Signal is optional and the agent is not, so every failure here has to end with
a running worker. The cases worth pinning are the ones that leave the agent
half-equipped -- not set up, set up with files that changed, no linked
account, a channel that won't start -- because each of those silently
degrades into "the phone gets no reply" if the wiring gets it wrong.
"""

from __future__ import annotations

import pytest

from agent import runtime
from agent.worker import links
from tests.gate.extras import needs_voice


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    """Reset the module globals the startup path writes to."""
    monkeypatch.setattr(runtime, "signal", None)
    monkeypatch.setattr(runtime, "signal_state", {"state": "cold"})
    monkeypatch.setattr(runtime, "voice", None)
    monkeypatch.setattr(runtime, "conn", None)
    monkeypatch.setattr(links, "_voice_load", {"task": None})
    yield


class _FakeBridge:
    def __init__(self, channel, reply_fn, *, conn=None, transcribe=None, feedback=None,
                 waited=None, unpair=None):
        self.transcribe = transcribe
        self.feedback = feedback
        self.waited = waited
        self.unpair = unpair
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


class _FakeVoice:
    def __init__(self, *, speak: bool) -> None:
        self.speak = speak
        self.transcriber = object()
        self.speaker = object()

    def warm(self) -> dict:
        return {"stt": "fake"}


class _FakePlayer:
    """The speakers, as the desk's floor needs them to exist: nothing is ever
    queued by the hotkey tests below."""

    device_rate = 24_000

    def enqueue(self, audio, rate, owner=None):
        pass

    def take(self):
        return []


def _install(monkeypatch, bridge_cls=_FakeBridge, *, unready=None):
    """`unready`: why the channel's start refuses to run signal-cli."""
    if unready is not None:
        class Refusing(bridge_cls):
            async def start(self) -> None:
                raise links.Unready(unready)

        bridge_cls = Refusing
    monkeypatch.setattr(links, "SignalChannel", lambda *a, **k: object())
    monkeypatch.setattr(links, "ChannelBridge", bridge_cls)


# --------------------------------------------------------------------------
# Not linked
# --------------------------------------------------------------------------


async def test_an_unlinked_account_is_reported_not_raised(monkeypatch):
    """A fresh checkout has no Signal device. That is a normal state, and the
    status should say how to fix it rather than look like a crash."""

    class Unlinked(_FakeBridge):
        async def start(self) -> None:
            raise links.NotLinked("no linked Signal account")

    _install(monkeypatch, Unlinked)
    await links.start_signal()

    assert runtime.signal is None
    assert runtime.signal_state == {"state": "unlinked", "lost": False}


async def test_a_link_signal_still_holds_but_cannot_use_is_lost(monkeypatch):
    """The phone let the laptop go: Signal keeps the account's state here,
    which only the user's own unlink deletes. That, and not setup's answer,
    is what makes "Signal stopped" a note (agent/worker/needs.py): read from
    setup, a link made later from Channels and lost was no note."""

    class Unlinked(_FakeBridge):
        async def start(self) -> None:
            raise links.NotLinked("no linked Signal account")

    class Failing(_FakeBridge):
        async def start(self) -> None:
            raise RuntimeError("the daemon exited")

    monkeypatch.setattr(links, "signal_known", lambda: ["+15555550123"])
    for bridge, state in ((Unlinked, "unlinked"), (Failing, "failed")):
        _install(monkeypatch, bridge)
        await links.start_signal()
        assert (runtime.signal_state["state"], runtime.signal_state["lost"]) == (state, True)


async def test_the_account_is_never_looked_up_before_the_bridge_starts(monkeypatch):
    """A swap reported "unlinked" because the worker asked which accounts
    existed before anything had reaped the retiring worker's daemon -- so the
    question blocked on that daemon's lock and came back empty.

    Only the channel may resolve the account, because only it reaps first."""
    import channels.signal as signal_mod

    def forbidden():
        raise AssertionError("looked up the account before start() reaped")

    monkeypatch.setattr(signal_mod, "accounts", forbidden)
    _install(monkeypatch)
    await links.start_signal()

    assert runtime.signal_state["state"] == "ready"


async def test_a_missing_signal_cli_is_distinguished_from_an_unlinked_one(monkeypatch):
    """Different problems, different fixes: one is an install, one is a scan."""
    _install(monkeypatch, unready="signal-cli is not installed")
    await links.start_signal()

    assert runtime.signal is None
    assert runtime.signal_state == {"state": "unavailable",
                                    "hint": "signal-cli is not installed"}


async def test_an_add_on_that_changed_says_why_in_the_app(monkeypatch):
    """signal-cli runs holding the user's Signal keys: a jar replaced since
    it was installed is not run (the channel's start refuses it, see
    tests/unit/test_channels.py), and the app says why (core/addons.py)."""
    why = "Java has changed since it was installed, so it is not run; set it up again"
    _install(monkeypatch, unready=why)
    await links.start_signal()

    assert runtime.signal is None
    assert runtime.signal_state == {"state": "unavailable", "hint": why}


# --------------------------------------------------------------------------
# Failure
# --------------------------------------------------------------------------


async def test_a_channel_that_will_not_start_does_not_take_the_worker_down(monkeypatch):
    """signal-cli needs Java and a network; when it hasn't got them the agent
    still has to answer on every other surface."""

    class Exploding(_FakeBridge):
        async def start(self) -> None:
            raise RuntimeError("signal-cli not on PATH")

    _install(monkeypatch, Exploding)
    await links.start_signal()  # must not raise

    assert runtime.signal is None
    assert runtime.signal_state["state"] == "failed"
    assert "RuntimeError" in runtime.signal_state["error"]


async def test_a_signal_start_that_fails_is_tried_again_until_it_starts(monkeypatch):
    """A start was tried once: when a step of it failed on a busy machine,
    Signal stayed off until Steve restarted, and the phone went unanswered.
    Each failed try's daemon is stopped, or the next try would wait on its
    lock, and the waits grow as `_revive`'s do."""
    bridges = []

    class FailingTwice(_FakeBridge):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            bridges.append(self)

        async def start(self) -> None:
            if len(bridges) < 3:
                raise RuntimeError("signal-cli daemon never became reachable")
            self.started = True

    waits = []

    async def pause(seconds):
        waits.append(seconds)

    _install(monkeypatch, FailingTwice)
    monkeypatch.setattr(links, "signal_known", lambda: ["+15555550123"])
    await links.keep_signal(pause=pause)

    assert runtime.signal_state == {"state": "ready"}
    assert runtime.signal is bridges[-1] and bridges[-1].started
    assert [bridge.stopped for bridge in bridges] == [True, True, False]
    assert waits == [2.0, 4.0]


async def test_a_start_that_ends_in_the_users_hands_is_not_tried_again(monkeypatch):
    """Not linked, or an add-on changed: trying again cannot fix either, and
    each try runs signal-cli, a Java program, for nothing."""

    class Unlinked(_FakeBridge):
        async def start(self) -> None:
            raise links.NotLinked("no linked Signal account")

    async def pause(seconds):
        raise AssertionError("tried again")

    monkeypatch.setattr(links, "signal_known", lambda: [])
    _install(monkeypatch, Unlinked)
    await links.keep_signal(pause=pause)
    assert runtime.signal_state["state"] == "unlinked"

    _install(monkeypatch, unready="signal-cli is not installed")
    await links.keep_signal(pause=pause)
    assert runtime.signal_state["state"] == "unavailable"


async def test_a_start_begun_elsewhere_while_waiting_to_try_again_is_left_to_it(monkeypatch):
    """Linking from the app starts Signal itself (agent/worker/pairing.py).
    A try of the duty's on top would start a second daemon on one account."""
    tries = []

    class Failing(_FakeBridge):
        async def start(self) -> None:
            tries.append(1)
            raise RuntimeError("signal-cli daemon exited during startup")

    async def pause(seconds):
        links.signal_is("starting")

    monkeypatch.setattr(links, "signal_known", lambda: [])
    _install(monkeypatch, Failing)
    await links.keep_signal(pause=pause)

    assert len(tries) == 1
    assert runtime.signal_state == {"state": "starting"}


# --------------------------------------------------------------------------
# Voice notes, without the speech models (step 4)
# --------------------------------------------------------------------------


async def test_signal_hears_voice_notes_without_the_speech_models(monkeypatch):
    """Until step 4 a voice note was heard by the local Whisper, which only
    the voice add-on installs; without it every one was answered "I can't
    transcribe voice notes right now". OpenAI hears them now, speech models
    or none."""
    _install(monkeypatch)
    await links.start_signal()

    assert runtime.voice is None
    assert runtime.signal_state == {"state": "ready"}
    assert runtime.signal.transcribe is links.hear


async def test_a_voice_note_is_billed_as_the_signal_conversations_chat(monkeypatch, tmp_path):
    """Heard off the loop on a connection of its own (agent/offloop.py),
    and billed to the conversation the note joins, under chat's cap."""
    from agent.worker import answering
    from core import db, hearing

    conn = db.open_store(tmp_path / "steve.db")
    session = conn.execute("INSERT INTO sessions (channel) VALUES ('signal')").lastrowid
    monkeypatch.setattr(runtime, "conn", conn)
    monkeypatch.setattr(answering, "recent_session",
                        lambda channel: session if channel == "signal" else None)
    monkeypatch.setattr(hearing, "_call", lambda name, data, key: {
        "text": "call the plumber", "usage": {"type": "tokens", "input_tokens": 300,
                                              "output_tokens": 5}})
    note = tmp_path / "note.m4a"
    note.write_bytes(b"\x00\x00\x00\x1cftypM4A " + bytes(40))
    try:
        assert await links.hear(note) == "call the plumber"
        rows = conn.execute("SELECT category, session_id, model FROM usage_events").fetchall()
    finally:
        conn.close()

    assert [tuple(r) for r in rows] == [("chat", session, "gpt-transcribe")]


async def test_signal_is_a_duty_of_its_own_not_waiting_for_speech(monkeypatch):
    """It waited for the speech models, ~24 s at every start, because the
    local Whisper heard its voice notes. Now nothing of it needs them: a
    worker takes it up as a duty of its own, and loading speech starts no
    channel."""
    import asyncio
    from types import SimpleNamespace

    from agent import issues, modbuild
    from agent.worker import duties

    started = []

    async def _start():
        started.append(True)

    # The duty keeps Signal (`keep_signal`), trying a failed start again.
    monkeypatch.setattr(links, "start_signal", _start)
    monkeypatch.setattr(links, "keep_signal", _start)
    monkeypatch.setattr(links, "VoiceSession", lambda *a, **k: _FakeVoice(speak=True))
    monkeypatch.setattr(links, "_voice_needed_at_start", lambda: True)
    monkeypatch.setattr(runtime, "voice_warm", {"state": "cold"})

    await asyncio.wait_for(links.warm_voice(), 5)

    assert started == []

    # Each duty is started, not awaited: run what is started, and nothing else.
    begun = []

    def create_task(coroutine):
        begun.append(coroutine.cr_code)
        coroutine.close()

    monkeypatch.setattr(duties, "asyncio", SimpleNamespace(create_task=create_task))
    monkeypatch.setattr(duties, "_duties", {"state": "standby"})
    monkeypatch.setattr(duties, "_duty_tasks", [])
    monkeypatch.setattr(runtime, "jobs", SimpleNamespace(start=lambda: None))
    monkeypatch.setattr(issues, "record_deaths", lambda conn: 0)
    monkeypatch.setattr(modbuild, "recover", lambda conn: 0)

    duties.take_up_duties()

    assert _start.__code__ in begun


# --------------------------------------------------------------------------
# Speech loads when it is first needed (5 October 2026)
# --------------------------------------------------------------------------


async def test_speech_is_not_loaded_at_start_when_nothing_needs_it(monkeypatch):
    """Loaded at every start, used or not, the models cost ~24 s of CPU at
    every swap and held the graphics chip awake (13 W at idle) for a
    push-to-talk that went unused for days."""
    import asyncio

    def _must_not_load(*_a, **_k):
        raise AssertionError("loaded speech nobody asked for")

    monkeypatch.setattr(links, "VoiceSession", _must_not_load)
    monkeypatch.setattr(runtime, "voice_warm", {"state": "cold"})
    monkeypatch.setattr(links, "_voice_needed_at_start", lambda: False)

    await asyncio.wait_for(links.warm_voice(), 5)

    assert runtime.voice is None and runtime.voice_warm["state"] == "cold"


async def test_everyone_asking_while_speech_loads_waits_for_the_one_load(monkeypatch):
    """A press of the hotkey and the hands-free switch at once must not load
    Whisper twice: two gigabytes of video memory each."""
    import asyncio

    made = []

    def _voice(*_a, **_k):
        made.append(1)
        return _FakeVoice(speak=True)

    async def _slowly(fn, *args):
        await asyncio.sleep(0.05)
        return fn(*args)

    monkeypatch.setattr(links, "VoiceSession", _voice)
    monkeypatch.setattr(links.asyncio, "to_thread", _slowly)
    monkeypatch.setattr(runtime, "voice_warm", {"state": "cold"})

    first, second = await asyncio.gather(links.ensure_voice(), links.ensure_voice())

    assert first is second is runtime.voice and made == [1]
    assert runtime.voice_warm["state"] == "ready"
    assert await links.ensure_voice() is first and made == [1]


@needs_voice
def test_a_press_while_speech_is_cold_is_recorded_at_once_and_answered(monkeypatch):
    """Loading on the press must not cost them their first words: the recording
    starts before the models are up, and that recording is what is heard."""
    from types import SimpleNamespace

    from starlette.applications import Starlette
    from starlette.routing import WebSocketRoute
    from starlette.testclient import TestClient

    from agent.worker import desk, routes
    from voice import audio

    events: list[str] = []
    voices: list[object] = []

    class _Recorder:
        def start(self, pressed=None):
            events.append("recording")

        def stop(self):
            events.append("stopped")
            return "her words"

    class _Session:
        player = _FakePlayer()

        async def finish_turn(self, recording=None, *, out=None, on_heard=None):
            events.append(f"heard {recording}")
            voices.append(out)
            transcript = SimpleNamespace(ok=True, rejected=None)
            return SimpleNamespace(transcript=transcript, error=None,
                                   as_dict=lambda: {"heard": recording})

    async def _load():
        events.append("loaded")
        runtime.voice = _Session()
        return runtime.voice

    monkeypatch.setattr(audio, "Recorder", _Recorder)
    monkeypatch.setattr(links, "ensure_voice", _load)
    monkeypatch.setattr(runtime, "listeners", set())
    # The turn is run through the desk's engine, made on the first one: put
    # back as it was, with the clock it starts.
    monkeypatch.setitem(desk._state, "engine", None)
    monkeypatch.setitem(desk._state, "ticker", None)
    app = Starlette(routes=[WebSocketRoute("/ws", routes.ws_session)])

    with TestClient(app).websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        ws.send_json({"type": "voice_start"})
        assert ws.receive_json()["type"] == "listening"
        ws.send_json({"type": "voice_stop"})
        said = ws.receive_json()

    assert said == {"type": "voice_turn", "heard": "her words"}
    assert events.index("recording") < events.index("stopped") < events.index(
        "heard her words")
    assert voices and voices[0] is not None, "the turn did not go through the desk's speakers"


def test_a_hotkey_turn_leaves_nothing_asked_of_the_hands_free_handler(monkeypatch):
    """8 October 2026 audit, finding 6: a turn the hotkey ran could set
    `desk_talk` and `desk_notes`, which only the "hey Steve" handler reads --
    on its next, unrelated, turn. What a tool asks now lives on the desk's
    turn (`runtime.desk_turn`, a context variable): it is the hotkey turn's
    own, visible to the tools of that turn and to nothing after it; and with
    hands-free off there is no microphone to open on the room, so the tool
    refuses and nothing is asked."""
    from types import SimpleNamespace

    from starlette.applications import Starlette
    from starlette.routing import WebSocketRoute
    from starlette.testclient import TestClient

    from agent import notes
    from agent.worker import desk, routes

    seen: list[object] = []
    refused: list[dict] = []
    started: list[tuple] = []
    after: list[object] = []

    class _Session:
        player = _FakePlayer()

        def start_listening(self, pressed=None):
            pass

        def stop_listening(self):
            return None

        async def finish_turn(self, recording=None, *, out=None, on_heard=None):
            turn = runtime.desk_turn.get()
            seen.append(turn)
            turn.asks["live"] = True  # as `talk_it_through` would, were it offered
            refused.append(await desk.notes_asked(
                "room", True, title="Meeting, Oct 8", channel="voice",
                asked="take notes of the room"))
            transcript = SimpleNamespace(ok=True, rejected=None)
            return SimpleNamespace(transcript=transcript, error=None, as_dict=lambda: {})

    # No database: the connection is made on the test's thread and the route
    # runs on another. What is pinned is that nothing reaches `notes.start`.
    monkeypatch.setattr(runtime, "conn", object())
    monkeypatch.setattr(notes, "open_note", lambda conn: None)
    monkeypatch.setattr(notes, "start", lambda *args, **kwargs: started.append(args) or 1)
    monkeypatch.setattr(runtime, "voice", _Session())
    monkeypatch.setattr(runtime, "listeners", set())
    monkeypatch.setitem(desk._state, "listener", None)  # hands-free is off: nothing to start
    monkeypatch.setitem(desk._state, "engine", None)
    monkeypatch.setitem(desk._state, "ticker", None)
    real_turn = desk.hotkey_turn

    async def hotkey_turn(run):
        """The route's own task, after the turn: is anything still set?"""
        result = await real_turn(run)
        after.append(runtime.desk_turn.get())
        return result

    monkeypatch.setattr(desk, "hotkey_turn", hotkey_turn)
    app = Starlette(routes=[WebSocketRoute("/ws", routes.ws_session)])

    with TestClient(app).websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        ws.send_json({"type": "voice_start"})
        assert ws.receive_json()["type"] == "listening"
        ws.send_json({"type": "voice_stop"})
        assert ws.receive_json()["type"] == "voice_turn"

    assert len(seen) == 1 and seen[0].source == "hotkey", "the turn was not the desk's own"
    assert not refused[0]["ok"] and "hands-free" in refused[0]["error"]
    assert "notes" not in seen[0].asks, "notes of the room were asked of a closed microphone"
    assert started == [], "notes of the room were started with hands-free off"
    assert after == [None], "what the turn asked outlived it, in the route's own context"
    engine = desk._state["engine"]
    assert engine.call is None and not engine.notes_on and engine.turn is None


@pytest.mark.parametrize("hands_free, call_open, needed", [
    (True, False, True),
    (False, True, True),
    (False, False, False),  # Signal does not: OpenAI hears its voice notes
])
def test_what_needs_speech_from_the_start(monkeypatch, hands_free, call_open, needed):
    from agent import notes
    from agent.worker import desk

    monkeypatch.setattr(desk, "switched_on", lambda: hands_free)
    monkeypatch.setattr(runtime, "conn", object())
    monkeypatch.setattr(notes, "open_note",
                        lambda conn: {"id": 1, "kind": "call"} if call_open else None)
    monkeypatch.delenv(links.VOICE_EAGER_ENV, raising=False)
    monkeypatch.setattr(links, "signal_installed", lambda: True)

    assert links._voice_needed_at_start() is needed


async def test_the_signal_bridge_records_their_thumbs(monkeypatch):
    """A thumbs on a reply in Signal is how the ledger hears a reply was
    wrong when they don't say so in words."""
    from agent.worker import answering

    heard = []
    monkeypatch.setattr(answering, "note_feedback",
                        lambda verdict, on: heard.append({"verdict": verdict, "on": on}))
    _install(monkeypatch)
    await links.start_signal()

    await runtime.signal.feedback("down", "Here's your agenda.")

    assert heard == [{"verdict": "down", "on": "Here's your agenda."}]


async def test_the_signal_bridge_asks_the_heartbeat_whether_a_message_waited_on_sleep(
        monkeypatch):
    """The line comes from what the heartbeat recorded (agent/awake.py), on
    the worker's database, for the moment the message was sent."""
    from datetime import datetime

    from agent import awake

    asked = []
    monkeypatch.setattr(awake, "waited_line",
                        lambda conn, sent: asked.append((conn, sent)) or "Sorry for the wait.")
    _install(monkeypatch)
    await links.start_signal()

    sent = datetime(2026, 10, 13, 23, 0)
    assert runtime.signal.waited(sent) == "Sorry for the wait."
    assert asked == [(runtime.conn, sent)]
