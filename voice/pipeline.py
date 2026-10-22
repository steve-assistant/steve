"""The voice loop: hold a key, talk, hear an answer.

    hotkey down  -> capture starts, and a tick says the microphone is live
    hotkey up    -> capture stops, transcribe, send to the agent
    reply        -> synthesise sentence by sentence, play as each arrives

It hears and speaks through OpenAI (voice/cloud.py) unless it is handed the
voice add-on's local models: core holds none.

The sentence-by-sentence part is what makes it feel responsive. Waiting for a
whole reply to be synthesised before any sound comes out adds the full
synthesis time to the silence; speaking the first clause while the second is
still being generated hides almost all of it.

Barge-in is just `stop()` on the player: pressing the hotkey again clears the
queue immediately rather than waiting for the current sentence to finish.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from voice import audio as audio_io
from voice.cloud import CloudSpeaker, CloudTranscriber
from voice.speaking import (
    PREAMBLE_LINES,
    Sentences,
    claims_an_effect,
    heard,
    preamble_for,
    rest_of,
)
from voice.stt import Transcriber, Transcript
from voice.tts import Speaker, TTSUnavailable

log = logging.getLogger("voice")


@dataclass
class TurnTiming:
    """Where the time in a spoken turn actually went.

    Recorded per turn because the interesting number is not the total but
    which leg moved: STT and TTS are stable, the model is not.
    """

    captured_s: float = 0.0
    # Press -> the microphone's first sample: what a word begun at the press
    # waits for. None for a turn with no press, or no sample.
    live_ms: int | None = None
    stt_ms: int = 0  # release -> the words
    agent_ms: int = 0
    first_token_ms: int = 0  # release -> the reply's first word written
    first_audio_ms: int = 0  # release -> first sound, the number that matters
    total_ms: int = 0
    spoken_s: float = 0.0
    # Whether a stall ("On it.") or a lookup's preamble was said before the
    # answer: for the turn's log line (agent/worker/desk.py), not the status.
    stalled: bool = False
    preambled: bool = False

    def as_dict(self) -> dict:
        return {
            "captured_s": round(self.captured_s, 2),
            "live_ms": self.live_ms,
            "stt_ms": self.stt_ms,
            "agent_ms": self.agent_ms,
            "first_token_ms": self.first_token_ms,
            "first_audio_ms": self.first_audio_ms,
            "total_ms": self.total_ms,
            "spoken_s": round(self.spoken_s, 2),
        }


@dataclass
class VoiceTurn:
    transcript: Transcript
    reply_text: str
    timing: TurnTiming
    interrupted: bool = False
    error: str | None = None
    meta: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "heard": self.transcript.text,
            "stt": self.transcript.as_dict(),
            "reply": self.reply_text,
            "timing": self.timing.as_dict(),
            "interrupted": self.interrupted,
            "error": self.error,
            **self.meta,
        }


# Given a transcript, produce a reply. Kept as a callback so the pipeline has
# no opinion about the agent -- tests pass a stub, the worker passes the real
# conversation.
ReplyFn = Callable[[str], Awaitable[tuple[str, dict]]]


# Said when a reply is slow to start, so a quiet laptop is not taken for one
# that did not hear. The maintainer, 7 October 2026: "sometimes it goes quiet for a
# while before responding" -- a reply that needs a lookup took 5 to 6
# seconds to its first word. Made once, at warm-up, by the local voice;
# never two the same in a row.
STALL_S = 1.5
# Slack on when a transcription is taken to have begun (`respond`): the thread
# that ran it wakes the loop a little after it ends.
STT_SLACK_S = 0.1
STALLS =("On it.", "One sec.", "Let me check.", "Okay, checking.", "Give me a moment.",
          "Looking now.")


class VoiceSession:
    """One microphone, one speaker, one agent.

    A reply is played through `out`: the player itself for a turn nothing
    else arbitrates, or a desk turn's hold on the speakers
    (voice/floor.FloorVoice), which drops its sound the moment the turn has
    lost the floor -- a cut-in, a hotkey press -- whatever is still being
    synthesised."""

    # What this reply has queued, when a turn is keeping count (`respond`);
    # a session built bare, as tests/gate/test_desk.py builds one, keeps none.
    _said: list | None = None
    # When `interrupt` last ran, on `time.perf_counter`'s clock (`respond`).
    _cut_at: float = float("-inf")
    # Where this turn's sound goes (`respond`); None is the player.
    _out = None
    # How blocking work and waits are run: a thread and the loop's clock,
    # unless the desk's simulator runs them on its own (tests/unit).
    blocking = None
    sleep = None

    # The voice failed this turn: the rest of the reply is shown, not tried
    # again sentence by sentence (`_speak`). On the class, as a default: the
    # speaking tests build a session without its __init__.
    _mute = False

    def __init__(
        self,
        reply_fn: ReplyFn,
        *,
        transcriber: Transcriber | CloudTranscriber | None = None,
        speaker: Speaker | CloudSpeaker | None = None,
        input_device: int | None = None,
        output_device: int | None = None,
        speak: bool = True,
        streams: bool = False,
        on_cut: Callable[[str], None] | None = None,
    ) -> None:
        self.reply_fn = reply_fn
        # Whether `reply_fn` takes `on_text`, `on_tool` and `on_restart` and
        # calls them as the reply is written (agent/worker/answering.voice_reply
        # does); and what to tell when the user cuts a reply off: what they heard of it.
        self.streams = streams
        self.on_cut = on_cut
        self.transcriber = transcriber or CloudTranscriber()
        self.speaker = speaker or CloudSpeaker(kept=(*STALLS, *PREAMBLE_LINES))
        self.recorder = audio_io.Recorder(device=input_device)
        self.player = audio_io.Player(device=output_device)
        self.speak = speak
        self._listening = False
        self._cut = False
        self._stalls: list[tuple] = []
        self._preambles: dict[str, tuple] = {}
        self._last_stall = -1
        # This reply as queued for the speaker, in order -- (sentence, seconds),
        # a line of its own as None -- and what was still to play when the user cut in.
        self._said: list[tuple[str | None, float]] = []
        self._unplayed: float | None = None

    # -- lifecycle ---------------------------------------------------------

    def warm(self) -> dict:
        """Load both models before the first turn.

        Whisper takes ~20s to load and the TTS process ~4s to become ready. A
        user who has just pressed the hotkey should not be paying for either.
        OpenAI's have nothing to load, but the stalls and preambles are a dozen
        calls the first time: made side by side, as one would be.
        """
        report: dict = {}
        started = time.perf_counter()
        self.transcriber.load()
        report["stt"] = self.transcriber.status()

        if self.speak:
            try:
                self.speaker.start()
                report["tts"] = self.speaker.status()
            except TTSUnavailable as exc:
                # Losing the voice is worse than losing the speech: the turn
                # still works, the reply just arrives as text.
                self.speak = False
                report["tts"] = {"available": False, "error": str(exc)}
        if self.speak:
            try:
                with ThreadPoolExecutor(max_workers=8) as pool:
                    stalls = list(pool.map(self.speaker.say, STALLS))
                    preambles = list(pool.map(self.speaker.say, PREAMBLE_LINES))
                self._stalls = [(s.audio, s.sample_rate, s.duration_s) for s in stalls]
                self._preambles = {line: (s.audio, s.sample_rate, s.duration_s)
                                   for line, s in zip(PREAMBLE_LINES, preambles, strict=True)}
            except TTSUnavailable as exc:
                # The voice is there and a line was not made -- OpenAI did not
                # answer this once. Without its stalls the desk still speaks;
                # turned off, it would stay quiet until the worker restarted.
                log.warning("the stalls and preambles were not made: %s", exc)
                report["tts"] = {**report["tts"], "lines": f"not made: {exc}"}

        report["warm_s"] = round(time.perf_counter() - started, 1)
        return report

    def close(self) -> None:
        if self.recorder.recording:
            self.recorder.stop()
        self.player.close()
        self.speaker.stop()

    def status(self) -> dict:
        return {
            "listening": self._listening,
            "speaking": self.player.is_playing,
            "stt": self.transcriber.status(),
            "tts": self.speaker.status() if self.speak else {"available": False},
            "devices": audio_io.describe_devices(),
        }

    # -- where the sound goes, and how work is run --------------------------

    @property
    def out(self):
        return self._out if self._out is not None else self.player

    async def _blocking(self, fn, *args):
        run = self.blocking or asyncio.to_thread
        return await run(fn, *args)

    async def _sleep(self, seconds: float) -> None:
        await (self.sleep or asyncio.sleep)(seconds)

    def _enqueue(self, audio, rate: int, text: str | None = None) -> None:
        from voice.floor import FloorVoice

        out = self.out
        if isinstance(out, FloorVoice):
            out.enqueue(audio, rate, text=text)
        else:
            out.enqueue(audio, rate)

    async def _drained(self, timeout: float) -> None:
        out = self.out
        if hasattr(out, "drained"):
            await out.drained(timeout)
        else:
            await self._blocking(out.wait, timeout)

    # -- the turn ----------------------------------------------------------

    def start_listening(self, pressed: float | None = None) -> None:
        """Hotkey down, at `pressed` (`time.perf_counter()`), now if None.

        Pressing to talk also stops any reply in progress: if the user starts
        speaking, whatever the agent was saying has been superseded. Then the
        tick, once the microphone is open: before it, a user who began at the
        press could not tell whether their first word was kept.
        """
        self._unplayed = self.out.queued_seconds
        self.out.stop()
        self.player.stop()
        self.recorder.start(pressed)
        self._listening = True
        self.tick()

    def stop_listening(self) -> audio_io.Recording:
        """Hotkey up: the microphone closes now, and `finish_turn` is given
        what it heard. A turn waits for the one before it, whose model may
        still be writing; closed by the turn, the microphone stayed open
        that long after the release."""
        self._listening = False
        return self.recorder.stop()

    def cancel_listening(self) -> None:
        """The press's socket went away before its release: the microphone
        closes, and what it heard is dropped, never sent. Left open, it kept
        recording until the next release, which sent minutes of the room to
        be transcribed and billed (audit of 16 October 2026)."""
        if not self._listening:
            return
        self._listening = False
        try:
            self.recorder.stop()
        except Exception:  # noqa: BLE001 - closing is all that is asked
            log.warning("could not stop the microphone", exc_info=True)

    def tick(self) -> None:
        """The soft tick that says the microphone is live. Never a reason to
        lose the turn: a speaker that cannot play it is only quiet."""
        try:
            self.player.enqueue(*audio_io.TICK)
        except Exception:  # noqa: BLE001 - the capture matters, the tick does not
            log.warning("could not play the tick", exc_info=True)

    def interrupt(self) -> bool:
        # Also stops a reply still being synthesised: a barge-in from "hey
        # Steve" has no hotkey held to tell `_speak_streaming` to stop.
        self._cut = True
        self._cut_at = time.perf_counter()
        self._unplayed = self.out.queued_seconds
        return self.out.stop()

    async def finish_turn(self, recording: audio_io.Recording | None = None, *,
                          out=None, on_heard: Callable[[str], Awaitable[None]] | None = None,
                          ) -> VoiceTurn:
        """Hotkey up: transcribe, answer, speak. `recording` is one made
        before this session existed -- the press that woke the models.
        `on_heard` is given the words as soon as they are heard, before the
        reply: the app shows them on release, not once the reply is over."""
        released = time.perf_counter()
        if recording is None:
            recording = self.recorder.stop()
        self._listening = False
        self._mute = False
        timing = TurnTiming(captured_s=recording.duration_s, live_ms=recording.live_ms)

        if recording.silent_after(audio_io.TICK_SPAN_S) or recording.duration_s < 0.25:
            # Sending this to Whisper invites a confident transcription of
            # nothing, which the agent would then answer.
            empty = Transcript(
                text="", language="en", duration_s=recording.duration_s,
                transcribe_ms=0, device=self.transcriber.device or "?",
                rejected="too short or silent",
            )
            return VoiceTurn(transcript=empty, reply_text="", timing=timing)

        transcript = await self._blocking(self.transcriber.transcribe, recording.audio)
        timing.stt_ms = int((time.perf_counter() - released) * 1000)
        if not transcript.ok:
            return VoiceTurn(transcript=transcript, reply_text="", timing=timing)
        if on_heard is not None:
            try:
                await on_heard(transcript.text)
            except Exception:  # noqa: BLE001 - the app's view is worth less than the turn
                log.warning("could not show what was heard", exc_info=True)
        return await self.respond(transcript, released=released, timing=timing, out=out)

    async def respond(self, transcript: Transcript, *, released: float,
                      timing: TurnTiming | None = None, text: str | None = None,
                      out=None) -> VoiceTurn:
        """Answer what was heard and speak the reply: the second half of a
        turn, the same for the hotkey and for "hey Steve" (voice/engine.py,
        which passes `text` -- the request, without the wake phrase -- and
        `out`, the turn's hold on the speakers). `released` is when the user
        stopped talking, for the timings.

        With `streams`, the reply is spoken as it is written (voice/speaking.py)
        and what the check after it changes is spoken from the checked text."""
        from core.desk import never_spoken
        from voice.floor import FloorVoice

        self._out = out
        timing = timing or TurnTiming(captured_s=transcript.duration_s,
                                      stt_ms=transcript.transcribe_ms)
        if isinstance(out, FloorVoice):
            # A desk turn: the desk's engine cuts it, by taking it off the
            # floor, and a "hey Steve" during its transcription supersedes it
            # before it gets here (voice/engine.py). An earlier turn's cut is
            # that turn's.
            self._cut = False
        else:
            # An earlier reply's cut is over; one that came while this request
            # was being transcribed is not. Cleared unconditionally, a "hey
            # Steve" said over the ~0.3 s of Whisper was forgotten, and the
            # reply the user had already talked over was spoken in full (audit of 8
            # October 2026). The capture's transcription began transcribe_ms
            # before now.
            began = time.perf_counter() - transcript.transcribe_ms / 1000 - STT_SLACK_S
            self._cut = self._cut and self._cut_at >= began
        self._said, self._unplayed = [], None
        reply = _Reply(self, released, timing)
        agent_started = time.perf_counter()
        stall = asyncio.ensure_future(self._stall(reply)) if self.speak and self._stalls else None
        streaming = self.speak and self.streams
        speaking = asyncio.ensure_future(reply.speak()) if streaming else None
        hooks = ({"on_text": reply.text, "on_tool": reply.tool, "on_restart": reply.restart}
                 if streaming else {})
        try:
            reply_text, meta = await self.reply_fn(
                transcript.text if text is None else text, **hooks)
        except Exception as exc:  # noqa: BLE001 - speak the failure, do not vanish
            if speaking is not None:
                speaking.cancel()
            timing.total_ms = int((time.perf_counter() - released) * 1000)
            return VoiceTurn(
                transcript=transcript, reply_text="", timing=timing,
                error=f"{type(exc).__name__}: {exc}",
            )
        finally:
            if stall is not None:
                stall.cancel()
        timing.agent_ms = int((time.perf_counter() - agent_started) * 1000)

        interrupted = False
        if self.speak and reply_text.strip():
            rest = reply_text
            if speaking is not None:
                reply.close()
                interrupted = await speaking
                rest = "" if interrupted else rest_of(never_spoken(reply_text), reply.spoken)
            if not interrupted:
                interrupted = await self._speak_streaming(rest, released, timing)
        elif speaking is not None:
            reply.close()
            interrupted = await speaking
        if interrupted and self.on_cut is not None:
            try:
                self.on_cut(heard(self._said, self._unplayed or 0.0))
            except Exception:  # noqa: BLE001 - the record is worth less than the turn
                log.exception("could not keep what the user heard of the reply")

        timing.total_ms = int((time.perf_counter() - released) * 1000)
        return VoiceTurn(
            transcript=transcript,
            reply_text=reply_text,
            timing=timing,
            interrupted=interrupted,
            meta=meta or {},
        )

    def _line(self, sound: tuple, reply: _Reply, *, stall: bool = False) -> None:
        """A line of its own -- a preamble, a stall -- queued for the speaker."""
        audio, rate, seconds = sound
        self._enqueue(audio, rate)
        self._said.append((None, seconds))
        reply.started = True
        if stall:
            reply.timing.stalled = True
        else:
            reply.timing.preambled = True

    async def _stall(self, reply: _Reply) -> None:
        """A few words, if nothing has been said in STALL_S."""
        await self._sleep(STALL_S)
        if self._listening or self._cut or reply.started:
            return
        choices = [i for i in range(len(self._stalls)) if i != self._last_stall]
        self._last_stall = random.choice(choices or [0])
        self._line(self._stalls[self._last_stall], reply, stall=True)

    async def _speak(self, text: str, released: float, timing: TurnTiming) -> bool:
        """One piece of a reply, synthesised and queued sentence by sentence.
        Returns True if the user cut in."""
        from core.desk import never_spoken
        from voice.tts import split_for_streaming

        for chunk in split_for_streaming(never_spoken(text)):
            if self._listening or self._cut:
                return True  # the user started talking again; abandon the rest
            if self._mute:
                return False
            try:
                speech = await self._blocking(self.speaker.say, chunk)
            except TTSUnavailable:
                # Once is enough: each sentence tried again waited out its own
                # timeout, silently, and the reply's text is shown only when
                # they are all over -- minutes, in an outage.
                log.warning("the voice failed; the rest of this reply is shown, not said",
                            exc_info=True)
                self._mute = True
                return False
            # The user may have cut in while this was being synthesised: `Player.stop`
            # clears the queue and sets no flag, so a sentence finished across
            # the cut was queued after it and played -- one more sentence, the
            # likelier the more is streamed (audit of 8 October 2026).
            if self._listening or self._cut:
                return True
            if not timing.first_audio_ms:
                timing.first_audio_ms = int((time.perf_counter() - released) * 1000)
            self._enqueue(speech.audio, speech.sample_rate, chunk)
            if self._said is not None:
                self._said.append((chunk, speech.duration_s))
            timing.spoken_s += speech.duration_s
        return self._listening or self._cut

    async def _speak_streaming(
        self, text: str, released: float, timing: TurnTiming
    ) -> bool:
        """Synthesise and play sentence by sentence.

        Each chunk is queued the moment it exists, so playback of sentence one
        overlaps synthesis of sentence two. Returns True if the user cut in --
        while it was being written, or while it played.
        """
        if text.strip() and await self._speak(text, released, timing):
            return True
        await self._drained(120.0)
        return self._listening or self._cut

    async def say(self, text: str, *, out=None) -> float:
        """Speak without a preceding turn -- background job results, alerts,
        the desk's own lines -- through `out` (the player by default)."""
        if not self.speak or not text.strip():
            return 0.0
        total = 0.0
        from core.desk import never_spoken
        from voice.floor import FloorVoice
        from voice.tts import split_for_streaming

        out = out if out is not None else self.player
        for chunk in split_for_streaming(never_spoken(text)):
            try:
                speech = await self._blocking(self.speaker.say, chunk)
            except TTSUnavailable:
                # As a reply's sentences do: the rest goes unsaid, and the
                # caller -- a desk line, an alert -- carries on. Raised, it
                # reached the desk's turn as an error.
                log.warning("the voice failed; the rest of this line is not said",
                            exc_info=True)
                break
            if isinstance(out, FloorVoice):
                out.enqueue(speech.audio, speech.sample_rate, text=chunk)
            else:
                out.enqueue(speech.audio, speech.sample_rate)
            total += speech.duration_s
        return total


class _Reply:
    """One reply as it is written: its sentences to the speaker as they
    complete, the preamble for the first tool it reaches for, and a hold on
    everything from the first sentence that claims something was done --
    the check after the reply may correct it (voice/speaking.py)."""

    def __init__(self, session: VoiceSession, released: float, timing: TurnTiming) -> None:
        self.session, self.released, self.timing = session, released, timing
        self.sentences = Sentences()
        self.queue: asyncio.Queue[str | None] = asyncio.Queue()
        self.spoken: list[str] = []
        self.held = False
        self.started = False

    def text(self, delta: str) -> None:
        from agent.history import clean

        if not self.timing.first_token_ms and delta.strip():
            self.timing.first_token_ms = int((time.perf_counter() - self.released) * 1000)
        if self.held:
            return
        for sentence in self.sentences.feed(delta):
            # As the final reply is cleaned (`Conversation.send`) before it is
            # spoken whole: the model's private citation markers, a note of
            # the history's it copied, the cut-in marker. Streamed raw, they
            # were said aloud (audit of 8 October 2026).
            sentence = clean(sentence)
            if not sentence:
                continue
            if claims_an_effect(sentence):
                self.held = True
                return
            self.spoken.append(sentence)
            self.started = True
            self.queue.put_nowait(sentence)

    def restart(self) -> None:
        """The run is starting over -- a retry after a failure, or the turn
        replayed on a stronger model -- and will stream again from its first
        word. What was already said stays said, and the new run's text is
        held for the checked reply, which `rest_of` trims to what was not
        said. Before, the replay re-spoke every sentence (audit of 8 October
        2026)."""
        self.sentences = Sentences()
        if self.spoken:
            self.held = True

    def tool(self, name: str) -> None:
        line = preamble_for(name)
        sound = self.session._preambles.get(line or "")
        if sound is None or self.started or self.session._cut or self.session._listening:
            return
        self.session._line(sound, self)

    def close(self) -> None:
        self.queue.put_nowait(None)

    async def speak(self) -> bool:
        """Speak each sentence as it comes, until `close`. True if the user cut in."""
        while (sentence := await self.queue.get()) is not None:
            if await self.session._speak(sentence, self.released, self.timing):
                return True
        return self.session._listening or self.session._cut
