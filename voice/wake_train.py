"""Training the "hey Steve" head for `voice/wake.py`: by hand, once.

    python -m voice.wake_train --download   # fetch the data, ~2.5 GB, once
    python -m voice.wake_train               # ~1 h on the laptop's GPU and CPU

The assistant is named Steve, and no stock wake model knows the word.
There are no recordings of the user saying it either, so the positives are
made: the local TTS (Kokoro, the voice the desk already
speaks with) says "hey Steve" in 23 of its English voices and blends of
them, at different speeds and pronunciations, often running on into a
request as he would ("hey Steve, what's the weather"). Each is placed in two
seconds of room: speech behind it, noise, a reverberant room, a cheap mic's
coloring.

**What it must not fire on** is the larger half:
- openWakeWord's own features of ~230 hours of ordinary audio (a 2 GB prefix
  of its ACAV100M set);
- LibriSpeech's dev-clean readers, with some speakers kept back for
  measuring;
- and -- because every positive is a TTS voice, and a head can learn "a TTS
  voice is speaking" instead of "hey Steve" -- thousands of other sentences
  in the same voices: near misses ("hey Steph", "hey Steven", "Steve,
  come here"), a request with no wake word, prose. And "hey Ste--", the
  phrase cut short, so it fires as the word ends rather than before;
- the desk's own voice as the microphone hears it while the desk talks,
  before and after the echo canceller (`_over_its_voice`). The first head
  never heard it, and in the rehearsal rig (10 October 2026) it fired in the
  middle of the desk's sentences about twenty times an hour of its speech,
  at the lower bar it has while the desk talks; one such wake cut a live
  conversation after its first sentence. So "hey Steve" said over the desk
  is made too, as positives, or the head would learn that anything under
  the desk's echo is never him.

**What it reports**, in `hey_steve.json` beside the weights:
- the threshold it chose, the highest recall with at most two false wakes
  an hour of ordinary audio and eight of non-stop speech (`choose`);
- the bar while the desk talks (`over_voice`, the ear's
  `voice/listen.over_voice_bar`), the highest recall over the desk's voice
  with at most one false wake an hour of its speech;
- recall on five voices it never trained on, in quiet and over the desk;
- false wakes an hour on openWakeWord's 10.7-hour validation set, on
  the held-back LibriSpeech speakers, and on an hour of the desk talking.

A false wake is cheap in quiet -- `core/desk.wake_phrase` drops what follows
unless Whisper hears the phrase -- so recall is worth more than the last
false wake. Over the desk's voice it is not cheap: a wake cuts its answer
for good.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import multiprocessing as mp
import os
import re
import subprocess
import tarfile
import time
import urllib.request
from pathlib import Path

import numpy as np

from core import paths
from voice import wake


def stored(*parts: str) -> Path:
    """The training sets' folder, or a file in it. A worktree can point at the
    live checkout's downloads, as at its models.

    Asked for each time, not bound at import: as a constant it was the data
    folder of whichever process imported this first, before a test or a
    worktree had chosen its own."""
    root = Path(os.environ.get("STEVE_WAKE_DATA") or paths.data_dir() / "wake_training")
    return root.joinpath(*parts)


_HF = "https://huggingface.co/datasets/davidscripka/openwakeword_features/resolve/main/"
VALIDATION = "validation_set_features.npy"
ACAV = "acav_prefix.npy"
ACAV_BYTES = 2_000_000_000
LIBRI = ("LibriSpeech", "dev-clean")
LIBRI_URL = "https://www.openslr.org/resources/12/dev-clean.tar.gz"

SR = wake.SAMPLE_RATE
CLIP = 32_000  # two seconds: 25 chunks, exactly one 16-embedding window
# Where the last window's audio ends (`Frontend.stream`'s last window covers
# batch mel frames up to 195, which end at sample 31712).
WINDOW_END = 31_712

ENGLISH = ("af_", "am_", "bf_", "bm_")
HELD_OUT_VOICES = ("af_sky", "am_michael", "bf_lily", "bm_lewis", "am_echo")

POSITIVE_TEXTS = ("Hey Steve.", "Hey Steve!", "Hey, Steve.", "Hey Steve?", "Hey Steve,",
                  "hey steve")
# Said as people say it, not as a dictionary does: "ay Steve", "huh Steve".
POSITIVE_PHONEMES = ("hˈeɪ stˈiːv", "hˈe stˈiːv", "heɪ stˈiːv", "ˈeɪ stˈiːv", "hɐ stˈiːv")
FOLLOWING = (
    "what's the weather", "set a reminder for six", "take a note", "what's on my calendar",
    "send an email to Dana", "how long until my next meeting", "stop", "start taking notes",
    "remind me to call mom", "what time is it", "read me my last email", "let's talk",
)
NEAR_MISSES = (
    "Hey Steph.", "Hey Stella.", "Hey Stan.", "Hey Eve.", "Hey Steven.", "Steve.",
    "Hey Siri.", "Hey Jarvis.", "Hey Dave.", "Hey sleeve.", "Hey, see.", "Heavy.",
    "Hey, stay.", "Hey, step back.", "Stevie!", "Hey Sean.", "Hey sweet.", "Hey Stu.",
    "Hey Steel.", "Hey, steep.", "Hey Stephanie.", "They've been there.", "Hey, it's me.",
    "Say Steve.", "Hey Stefan.", "Hey Stacey.", "Steve, come here.", "Steve's on the phone.",
    "I asked Steve about it.", "Tell Steve I said hi.", "Okay, Steven.", "A steep hill.",
    "Hey, stop.", "Hey, Eve's here.", "Hey Stevens.", "Hate, Steve.", "Hey Kevin.",
    "Hey Keith.", "Hey Pete.", "Hey Steve Jobs fans.", "Stephen said hey.",
)
REQUESTS = (
    "What's the weather like today?", "Set a reminder for six p.m.", "Take a note.",
    "Send an email to Dana about the budget.", "What's on my calendar tomorrow?",
    "Can you read me my last email?", "Start taking notes on this lecture.",
)
# What the desk says while the user talks over it: answers like its own, with
# prose for the rest. In its own voice (`voice/tts.VOICE`) most of the time,
# and in others for GPT-Live's, which cannot be made here.
DESK_ANSWERS = (
    "It's fifty-eight degrees and cloudy, with rain likely after three.",
    "You have two things this afternoon: the design review at one, and a call with Sam "
    "at four.",
    "Your next meeting starts in twenty-five minutes, in the large meeting room.",
    "I found three emails from Alex this week. The latest asks whether you can make the "
    "workshop.",
    "Done. I've set a reminder for six p.m. to call your mom.",
    "The flight to Chicago leaves at seven forty, from gate twelve.",
    "Here's the gist of the article: the Fed held rates steady, and markets barely moved.",
    "Sure. Starting notes now; say stop taking notes when you're done.",
    "Nothing is on your calendar for Friday morning, so you're free until noon.",
    "Okay, I've drafted a reply to Jordan saying Thursday works. It's in your drafts.",
    "There are two places nearby: a Thai place on Main Street, and the diner on the corner.",
    "That's about a twelve-minute walk, or four minutes by car.",
)
DESK_SHARE = 0.6  # of its sentences said in the desk's own voice


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


def download() -> None:
    """The three sources, once. The ACAV set is 17 GB; its first 2 GB are
    ~650,000 windows, which is plenty, and an .npy is readable from a prefix."""
    stored().mkdir(parents=True, exist_ok=True)
    if not stored(VALIDATION).exists():
        urllib.request.urlretrieve(_HF + "validation_set_features.npy", stored(VALIDATION))
    if not stored(ACAV).exists():
        request = urllib.request.Request(
            _HF + "openwakeword_features_ACAV100M_2000_hrs_16bit.npy",
            headers={"Range": f"bytes=0-{ACAV_BYTES}"})
        with urllib.request.urlopen(request) as response, stored(ACAV).open("wb") as out:
            while block := response.read(1 << 20):
                out.write(block)
    if not stored(*LIBRI).exists():
        archive = stored() / "dev-clean.tar.gz"
        urllib.request.urlretrieve(LIBRI_URL, archive)
        with tarfile.open(archive) as tar:
            tar.extractall(stored(), filter="data")
        archive.unlink()


def acav_windows() -> np.ndarray:
    """The ACAV prefix as [n, 16, 96] float16, memory-mapped."""
    import numpy.lib.format as fmt

    with stored(ACAV).open("rb") as f:
        version = fmt.read_magic(f)
        read = fmt.read_array_header_1_0 if version == (1, 0) else fmt.read_array_header_2_0
        shape, _, dtype = read(f)
        offset = f.tell()
    row = int(np.prod(shape[1:])) * np.dtype(dtype).itemsize
    rows = (stored(ACAV).stat().st_size - offset) // row
    return np.memmap(stored(ACAV), dtype=dtype, mode="r", offset=offset, shape=(rows, *shape[1:]))


def libri_files() -> tuple[list[Path], list[Path]]:
    """dev-clean's files, split by speaker: four fifths to train on, the rest
    kept back to count false wakes on voices it never heard."""
    speakers = sorted(p.name for p in stored(*LIBRI).iterdir() if p.is_dir())
    held = set(speakers[::5])
    train, test = [], []
    for path in sorted(stored(*LIBRI).rglob("*.flac")):
        (test if path.parts[-3] in held else train).append(path)
    return train, test


def prose(limit: int, rng: np.random.Generator) -> list[str]:
    """Sentences to say that are not the wake word: the repo's own docs, in
    plain words. They are English prose in his kind of register, and here."""
    text = " ".join(p.read_text(encoding="utf-8") for p in (paths.SOURCE / "docs").glob("*.md"))
    text = re.sub(r"`[^`]*`|\[[^\]]*\]\([^)]*\)|[#*|>_]", " ", text)
    sentences = [" ".join(s.split()) for s in re.split(r"(?<=[.!?])\s+", text)]
    sentences = [s for s in sentences
                 if 4 <= len(s.split()) <= 18 and "steve" not in s.lower() and s[:1].isalpha()]
    picked = rng.choice(len(sentences), size=min(limit, len(sentences)), replace=False)
    return [sentences[i] for i in picked]


# ---------------------------------------------------------------------------
# Voices
# ---------------------------------------------------------------------------


class Voices:
    """Kokoro, in-process, on the GPU if it will go."""

    def __init__(self) -> None:
        from voice import tts_server

        tts_server._register_cuda_dlls()
        import onnxruntime as ort

        ort.set_default_logger_severity(3)
        from kokoro_onnx import Kokoro

        session = ort.InferenceSession(str(tts_server.model_file()),
                                       providers=["CUDAExecutionProvider", "CPUExecutionProvider"])
        self.kokoro = Kokoro.from_session(session, str(tts_server.voices_file()))
        names = self.kokoro.get_voices()
        self.english = [v for v in names if v.startswith(ENGLISH)]
        self.other = [v for v in names if not v.startswith(ENGLISH)]

    def style(self, rng: np.random.Generator, pool: list[str]):
        """A voice from the pool, or a blend of two -- one of them sometimes
        from another language, for accents."""
        if rng.random() < 0.4:
            return pool[rng.integers(len(pool))]
        a = self.kokoro.get_voice_style(pool[rng.integers(len(pool))])
        other = self.other if rng.random() < 0.25 else pool
        b = self.kokoro.get_voice_style(other[rng.integers(len(other))])
        w = rng.uniform(0.25, 0.75)
        return (w * a + (1 - w) * b).astype(np.float32)

    def say(self, text: str, voice, speed: float, *, phonemes: bool = False) -> np.ndarray | None:
        import soxr

        try:
            audio, rate = self.kokoro.create(text, voice=voice, speed=speed, lang="en-us",
                                             is_phonemes=phonemes)
        except Exception:  # noqa: BLE001 - a phoneme string it cannot read is skipped
            return None
        return _trim(soxr.resample(audio.astype(np.float32), rate, SR))


def _trim(audio: np.ndarray) -> np.ndarray:
    """Cut leading and trailing quiet, so a clip's end is where the word ends."""
    if not len(audio):
        return audio
    level = np.convolve(np.abs(audio), np.ones(160) / 160, mode="same")
    loud = np.nonzero(level > level.max() * 0.03)[0]
    return audio[loud[0]:loud[-1] + 1] if len(loud) else audio


