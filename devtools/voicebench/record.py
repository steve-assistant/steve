"""Recording the maintainer's voice and room for the voice bench, by hand.

    python -m devtools.voicebench.record a    # wake takes, about 10 minutes
    python -m devtools.voicebench.record b    # 60 requests, Enter at each end, 15 minutes
    python -m devtools.voicebench.record c    # echo: plays the desk's answers, 10 minutes
    python -m devtools.voicebench.record d    # an hour of the room, unattended
    python -m devtools.voicebench.record e    # push-to-talk in the app, 5 minutes

Sessions a to d need soundcard, which only the voice extra holds: `uv run
--with soundcard python -m devtools.voicebench.record a` brings it for the
run. Session e opens no device: the app hears each request, and this reads
the timings the worker logs for it ("push-to-talk turn:" lines, from
`agent/worker/desk.log_hotkey_turn`), never the words, and scores
push-to-talk's exit (`docs/plan/07-voice.md`, section 1).

The design step 7 replaces was tuned on 44 takes from one day, and its end
of turn on how a few turns felt: nothing kept what it was judged on, so no
change could be scored against the one before it. These sessions are that
record (`docs/plan/07-voice.md`, section 4, says what each decides). Each
session's folder holds 16 kHz mono WAVs and a manifest saying what every
file is: which take, where the speaker pressed Enter, when the answer began
to play. The manifest is written again after every take, so a session
stopped with Ctrl+C keeps what it recorded.

**This opens the microphone, and session c plays sound: only the maintainer
runs it.** Session e has the app open it, which is the same. Nothing opens a
device on import, and the tests use fakes. The recordings go to
`private/voice/` in the main checkout, which git ignores:
not a worktree's own `private/`, which goes when the worktree is cleaned up,
and the recordings with it. A folder git would commit is refused. Nothing
is sent anywhere, and nothing recorded enters the repo or the product.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import ctypes
import json
import random
import subprocess
import threading
import time
import wave
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
SR = 16_000  # voice/wake.SAMPLE_RATE: what the wake head and the turn judge hear
LOOPBACK_SR = 48_000  # the speakers' loopback opens at their own rate (voice/listen.py)
SETTLE_S = 1.5  # a microphone's first moments are not its sound (voice/listen.SETTLE_S)
READ_S = 0.1
REOPEN_S = 0.5  # after a reset, before opening the device again
GIVE_UP_S = 30.0  # a device that won't reopen for this long ends the session
GAP_S = 1.0  # between takes: time to breathe, and a gap no two clips share
TAIL_S = 1.5  # kept after Enter in session b: the end the bench looks for is before it
LEAD_S = 0.5  # session c: the room before the answer starts
TRIALS = 20
CHUNK_S = 300.0  # session d's files: a crash loses five minutes, not the hour
CHOOSE = "Press Enter to start, s and Enter to skip, q and Enter to stop: "

SESSIONS = {"a": "wake takes", "b": "natural requests", "c": "echo", "d": "the room",
            "e": "push-to-talk"}
TURN_LINE = "push-to-talk turn: "  # agent/worker/desk.log_hotkey_turn

WAKE = (
    ("plain", 25, 2.5, 'Say "hey Steve", as you would to the desk, from where you sit.'),
    ("request", 10, 4.5, 'Say "hey Steve" and then the request shown, without a pause.'),
    ("far", 10, 2.5, 'Step two or three metres back, or turn away, and say "hey Steve".'),
    ("media", 10, 2.5, "Play music or TV in the room as you usually do (not from this "
                       'laptop), and say "hey Steve" over it.'),
    ("near", 1, 60.0, "Read the paragraph aloud at your normal pace. It is full of near "
                      'misses: do not say "hey Steve".'),
)

REQUESTS = (
    "what's the weather tomorrow",
    "set a timer for ten minutes",
    "what's on my calendar this afternoon",
    "remind me to water the plants at six",
    "read me my newest email",
    "how long is the drive to the airport",
    "add olive oil to the shopping list",
    "what time is it in Tokyo",
    "tell them I'm running ten minutes late",
    "turn that down a bit",
)

NEAR_MISSES = """\
They've said the stove needs a new valve, so we'll heave it out on Thursday.
Hey, see if the sleeve fits before you leave. The list for the eve of the
trip said pack the stereo, but the steamer came first. Hey, Stella, did you
save the even sheets? I believe we'd better weave the cable behind the
stand. A steep street, a stray sleeve, and they've all gone home. Hey, stay
with me. Every evening the steady breeze leaves the eaves wet. Hey, Stan,
have you seen the steel sieve? Stephen's steak was a steal, they say."""

