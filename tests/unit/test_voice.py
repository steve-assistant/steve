"""Voice pipeline tests.

Split by what they need. The pure logic -- chunking, silence detection,
resampling, hallucination filtering -- runs everywhere. Anything needing a GPU
or a soundcard is skipped rather than failed, because those are legitimately
absent on a CI box and a suite you learn to ignore protects nothing.
"""

from __future__ import annotations

import numpy as np
import pytest

import tests.gate.voice_extra  # noqa: F401 - skips this module without the voice extra
from voice import audio as aio
from voice.stt import HALLUCINATION_PHRASES, Transcript
from voice.tts import split_for_streaming


def _tone(seconds: float, rate: int, hz: float = 440.0, amp: float = 0.5) -> np.ndarray:
    t = np.arange(int(seconds * rate)) / rate
    return (amp * np.sin(2 * np.pi * hz * t)).astype(np.float32)


# --------------------------------------------------------------------------
# Chunking for streaming playback
# --------------------------------------------------------------------------


def test_a_single_sentence_is_one_chunk():
    assert split_for_streaming("The capital of Japan is Tokyo.") == [
        "The capital of Japan is Tokyo."
    ]


def test_long_replies_split_so_speech_can_start_early():
    text = (
        "The Chesapeake Bay is the largest estuary in the United States. "
        "It borders Maryland and Virginia. Many rivers feed into it."
    )
    chunks = split_for_streaming(text)
    assert len(chunks) >= 2
    assert chunks[0].endswith(".")
    # Nothing may be dropped: this text is what the user actually hears.
    assert "".join(chunks).replace(" ", "") == text.replace(" ", "")


def test_short_fragments_merge_forward():
    """Synthesising "Yes." alone costs the same fixed overhead as a sentence.

    Emitting it as its own chunk would add a synthesis round trip before the
    real answer, which is the opposite of why we chunk at all.
    """
    chunks = split_for_streaming("Yes. The answer is forty two and here is why.")
    assert len(chunks) == 1


def test_text_without_terminal_punctuation_still_speaks():
    """Normalisation supplies the missing full stop.

    Not cosmetic: the chunker splits on sentence endings, so a line without
    one would never be a chunk boundary and a long unpunctuated reply would
    be synthesised in a single block.
    """
    assert split_for_streaming("no trailing period here") == [
        "no trailing period here."
    ]


def test_empty_text_produces_nothing_to_say():
    assert split_for_streaming("") == []
    assert split_for_streaming("   ") == []


# --------------------------------------------------------------------------
# Silence handling
# --------------------------------------------------------------------------


def test_silence_is_detected_before_it_reaches_the_model():
    """Whisper answers silence with confident nonsense.

    A left-on microphone reliably produces "Thank you." or "Bye.", and the
    agent would then reply to something nobody said.
    """
    assert aio.Recording(audio=np.zeros(16_000, dtype=np.float32)).silent_after(0)
    room_tone = (np.random.default_rng(0).standard_normal(16_000) * 0.001).astype(
        np.float32
    )
    assert aio.Recording(audio=room_tone).silent_after(0)


def test_actual_speech_is_not_treated_as_silence():
    assert not aio.Recording(audio=_tone(1.0, 16_000)).silent_after(0)


def test_recording_reports_duration_at_the_stt_rate():
    rec = aio.Recording(audio=np.zeros(24_000, dtype=np.float32))
    assert rec.duration_s == pytest.approx(1.5)


@pytest.mark.parametrize("phrase", ["Thank you.", "THANK YOU.", " bye. ", "you"])
def test_known_hallucinations_are_rejected(phrase):
    assert phrase.lower().strip() in HALLUCINATION_PHRASES


def test_real_speech_is_not_mistaken_for_a_hallucination():
    for phrase in ["What is the capital of Japan?", "Queue a research task.", "Thanks"]:
        assert phrase.lower().strip() not in HALLUCINATION_PHRASES


def test_a_rejected_transcript_is_not_ok():
    t = Transcript(text="", language="en", duration_s=1.0, transcribe_ms=5,
                   device="cpu", rejected="looks like a silence hallucination")
    assert t.ok is False


class _Whisper:
    """faster-whisper's model as the warm-up uses it: what it was asked, and
    whether the decoding was run (its segments are a lazy generator)."""

    def __init__(self):
        self.asked: list[dict] = []
        self.decoded = 0

    def transcribe(self, audio, **options):
        self.asked.append({"peak": float(np.abs(audio).max()), **options})

        def segments():
            self.decoded += 1
            yield type("Segment", (), {"text": " you"})()

        return segments(), type("Info", (), {"language": "en"})()


