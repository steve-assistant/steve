"""The voice bench's recorder keeps what each take is, and only where git ignores it.

The maintainer runs `devtools.voicebench.record` once per session, and the
bench scores everything later from its files: a take whose manifest says the
wrong thing, or an Enter time that is not where Enter was pressed, would
score every judge wrongly and be found only when the numbers made no sense.
The microphone and speakers are fakes on a shared clock; the parts that are
not devices (WAV files, git, Windows' voice) run for real.
"""

from __future__ import annotations

import json
import random
import subprocess
import sys
import time
import wave
from pathlib import Path

import numpy as np
import pytest

from devtools.voicebench import record


class Clock:
    now = 0.0


class FakeCapture:
    """A device on a clock: it hears a quiet tone for exactly as long as
    the clock moves while it keeps."""

    def __init__(self, clock: Clock, *, rate: int = record.SR, name: str = "Desk mic",
                 interrupt_at: float | None = None, reset_at: float | None = None,
                 fail_at: float | None = None) -> None:
        self.clock, self.rate, self.name = clock, rate, name
        self.interrupt_at, self.reset_at, self.fail_at = interrupt_at, reset_at, fail_at
        self.resets, self.last_reset = 0, ""
        self.starts = 0
        self.closed = False
        self._from: float | None = None

    def start(self) -> None:
        self._from = self.clock.now
        self.starts += 1

    def elapsed(self) -> float:
        return 0.0 if self._from is None else self.clock.now - self._from

    def wait(self, seconds: float) -> None:
        later = self.clock.now + seconds
        if self.interrupt_at is not None and later > self.interrupt_at:
            self.clock.now = self.interrupt_at
            raise KeyboardInterrupt
        if self.fail_at is not None and later > self.fail_at:
            self.clock.now = self.fail_at
            raise record.DeviceFailed("the device failed: Error 0x88890004")
        if self.reset_at is not None and self.clock.now < self.reset_at <= later:
            self.resets += 1  # Windows invalidated the stream; it came back as another
            self.last_reset, self.name = "Error 0x88890004", "Headset mic"
        self.clock.now = later

    def cut(self) -> np.ndarray:
        frames = round(self.elapsed() * self.rate)
        self._from = self.clock.now
        return (0.1 * np.sin(np.arange(frames) / 10)).astype(np.float32)

    def stop(self) -> np.ndarray:
        audio = self.cut()
        self._from = None
        return audio

    def close(self) -> None:
        self.closed = True


class FakeSpeaker:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.played: list[tuple[float, float]] = []  # (when, seconds)

    def play(self, audio: np.ndarray, rate: int) -> None:
        self.played.append((self.clock.now, len(audio) / rate))

    def wait(self) -> None:
        when, seconds = self.played[-1]
        self.clock.now = max(self.clock.now, when + seconds)


class Terminal:
    """Answers each prompt from a script; the bare Enter that ends a request
    is when the speaker has been talking `speaking_s`."""

    def __init__(self, replies=(), *, clock: Clock | None = None, speaking_s: float = 0.0):
        self.replies = list(replies)
        self.clock, self.speaking_s = clock, speaking_s
        self.said: list[str] = []

    def ask(self, prompt: str) -> str:
        if prompt == "" and self.clock is not None:
            self.clock.now += self.speaking_s
            return ""
        return self.replies.pop(0) if self.replies else ""

    def say(self, text: str) -> None:
        self.said.append(text)


class FakeDevices:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.opened: list[FakeCapture] = []

    def mic(self) -> FakeCapture:
        self.opened.append(FakeCapture(self.clock))
        return self.opened[-1]

    def loopback(self) -> FakeCapture:
        self.opened.append(FakeCapture(self.clock, rate=record.LOOPBACK_SR, name="Speakers"))
        return self.opened[-1]

    def speaker(self) -> FakeSpeaker:
        return FakeSpeaker(self.clock)


def _seconds(path: Path) -> tuple[float, int, int]:
    with wave.open(str(path), "rb") as src:
        return src.getnframes() / src.getframerate(), src.getframerate(), src.getnchannels()


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, timeout=60)