# Situations, not scripts: a request read aloud has none of the pauses the
# end of turn has to wait through. The second part invites a request in two
# parts, said as the thought comes.
SITUATIONS: tuple[tuple[str, bool], ...] = (
    ("You want to know whether you need an umbrella tomorrow.", False),
    ("Pasta is on the stove, and you want a timer for the time on the packet.", False),
    ("You can't remember what's on your calendar this afternoon.", False),
    ("You want a reminder to water the plants this evening.", False),
    ("You want to hear your newest email.", False),
    ("You want to know how long the drive to the airport takes right now.", False),
    ("You've run out of olive oil and want it on the shopping list.", False),
    ("You want to know what time it is in Tokyo.", False),
    ("You're running late and want to tell the person you're meeting.", False),
    ("You want to know when the pharmacy closes tonight.", False),
    ("You want the meaning of a word you just read.", False),
    ("You want to move tomorrow's dentist appointment an hour later.", False),
    ("You want to know whether you answered a neighbour's email.", False),
    ("You want a short summary of today's news.", False),
    ("A recipe gives grams of flour, and you want cups.", False),
    ("You want a reminder to take the bins out on Thursday night.", False),
    ("You want to know what the weekend's weather looks like.", False),
    ("You want the train times into town tomorrow morning.", False),
    ("You want to know how much you spent on groceries last month.", False),
    ("You're cooking and want to know what to use instead of buttermilk.", False),
    ("You want to know if anything urgent came in by email today.", False),
    ("You want two hours on Friday blocked out for focused work.", False),
    ("You want to know who sent the message you missed this morning.", False),
    ("You want to hear what tomorrow's first meeting is.", False),
    ("You want a reminder to call a friend on their birthday next week.", False),
    ("You want to know how many days are left in the month.", False),
    ("You want to check how tall the highest mountain in the world is.", False),
    ("You want the timer you set earlier cancelled.", False),
    ("You want an idea noted down before you forget it.", False),
    ("You want to know whether a parcel you're expecting has shipped.", False),
    ("You want a polite reply drafted, turning down an invitation.", False),
    ("You want to know when the sun sets today.", False),
    ("You want a gym session on your calendar tomorrow at seven.", False),
    ("You want to know what you have on next Monday.", False),
    ("You want a hardware store nearby that's open now.", False),
    ("You want to know how to say thank you in Portuguese.", False),
    ("You want a reminder to stretch every hour this afternoon.", False),
    ("You want to know whether it will be windy for a bike ride later.", False),
    ("You didn't catch the last thing the desk said, and want it again.", False),
    ("The desk is talking, and you want it to stop.", False),
    ("You want a reminder, but have to think for a moment about which day suits.", True),
    ("You ask about tomorrow's weather, then add a question about the weekend.", True),
    ("You want a message to a neighbour, and work out what to say as you go.", True),
    ("You ask for a dinner idea, then add what you already have in the fridge.", True),
    ("You want to move a meeting, and aren't sure yet to which day.", True),
    ("Three things belong on the shopping list, and you remember them one at a time.",
     True),
    ("You want a restaurant: first the area, then, after a moment, the kind of food.", True),
    ("You're replying to an email and change your mind halfway about how firm to be.",
     True),
    ("You ask how long a trip takes, then remember you'd leave at rush hour.", True),
    ("You set a timer, then realise you need a second one for something else.", True),
    ("You're choosing between two weekend plans, and describe one, then the other.", True),
    ("You ask about tomorrow, then whether there's room in it for a long lunch.", True),
    ("You want a reminder to buy a present, and stop to think who for and by when.",
     True),
    ("You want to look up something you half remember, and search for its name as "
     "you talk.", True),
    ("You start asking for one thing, stop, and ask for something a little different.",
     True),
    ("You want to see a friend, and think aloud about which evening works.", True),
    ("You want a note written down: the first part, a pause, then the rest.", True),
    ("A tap is dripping, and you ask how to fix it, adding details as they come.", True),
    ("You cancel something on your calendar, then ask what that frees up.", True),
    ("Your question needs a number you have to look up while you talk.", True),
)

ANSWERS = (
    "Tomorrow looks dry in the morning, with clouds building after lunch and a chance of "
    "showers by the evening. The high is about eighteen degrees, so a light jacket should do.",
    "You have three things this afternoon. A design review at two, a call at half past "
    "three that's marked optional, and a reminder at five to collect the dry cleaning.",
    "Your newest email is from the bike shop. The repair is finished, and you can collect "
    "it any time before six. They've attached the invoice, which comes to forty-two dollars.",
    "Here's a quick idea for dinner. Soften an onion and some garlic, add a tin of chopped "
    "tomatoes and a pinch of chili, simmer it for ten minutes, then stir in the pasta.",
    "The drive to the airport is about thirty-five minutes right now. There's slow traffic "
    "on the bridge, so if you leave at four you should arrive a little before five.",
    "The pharmacy on the corner closes at nine tonight. Tomorrow it opens at eight, and on "
    "Sunday it keeps shorter hours, from ten until four.",
    "I've drafted a reply. It thanks them for the invitation, says you can't make Saturday "
    "because of an earlier plan, and suggests the following week instead. Shall I send it?",
    "In the news today: a new bridge opened after three years of work, the council approved "
    "money for more bike lanes, and the local team won its third game in a row.",
    "There are twelve days left in the month. Your next bill is due in five of them, and "
    "the subscription you asked about renews on the last day.",
    "A dripping tap usually means a worn washer. Turn off the water under the sink, unscrew "
    "the handle, replace the washer, and put it back together. It takes about twenty minutes.",
)