# ---------------------------------------------------------------------------
# Rooms
# ---------------------------------------------------------------------------


def _convolve(x: np.ndarray, h: np.ndarray) -> np.ndarray:
    n = len(x) + len(h) - 1
    size = 1 << (n - 1).bit_length()
    return np.fft.irfft(np.fft.rfft(x, size) * np.fft.rfft(h, size), size)[:len(x)]


def _room(x: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """A reverberant room: exponentially decaying noise behind the direct sound."""
    rt60 = rng.uniform(0.15, 0.8)
    t = np.arange(int(rt60 * SR)) / SR
    tail = rng.standard_normal(len(t)) * np.exp(-6.9 * t / rt60) * rng.uniform(0.05, 0.4)
    tail[0] = 1.0
    return _convolve(x, tail / np.sqrt((tail ** 2).sum()))


def _mic(x: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """A cheap microphone: less bass, less treble, unevenly."""
    spec = np.fft.rfft(x)
    f = np.fft.rfftfreq(len(x), 1 / SR)
    low, high = rng.uniform(60, 300), rng.uniform(3500, 7800)
    gain = 1 / np.sqrt(1 + (low / np.maximum(f, 1)) ** 4) / np.sqrt(1 + (f / high) ** 4)
    return np.fft.irfft(spec * gain, len(x))


def _noise(n: int, rng: np.random.Generator) -> np.ndarray:
    """White, pink or brown noise at unit RMS."""
    kind = rng.integers(3)
    spec = np.fft.rfft(rng.standard_normal(n))
    f = np.maximum(np.fft.rfftfreq(n, 1 / SR), 1.0)
    spec = spec * (1.0 if kind == 0 else (1 / np.sqrt(f) if kind == 1 else 1 / f))
    x = np.fft.irfft(spec, n)
    return x / (np.sqrt((x ** 2).mean()) + 1e-12)


def _rms(x: np.ndarray) -> float:
    return float(np.sqrt((x ** 2).mean()) + 1e-12)


class Room:
    """Backgrounds, per worker process: LibriSpeech read on demand, noise made."""

    def __init__(self, libri: list[Path]) -> None:
        self.libri = libri

    def background(self, n: int, rng: np.random.Generator, level: float) -> np.ndarray:
        import soundfile as sf

        choice = rng.random()
        if choice < 0.35 and self.libri:
            speech, _ = sf.read(str(self.libri[rng.integers(len(self.libri))]), dtype="float32")
            if len(speech) < n:
                speech = np.tile(speech, n // len(speech) + 1)
            start = rng.integers(len(speech) - n + 1)
            bed = speech[start:start + n]
            db = rng.uniform(-28, -8)
        elif choice < 0.8:
            bed = _noise(n, rng)
            db = rng.uniform(-40, -8)
        else:
            bed = _noise(n, rng)
            db = rng.uniform(-60, -40)  # a quiet room: still never digital silence
        return bed / _rms(bed) * level * 10 ** (db / 20)

    def mix(self, foreground: np.ndarray, rng: np.random.Generator) -> np.ndarray:
        """Foreground in a room, at a level a laptop mic might hear him."""
        x = foreground.astype(np.float64)
        if rng.random() < 0.6:
            x = _room(x, rng)
        level = _rms(x[np.abs(x) > np.abs(x).max() * 0.05]) if np.abs(x).max() > 0 else 1e-3
        x = x + self.background(len(x), rng, level)
        if rng.random() < 0.5:
            x = _mic(x, rng)
        x = x / (np.abs(x).max() + 1e-9) * 10 ** (rng.uniform(-30, -3) / 20)
        return np.clip(x, -1, 1).astype(np.float32)


# ---------------------------------------------------------------------------
# Clips, made and heard in worker processes
# ---------------------------------------------------------------------------

_W: dict = {}


def _init(libri: list[Path], phrases: dict[str, list[np.ndarray]],
          following: list[np.ndarray], desk: list[np.ndarray] | None = None) -> None:
    _W["front"] = wake.Frontend(threads=1)
    _W["room"] = Room(libri)
    _W["phrases"], _W["following"], _W["desk"] = phrases, following, desk or []


def _phrase_clip(which: str, i: int, seed: int, cut: bool) -> np.ndarray:
    """One phrase in two seconds of room. Ending inside the last window (a
    positive), or -- `cut` -- running past it, so the window hears only
    "hey Ste" (a negative)."""
    rng = np.random.default_rng(seed)
    phrase = _W["phrases"][which][i]
    if cut:
        end = WINDOW_END + int(rng.uniform(0.12, 0.35) * SR)
    else:
        end = WINDOW_END - int(rng.uniform(0.02, 0.40) * SR)
    after = np.zeros(0, np.float32)
    if rng.random() < 0.6:
        gap = np.zeros(int(rng.uniform(0, 0.15) * SR), np.float32)
        tail = _W["following"][rng.integers(len(_W["following"]))]
        after = np.concatenate([gap, tail * (_rms(phrase) / _rms(tail))])
    fg = np.zeros(CLIP + len(after) + SR, np.float32)
    start = end - len(phrase)
    if start < 0:
        phrase, start = phrase[-start:], 0
    fg[start:end] = phrase[:end - start]
    fg[end:end + len(after)] = after
    return _W["room"].mix(fg, rng)[:CLIP]


def _positive(task: tuple) -> np.ndarray:
    which, i, seed = task
    return _W["front"].stream(_phrase_clip(which, i, seed, cut=False)).astype(np.float16)


def _partial(task: tuple) -> np.ndarray:
    which, i, seed = task
    return _W["front"].stream(_phrase_clip(which, i, seed, cut=True)).astype(np.float16)


def _windows(embeddings: np.ndarray, stride: int) -> np.ndarray:
    if len(embeddings) < wake.WINDOW:
        return np.zeros((0, wake.WINDOW, wake.EMBED_DIM), np.float16)
    view = np.lib.stride_tricks.sliding_window_view(embeddings, wake.WINDOW, axis=0)
    return view.transpose(0, 2, 1)[::stride].astype(np.float16)


def _speech(task: tuple) -> np.ndarray:
    """Other speech in a room, every `stride`th window of it."""
    audio, seed, stride = task
    rng = np.random.default_rng(seed)
    lead = np.zeros(int(rng.uniform(0.3, 1.3) * SR), np.float32)
    clip = _W["room"].mix(np.concatenate([lead, audio, lead]), rng)
    return _windows(_W["front"].stream(clip), stride)


def _libri(task: tuple) -> np.ndarray:
    import soundfile as sf

    path, seed, stride = task
    audio, _ = sf.read(str(path), dtype="float32")
    return _speech((audio, seed, stride))


def _over_its_voice(task: tuple) -> tuple:
    """The desk talking in a room, as embeddings of what the microphone hears
    and of what the echo canceller (voice/echo.py) leaves of it: the two
    streams the ear reads while the desk speaks (`voice/listen.Ear._raw_score`).
    The room is the rehearsal rig's (devtools/rehearse/room.py): a delay, a
    reverberant room, small speakers' clipping and lost bass, its echo from
    well under what was played to as loud.

    With `phrase` -- ("train" or "held", its index) -- his "hey Steve" is said
    over it, from well under the desk's echo to above it, and the window that
    ends with it comes back from each stream, with how loud he was against
    the echo (dB). Otherwise both streams come back whole."""
    from devtools.rehearse import room as rooms
    from voice.echo import EchoCanceller

    picks, phrase, seed = task
    rng = np.random.default_rng(seed)
    volume = rng.uniform(0.3, 0.9)
    # Sentence by sentence, as it streams them (voice/tts.split_for_streaming);
    # under him, barely a pause, so he talks over its voice.
    pause = (0.15, 1.2) if phrase is None else (0.1, 0.4)
    parts = []
    for k in picks:
        said = _W["desk"][k].astype(np.float32) / 32767
        parts += [np.zeros(int(rng.uniform(*pause) * SR), np.float32),
                  said * np.float32(volume / (np.abs(said).max() + 1e-9))]
    desk = np.concatenate([*parts, np.zeros(SR // 2, np.float32)])
    acoustics = rooms.Acoustics(
        echo=rng.uniform(-24, 0), delay=rng.uniform(20, 160), rt60=rng.uniform(0.15, 0.6),
        reverb=rng.uniform(0.1, 0.4), drive=rng.uniform(0, 3), highpass=rng.uniform(80, 300),
        noise=rng.uniform(-70, -50))
    echo = rooms.EchoPath(acoustics, seed=int(rng.integers(1 << 30))).push(desk)
    mic = echo + rooms.gain(acoustics.noise) * rng.standard_normal(len(desk))
    end, sir = None, None
    if phrase is not None:
        his = _W["phrases"][phrase[0]][phrase[1]].astype(np.float64)
        if rng.random() < 0.6:
            his = _room(his, rng)
        # Once the canceller has had a few seconds of its voice to learn from.
        end = int(rng.integers(int(3.5 * SR) + len(his), len(desk) - SR // 2))
        start = end - len(his)
        sir = float(rng.uniform(-12, 6))
        level = _rms(echo[start:end]) if _rms(echo[start:end]) > 1e-4 else _rms(echo)
        his = his / _rms(his) * level * 10 ** (sir / 20)
        mic[start:end] += his
        if rng.random() < 0.6:  # running on into a request, as in `_phrase_clip`
            tail = _W["following"][rng.integers(len(_W["following"]))]
            after = end + int(rng.uniform(0, 0.15) * SR)
            tail = (tail * (_rms(his) / _rms(tail)))[:len(mic) - after]
            mic[after:after + len(tail)] += tail
    mic = np.clip(mic, -1, 1).astype(np.float32)
    canceller = EchoCanceller()
    if not canceller.available:
        raise RuntimeError("no echo canceller to train against (livekit is missing)")
    block = SR // 10  # the ear's microphone reads (`listen.Ears.MIC_READ_S`)
    cleaned = np.zeros_like(mic)
    for a in range(0, len(mic) - block + 1, block):
        canceller.far(desk[a:a + block], SR)
        cleaned[a:a + block] = canceller.near(mic[a:a + block])
    front = _W["front"]
    if end is None:
        return front.stream(cleaned).astype(np.float16), front.stream(mic).astype(np.float16)
    a = end - (WINDOW_END - int(rng.uniform(0.02, 0.40) * SR))
    return (front.stream(cleaned[a:a + CLIP]).astype(np.float16),
            front.stream(mic[a:a + CLIP]).astype(np.float16), sir)


# ---------------------------------------------------------------------------
# The head
# ---------------------------------------------------------------------------


def _flat(x: np.ndarray) -> np.ndarray:
    return x.reshape(len(x), -1).astype(np.float32)


def train(pos: np.ndarray, pools: dict[str, tuple[np.ndarray, float]], *, steps: int,
          hidden: int = 64, seed: int = 0, neg_weight: float = 30.0,
          dropout: float = 0.25) -> wake.Head:
    """A two-layer network, Adam, in numpy. Each batch is a quarter positives
    and the rest drawn from the negative pools in proportion to their weight.

    What the first run (5 October 2026) lacked: it fired 30-45 times an hour
    on ordinary speech at an even threshold. So, as openWakeWord trains its
    own: negatives weigh more as training goes on (to `neg_weight`, a false
    wake being worse than a phrase it must hear twice), and dropout, so 1,536
    numbers per window are not memorised. Hard negatives -- what a first
    pass wrongly fired on -- come in as a pool of their own (`mine`)."""
    rng = np.random.default_rng(seed)
    sample = np.concatenate(
        [_flat(pos[rng.integers(len(pos), size=4000)])]
        + [_flat(p[rng.integers(len(p), size=4000)]) for p, _ in pools.values()])
    mean, scale = sample.mean(0), sample.std(0) + 1e-3
    d = mean.size
    params = {
        "w1": rng.standard_normal((d, hidden)).astype(np.float32) * np.sqrt(2 / d),
        "b1": np.zeros(hidden, np.float32),
        "w2": rng.standard_normal((hidden, hidden)).astype(np.float32) * np.sqrt(2 / hidden),
        "b2": np.zeros(hidden, np.float32),
        "w3": rng.standard_normal((hidden, 1)).astype(np.float32) * np.sqrt(1 / hidden),
        "b3": np.zeros(1, np.float32),
    }
    m = {k: np.zeros_like(v) for k, v in params.items()}
    v = {k: np.zeros_like(p) for k, p in params.items()}
    names = list(pools)
    weights = np.array([pools[n][1] for n in names])
    weights = weights / weights.sum()
    batch, n_pos = 1024, 256
    for step in range(1, steps + 1):
        counts = rng.multinomial(batch - n_pos, weights)
        parts = [pos[rng.integers(len(pos), size=n_pos)]]
        parts += [pools[n][0][rng.integers(len(pools[n][0]), size=c)]
                  for n, c in zip(names, counts, strict=True)]
        x = (_flat(np.concatenate(parts)) - mean) / scale
        y = np.zeros((batch, 1), np.float32)
        y[:n_pos] = 1
        ramp = 1.0 + (neg_weight - 1.0) * min(1.0, step / (0.6 * steps))
        w = np.where(y > 0, 1.0, ramp).astype(np.float32)
        keep = 1.0 - dropout
        d1 = (rng.random((batch, hidden)) < keep).astype(np.float32) / keep
        d2 = (rng.random((batch, hidden)) < keep).astype(np.float32) / keep
        z1 = x @ params["w1"] + params["b1"]
        h1 = np.maximum(z1, 0) * d1
        z2 = h1 @ params["w2"] + params["b2"]
        h2 = np.maximum(z2, 0) * d2
        z3 = h2 @ params["w3"] + params["b3"]
        p = 1 / (1 + np.exp(-np.clip(z3, -30, 30)))
        g3 = (p - y) * w / w.sum()
        grads = {"w3": h2.T @ g3, "b3": g3.sum(0)}
        g2 = (g3 @ params["w3"].T) * (z2 > 0) * d2
        grads.update(w2=h1.T @ g2, b2=g2.sum(0))
        g1 = (g2 @ params["w2"].T) * (z1 > 0) * d1
        grads.update(w1=x.T @ g1, b1=g1.sum(0))
        lr = 1e-3 * min(1.0, step / 500) * (0.1 if step > steps * 0.8 else 1.0)
        for k in params:
            grad = grads[k] + (1e-4 * params[k] if k.startswith("w") else 0)
            m[k] = 0.9 * m[k] + 0.1 * grad
            v[k] = 0.999 * v[k] + 0.001 * grad ** 2
            fitted = m[k] / (1 - 0.9 ** step)
            params[k] -= lr * fitted / (np.sqrt(v[k] / (1 - 0.999 ** step)) + 1e-8)
        if step % 2000 == 0:
            each = y * np.log(p + 1e-7) + (1 - y) * np.log(1 - p + 1e-7)
            loss = float(-(w * each).sum() / w.sum())
            print(f"  step {step}: loss {loss:.4f}", flush=True)
    return wake.Head(**params, mean=mean, scale=scale)


def mine(head: wake.Head, pools: dict[str, tuple[np.ndarray, float]], *, top: int = 30_000,
         floor: float = 0.02) -> np.ndarray:
    """The negatives a head wrongly scores highest, from every pool: what
    the next round must learn not to fire on."""
    found = []
    for windows, _ in pools.values():
        s = scores(head, windows)
        idx = np.nonzero(s >= floor)[0]
        found.append((s[idx], windows, idx))
    ranked = sorted(((score, k, i) for k, (ss, _, ii) in enumerate(found)
                     for score, i in zip(ss, ii, strict=True)), reverse=True)[:top]
    if not ranked:
        return np.zeros((0, wake.WINDOW, wake.EMBED_DIM), np.float16)
    return np.stack([np.asarray(found[k][1][i], dtype=np.float16) for _, k, i in ranked])


def scores(head: wake.Head, windows: np.ndarray, batch: int = 20_000) -> np.ndarray:
    return np.concatenate([head.scores(windows[i:i + batch]) for i in range(0, len(windows), batch)]
                          ) if len(windows) else np.zeros(0)


def false_wakes(stream_scores: np.ndarray, threshold: float) -> int:
    """Events in a stream scored every 80 ms, counted as the detector would:
    one per crossing, then quiet for REFRACTORY_S."""
    quiet = int(wake.REFRACTORY_S / 0.08)
    count, until = 0, -1
    for i in np.nonzero(stream_scores >= threshold)[0]:
        if i > until:
            count += 1
            until = i + quiet
    return count


def stream_scores(head: wake.Head, embeddings: np.ndarray, batch: int = 20_000) -> np.ndarray:
    """Every window of a stream, scored in batches: the whole validation set
    as windows at once would be 3 GB."""
    if len(embeddings) < wake.WINDOW:
        return np.zeros(0)
    view = np.lib.stride_tricks.sliding_window_view(embeddings, wake.WINDOW, axis=0)
    return np.concatenate([
        head.scores(np.ascontiguousarray(view[i:i + batch].transpose(0, 2, 1), dtype=np.float32))
        for i in range(0, len(view), batch)])


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


FEATURES = "features.npz"
DESK_FEATURES = "desk_features.npz"


def _phrases(voices: Voices, rng: np.random.Generator, pool: list[str],
             n: int) -> list[np.ndarray]:
    """`n` "hey Steve"s in voices from `pool`, as people say it."""
    out = []
    while len(out) < n:
        speed = rng.uniform(0.8, 1.3)
        if rng.random() < 0.3:
            said = voices.say(POSITIVE_PHONEMES[rng.integers(len(POSITIVE_PHONEMES))],
                              voices.style(rng, pool), speed, phonemes=True)
        else:
            said = voices.say(POSITIVE_TEXTS[rng.integers(len(POSITIVE_TEXTS))],
                              voices.style(rng, pool), speed)
        if said is not None and 0.3 * SR < len(said) < 1.4 * SR:
            out.append(said)
    return out


def _sentences(voices: Voices, rng: np.random.Generator, texts,
               pool: list[str]) -> list[np.ndarray]:
    out = []
    for text in texts:
        said = voices.say(text, voices.style(rng, pool), rng.uniform(0.85, 1.25))
        if said is not None and len(said):
            out.append(said)
    return out


def make_features(quick: bool) -> dict[str, np.ndarray]:
    """Everything the head learns and is measured from, as embeddings: the
    hour of making voices and hearing them in rooms, done once
    (`--retrain` reuses it). The desk's own voice is `make_desk_features`."""
    rng = np.random.default_rng(5)
    started = time.perf_counter()
    print("voices...", flush=True)
    voices = Voices()
    train_pool = [v for v in voices.english if v not in HELD_OUT_VOICES]
    held_pool = [v for v in voices.english if v in HELD_OUT_VOICES]

    positives_said = _phrases(voices, rng, train_pool, 120 if quick else 1500)
    held_said = _phrases(voices, rng, held_pool, 40 if quick else 300)
    following = _sentences(voices, rng, FOLLOWING * (1 if quick else 4), voices.english)
    near = _sentences(voices, rng, NEAR_MISSES * (1 if quick else 12), train_pool)
    near_held = _sentences(voices, rng, NEAR_MISSES * (1 if quick else 3), held_pool)
    other = _sentences(voices, rng,
                       list(REQUESTS) * (1 if quick else 8) + prose(150 if quick else 2500, rng),
                       voices.english)
    print(f"  said {len(positives_said)} + {len(held_said)} held-out phrases, "
          f"{len(near)} near misses, {len(other)} other sentences "
          f"({time.perf_counter() - started:.0f} s)", flush=True)
    voices.kokoro = None  # the GPU's back for the workers' turn

    libri_train, libri_test = libri_files()
    if quick:
        libri_train, libri_test = libri_train[:60], libri_test[:30]
    seeds = iter(range(10**9))
    per = 3 if quick else 12
    print("hearing them...", flush=True)
    sets = {"train": positives_said, "held": held_said}
    with mp.Pool(min(14, mp.cpu_count() - 2), initializer=_init,
                 initargs=(libri_train, sets, following)) as pool:
        def run(fn, tasks):
            return pool.map(fn, tasks, chunksize=8)

        found = {
            "pos": np.stack(run(_positive, [("train", i, next(seeds))
                                            for i in range(len(positives_said))
                                            for _ in range(per)])),
            "partial": np.stack(run(_partial, [("train", i, next(seeds))
                                               for i in range(len(positives_said))
                                               for _ in range(max(1, per // 3))])),
            "held": np.stack(run(_positive, [("held", i, next(seeds))
                                             for i in range(len(held_said)) for _ in range(4)])),
            "near": np.concatenate(run(_speech, [(a, next(seeds), 2) for a in near
                                                 for _ in range(2)])),
            "near_held": np.concatenate(run(_speech, [(a, next(seeds), 1) for a in near_held])),
            "other": np.concatenate(run(_speech, [(a, next(seeds), 3) for a in other])),
            "libri": np.concatenate(run(_libri, [(p, next(seeds), 4) for p in libri_train])),
        }
        # The held-back speakers, whole and in order: false wakes are
        # counted on streams, as the detector meets them.
        tests = run(_libri_stream, [(p, 10**6 + k) for k, p in enumerate(libri_test)])
    found["test"] = np.concatenate(tests)
    found["test_lengths"] = np.array([len(t) for t in tests])
    print("  " + ", ".join(f"{k} {len(v)}" for k, v in found.items() if k != "test_lengths")
          + f" ({time.perf_counter() - started:.0f} s)", flush=True)
    return found


def _libri_stream(task: tuple) -> np.ndarray:
    import soundfile as sf

    path, seed = task
    audio, _ = sf.read(str(path), dtype="float32")
    return _W["front"].stream(_W["room"].mix(audio, np.random.default_rng(seed))).astype(
        np.float16)


def make_desk_features(quick: bool) -> dict[str, np.ndarray]:
    """The desk's own voice in rooms, before and after the canceller, and his
    "hey Steve" over it (`_over_its_voice`), as embeddings; made once and
    kept (`DESK_FEATURES`). A fifth of its sentences, and five voices for
    him, are kept back to measure on."""
    from voice import tts

    rng = np.random.default_rng(11)
    started = time.perf_counter()
    print("the desk's voice...", flush=True)
    voices = Voices()
    train_pool = [v for v in voices.english if v not in HELD_OUT_VOICES]
    held_pool = [v for v in voices.english if v in HELD_OUT_VOICES]
    desk = []
    for text in list(DESK_ANSWERS) * (1 if quick else 4) + prose(60 if quick else 700, rng):
        voice = tts.VOICE if rng.random() < DESK_SHARE else voices.style(rng, voices.english)
        said = voices.say(text, voice, rng.uniform(0.9, 1.15))
        if said is not None and len(said) > SR // 2:
            # As 16-bit, half the size: every worker process gets a copy.
            desk.append((np.clip(said, -1, 1) * 32767).astype(np.int16))
    sets = {"train": _phrases(voices, rng, train_pool, 40 if quick else 500),
            "held": _phrases(voices, rng, held_pool, 20 if quick else 150)}
    following = _sentences(voices, rng, FOLLOWING, voices.english)
    voices.kokoro = None
    print(f"  said {len(desk)} of its sentences ({time.perf_counter() - started:.0f} s)",
          flush=True)

    held_lines = list(range(0, len(desk), 5))
    train_lines = [k for k in range(len(desk)) if k % 5]

    def lines(pool: list[int], seconds: float) -> list[int]:
        out, total = [], 0.0
        while total < seconds:
            out.append(pool[rng.integers(len(pool))])
            total += len(desk[out[-1]]) / SR + 0.5
        return out

    seeds = iter(range(10**9))
    print("hearing it in rooms...", flush=True)
    # Fewer workers than `make_features`: a copy of the desk's sentences each.
    with mp.Pool(min(8, mp.cpu_count() - 4), initializer=_init,
                 initargs=([], sets, following, desk)) as pool:
        def run(tasks, chunksize):
            return pool.map(_over_its_voice, tasks, chunksize=chunksize)

        talk = run([(lines(train_lines, rng.uniform(30, 70)), None, next(seeds))
                    for _ in range(12 if quick else 260)], 2)
        talk_held = run([(lines(held_lines, rng.uniform(30, 70)), None, next(seeds))
                         for _ in range(6 if quick else 90)], 2)
        over = run([(lines(train_lines, rng.uniform(7, 16)), ("train", i), next(seeds))
                    for i in range(len(sets["train"])) for _ in range(1 if quick else 4)], 8)
        over_held = run([(lines(held_lines, rng.uniform(7, 16)), ("held", i), next(seeds))
                         for i in range(len(sets["held"])) for _ in range(2)], 8)
    # The raw microphone's positives only where he could be heard at all:
    # under an echo louder than he is, before the canceller, "hey Steve" is
    # not there to learn, and a head taught to find it there fires on the
    # desk's voice.
    found = {
        "desk": np.concatenate([_windows(cleaned, 2) for cleaned, _ in talk]),
        "desk_raw": np.concatenate([_windows(raw, 2) for _, raw in talk]),
        "desk_test": np.concatenate([cleaned for cleaned, _ in talk_held]),
        "desk_raw_test": np.concatenate([raw for _, raw in talk_held]),
        "desk_test_lengths": np.array([len(cleaned) for cleaned, _ in talk_held]),
        "pos_over": np.stack([cleaned for cleaned, _, _ in over]),
        "pos_over_raw": np.stack([raw for _, raw, sir in over if sir >= -3]),
        "held_over": np.stack([cleaned for cleaned, _, _ in over_held]),
        "held_over_raw": np.stack([raw for _, raw, _ in over_held]),
    }
    print("  " + ", ".join(f"{k} {len(v)}" for k, v in found.items() if "lengths" not in k)
          + f" ({time.perf_counter() - started:.0f} s)", flush=True)
    return found


def measure(head: wake.Head, f: dict[str, np.ndarray], d: dict[str, np.ndarray]) -> list[dict]:
    """The head at every threshold, on all that was kept back: new voices in
    quiet and over the desk, and false wakes an hour of ordinary audio, of
    LibriSpeech's held-back readers and of the desk talking.

    Over the desk a wake on either path counts, as the ear counts it
    (`voice/listen.RAW_WAKES`); the laptop's own copy is not checked here,
    so the desk's false wakes are if anything overcounted."""
    def streams(x: np.ndarray, lengths: np.ndarray) -> tuple[list[np.ndarray], float]:
        bounds = np.cumsum(np.concatenate([[0], lengths]))
        x = x.astype(np.float32)
        return ([stream_scores(head, x[a:b]) for a, b in zip(bounds[:-1], bounds[1:],
                                                             strict=True)],
                len(x) * 0.08 / 3600)

    validation = np.load(stored(VALIDATION), mmap_mode="r")
    val_scores = stream_scores(head, np.asarray(validation, dtype=np.float32))
    val_hours = len(validation) * 0.08 / 3600
    test_scores, test_hours = streams(f["test"], f["test_lengths"])
    desk_scores, desk_hours = streams(d["desk_test"], d["desk_test_lengths"])
    raw_scores, _ = streams(d["desk_raw_test"], d["desk_test_lengths"])
    either = [np.maximum(c, r) for c, r in zip(desk_scores, raw_scores, strict=True)]
    held_scores = scores(head, f["held"].astype(np.float32))
    near_held_scores = scores(head, f["near_held"].astype(np.float32))
    over = scores(head, d["held_over"].astype(np.float32))
    over_raw = scores(head, d["held_over_raw"].astype(np.float32))

    def per_hour(streams_: list[np.ndarray], t: float, hours: float) -> float:
        return round(sum(false_wakes(s, t) for s in streams_) / hours, 2)

    grid = [*np.round(np.arange(0.05, 0.96, 0.05), 2), 0.97, 0.98, 0.99, 0.995]
    return [{
        "threshold": float(t),
        "recall_held_out_voices": round(float((held_scores >= t).mean()), 4),
        "recall_over_its_voice": round(float((np.maximum(over, over_raw) >= t).mean()), 4),
        "recall_over_its_voice_cleaned": round(float((over >= t).mean()), 4),
        "false_wakes_per_hour_validation": round(false_wakes(val_scores, t) / val_hours, 2),
        "false_wakes_per_hour_librispeech": per_hour(test_scores, t, test_hours),
        "false_wakes_per_hour_its_voice": per_hour(either, t, desk_hours),
        "false_wakes_per_hour_its_voice_cleaned": per_hour(desk_scores, t, desk_hours),
        "near_miss_windows_firing": round(float((near_held_scores >= t).mean()), 4),
    } for t in grid]


def choose(table: list[dict]) -> tuple[dict, dict]:
    """The bar in quiet and the bar while the desk talks.

    In quiet a false wake costs ~0.3 s of local Whisper and nothing else (it
    must hear the phrase, or the capture is dropped unread), so the line is a
    couple an hour of ordinary audio and a few more of non-stop speech -- not
    zero. Held to two on both, the 5 October run chose 0.995 and heard 71% of
    new voices; at this line it chose 0.9 and heard 83%.

    While the desk talks a false wake cuts its answer, so the line is one an
    hour of its speech, on either path, and no more than five an hour of
    ordinary audio (what the old fixed bar of 0.7 allowed); never above the
    quiet bar, which the ear would keep."""
    fine = [r for r in table if r["false_wakes_per_hour_validation"] <= 2.0
            and r["false_wakes_per_hour_librispeech"] <= 8.0]
    quiet = max(fine, key=lambda r: (r["recall_held_out_voices"], r["threshold"])) if fine \
        else min(table, key=lambda r: r["false_wakes_per_hour_validation"])
    fine = [r for r in table if r["threshold"] <= quiet["threshold"]
            and r["false_wakes_per_hour_its_voice"] <= 1.0
            and r["false_wakes_per_hour_validation"] <= 5.0]
    over = max(fine, key=lambda r: (r["recall_over_its_voice"], r["threshold"])) if fine \
        else quiet
    return quiet, over


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--download", action="store_true", help="fetch the data and stop")
    parser.add_argument("--quick", action="store_true", help="a small run, to check the pipeline")
    parser.add_argument("--retrain", action="store_true",
                        help="reuse the features of the last full run; only train and measure")
    parser.add_argument("--steps", type=int, default=24_000)
    parser.add_argument("--neg-weight", type=float, default=30.0)
    parser.add_argument("--rounds", type=int, default=2, help="rounds of hard-negative mining")
    parser.add_argument("--out", type=Path, help="where to write the head (default: in use)")
    parser.add_argument("--compare", type=Path,
                        help="also measure this head (the one in use) on the same data")
    args = parser.parse_args()
    if args.download:
        download()
        return
    started = time.perf_counter()
    if args.retrain:
        with np.load(stored(FEATURES)) as saved:
            f = {k: saved[k] for k in saved.files}
    else:
        f = make_features(args.quick)
        if not args.quick:
            np.savez(stored(FEATURES), **f)
    if stored(DESK_FEATURES).exists() and not args.quick:
        with np.load(stored(DESK_FEATURES)) as saved:
            d = {k: saved[k] for k in saved.files}
    else:
        d = make_desk_features(args.quick)
        if not args.quick:
            np.savez(stored(DESK_FEATURES), **d)

    # Into memory, whole: drawn at random from the memory map, a batch was a
    # few hundred page faults, and 24,000 steps took 24 minutes, not 4.
    acav = np.ascontiguousarray(acav_windows())
    pools = {
        "acav": (acav, 0.35),
        "libri": (f["libri"], 0.15),
        "other_tts": (f["other"], 0.25),
        "near": (f["near"], 0.15),
        "partial": (f["partial"], 0.10),
        "its_voice": (d["desk"], 0.20),
        "its_voice_raw": (d["desk_raw"], 0.10),
    }
    pos = np.concatenate([f["pos"], d["pos_over"], d["pos_over_raw"]])
    steps = 2000 if args.quick else args.steps
    print("training...", flush=True)
    head = train(pos, pools, steps=steps, neg_weight=args.neg_weight)
    for round_ in range(args.rounds):
        hard = mine(head, pools)
        print(f"  round {round_ + 1}: {len(hard)} hard negatives", flush=True)
        if not len(hard):
            break
        pools["hard"] = (hard if "hard" not in pools else np.concatenate(
            [pools["hard"][0], hard]), 0.30)
        head = train(pos, pools, steps=steps, neg_weight=args.neg_weight, seed=round_ + 1)

    print("measuring...", flush=True)
    table = measure(head, f, d)
    chosen, over = choose(table)
    head.threshold, head.over_voice = chosen["threshold"], over["threshold"]
    for row in table:
        print("  " + "  ".join(f"{k}={v}" for k, v in row.items()))
    if args.compare:
        old = wake.Head.load(args.compare)
        old_table = measure(old, f, d)
        print(f"{args.compare}, on the same data:", flush=True)
        for row in old_table:
            print("  " + "  ".join(f"{k}={v}" for k, v in row.items()))

    out = args.out or (stored() / "hey_steve_quick.npz" if args.quick
                       else wake.model_dir() / wake.HEAD_FILE)
    head.save(out)
    sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                         cwd=paths.SOURCE).stdout.strip()
    report = {
        "trained": dt.datetime.now().isoformat(timespec="seconds"),
        "commit": sha,
        "chosen": chosen,
        "over_voice": over,
        "table": table,
        "settings": {"steps": steps, "neg_weight": args.neg_weight, "rounds": args.rounds},
        "data": {**{k: len(v) for k, v in {**f, **d}.items() if "lengths" not in k},
                 "acav": len(acav), "hard": len(pools.get("hard", ([],))[0]),
                 "held_out_voices": list(HELD_OUT_VOICES),
                 "validation_hours": round(len(np.load(stored(VALIDATION), mmap_mode="r")) * 0.08
                                           / 3600, 2),
                 "librispeech_test_hours": round(len(f["test"]) * 0.08 / 3600, 2),
                 "its_voice_test_hours": round(len(d["desk_test"]) * 0.08 / 3600, 2)},
        "seconds": round(time.perf_counter() - started),
    }
    out.with_suffix(".json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8",
                                        newline="\n")
    print(f"wrote {out}: threshold {head.threshold}, over its voice {head.over_voice}; "
          f"{chosen}; {over}", flush=True)


if __name__ == "__main__":
    main()