def test_every_wake_take_is_a_16_khz_mono_wav_that_the_manifest_names_with_its_section(
        tmp_path):
    """The bench scores misses by section (plain, run on, far, over music):
    a take filed under the wrong one moves a miss into a row it isn't in."""
    clock = Clock()
    plan = (("plain", 2, 2.5, "say it"), ("request", 2, 4.5, "say it on"))
    manifest = record.wake_takes(tmp_path, FakeCapture(clock), ask=Terminal().ask,
                                 say=Terminal().say, plan=plan)

    takes = manifest["takes"]
    assert [(t["file"], t["section"]) for t in takes] == [
        ("plain/01.wav", "plain"), ("plain/02.wav", "plain"),
        ("request/01.wav", "request"), ("request/02.wav", "request")]
    assert [t.get("request") for t in takes] == [None, None, *record.REQUESTS[:2]]
    assert _seconds(tmp_path / "plain/01.wav") == (2.5, record.SR, 1)
    assert _seconds(tmp_path / "request/02.wav") == (4.5, record.SR, 1)
    assert json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8")) == manifest
    assert manifest["microphone"] == "Desk mic" and not manifest["stopped_early"]


def test_a_skipped_section_records_nothing_and_q_ends_the_session_there(tmp_path):
    """A section the room can't do (no music at the desk) is skipped, not
    filled with silence the bench would score as misses."""
    plan = (("plain", 1, 2.5, "a"), ("far", 1, 2.5, "b"), ("media", 1, 2.5, "c"))
    manifest = record.wake_takes(tmp_path, FakeCapture(Clock()),
                                 ask=Terminal(["s", "", "q"]).ask, say=Terminal().say,
                                 plan=plan)

    assert [t["file"] for t in manifest["takes"]] == ["far/01.wav"]
    assert not (tmp_path / "plain").exists() and not (tmp_path / "media").exists()


def test_ctrl_c_keeps_the_takes_already_made_and_the_manifest_says_it_stopped_early(
        tmp_path):
    """Stopping halfway must not lose the half: the manifest is on disk
    after every take, and says the session is short."""
    clock = Clock()
    mic = FakeCapture(clock, interrupt_at=8.0)  # during the third take: 1 + 2.5 a take
    plan = (("plain", 5, 2.5, "say it"),)
    record.wake_takes(tmp_path, mic, ask=Terminal().ask, say=Terminal().say, plan=plan)

    saved = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert [t["file"] for t in saved["takes"]] == ["plain/01.wav", "plain/02.wav"]
    assert saved["stopped_early"] is True
    assert not (tmp_path / "plain/03.wav").exists()


def test_a_take_a_reset_fell_inside_is_taken_again_and_names_the_microphone_it_came_from(
        tmp_path):
    """A take with a gap in it is not the take the bench thinks it is. The
    first full run lost its last take to a reset, and the run with it."""
    clock = Clock()
    mic = FakeCapture(clock, reset_at=5.0)  # inside the second take: 1 + 2.5 a take
    terminal = Terminal()
    manifest = record.wake_takes(tmp_path, mic, ask=terminal.ask, say=terminal.say,
                                 plan=(("plain", 3, 2.5, "say it"),))

    assert mic.starts == 4  # the second take, twice
    assert [t["file"] for t in manifest["takes"]] == [
        "plain/01.wav", "plain/02.wav", "plain/03.wav"]
    assert [t.get("microphone") for t in manifest["takes"]] == [
        None, "Headset mic", "Headset mic"]
    assert any("0x88890004" in line and "again" in line for line in terminal.said)
    assert not manifest["stopped_early"]


def test_a_request_a_reset_fell_inside_waits_for_the_speaker_before_it_is_said_again(
        tmp_path):
    clock = Clock()
    mic = FakeCapture(clock, reset_at=4.0)  # in the tail after Enter, at 3 s
    terminal = Terminal(clock=clock, speaking_s=3.0)
    manifest = record.requests(tmp_path, mic, ask=terminal.ask, say=terminal.say,
                               situations=(("You want a timer.", False),))

    assert mic.starts == 2 and len(manifest["takes"]) == 1
    assert manifest["takes"][0]["enter_s"] == pytest.approx(3.0)


