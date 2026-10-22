"""How the worker reaches the user, and is reached: Signal, Telegram, and
the desk voice session.

`notify_user` is the one way to message them unprompted. It writes the
message to the outbox (agent/outbox.py) and returns; `send_due`, the outbox
duty's step, sends it to the paired remote channel.

Signal is its own duty (`start_signal`), and so is Telegram
(`start_telegram`). Neither needs anything of the speech models: a voice
note is heard by OpenAI (`hear`, core/hearing.py). Signal waited for them
until step 4, and without the voice add-on every voice note was answered
"I can't transcribe voice notes right now".
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from datetime import datetime
from functools import partial
from pathlib import Path

from agent import awake, cardwords, offloop, outbox, runtime
from agent.worker import answering
from channels import telegram
from channels.base import Outgoing
from channels.bridge import READ_OVER, ChannelBridge
from channels.signal import NotLinked, SignalChannel, Unready
from channels.signal import installed as signal_installed
from channels.signal import known_accounts as signal_known
from core import hearing, spend

# Voice is core's since step 7 (the microphone, and OpenAI's ear and voice);
# the local models are the add-on's (`_local_voice`). The import stays
# guarded: imported unconditionally, a machine missing the microphone's
# library could not start a worker at all -- every request failed with the
# worker never becoming healthy. Without it, voice reports itself failed and
# text works.
try:
    from voice.pipeline import VoiceSession
except ImportError as exc:
    VoiceSession = None
    _VOICE_MISSING = f"voice's packages are not installed ({exc.name})"

log = logging.getLogger("worker")


async def notify_user(text: str, *, kind: str = "notice", noted: str | None = None,
                      until: datetime | None = None, action_id: int | None = None,
                      reminder_id: int | None = None) -> int | None:
    """Tell the user something they did not ask for: the outbox row's id, or
    None if there was nothing to say.

    Everything that reaches out unprompted -- the morning digest, a bill due
    tomorrow -- asks here rather than assuming a bridge of its own. It never
    sends inline and never fails for a channel being down: the row is in the
    app's inbox at once, and the outbox duty sends it to the remote channel
    until it goes or `until` passes (agent/outbox.py).

    `noted` is what the conversation records instead of `text`, when the
    model reading their reply needs to know more than the message said: that
    "Mop" was a reminder going off, not a word out of nowhere. `action_id`
    is a card's: once it is answered, an unsent card is not sent.
    `reminder_id` is a reminder's, for its note's "Later" (agent/worker/needs.py).
    """
    text = (text or "").strip()
    if not text:
        return None
    message_id = outbox.add(runtime.conn, text, kind=kind, noted=noted,
                            remote=remote_channel(), action_id=action_id, until=until,
                            reminder_id=reminder_id)
    message = outbox.get(runtime.conn, message_id)
    for ws in list(runtime.listeners):
        with contextlib.suppress(Exception):
            asyncio.create_task(ws.send_json({"type": "inbox", "message": message.as_dict()}))
    wake_outbox()
    return message_id


# Signal's states in which it is not paired: not set up, being set up, or
# not linked. A message made then is for the app alone.
UNPAIRED = frozenset({"unavailable", "installing", "unlinked", "linking"})


def remote_channel() -> str | None:
    """The remote channel a message goes to as well as the app, or None for
    the app alone. Signal once it is set up on this machine, linked, whether
    or not it is running this minute: a message made while it starts, or
    while it is down, waits for it. Else Telegram, once a chat is paired."""
    if runtime.signal is not None:
        return "signal"
    if signal_installed() and runtime.signal_state.get("state") not in UNPAIRED:
        return "signal"
    if runtime.telegram is not None:
        return "telegram" if runtime.telegram.channel.chat_id is not None else None
    if telegram.paired():
        return "telegram"
    return None


def answers_from(channel: str) -> bool:
    """Whether an answer recorded as `channel`'s still counts. The app's and
    the desk's always. Signal's and Telegram's only when read over the link
    that is up now: the bridge running as that channel and, for Telegram,
    the pairing it came over (`bridge.READ_OVER`). Any other name, or an
    answer read over no bridge, never.

    `approvals.decide` asks as it records one, since unpairing forgets the
    chat but cannot stop a reply already being read. Asked of the channel's
    name alone, this held for a reply read before an unpair once another
    chat, or the same one, was paired again, and for any name it did not
    know (step 11's third review)."""
    if channel in ("tray", "voice"):
        return True
    read = READ_OVER.get()
    if read is None:
        return False
    bridge, link = read
    if channel == "signal":
        return bridge is runtime.signal
    if channel == "telegram":
        return (bridge is runtime.telegram and bridge.channel.chat_id is not None
                and link == bridge.channel.pairing)
    return False


def signal_is(state: str, **more) -> None:
    """Signal's state, as the app reads it at `/signal/status`, and nothing
    left over from the one before."""
    runtime.signal_state.clear()
    runtime.signal_state.update(state=state, **more)


def telegram_is(state: str, **more) -> None:
    """Telegram's, at `/telegram/status`: "unavailable" with no bot,
    "unpaired", "pairing" while a code waits, "ready" once a chat is
    paired, or "failed"."""
    runtime.telegram_state.clear()
    runtime.telegram_state.update(state=state, **more)


def wake_outbox() -> None:
    """Have the outbox duty look now rather than at its next round."""
    if runtime.outbox_wake is not None:
        runtime.outbox_wake.set()


async def send_due(now: datetime | None = None) -> int:
    """One round of the outbox duty: every row due a try, oldest first, to
    its channel. Stops at the first that does not go -- the rest would not
    either, and each try counts against a row's back-off. Returns how many
    went."""
    from agent import approvals

    went = 0
    while (message := outbox.next_due(runtime.conn, now)) is not None:
        if message.action_id is not None:
            action = approvals.get(runtime.conn, message.action_id)
            if action is None or action.status != "pending":
                outbox.stop(runtime.conn, message.id, "answered before it could be sent")
                continue
        why = await _send(message)
        if why:
            log.warning("message %s did not go by %s: %s", message.id, message.remote, why)
            outbox.failed(runtime.conn, message.id, why, now)
            break
        outbox.sent(runtime.conn, message.id, now)
        answering.note_sent(_recorded(message), channel=message.remote)
        went += 1
    return went


# The remote channels, by the name the outbox records: what each is called
# to the user, and where its bridge and state are.
REMOTE = {"signal": "Signal", "telegram": "Telegram"}


async def _send(message: outbox.Message) -> str | None:
    """Hand one message to its channel: None if it took it, else why not."""
    if message.remote not in REMOTE:
        return f"no channel called {message.remote!r}"
    called = REMOTE[message.remote]
    bridge = getattr(runtime, message.remote)
    if bridge is None:
        state = getattr(runtime, f"{message.remote}_state").get("state")
        return f"{called} is not running ({state})"
    # Only the card says its word: anything else that did -- a routine's
    # report, a module's notice -- could pose as a card. The channel takes
    # it out, of the text as the phone shows it (channels/plaintext.shown):
    # taken out here, before Markdown was, the brackets of "MA[PLE]()"
    # hid the word from the reading and not from the phone.
    card = message.action_id if message.kind == "card" else None
    try:
        took = await bridge.channel.send(Outgoing(text=message.text, card=card))
    except Exception as exc:  # noqa: BLE001 - a failed send is tried again
        return f"{type(exc).__name__}: {exc}"[:300]
    return None if took else f"{called} did not take it"


def seen_in_app(ids: list[int]) -> int:
    """The app showed these: each joins the app's conversation, the one a
    reply typed there lands in. Returns how many were new to it."""
    fresh = outbox.seen(runtime.conn, ids)
    for message in fresh:
        answering.note_sent(_recorded(message), channel="tray")
    return len(fresh)


def _recorded(message: outbox.Message) -> str:
    """What the conversation keeps of a message the user has: no waiting
    card's word, which the model would read there (agent/cardwords.py).
    A card is recorded with its own word out already (`approvals._ask`),
    and exactly as the outbox holds it, which is how
    `approvals.moved_on_since` finds it in the chat."""
    if message.kind == "card":
        return message.recorded
    return cardwords.mask(message.recorded,
                          [card.word for card in cardwords.waiting(runtime.conn)])


# Load the speech models when a worker takes up its duties even when
# nothing needs them yet: "1". For a machine where the first spoken word
# must not wait the few seconds a load takes.
VOICE_EAGER_ENV = "STEVE_VOICE_EAGER"

_voice_load: dict = {"task": None}


def _voice_needed_at_start() -> bool:
    """Whether something will use speech as soon as this worker is up:
    hands-free left on, or a call being taken. Otherwise the models load on
    first use."""
    from agent import notes
    from agent.worker import desk

    if os.environ.get(VOICE_EAGER_ENV, "").strip() == "1" or desk.switched_on():
        return True
    note = notes.open_note(runtime.conn) if runtime.conn is not None else None
    return note is not None and note["kind"] == "call"


LOCAL_VOICE_ENV = "STEVE_LOCAL_VOICE"
ADD_ON = ("faster_whisper", "kokoro_onnx")


class LocalVoiceMissing(RuntimeError):
    """Local speech was chosen, and the add-on is not installed."""


def _local_voice() -> dict:
    """The ear and the voice, when the user chose local ones: the add-on's
    Whisper and Kokoro, by STEVE_LOCAL_VOICE=1. Otherwise nothing, and the
    session hears and speaks through OpenAI (voice/cloud.py), core's. The
    desk's only: voice notes from the phone are heard by OpenAI either way
    (`hear`, core/hearing.py), as the README says.

    Installing the add-on does not choose it. A developer's checkout has
    every extra, and a push-to-talk session measured there (session E) would
    have timed local Whisper while claiming OpenAI's numbers. Chosen and
    missing, it fails rather than falling back to OpenAI: someone who chose
    local speech chose that their voice stays on the machine."""
    if os.environ.get("STEVE_LOCAL_VOICE", "").strip() != "1":
        return {}
    from importlib.util import find_spec

    missing = [name for name in ADD_ON if find_spec(name) is None]
    if missing:
        raise LocalVoiceMissing(f"local speech is chosen ({LOCAL_VOICE_ENV}=1), and the "
                                f"voice add-on is not installed ({', '.join(missing)})")
    from voice.stt import Transcriber
    from voice.tts import Speaker

    return {"transcriber": Transcriber(), "speaker": Speaker()}


async def _load_voice() -> None:
    runtime.voice_warm.clear()
    runtime.voice_warm["state"] = "warming"
    if VoiceSession is None:
        runtime.voice_warm.update(state="failed", error=_VOICE_MISSING)
        return
    try:
        session = VoiceSession(partial(answering.voice_reply, channel="voice"), streams=True,
                               on_cut=partial(answering.cut_short, "voice"), **_local_voice())
        report = await asyncio.to_thread(session.warm)
        runtime.voice = session
        runtime.voice_warm.update(state="ready", **report)
    except Exception as exc:  # noqa: BLE001 - voice is optional, text is not
        runtime.voice_warm.update(state="failed", error=f"{type(exc).__name__}: {exc}"[:300])


async def ensure_voice() -> VoiceSession | None:
    """The speech models, loaded if they are not: the warm session, or None
    if they could not load. Everyone asking while a load runs waits for that
    one load."""
    if runtime.voice is not None:
        return runtime.voice
    loop = asyncio.get_running_loop()
    task = _voice_load["task"]
    if task is None or task.done() or task.get_loop() is not loop:
        task = _voice_load["task"] = loop.create_task(_load_voice())
    await asyncio.shield(task)
    return runtime.voice


async def warm_voice() -> None:
    """Load the speech models now if something needs them from the start;
    otherwise leave them for the first use (`ensure_voice`).

    They loaded at every start, used or not: ~24 s of CPU and disk at every
    swap, two gigabytes of video memory, and the TTS process's gigabyte of
    RAM -- and two CUDA contexts, which keep the laptop's graphics chip
    awake (13 W at idle, measured 5 October 2026) for push-to-talk that
    went unused for days. Loaded on the press instead, they cost a few
    seconds once (4.8 s for Whisper alone, measured), mostly while the user is
    still talking: the recording starts at once (agent/worker/routes.py).

    Deliberately not awaited in the lifespan when it does load: Whisper
    takes ~24 s at start, and the supervisor gives a new worker 30 s to
    report healthy.
    """
    if _voice_needed_at_start():
        await ensure_voice()
        if runtime.voice is not None:
            # Hands-free, if the user left it on (agent/worker/desk.py).
            from agent.worker import desk

            await desk.start()
            desk.resume_tap()


async def start_signal() -> None:
    """Bring up the Signal bridge, if Signal is set up and linked. Its own
    duty: it needs nothing of the speech models."""
    from agent.worker import pairing

    signal_is("starting")
    try:
        async def feedback(verdict: str, on: str) -> None:
            answering.note_feedback(verdict, on)

        bridge = ChannelBridge(
            SignalChannel(),
            answering.voice_reply,
            conn=runtime.conn,
            transcribe=hear,
            feedback=feedback,
            waited=lambda sent: awake.waited_line(runtime.conn, sent),
            unpair=pairing.unlink_signal,
        )
        # start() resolves the account itself, and does it *after* reaping
        # stale daemons. Asking here first is what made a swap report
        # "unlinked": the question blocks on the lock the retiring worker's
        # daemon still holds, and comes back with nothing.
        await bridge.start()
        runtime.signal = bridge
        signal_is("ready")
    # Checked by the channel's start, which runs signal-cli: a program that
    # is not the one installed is not run (core/addons.py).
    except Unready as exc:
        signal_is("unavailable", hint=str(exc))
    # Lost, when Signal still holds an account here: linked from this laptop
    # and not removed by the user, whose unlink deletes it. Only a lost link
    # is a note (agent/worker/needs.py): "unlinked" alone is also Signal
    # installed and never linked, or removed on purpose.
    except NotLinked:
        signal_is("unlinked", lost=bool(await asyncio.to_thread(signal_known)))
    except Exception as exc:  # noqa: BLE001 - Signal is optional, the agent is not
        signal_is("failed", error=f"{type(exc).__name__}: {exc}"[:300],
                  lost=bool(await asyncio.to_thread(signal_known)))


async def hear(path: Path, channel: str = "signal") -> str:
    """What a voice note says, by OpenAI (core/hearing.py), billed to its
    channel's conversation as chat. `hearing.HearingUnavailable` with why if
    it cannot be heard."""
    return await offloop.with_own_conn(runtime.conn, _heard, path,
                                       answering.recent_session(channel))


async def start_telegram() -> None:
    """Bring up Telegram, if the user gave Steve a bot: paired, it answers
    that chat; not yet, it waits for the app's code. Its own duty."""
    from agent.worker import pairing

    token = await asyncio.to_thread(telegram.token)
    if not token:
        telegram_is("unavailable")
        return
    telegram_is("starting")
    channel = telegram.TelegramChannel(token, on_paired=telegram_paired)
    bridge = ChannelBridge(
        channel,
        partial(answering.voice_reply, channel="telegram"),
        conn=runtime.conn,
        transcribe=partial(hear, channel="telegram"),
        waited=lambda sent: awake.waited_line(runtime.conn, sent),
        unpair=pairing.unpair_telegram,
    )
    try:
        await bridge.start()
    except Exception as exc:  # noqa: BLE001 - Telegram is optional, the agent is not
        telegram_is("failed", error=f"{type(exc).__name__}: {exc}"[:300])
        return
    runtime.telegram = bridge
    if channel.chat_id is not None:
        telegram_paired()
    else:
        telegram_is("unpaired", bot=channel.bot)


def telegram_paired() -> None:
    bot = runtime.telegram.channel.bot if runtime.telegram is not None else None
    telegram_is("ready", bot=bot)


def _heard(conn, path: Path, session_id: int | None) -> str:
    with spend.spending("chat", conn=conn, session_id=session_id):
        return hearing.text(path, conn=conn)