def test_warming_whisper_runs_the_model_not_only_its_voice_filter():
    """9 October 2026 retest: the warm-up transcribed a second of zeros with
    the VAD filter on, which removed all of it ("VAD filter removed
    00:01.000 of audio"), so Whisper never ran, and the first "hey Steve"
    paid the CUDA kernels: 2,433 ms against ~450 warm, while the question the user
    asked meanwhile went unheard. Warming runs the decoder on sound the
    filter would not have kept anyway."""
    from voice.stt import Transcriber

    transcriber = Transcriber()
    transcriber._model = whisper = _Whisper()

    transcriber.warm()

    assert whisper.decoded == 1, "the segments were never decoded"
    assert whisper.asked[0]["vad_filter"] is False and whisper.asked[0]["peak"] > 0


async def test_switching_hands_free_on_warms_whisper_off_the_event_loop(monkeypatch):
    """The GPU is slow to its first run after a while idle, so hands-free
    warms Whisper as it opens (agent/worker/desk.start) -- in a thread:
    seconds of GPU work on the event loop would freeze the worker."""
    import threading

    from agent import runtime
    from agent.worker import desk

    ran = []

    class Transcriber:
        def warm(self):
            ran.append(threading.current_thread() is threading.main_thread())
            return 12

    monkeypatch.setattr(runtime, "voice", type("Voice", (), {"transcriber": Transcriber()})())

    await desk._warm_whisper()

    assert ran == [False], "not warmed, or warmed on the event loop's thread"


# --------------------------------------------------------------------------
# Resampling
#
# Every device rate on this machine disagrees with both Whisper (16k) and
# Kokoro (24k), so this conversion is on the hot path of every single turn.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "src,dst",
    [(32_000, 16_000), (48_000, 16_000), (44_100, 16_000), (24_000, 48_000)],
)
def test_resampling_preserves_duration_and_amplitude(src, dst):
    original = _tone(1.0, src)
    converted = aio.resample(original, src, dst)
    assert len(converted) == pytest.approx(dst, rel=0.01)
    assert float(np.abs(converted).max()) == pytest.approx(0.5, abs=0.05)
    assert converted.dtype == np.float32


def test_resampling_to_the_same_rate_is_a_no_op():
    original = _tone(0.5, 16_000)
    assert np.array_equal(aio.resample(original, 16_000, 16_000), original)


def test_the_full_capture_chain_survives_a_round_trip():
    """Kokoro 24k -> speaker 48k -> mic 32k -> Whisper 16k.

    Four conversions on every spoken turn; this asserts the signal is still
    the signal at the end of them.
    """
    original = _tone(1.0, 24_000, hz=440.0)
    at_speaker = aio.resample(original, 24_000, 48_000)
    at_mic = aio.resample(at_speaker, 48_000, 32_000)
    for_whisper = aio.resample(at_mic, 32_000, 16_000)

    assert len(for_whisper) == pytest.approx(16_000, rel=0.01)
    assert float(np.abs(for_whisper).max()) == pytest.approx(0.5, abs=0.05)

    # The tone must still be 440Hz: a resampler that silently shifts pitch
    # would also be mangling speech.
    spectrum = np.abs(np.fft.rfft(for_whisper))
    peak_hz = float(np.fft.rfftfreq(len(for_whisper), 1 / 16_000)[spectrum.argmax()])
    assert peak_hz == pytest.approx(440.0, abs=15.0)


# --------------------------------------------------------------------------
# Devices (needs a soundcard, not a GPU)
# --------------------------------------------------------------------------


def _has_audio() -> bool:
    try:
        return bool(aio.list_devices("output"))
    except Exception:  # noqa: BLE001
        return False


audio_required = pytest.mark.skipif(not _has_audio(), reason="no audio devices")


@audio_required
def test_device_enumeration_is_scoped_to_one_host_api():
    """Windows lists the same microphone under four APIs; showing all of them
    makes device choice a guessing game."""
    devices = aio.list_devices("input")
    assert devices
    assert len({d.hostapi for d in devices}) == 1


@audio_required
def test_there_is_a_default_input_and_output():
    assert aio.default_device("input") is not None
    assert aio.default_device("output") is not None


@audio_required
def test_describe_devices_shape():
    described = aio.describe_devices()
    assert described["stt_sample_rate"] == 16_000
    assert "input" in described and "output" in described


@audio_required
def test_stopping_a_recorder_that_never_started_is_harmless():
    """The hotkey can release without a matching press -- a dropped websocket,
    a window losing focus. That must not raise."""
    rec = aio.Recorder()
    result = rec.stop()
    assert len(result.audio) == 0
    assert result.silent_after(0)


class _Stream:
    """sounddevice's OutputStream as far as the player uses it."""

    made: list[_Stream] = []

    def __init__(self, **kwargs):
        self.active = False
        self.closed = False
        _Stream.made.append(self)

    def start(self):
        self.active = True

    def stop(self):
        self.stopped = True
        self.active = False

    def abort(self):
        self.aborted = True
        self.active = False

    def close(self):
        self.closed = True