def test_a_device_that_will_not_reopen_ends_the_session_keeping_its_takes_and_the_error(
        tmp_path):
    clock = Clock()
    mic = FakeCapture(clock, fail_at=5.0)
    terminal = Terminal()
    record.wake_takes(tmp_path, mic, ask=terminal.ask, say=terminal.say,
                      plan=(("plain", 3, 2.5, "say it"),))

    saved = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert [t["file"] for t in saved["takes"]] == ["plain/01.wav"]
    assert saved["stopped_early"] is True and "0x88890004" in saved["error"]
    assert any("--resume" in line for line in terminal.said)


def test_a_resumed_session_records_only_what_its_folder_is_missing(tmp_path):
    """The first full run kept 55 of 56 takes; finishing it should ask for
    the one paragraph, not the 55 again."""
    plan = (("plain", 2, 2.5, "a"), ("far", 2, 2.5, "b"), ("near", 1, 60.0, "c"))
    first = record.wake_takes(tmp_path, FakeCapture(Clock()), ask=Terminal(["", "", "q"]).ask,
                              say=Terminal().say, plan=plan)
    assert len(first["takes"]) == 4

    mic, terminal = FakeCapture(Clock()), Terminal()
    resumed = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    manifest = record.wake_takes(tmp_path, mic, ask=terminal.ask, say=terminal.say,
                                 plan=plan, manifest=resumed)

    assert mic.starts == 1
    assert [t["file"] for t in manifest["takes"]] == [
        "plain/01.wav", "plain/02.wav", "far/01.wav", "far/02.wav", "near/01.wav"]
    assert len(manifest["resumed"]) == 1
    assert not any("== plain" in line or "== far" in line for line in terminal.said)


def test_a_partly_done_section_says_how_many_takes_are_left(tmp_path):
    plan = (("plain", 3, 2.5, "a"),)
    clock = Clock()
    record.wake_takes(tmp_path, FakeCapture(clock, interrupt_at=4.0), ask=Terminal().ask,
                      say=Terminal().say, plan=plan)
    resumed = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    terminal = Terminal()
    manifest = record.wake_takes(tmp_path, FakeCapture(Clock()), ask=terminal.ask,
                                 say=terminal.say, plan=plan, manifest=resumed)

    assert [t["file"] for t in manifest["takes"]] == [
        "plain/01.wav", "plain/02.wav", "plain/03.wav"]
    assert any("(2 of 3 left)" in line for line in terminal.said)
    assert manifest["stopped_early"] is False


def test_a_resumed_room_records_only_the_minutes_it_is_short(tmp_path):
    record.room(tmp_path, FakeCapture(Clock(), interrupt_at=20.0), ask=Terminal().ask,
                say=Terminal().say, minutes=1.0, chunk_s=10.0)
    resumed = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    manifest = record.room(tmp_path, FakeCapture(Clock()), ask=Terminal().ask,
                           say=Terminal().say, minutes=5.0, chunk_s=10.0, manifest=resumed)

    assert manifest["minutes"] == 1.0  # the session's own length, not the new run's
    assert sum(t["seconds"] for t in manifest["takes"]) == pytest.approx(60.0)
    assert [t["file"] for t in manifest["takes"]][-1] == "06.wav"


def test_main_resumes_into_the_folder_given_and_refuses_another_sessions_folder(tmp_path):
    clock = Clock()
    folder = tmp_path / "out" / "20261016-072533-a"
    plan_one = (("plain", 1, 2.5, "a"),)
    record.wake_takes(folder, FakeCapture(clock), ask=Terminal().ask, say=Terminal().say,
                      plan=plan_one)

    code, devices, terminal = _main(tmp_path, ["b", "--resume", str(folder)])
    assert code == 2 and devices.opened == []
    assert "holds session a" in terminal.said[0]

    code, devices, terminal = _main(tmp_path, ["a", "--resume", str(folder)],
                                    replies=["y", "", *["s"] * 4])
    assert code == 0
    assert any("Resuming" in line and "1 takes there" in line for line in terminal.said)
    assert [p.name for p in (tmp_path / "out").iterdir()] == ["20261016-072533-a"]