# Windows' own voice, into files: making the answers plays nothing, and needs
# nothing installed. The job's path is the only thing put into the script,
# quoted for PowerShell; the texts travel in the job file, never the command.
SPEAK_SCRIPT = """\
Add-Type -AssemblyName System.Speech
$job = Get-Content -Raw -Encoding UTF8 -LiteralPath '{job}' | ConvertFrom-Json
$format = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000,
    [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
    [System.Speech.AudioFormat.AudioChannel]::Mono)
$voice = New-Object System.Speech.Synthesis.SpeechSynthesizer
$n = 0
foreach ($text in $job.texts) {
    $n++
    $voice.SetOutputToWaveFile((Join-Path $job.folder ('{0:d2}.wav' -f $n)), $format)
    $voice.Speak($text)
}
$voice.SetOutputToNull()
$voice.Dispose()
"""

INTRO_B = """\
Each line describes a situation, not words to say. Say what you would say to
the desk, the way you would say it, pauses and all, without "hey Steve".
Press Enter as soon as you have finished: where you press it is how the bench
knows the request was over."""

INTRO_C = """\
The laptop will play the desk's answers through its speakers, one at a time.
Set the volume where you usually have it. When NOW appears, say "hey Steve,
stop" as you would to make the desk stop, and let the answer play on."""

INTRO_D = """\
An hour of an ordinary evening at the desk: TV, music, a call, typing, as it
comes. Nobody else in the room who has not agreed to be recorded. You can
leave it running; Ctrl+C stops it and keeps what it has."""

OTHERS = ('Is every other voice assistant on this computer switched off? One listening for '
          '"hey Steve" would answer every take. [y/N] ')


# ---------------------------------------------------------------------------
# The devices
# ---------------------------------------------------------------------------


def _com_init() -> None:
    """soundcard talks to WASAPI through COM, which each thread must join
    (as voice/listen.py's threads do)."""
    with contextlib.suppress(AttributeError):
        ctypes.windll.ole32.CoInitializeEx(None, 0)


def load_soundcard():
    """soundcard, imported on a thread of its own that has never joined COM.

    Its import joins COM itself and raises if the thread has joined already:
    COM's "already joined" (S_FALSE) is not one of the answers it allows. The
    first real run, 16 October 2026, joined first in the capture thread and
    then imported, and stopped with "the device failed: Error 0x100000001".
    So nothing of ours imports it: this does, once, and the threads that use
    it are handed the module and join COM for themselves."""
    loaded: dict = {}

    def load() -> None:
        try:
            import soundcard

            loaded["module"] = soundcard
        except BaseException as exc:  # raised below, on the caller's thread
            loaded["error"] = exc

    thread = threading.Thread(target=load, name="voicebench-load")
    thread.start()
    thread.join()
    if "error" in loaded:
        raise loaded["error"]
    return loaded["module"]


class DeviceFailed(RuntimeError):
    """A device would not open, or would not open again after a reset."""


class Capture:
    """A microphone, or the speakers' loopback, read on its own thread for
    the whole session and kept only between `start` and `stop`: opened per
    take, its first moments would be in every take.

    Windows invalidates a stream when the default device changes or another
    program takes the device (AUDCLNT_E_DEVICE_INVALIDATED, 0x88890004). The
    first full run of session a lost its last take to one, 16 October 2026,
    and the error soundcard raised while closing the dead stream hid it. So a
    failed read closes the stream quietly, opens the default device again,
    and counts a reset: a session takes again what a reset fell inside
    (`resets`). Only a device that won't reopen in `GIVE_UP_S` ends one."""

    def __init__(self, soundcard, *, loopback: bool = False) -> None:
        self.rate = LOOPBACK_SR if loopback else SR
        self.name = ""
        self.resets = 0
        self.last_reset = ""
        self._soundcard = soundcard
        self._loopback = loopback
        self._lock = threading.Lock()
        self._kept: list[np.ndarray] | None = None
        self._frames = 0
        self._arrived = 0.0
        self._closed = False
        self._error: BaseException | None = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._read, daemon=True,
                                        name="voicebench-capture")
        self._thread.start()
        if not self._ready.wait(15):
            raise DeviceFailed("the device did not open in 15 s")
        self._raise()

    def _device(self):
        soundcard = self._soundcard
        if self._loopback:
            speaker = soundcard.default_speaker()
            return soundcard.get_microphone(id=str(speaker.name), include_loopback=True)
        return soundcard.default_microphone()

    def _read(self) -> None:
        _com_init()
        frames = int(self.rate * READ_S)
        opened_once = False
        failing_since: float | None = None
        while not self._closed:
            try:
                device = self._device()
                handle = device.recorder(samplerate=self.rate, channels=1, blocksize=frames)
                recorder = handle.__enter__()
            except Exception as exc:
                now = time.monotonic()
                failing_since = failing_since or now
                if not opened_once or now - failing_since > GIVE_UP_S:
                    self._error = exc  # raised on the main thread, at its next call
                    self._ready.set()
                    return
                time.sleep(REOPEN_S)
                continue
            try:
                self.name = str(device.name)
                recorder.record(numframes=int(self.rate * SETTLE_S))
                opened_once, failing_since = True, None
                self._ready.set()
                while not self._closed:
                    data = np.ascontiguousarray(recorder.record(numframes=frames)[:, 0],
                                                dtype=np.float32)
                    with self._lock:
                        self._arrived = time.monotonic()
                        if self._kept is not None:
                            self._kept.append(data)
                            self._frames += len(data)
            except Exception as exc:
                with self._lock:
                    self.resets += 1
                    self.last_reset = str(exc)
                time.sleep(REOPEN_S)
            finally:
                # A dead stream's close raises too, and would hide why it died.
                with contextlib.suppress(Exception):
                    handle.__exit__(None, None, None)

    def _raise(self) -> None:
        if self._error is not None:
            raise DeviceFailed(f"the device failed: {self._error}") from self._error

    def start(self) -> None:
        with self._lock:
            self._kept, self._frames = [], 0

    def elapsed(self) -> float:
        """Seconds kept so far, to within one read: what has arrived, and the
        time since it did."""
        with self._lock:
            if self._kept is None:
                return 0.0
            since = min(max(time.monotonic() - self._arrived, 0.0), READ_S)
            return self._frames / self.rate + since

    def cut(self) -> np.ndarray:
        """What was kept since `start` or the last cut, keeping on."""
        self._raise()
        with self._lock:
            kept, self._kept, self._frames = self._kept or [], [], 0
        return np.concatenate(kept) if kept else np.zeros(0, np.float32)

    def stop(self) -> np.ndarray:
        audio = self.cut()
        with self._lock:
            self._kept = None
        return audio

    def wait(self, seconds: float) -> None:
        time.sleep(seconds)

    def close(self) -> None:
        self._closed = True
        self._thread.join(2)