def test_a_speaker_stream_that_stopped_is_opened_again_and_its_stale_audio_dropped(monkeypatch):
    """On the 9 October 2026 sound test the desk reported "speaking" for
    minutes after GPT-Live had finished, holding a live conversation open:
    a queue that never drains. A stopped stream was never reopened, so its
    queue stayed full and every later reply was silent until a restart."""
    _Stream.made = []
    monkeypatch.setattr(aio.sd, "OutputStream", _Stream)
    monkeypatch.setattr(aio.sd, "query_devices", lambda device: {"default_samplerate": 48_000})
    monkeypatch.setattr(aio, "default_device", lambda kind: type("D", (), {"index": 0})())
    player = aio.Player()
    player.enqueue(_tone(1.0, 24_000), 24_000)
    first = _Stream.made[0]
    first.active = False  # the device went away mid-reply

    player.enqueue(_tone(0.5, 24_000), 24_000)

    assert first.closed and len(_Stream.made) == 2 and _Stream.made[1].active
    assert player.queued_seconds == pytest.approx(0.5, abs=0.05), "only the new audio is queued"


def test_a_stream_reopened_mid_reply_starts_clean_at_the_next_sound(monkeypatch):
    """A reopen must never play broken audio: what the old stream held is
    discarded (aborted, not stopped, which would play it out), and what was
    left of the chunk it was in the middle of is not resumed mid-word on the
    new one. The next sound plays from its first sample."""
    _Stream.made = []
    monkeypatch.setattr(aio.sd, "OutputStream", _Stream)
    monkeypatch.setattr(aio.sd, "query_devices", lambda device: {"default_samplerate": 48_000})
    monkeypatch.setattr(aio, "default_device", lambda kind: type("D", (), {"index": 0})())
    player = aio.Player()
    player.enqueue(_tone(1.0, 48_000), 48_000)
    player._callback(np.zeros((1024, 1), np.float32), 1024, None, None)  # part-way through
    old = _Stream.made[0]

    dropped = player.reopen()

    assert dropped == pytest.approx(1.0 - 1024 / 48_000, abs=0.01)
    assert getattr(old, "aborted", False) and not getattr(old, "stopped", False) and old.closed
    assert len(_Stream.made) == 2 and _Stream.made[1].active and not player.is_playing
    fresh = np.linspace(0.1, 0.2, 2048, dtype=np.float32)
    player.enqueue(fresh, 48_000)
    out = np.zeros((1024, 1), np.float32)
    player._callback(out, 1024, None, None)
    assert np.allclose(out[:, 0], fresh[:1024]), "the new stream did not start at the new sound"


@audio_required
class _Speakers:
    """The output stream, faked: it runs and takes the callback, and plays
    nothing. The real one played this test's two-second tone aloud at every
    run of the suite, until the fences refused the speakers (16 October 2026)."""

    def __init__(self, *, callback, **kwargs) -> None:
        self.callback, self.active = callback, False

    def start(self) -> None:
        self.active = True

    def stop(self) -> None:
        self.active = False

    def close(self) -> None:
        self.active = False


def test_player_stop_reports_whether_it_interrupted_anything(monkeypatch):
    monkeypatch.setattr(aio.sd, "OutputStream", _Speakers)
    monkeypatch.setattr(aio.sd, "query_devices", lambda device=None: {"default_samplerate": 48_000})
    player = aio.Player(device=0)
    try:
        assert player.stop() is False  # nothing queued yet
        player.enqueue(_tone(2.0, 24_000), 24_000)
        assert player.is_playing
        assert player.stop() is True  # barge-in actually cut something off
        assert not player.is_playing
    finally:
        player.close()


# --------------------------------------------------------------------------
# Packaging
# --------------------------------------------------------------------------


def test_only_one_onnxruntime_is_installed():
    """`onnxruntime` and `onnxruntime-gpu` install into the same package
    directory, so having both means whichever landed last wins.

    This is not hypothetical: the old memory embedder declared plain
    `onnxruntime`, it got installed over the GPU build, and TTS went from
    0.30s to 1.07s per sentence with nothing raised anywhere --
    `get_available_providers()` simply stopped listing CUDA. faster-whisper
    and kokoro-onnx declare it too. An override in [tool.uv] keeps it out;
    this fails loudly if that ever stops working.

    Asserted on distributions rather than on CUDA being available, so it still
    means something on a machine without a GPU.
    """
    from importlib.metadata import distributions

    installed = {d.metadata["Name"] for d in distributions() if d.metadata["Name"]}
    both = installed & {"onnxruntime", "onnxruntime-gpu"}
    assert both != {"onnxruntime", "onnxruntime-gpu"}, (
        "onnxruntime and onnxruntime-gpu are both installed; the CPU build "
        "shadows the CUDA provider. Uninstall both, reinstall onnxruntime-gpu."
    )