def test_a_request_marks_where_enter_was_pressed_and_keeps_a_tail_after_it(tmp_path):
    """Enter is where the turn ended: the bench scores a cut before it as
    early. The tail after it is where a judge that waits too long shows."""
    clock = Clock()
    terminal = Terminal(clock=clock, speaking_s=3.2)
    situations = (("You want a timer.", False), ("You think aloud about a day.", True))
    manifest = record.requests(tmp_path, FakeCapture(clock), ask=terminal.ask,
                               say=terminal.say, situations=situations)

    assert [(t["file"], t["two_part"]) for t in manifest["takes"]] == [
        ("01.wav", False), ("02.wav", True)]
    assert all(t["enter_s"] == pytest.approx(3.2) for t in manifest["takes"])
    seconds, rate, _ = _seconds(tmp_path / "02.wav")
    assert (seconds, rate) == (pytest.approx(3.2 + record.TAIL_S), record.SR)
    assert manifest["tail_s"] == record.TAIL_S


def test_the_echo_session_keeps_the_mic_and_the_loopback_and_when_the_prompt_came(
        tmp_path):
    """A canceller is scored on what it would have been given: the mic, the
    speakers' own output, and where in the answer "stop" was asked for."""
    clock = Clock()
    answer = tmp_path / "answers" / "01.wav"
    record.write_wav(answer, np.zeros(8 * record.SR, np.float32))  # an 8 s answer
    speaker = FakeSpeaker(clock)
    folder = tmp_path / "session"
    manifest = record.echo(
        folder, FakeCapture(clock), FakeCapture(clock, rate=record.LOOPBACK_SR, name="Spk"),
        speaker, [answer], ask=Terminal().ask, say=Terminal().say,
        rng=random.Random(7), trials=3)

    assert len(manifest["takes"]) == 3 and len(speaker.played) == 3
    for take in manifest["takes"]:
        assert take["play_s"] == pytest.approx(record.LEAD_S)
        assert 1.0 <= take["prompt_s"] - take["play_s"] <= 8 - 2.0
        mic_s, mic_rate, _ = _seconds(folder / take["mic"])
        loop_s, loop_rate, _ = _seconds(folder / take["loopback"])
        assert (mic_rate, loop_rate) == (record.SR, record.LOOPBACK_SR)
        assert mic_s == pytest.approx(record.LEAD_S + 8 + record.TAIL_S)
        assert loop_s == pytest.approx(mic_s)
    assert manifest["loopback"] == "Spk" and manifest["loopback_rate"] == record.LOOPBACK_SR


def test_the_room_is_kept_in_chunks_and_ctrl_c_keeps_the_part_since_the_last(tmp_path):
    """An hour in one file is lost whole if anything fails at minute 59; in
    chunks, a stop keeps everything up to it."""
    clock = Clock()
    mic = FakeCapture(clock, interrupt_at=25.0)
    manifest = record.room(tmp_path, mic, ask=Terminal().ask, say=Terminal().say,
                           minutes=1.0, chunk_s=10.0)

    assert [(t["file"], t["seconds"]) for t in manifest["takes"]] == [
        ("01.wav", 10.0), ("02.wav", 10.0), ("03.wav", 5.0)]
    assert manifest["stopped_early"] is True
    assert _seconds(tmp_path / "03.wav")[0] == pytest.approx(5.0)


def test_the_room_runs_its_full_length_when_left_alone(tmp_path):
    clock = Clock()
    manifest = record.room(tmp_path, FakeCapture(clock), ask=Terminal().ask,
                           say=Terminal().say, minutes=0.5, chunk_s=12.0)

    assert [t["seconds"] for t in manifest["takes"]] == [12.0, 12.0, 6.0]
    assert not manifest["stopped_early"]


def _main(tmp_path, argv, replies=(), **kwargs):
    clock = Clock()
    devices = FakeDevices(clock)
    terminal = Terminal(replies, clock=clock, speaking_s=2.0)
    options = dict(devices=devices, ask=terminal.ask, say=terminal.say,
                   hands_free_on=lambda: False, committable=lambda folder: False)
    options.update(kwargs)
    code = record.main([*argv, "--out", str(tmp_path / "out")], **options)
    return code, devices, terminal