class Speaker:
    """The default speakers, playing one answer on a thread, so the prompt
    can come while it plays."""

    def __init__(self, soundcard) -> None:
        self._soundcard = soundcard
        self._thread: threading.Thread | None = None
        self._error: BaseException | None = None

    def play(self, audio: np.ndarray, rate: int) -> None:
        soundcard = self._soundcard

        def run() -> None:
            try:
                _com_init()
                with soundcard.default_speaker().player(samplerate=rate, channels=1) as player:
                    player.play(audio)
            except Exception as exc:  # raised on the main thread by `wait`
                self._error = exc

        self._thread = threading.Thread(target=run, daemon=True, name="voicebench-play")
        self._thread.start()

    def wait(self) -> None:
        if self._thread is not None:
            self._thread.join()
        if self._error is not None:
            raise RuntimeError(f"the speakers failed: {self._error}") from self._error


class Devices:
    def __init__(self, load=load_soundcard) -> None:
        self._soundcard = load()

    def mic(self) -> Capture:
        return Capture(self._soundcard)

    def loopback(self) -> Capture:
        return Capture(self._soundcard, loopback=True)

    def speaker(self) -> Speaker:
        return Speaker(self._soundcard)


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


def write_wav(path: Path, audio: np.ndarray, rate: int = SR) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(pcm.tobytes())


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    """A mono 16-bit WAV as float32, and its rate."""
    with wave.open(str(path), "rb") as src:
        if src.getnchannels() != 1 or src.getsampwidth() != 2:
            raise ValueError(f"{path.name}: not mono 16-bit")
        pcm = np.frombuffer(src.readframes(src.getnframes()), np.int16)
        return pcm.astype(np.float32) / 32767, src.getframerate()


def _manifest(session: str, mic) -> dict:
    return {"session": session, "what": SESSIONS[session],
            "started": datetime.now(UTC).isoformat(timespec="seconds"),
            "microphone": mic.name, "rate": SR, "takes": [], "stopped_early": False}


def _save(folder: Path, manifest: dict) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n",
                                          encoding="utf-8", newline="\n")


def default_root(here: Path = ROOT) -> Path:
    """`private/voice/` in the main checkout, found through git's common
    folder: run from a worktree, its own `private/` would be deleted with it."""
    try:
        common = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=here, capture_output=True, text=True, check=True, timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return here / "private" / "voice"
    main = Path(common).parent if Path(common).name == ".git" else here
    return main / "private" / "voice"


def would_commit(folder: Path) -> bool:
    """Whether git would take a file in `folder` into a checkout: True only
    inside a work tree that does not ignore it."""
    probe = folder / "probe.wav"
    near = next((p for p in (folder, *folder.parents) if p.is_dir()), None)
    if near is None:
        return False
    try:
        done = subprocess.run(["git", "check-ignore", "-q", str(probe)], cwd=near,
                              capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 1  # 0: ignored; 128: not in a work tree


def synthesize(texts, folder: Path, *, run=subprocess.run) -> list[Path]:
    """The desk's answers in Windows' own voice, as 16 kHz WAVs in `folder`."""
    folder.mkdir(parents=True, exist_ok=True)
    job = folder / "answers.json"
    job.write_text(json.dumps({"folder": str(folder), "texts": list(texts)}, indent=1) + "\n",
                   encoding="utf-8", newline="\n")
    script = SPEAK_SCRIPT.replace("{job}", str(job).replace("'", "''"))
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        check=True, capture_output=True, timeout=300)
    files = [folder / f"{n:02d}.wav" for n in range(1, len(texts) + 1)]
    missing = [f.name for f in files if not f.is_file()]
    if missing:
        raise RuntimeError(f"Windows' voice wrote no {', '.join(missing)}")
    return files


# ---------------------------------------------------------------------------
# The sessions
# ---------------------------------------------------------------------------


