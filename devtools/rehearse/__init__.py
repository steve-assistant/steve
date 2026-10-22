"""A rehearsal rig for the desk's voice: the real desk, end to end, in software.

The maintainer, 10 October 2026: "could you create some sort of sandbox where you
can test speaking and playback without running stuff through speakers?".
Every sound test of the desk so far has found something the unit tests
could not: the fake world (tests/unit/desk_world.py) runs the real engine,
ear, floor and live client against fakes that behave as the real parts did,
but its wake detector reads a marker, its echo canceller subtracts a number
and its Whisper returns the script. The 10 October 00:19 retest failed on
exactly what those fakes leave out: over a live conversation the raw
microphone scored "hey Steve, stop" 0.91 and the echo canceller left 0.00 of
it, so the first stop was never heard.

So this runs the desk as production builds it -- `agent/worker/desk.switch`,
`_build_ear`, the real `Ear` and `Ears` threads, WebRTC's AEC3, the trained
wake detector, Silero, the end-of-turn judge, Whisper large-v3-turbo on the
GPU, Kokoro and the real `Player` -- and replaces only what touches the
world:

- **the microphone** is a room (`room.py`): the user's lines said by Kokoro in
  voices the wake detector never trained on, the echo of what the speakers
  played (delay, a room's reverb, gain, small-speaker clipping), noise, and a
  video on the laptop, heard both through the room and on the loopback;
- **the speakers** are a sound card's clock (`speaker.py`) that pulls from
  the real `Player` in real time, can stall, and keeps what was audible;
- **the assistant and GPT-Live** are scripted fakes (`fakes.py`), with
  canned answers of realistic length and latency -- no model is called and
  no tool touches the user's mail or calendar. Real GPT-Live is behind `--live`.

It is a developer's tool, run by hand (`python -m devtools.rehearse`), not
part of the suite: it loads real models, takes real time and the laptop's
GPU. Its pure parts -- the room's mixer, the speaker's clock, the script --
have unit tests (tests/unit/test_rehearse.py).

It can never open a real device: `rig.no_devices` replaces soundcard and
sounddevice's streams with ones that refuse before anything is built, and
the ears it gives the desk read the room, not WASAPI. What it writes is
synthetic audio only, and only with `--save-audio`, to data/rehearsals/
(gitignored with the rest of data/). The desk's guarantee that no sound of
the user's room is ever written stays as it was: nothing here is the user's room.
"""