def test_hands_free_on_refuses_before_any_device_opens(tmp_path):
    """The desk would answer every take, and its answers would be in them."""
    code, devices, terminal = _main(tmp_path, ["b"], hands_free_on=lambda: True)

    assert code == 2 and devices.opened == []
    assert "Hands-free is on" in terminal.said[0]


def test_a_folder_git_would_commit_is_refused_before_any_device_opens(tmp_path):
    """Recordings of a voice must never enter the repo, even through --out."""
    code, devices, terminal = _main(tmp_path, ["b"], committable=lambda folder: True)

    assert code == 2 and devices.opened == []
    assert "could be committed" in terminal.said[0]


@pytest.mark.parametrize("session", ["a", "c"])
def test_a_session_saying_the_wake_phrase_waits_for_a_yes_that_no_other_assistant_listens(
        tmp_path, session):
    """Another assistant on the same computer listening for "hey Steve"
    would wake at every take and talk over it."""
    code, devices, terminal = _main(tmp_path, [session], replies=[""])

    assert code == 2 and devices.opened == []


def test_a_session_runs_in_a_dated_folder_under_out_and_closes_its_devices(tmp_path):
    situations = record.SITUATIONS
    replies = ["", *[""] * len(situations)]  # the microphone is right; start each
    code, devices, terminal = _main(tmp_path, ["b"], replies=replies)

    assert code == 0
    (folder,) = (tmp_path / "out").iterdir()
    assert folder.name.endswith("-b") and len(folder.name) == len("20261016-120000-b")
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["takes"]) == len(situations)
    assert all(device.closed for device in devices.opened)


def test_the_echo_session_closes_the_loopback_too_and_plays_the_answers_it_was_given(
        tmp_path):
    answers = tmp_path / "given"
    for n in (1, 2):
        record.write_wav(answers / f"{n:02d}.wav", np.zeros(5 * record.SR, np.float32))
    out = tmp_path / "out"
    clock = Clock()
    devices = FakeDevices(clock)
    terminal = Terminal(["y", "", ""])

    def never(texts, folder):
        raise AssertionError("given answers are played, not made")

    code = record.main(["c", "--out", str(out), "--answers", str(answers)],
                       devices=devices, ask=terminal.ask, say=terminal.say,
                       hands_free_on=lambda: False, committable=lambda folder: False,
                       speak=never)

    assert code == 0
    assert [d.name for d in devices.opened] == ["Desk mic", "Speakers"]
    assert all(d.closed for d in devices.opened)
    (folder,) = out.iterdir()
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    assert [t["answer"] for t in manifest["takes"][:3]] == ["01.wav", "02.wav", "01.wav"]


def test_the_request_prompts_are_sixty_situations_a_third_in_two_parts_none_with_the_wake():
    """Session b is scored for the turn's end, not the wake: a prompt that
    says "hey Steve" mixes the two. A third invite a pause mid-request,
    where an early cut happens."""
    assert len(record.SITUATIONS) == 60
    assert sum(two for _, two in record.SITUATIONS) == 20
    texts = [s for s, _ in record.SITUATIONS] + [record.NEAR_MISSES, *record.ANSWERS]
    assert not [t for t in texts if "hey steve" in " ".join(t.lower().split())]


def test_a_wav_reads_back_as_it_was_written(tmp_path):
    audio = (0.5 * np.sin(np.arange(1600) / 7)).astype(np.float32)
    record.write_wav(tmp_path / "x.wav", audio, 16_000)

    back, rate = record.read_wav(tmp_path / "x.wav")
    assert rate == 16_000 and np.max(np.abs(back - audio)) < 1e-4