def _begin(session: str, mic, manifest: dict | None) -> dict:
    """A new session's manifest, or a resumed one's, ready for more takes."""
    if manifest is None:
        return _manifest(session, mic)
    manifest.setdefault("resumed", []).append(datetime.now(UTC).isoformat(timespec="seconds"))
    manifest["stopped_early"] = False
    manifest.pop("error", None)
    return manifest


def _recorded(manifest: dict) -> set[str]:
    return {take.get("file") or take.get("mic") for take in manifest["takes"]}


def _keep(folder: Path, manifest: dict, take: dict, mic) -> None:
    if mic.name != manifest["microphone"]:  # a reset can bring another default device
        take["microphone"] = mic.name
    manifest["takes"].append(take)
    _save(folder, manifest)


def _again(say, *devices) -> None:
    for device in devices:
        if device.last_reset:
            say(f"  Windows reset the device ({device.last_reset}); it is now "
                f"{device.name}. That take again.")
            device.last_reset = ""


def _stopped(manifest: dict, exc: BaseException, say) -> None:
    manifest["stopped_early"] = True
    if isinstance(exc, DeviceFailed):
        manifest["error"] = str(exc)
        say(f"\n{exc}. The takes so far are kept: --resume with this folder finishes it.")


def wake_takes(folder: Path, mic, *, ask=input, say=print, plan=WAKE,
               manifest: dict | None = None) -> dict:
    """Session a: "hey Steve" plain, run on into a request, from across the
    room and over music, and a paragraph of near misses."""
    manifest = _begin("a", mic, manifest)
    recorded = _recorded(manifest)
    try:
        for section, takes, seconds, what in plan:
            todo = [n for n in range(1, takes + 1) if f"{section}/{n:02d}.wav" not in recorded]
            if not todo:
                continue
            left = f" ({len(todo)} of {takes} left)" if len(todo) < takes else ""
            say(f"\n== {section}{left}: {what}")
            if section == "near":
                say("\n" + NEAR_MISSES)
            choice = ask(CHOOSE).strip().lower()
            if choice == "q":
                break
            if choice == "s":
                continue
            for n in todo:
                request = REQUESTS[(n - 1) % len(REQUESTS)] if section == "request" else None
                while True:
                    mic.wait(GAP_S)
                    resets = mic.resets
                    mic.start()
                    if request:
                        say(f"  {n}/{takes}  NOW: hey Steve, {request}")
                    elif takes > 1:
                        say(f"  {n}/{takes}  NOW")
                    else:
                        say(f"  recording {seconds:.0f} s ... NOW")
                    mic.wait(seconds)
                    audio = mic.stop()
                    if mic.resets == resets:
                        break
                    _again(say, mic)
                name = f"{section}/{n:02d}.wav"
                write_wav(folder / name, audio)
                take = {"file": name, "section": section, "seconds": seconds}
                if request:
                    take["request"] = request
                _keep(folder, manifest, take, mic)
    except (KeyboardInterrupt, DeviceFailed) as exc:
        _stopped(manifest, exc, say)
    _save(folder, manifest)
    return manifest


def requests(folder: Path, mic, *, ask=input, say=print, situations=SITUATIONS,
             manifest: dict | None = None) -> dict:
    """Session b: requests as they come, each ended by Enter, so the bench
    knows where every turn really ended and can score a judge's cut against
    it."""
    manifest = _begin("b", mic, manifest)
    manifest["tail_s"] = TAIL_S
    recorded = _recorded(manifest)
    say(INTRO_B)
    try:
        for n, (situation, two_part) in enumerate(situations, 1):
            name = f"{n:02d}.wav"
            if name in recorded:
                continue
            say(f"\n{n}/{len(situations)}  {situation}")
            choice = ask(CHOOSE).strip().lower()
            if choice == "q":
                break
            if choice == "s":
                continue
            while True:
                resets = mic.resets
                mic.start()
                say("  NOW. Press Enter when you have finished.")
                ask("")
                enter_s = mic.elapsed()
                mic.wait(TAIL_S)
                audio = mic.stop()
                if mic.resets == resets:
                    break
                _again(say, mic)
                ask("  Press Enter when you are ready to say it again: ")
            write_wav(folder / name, audio)
            _keep(folder, manifest, {"file": name, "situation": situation,
                                     "two_part": two_part, "enter_s": round(enter_s, 3)}, mic)
    except (KeyboardInterrupt, DeviceFailed) as exc:
        _stopped(manifest, exc, say)
    _save(folder, manifest)
    return manifest