# soundcard as far as the recorder can tell: its import joins COM as
# soundcard 0.4's does (mediafoundation.py), raising on what that raises on,
# and its devices give silence in real time.
STAND_IN = '''\
import ctypes
import time

import numpy as np

_hr = ctypes.windll.ole32.CoInitializeEx(None, 0)
if _hr != 0 and _hr + 2**32 != 0x80010106:
    raise RuntimeError(f"Error {hex(_hr + 2**32)}")

FAIL_READS = set()  # which reads, counted from 1, Windows invalidates
MIC_NAMES = ["Stand-in microphone"]  # the default microphone at each open
_reads = 0
_opens = 0


class _Recorder:
    def __init__(self, samplerate):
        self.rate = samplerate
        self.failed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if self.failed:  # as the real one's does, closing a dead stream
            raise RuntimeError("Error 0x100000001")
        return False

    def record(self, numframes):
        global _reads
        _reads += 1
        if _reads in FAIL_READS:
            self.failed = True
            raise RuntimeError("Error 0x88890004")
        time.sleep(numframes / self.rate)
        return np.zeros((numframes, 1), np.float32)


class _Player(_Recorder):
    def play(self, audio):
        time.sleep(len(audio) / self.rate)


class _Device:
    def __init__(self, name):
        self.name = name

    def recorder(self, samplerate, channels, blocksize):
        return _Recorder(samplerate)

    def player(self, samplerate, channels):
        return _Player(samplerate)


def default_microphone():
    global _opens
    _opens += 1
    return _Device(MIC_NAMES[min(_opens, len(MIC_NAMES)) - 1])


def default_speaker():
    return _Device("Stand-in speakers")


def get_microphone(id, include_loopback=False):
    assert include_loopback and id == "Stand-in speakers"
    return _Device("Stand-in loopback")
'''


@pytest.fixture
def stand_in(tmp_path, monkeypatch):
    folder = tmp_path / "stand_in"
    folder.mkdir()
    (folder / "soundcard.py").write_text(STAND_IN, encoding="utf-8", newline="\n")
    monkeypatch.syspath_prepend(str(folder))
    monkeypatch.setattr(record, "SETTLE_S", 0.05)
    sys.modules.pop("soundcard", None)
    yield
    sys.modules.pop("soundcard", None)


@pytest.mark.skipif(sys.platform != "win32", reason="COM is Windows'")
def test_soundcard_is_imported_where_its_own_com_join_succeeds_so_the_microphone_opens(
        stand_in):
    """soundcard's import joins COM and raises on "already joined". The
    first real run joined first in the capture thread, then imported, and
    stopped with "the device failed: Error 0x100000001"."""
    mic = record.Devices().mic()
    try:
        assert mic.name == "Stand-in microphone"
        time.sleep(0.3)  # heard before start: not kept
        mic.start()
        time.sleep(0.4)
        audio = mic.stop()
    finally:
        mic.close()

    assert 0.1 <= len(audio) / record.SR <= 1.0


@pytest.mark.skipif(sys.platform != "win32", reason="COM is Windows'")
def test_a_capture_opens_the_default_device_again_after_windows_invalidates_its_stream(
        stand_in, monkeypatch):
    """The first full run of session a stopped at its last take: Windows
    invalidated the stream (0x88890004), and soundcard's close of the dead
    stream raised "Error 0x100000001" over it."""
    monkeypatch.setattr(record, "REOPEN_S", 0.05)
    soundcard = record.load_soundcard()
    soundcard.FAIL_READS = {4}
    soundcard.MIC_NAMES = ["Stand-in microphone", "Stand-in headset"]
    mic = record.Capture(soundcard)
    try:
        deadline = time.monotonic() + 5
        while mic.resets == 0 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert mic.resets == 1 and "0x88890004" in mic.last_reset
        time.sleep(0.2)  # the reopened device's first moments
        mic.start()
        time.sleep(0.4)
        audio = mic.stop()
    finally:
        mic.close()

    assert mic.name == "Stand-in headset"
    assert len(audio) > 0


@pytest.mark.skipif(sys.platform != "win32", reason="COM is Windows'")
def test_the_loopback_and_the_speakers_open_on_their_own_threads_too(stand_in):
    devices = record.Devices()
    loopback = devices.loopback()
    try:
        assert (loopback.name, loopback.rate) == ("Stand-in loopback", record.LOOPBACK_SR)
        speaker = devices.speaker()
        speaker.play(np.zeros(1600, np.float32), record.SR)
        speaker.wait()
    finally:
        loopback.close()


@pytest.mark.processes
def test_recordings_from_a_worktree_go_to_the_main_checkouts_private_folder(tmp_path):
    """A worktree's own private/ is deleted with the worktree, and the
    recordings in it with it."""
    main = tmp_path / "main"
    main.mkdir()
    _git("init", "-q", "-b", "main", cwd=main)
    _git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q",
         "--allow-empty", "-m", "start", cwd=main)
    _git("worktree", "add", "-q", str(tmp_path / "tree"), cwd=main)

    expected = (main / "private" / "voice").resolve()
    assert record.default_root(tmp_path / "tree").resolve() == expected
    assert record.default_root(main).resolve() == expected


@pytest.mark.processes
def test_git_is_asked_whether_a_folder_would_be_committed(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    (repo / ".gitignore").write_text("/private/\n", encoding="utf-8", newline="\n")

    assert record.would_commit(repo / "voice") is True
    assert record.would_commit(repo / "private" / "voice") is False
    assert record.would_commit(tmp_path / "outside") is False


@pytest.mark.processes
@pytest.mark.skipif(sys.platform != "win32", reason="Windows' own voice")
def test_windows_voice_speaks_the_answers_into_16_khz_files_and_plays_nothing(tmp_path):
    """Session c's answers are made before the microphone opens; the
    script runs as written, quotes and all, in the folder's path."""
    folder = tmp_path / "it's here"
    files = record.synthesize(["The high is about eighteen degrees.", "Shall I send it?"],
                              folder)

    assert [f.name for f in files] == ["01.wav", "02.wav"]
    for f in files:
        audio, rate = record.read_wav(f)
        assert rate == record.SR and len(audio) / rate > 0.5
        assert np.max(np.abs(audio)) > 0.05


# ---------------------------------------------------------------------------
# Session e: push-to-talk in the app, scored from the worker's log
# ---------------------------------------------------------------------------


def _turn(outcome="answered", live_ms=40, first_audio_ms=1200, stt_ms=500,
          first_token_ms=300, agent_ms=900) -> str:
    return (f"2026-10-20 18:00:00,000 INFO agent.worker.desk: push-to-talk turn: "
            f"outcome={outcome} held_s=2.10 live_ms={live_ms} stt_ms={stt_ms} "
            f"first_token_ms={first_token_ms} agent_ms={agent_ms} "
            f"first_audio_ms={first_audio_ms} spoken_s=3.20 stall=False preamble=False "
            f"total_ms=4800 rejected=no\n")


class App:
    """The maintainer at the app: each take's Enter comes after the worker
    has logged what `turns` says for it, and each "first word" question is
    answered from `kept`."""

    def __init__(self, log: Path, turns, kept=(), choices=()):
        self.log, self.turns, self.kept = log, list(turns), list(kept)
        self.choices = list(choices)
        self.said: list[str] = []

    def ask(self, prompt: str) -> str:
        if "first word" in prompt or "begin with" in prompt:
            return "y" if self.kept.pop(0) else "n"
        if "Is that the microphone" in prompt:
            raise AssertionError("session e asked about a microphone it never opens")
        choice = self.choices.pop(0) if self.choices else ""
        if choice == "":
            with self.log.open("a", encoding="utf-8", newline="\n") as handle:
                handle.writelines(self.turns.pop(0))
        return choice

    def say(self, text: str) -> None:
        self.said.append(text)


def _logs(tmp_path) -> Path:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "worker-20261020-180000-w-5000.log").write_text(
        _turn(first_audio_ms=99_999), encoding="utf-8", newline="\n")  # before the session
    return logs


def _session_e(tmp_path, turns, kept, **kwargs):
    logs = _logs(tmp_path)
    app = App(logs / "worker-20261020-180000-w-5000.log", turns, kept, **kwargs)
    clock = Clock()
    devices = FakeDevices(clock)
    code = record.main(["e", "--out", str(tmp_path / "out"), "--logs", str(logs)],
                       devices=devices, ask=app.ask, say=app.say,
                       hands_free_on=lambda: False, committable=lambda folder: False)
    folder = next((tmp_path / "out").iterdir())
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    return code, devices, app, manifest


def _report(app) -> str:
    return next(line for line in reversed(app.said) if "Answered:" in line)