def echo(folder: Path, mic, loopback, speaker, answers: list[Path], *, ask=input,
         say=print, rng: random.Random, trials: int = TRIALS,
         manifest: dict | None = None) -> dict:
    """Session c: "hey Steve, stop" over the desk's own voice, with the
    microphone and the speakers' loopback both kept, so a canceller can be
    scored on what it would have been given."""
    manifest = _begin("c", mic, manifest)
    manifest.setdefault("loopback", loopback.name)
    manifest.setdefault("loopback_rate", loopback.rate)
    manifest.setdefault("answers", [str(a) for a in answers])
    recorded = _recorded(manifest)
    todo = [n for n in range(1, trials + 1) if f"mic/{n:02d}.wav" not in recorded]
    if not todo:
        _save(folder, manifest)
        return manifest
    say(INTRO_C)
    if ask(CHOOSE).strip().lower() in ("s", "q"):
        _save(folder, manifest)
        return manifest
    try:
        for n in todo:
            answer = answers[(n - 1) % len(answers)]
            audio, rate = read_wav(answer)
            duration = len(audio) / rate
            while True:
                mic.wait(GAP_S)
                resets = (mic.resets, loopback.resets)
                mic.start()
                loopback.start()
                mic.wait(LEAD_S)
                play_s = mic.elapsed()
                speaker.play(audio, rate)
                mic.wait(rng.uniform(1.0, max(1.0, duration - 2.0)))
                say(f"  {n}/{trials}  NOW: hey Steve, stop")
                prompt_s = mic.elapsed()
                speaker.wait()
                mic.wait(TAIL_S)
                heard, played = mic.stop(), loopback.stop()
                if (mic.resets, loopback.resets) == resets:
                    break
                _again(say, mic, loopback)
            write_wav(folder / f"mic/{n:02d}.wav", heard)
            write_wav(folder / f"loopback/{n:02d}.wav", played, loopback.rate)
            _keep(folder, manifest, {
                "mic": f"mic/{n:02d}.wav", "loopback": f"loopback/{n:02d}.wav",
                "answer": answer.name, "play_s": round(play_s, 3),
                "prompt_s": round(prompt_s, 3)}, mic)
    except (KeyboardInterrupt, DeviceFailed) as exc:
        _stopped(manifest, exc, say)
    _save(folder, manifest)
    return manifest


def room(folder: Path, mic, *, ask=input, say=print, minutes: float = 60.0,
         chunk_s: float = CHUNK_S, manifest: dict | None = None) -> dict:
    """Session d: an hour of the room, for false wakes an hour, in files of
    five minutes. A reset inside a file leaves a gap, and the file says so:
    an hour is not taken again for one."""
    manifest = _begin("d", mic, manifest)
    minutes = manifest.setdefault("minutes", minutes)
    total = minutes * 60
    done = sum(take["seconds"] for take in manifest["takes"])
    if done >= total:
        _save(folder, manifest)
        return manifest
    say(INTRO_D)
    if ask("Press Enter to start, q and Enter to stop: ").strip().lower() == "q":
        _save(folder, manifest)
        return manifest

    def keep(audio: np.ndarray, resets: int) -> None:
        name = f"{len(manifest['takes']) + 1:02d}.wav"
        write_wav(folder / name, audio)
        take = {"file": name, "seconds": round(len(audio) / SR, 2)}
        if resets:
            take["resets"] = resets
        _keep(folder, manifest, take, mic)

    resets = mic.resets
    mic.start()
    try:
        while done < total:
            mic.wait(min(chunk_s, total - done))
            audio = mic.cut()
            keep(audio, mic.resets - resets)
            resets = mic.resets
            done += len(audio) / SR
            say(f"  {done / 60:.0f} of {minutes:g} minutes")
    except (KeyboardInterrupt, DeviceFailed) as exc:
        _stopped(manifest, exc, say)
        with contextlib.suppress(DeviceFailed):
            audio = mic.cut()
            if len(audio):
                keep(audio, mic.resets - resets)
    with contextlib.suppress(DeviceFailed):
        mic.stop()
    _save(folder, manifest)
    return manifest


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


PUSHES = (
    ("what's the weather tomorrow", False),
    ("remind me to water the plants at six", True),
    ("how long would it take to walk to the station", False),
    ("add olive oil and lemons to the shopping list", False),
    ("set a timer for twenty minutes", True),
    ("what's on my calendar on Friday", False),
    ("read me the last email I got", False),
    ("tell me a short fact about octopuses", True),
    ("move my three o'clock to four", False),
    ("what time is it in Tokyo", True),
)

INTRO_E = """
Session e: push-to-talk, in the app, about 5 minutes.

Steve must be running, with an OpenAI key stored, and hands-free off. Each
request is shown here. Hold the push-to-talk key (or the microphone button),
say it, and let go. Wait for the whole reply, then come back here and press
Enter. Some are marked to begin the instant the key goes down: start speaking
as you press, not after the tick. Nothing is recorded here: the app hears you,
and this reads the timings it logs, never your words."""

TARGETS = {"live_ms": 150, "first_audio_p50_ms": 1500, "first_audio_p90_ms": 2500}


def _number(value: str):
    if value in ("-", "None"):
        return None
    try:
        return int(value)
    except ValueError:
        try:
            return float(value)
        except ValueError:
            return value


def parse_turn(line: str) -> dict | None:
    """The fields of one "push-to-talk turn:" line (agent/worker/desk.
    log_hotkey_turn), or None for any other line."""
    _, found, rest = line.partition(TURN_LINE)
    if not found:
        return None
    turn = {}
    for pair in rest.split():
        key, sep, value = pair.partition("=")
        if sep:
            turn[key] = _number(value)
    return turn


class TurnLog:
    """The push-to-talk turns the worker logs from now on. Each worker
    writes its own file in the logs folder, and a restart starts another, so
    this keeps how far it has read in each, and reads only whole lines."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.read = {path: path.stat().st_size for path in self._files()}

    def _files(self) -> list[Path]:
        return sorted(self.folder.glob("worker-*.log"))

    def new_turns(self) -> list[dict]:
        turns = []
        for path in self._files():
            start = self.read.get(path, 0)
            with path.open("rb") as handle:
                handle.seek(start)
                data = handle.read()
            whole = data[:data.rfind(b"\n") + 1]
            self.read[path] = start + len(whole)
            for line in whole.decode("utf-8", errors="replace").splitlines():
                if (turn := parse_turn(line)) is not None:
                    turns.append(turn)
        return turns


def _percentile(values: list[float], q: float) -> float:
    return float(np.percentile(values, q))


def score_e(manifest: dict) -> dict:
    """Push-to-talk's exit (docs/plan/07-voice.md, section 1): the press to
    a live microphone, the release to the reply's first word at P50 and P90,
    and the first word kept in every take begun at the press. Each take is
    judged on its last answered turn: a take said again counts once."""
    answered = []
    for take in manifest["takes"]:
        turns = [t for t in take.get("turns", []) if t.get("outcome") == "answered"]
        if turns:
            answered.append(turns[-1])
    lives = [t["live_ms"] for t in answered if isinstance(t.get("live_ms"), int)]
    firsts = [t["first_audio_ms"] for t in answered if isinstance(t.get("first_audio_ms"), int)]
    at_press = [take for take in manifest["takes"] if take.get("at_press")]
    kept = [take for take in at_press if take.get("first_word_kept")]
    score: dict = {"takes": len(manifest["takes"]), "answered": len(answered),
                   "first_word_kept": f"{len(kept)} of {len(at_press)}"}
    if lives:
        score["live_ms"] = {"p50": _percentile(lives, 50), "worst": max(lives)}
    if firsts:
        score["first_audio_ms"] = {"p50": _percentile(firsts, 50),
                                   "p90": _percentile(firsts, 90)}
    for leg in ("stt_ms", "first_token_ms", "agent_ms"):
        values = [t[leg] for t in answered if isinstance(t.get(leg), int)]
        if values:
            score[leg] = {"p50": _percentile(values, 50), "p90": _percentile(values, 90)}
    score["passes"] = {
        "answered": bool(manifest["takes"]) and len(answered) == len(manifest["takes"]),
        "live": bool(lives) and max(lives) <= TARGETS["live_ms"],
        "first_audio": bool(firsts)
        and score["first_audio_ms"]["p50"] <= TARGETS["first_audio_p50_ms"]
        and score["first_audio_ms"]["p90"] <= TARGETS["first_audio_p90_ms"],
        "first_word": len(kept) == len(at_press),
    }
    return score


def report_e(score: dict) -> str:
    def mark(ok: bool) -> str:
        return "pass" if ok else "MISS"

    passes = score["passes"]
    lines = [f"Answered: {score['answered']} of {score['takes']}  "
             f"[{mark(passes['answered'])}]"]
    live = score.get("live_ms")
    lines.append(f"Press to a live microphone: P50 {live['p50']:.0f} ms, worst "
                 f"{live['worst']} ms (at most {TARGETS['live_ms']})  [{mark(passes['live'])}]"
                 if live else "Press to a live microphone: not logged  [MISS]")
    first = score.get("first_audio_ms")
    lines.append(f"Release to the first word: P50 {first['p50']:.0f} ms, P90 "
                 f"{first['p90']:.0f} ms (at most {TARGETS['first_audio_p50_ms']} and "
                 f"{TARGETS['first_audio_p90_ms']})  [{mark(passes['first_audio'])}]"
                 if first else "Release to the first word: not logged  [MISS]")
    lines.append(f"First word kept when begun at the press: {score['first_word_kept']}  "
                 f"[{mark(passes['first_word'])}]")
    legs = [f"{name} P50 {score[key]['p50']:.0f} ms, P90 {score[key]['p90']:.0f} ms"
            for key, name in (("stt_ms", "release to the words"),
                              ("first_token_ms", "to the reply's first token"),
                              ("agent_ms", "the reply written"))
            if key in score]
    if legs:
        lines.append("Where the time went: " + "; ".join(legs) + ".")
    return "\n".join(lines)


def push_to_talk(folder: Path, logs: Path, *, ask=input, say=print, pushes=PUSHES,
                 manifest: dict | None = None) -> dict:
    """Session e: ten requests through the app's push-to-talk, a few begun
    the instant the key goes down. The app does the hearing; this shows each
    request, keeps the turns the worker logs for it, asks whether a take
    begun at the press kept its first word, and scores the lot."""
    if manifest is None:
        manifest = {"session": "e", "what": SESSIONS["e"], "logs": str(logs),
                    "started": datetime.now(UTC).isoformat(timespec="seconds"),
                    "takes": [], "stopped_early": False}
    else:
        manifest.setdefault("resumed", []).append(
            datetime.now(UTC).isoformat(timespec="seconds"))
        manifest["stopped_early"] = False
    done = {take["n"] for take in manifest["takes"]}
    log = TurnLog(logs)
    say(INTRO_E)
    try:
        for n, (request, at_press) in enumerate(pushes, 1):
            if n in done:
                continue
            how = "Begin the instant the key goes down: " if at_press else ""
            say(f"\n{n}/{len(pushes)}  {how}\"{request}\"")
            choice = ask("  Press the key, say it, let go; when the reply has ended, Enter "
                         "(s and Enter to skip, q and Enter to stop): ").strip().lower()
            if choice == "q":
                break
            take = {"n": n, "request": request, "at_press": at_press}
            if choice == "s":
                log.new_turns()  # a skipped take's turns are not the next one's
                continue
            if at_press:
                first = request.split()[0]
                take["first_word_kept"] = ask(
                    f'  Did the words shown begin with "{first}"? [Y/n] ').strip().lower() != "n"
            take["turns"] = log.new_turns()
            if not take["turns"]:
                say("  No turn was logged for that one: is Steve running from this "
                    f"data folder ({logs})?")
            manifest["takes"].append(take)
            _save(folder, manifest)
    except KeyboardInterrupt:
        manifest["stopped_early"] = True
    manifest["score"] = score_e(manifest)
    _save(folder, manifest)
    say("\n" + report_e(manifest["score"]))
    return manifest


def _logs_dir() -> Path:
    from core import paths

    return paths.logs_dir()


def _hands_free_on() -> bool:
    from agent.worker import desk

    return desk.switched_on()


def main(argv: list[str] | None = None, *, devices=None, ask=input, say=print,
         hands_free_on=_hands_free_on, committable=would_commit,
         speak=synthesize) -> int:
    parser = argparse.ArgumentParser(prog="python -m devtools.voicebench.record",
                                     description=__doc__.split("\n")[0])
    parser.add_argument("session", choices=sorted(SESSIONS),
                        help="; ".join(f"{k}: {v}" for k, v in SESSIONS.items()))
    parser.add_argument("--out", type=Path,
                        help="where sessions go (default: private/voice/ in the main checkout)")
    parser.add_argument("--resume", type=Path, metavar="FOLDER",
                        help="record what a session's folder is missing, into it")
    parser.add_argument("--minutes", type=float, default=60.0, help="session d's length")
    parser.add_argument("--answers", type=Path,
                        help="session c: a folder of mono WAVs to play, not Windows' voice")
    parser.add_argument("--logs", type=Path,
                        help="session e: the worker's logs (default: the data folder's logs/)")
    args = parser.parse_args(argv)

    if hands_free_on():
        say("Hands-free is on: the desk would answer every take. Switch it off, then run "
            "this again.")
        return 2
    resumed: dict | None = None
    if args.resume:
        folder = args.resume.resolve()
        try:
            resumed = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            say(f"No session to resume in {folder}: {exc}")
            return 2
        if resumed.get("session") != args.session:
            say(f"{folder} holds session {resumed.get('session')}, not {args.session}.")
            return 2
        root = folder.parent
    else:
        root = (args.out or default_root()).resolve()
        folder = root / f"{datetime.now():%Y%m%d-%H%M%S}-{args.session}"
    if committable(root):
        say(f"{root} is in a git checkout that does not ignore it, so the recordings could "
            "be committed. Choose a folder git ignores, such as private/voice/.")
        return 2
    if args.session in ("a", "c") and ask(OTHERS).strip().lower() != "y":
        say("Then not now: switch it off first.")
        return 2
    if resumed:
        say(f"Resuming {folder}: {len(resumed['takes'])} takes there already.")
    else:
        say(f"Recordings go to {folder}")

    if args.session == "e":
        logs = (args.logs or _logs_dir()).resolve()
        if not logs.is_dir():
            say(f"No worker logs in {logs}: start Steve from this checkout, or give --logs.")
            return 2
        folder.mkdir(parents=True, exist_ok=True)
        manifest = push_to_talk(folder, logs, ask=ask, say=say, manifest=resumed)
        early = " (stopped early)" if manifest["stopped_early"] else ""
        say(f"\nDone{early}: {len(manifest['takes'])} takes, the score in "
            f"{folder / 'manifest.json'}.")
        return 0

    answers: list[Path] = []
    if args.session == "c":
        if args.answers:
            answers = sorted(args.answers.glob("*.wav"))
        elif resumed:
            answers = [Path(a) for a in resumed.get("answers", []) if Path(a).is_file()]
        else:
            say("Making the answers in Windows' voice ...")
            answers = speak(ANSWERS, folder / "answers")
        if not answers:
            say("No answers to play: give a folder of WAVs with --answers.")
            return 2

    devices = devices or Devices()
    opened = [devices.mic()]
    mic = opened[0]
    try:
        say(f"Microphone: {mic.name}")
        if ask("Is that the microphone the desk uses? [Y/n] ").strip().lower() == "n":
            say("Set it as Windows' default microphone (Sound settings), then run this "
                "again.")
            return 2
        if args.session == "a":
            manifest = wake_takes(folder, mic, ask=ask, say=say, manifest=resumed)
        elif args.session == "b":
            manifest = requests(folder, mic, ask=ask, say=say, manifest=resumed)
        elif args.session == "c":
            opened.append(devices.loopback())
            manifest = echo(folder, mic, opened[1], devices.speaker(), answers, ask=ask,
                            say=say, rng=random.Random(), manifest=resumed)
        else:
            manifest = room(folder, mic, ask=ask, say=say, minutes=args.minutes,
                            manifest=resumed)
    finally:
        for device in opened:
            device.close()
    early = " (stopped early)" if manifest["stopped_early"] else ""
    say(f"\nDone{early}: {len(manifest['takes'])} takes in {folder}.")
    return 1 if manifest.get("error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