def _at_press() -> int:
    return sum(at_press for _, at_press in record.PUSHES)


def test_session_e_scores_push_to_talks_exit_from_the_turns_the_app_logged_and_opens_no_device(
        tmp_path):
    """Session e is the only measure of push-to-talk on a real desk: the app
    hears and answers, and the recorder must read the worker's own timings
    for each take, not open a microphone of its own beside the app's."""
    turns = [[_turn(live_ms=30 + n, first_audio_ms=1000 + 100 * n)] for n in range(10)]
    code, devices, app, manifest = _session_e(tmp_path, turns, [True] * _at_press())

    assert code == 0 and devices.opened == []
    assert [take["n"] for take in manifest["takes"]] == list(range(1, 11))
    assert manifest["takes"][0]["turns"][0]["first_audio_ms"] == 1000  # not the old line
    score = manifest["score"]
    assert score["answered"] == 10 and score["live_ms"]["worst"] == 39
    assert score["first_audio_ms"]["p50"] == pytest.approx(1450)
    assert score["first_word_kept"] == f"{_at_press()} of {_at_press()}"
    assert all(score["passes"].values())
    assert "MISS" not in _report(app) and "Release to the first word: P50 1450 ms" in _report(app)


def test_a_slow_reply_a_slow_microphone_or_a_lost_first_word_is_a_miss(tmp_path):
    """The targets are the plan's (section 1): a pass on a slow desk would
    ship a push-to-talk that feels broken."""
    # The median is quick and the slowest two are not: only the P90 misses.
    turns = [[_turn(live_ms=200 if n == 3 else 40, first_audio_ms=3000 if n in (4, 7) else 1000)]
             for n in range(10)]
    kept = [True] * (_at_press() - 1) + [False]
    code, _, app, manifest = _session_e(tmp_path, turns, kept)

    passes = manifest["score"]["passes"]
    assert passes == {"answered": True, "live": False, "first_audio": False,
                      "first_word": False}
    assert _report(app).count("MISS") == 3


def test_a_take_said_again_is_judged_once_on_its_last_answered_turn(tmp_path):
    """A request said badly and said again, then a stray press, is one
    request: counting the try the maintainer threw away, or the press that
    heard nothing, would score turns that were not the request."""
    turns = [[_turn(first_audio_ms=5000), _turn(first_audio_ms=800),
              _turn("empty", live_ms=999, first_audio_ms="-")]]
    turns += [[_turn(first_audio_ms=800)] for _ in range(9)]
    _, _, _, manifest = _session_e(tmp_path, turns, [True] * _at_press())

    assert len(manifest["takes"][0]["turns"]) == 3
    assert manifest["score"]["answered"] == 10
    assert manifest["score"]["first_audio_ms"]["p90"] == 800
    assert all(manifest["score"]["passes"].values())


def test_a_take_with_no_turn_logged_is_said_at_once_and_fails_the_answered_count(tmp_path):
    """A worker writing to another data folder logs nowhere this reads: told
    only at the end, the maintainer would have said ten requests for nothing."""
    turns = [[] for _ in range(10)]
    _, _, app, manifest = _session_e(tmp_path, turns, [True] * _at_press())

    assert any("No turn was logged" in line for line in app.said)
    assert manifest["score"]["passes"]["answered"] is False


def test_the_turn_log_reads_whole_lines_written_since_it_began_in_every_workers_file(tmp_path):
    """A restarted worker writes a new file, and a line half written when
    Enter is pressed must be read whole with the next take, not lost or split."""
    logs = tmp_path / "logs"
    logs.mkdir()
    first = logs / "worker-1-w-1.log"
    first.write_text(_turn(first_audio_ms=1), encoding="utf-8", newline="\n")
    log = record.TurnLog(logs)
    line = _turn(first_audio_ms=2)
    with first.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write("2026 INFO other: not a turn\n" + line[:-40])

    assert log.new_turns() == []
    with first.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line[-40:])
    (logs / "worker-2-w-2.log").write_text(_turn(first_audio_ms=3), encoding="utf-8",
                                           newline="\n")

    assert [turn["first_audio_ms"] for turn in log.new_turns()] == [2, 3]
    assert log.new_turns() == []
