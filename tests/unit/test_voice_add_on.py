"""The worker starts without the voice add-on, and core never needs it.

The speech models were the `voice` extra, which Steve's base install leaves
out. agent/worker/links.py imported them unconditionally, so on a machine
without them no worker could start: every gate test that swapped workers
failed with the new worker never becoming healthy. A machine with the extra
cannot see that, so these tests hide the extra's packages from a fresh
interpreter and start the worker's imports there.

Since step 7, core's voice is OpenAI's, and the add-on holds only the local
models, used when chosen (links._local_voice). The rest of this file checks
that boundary from both sides.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from agent import runtime
from agent.worker import links
from tests.gate.extras import needs_addon

ROOT = Path(__file__).resolve().parents[2]

# Every package voice has ever needed beyond the base install, by the names
# they import as: the add-on's, hands-free's, and the microphone's, which
# became core's in step 7. The worker must start without any of them.
VOICE_PACKAGES = ["faster_whisper", "kokoro_onnx", "onnxruntime", "sounddevice",
                  "soundfile", "soxr", "soundcard", "livekit"]


def test_the_worker_imports_without_the_voice_extra():
    # None in sys.modules makes any import of that name raise ImportError.
    hide = "; ".join(f"sys.modules[{name!r}] = None" for name in VOICE_PACKAGES)
    code = f"import sys; {hide}; import agent.server"
    done = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]


def test_without_the_microphone_library_voice_reports_what_is_missing():
    code = ("import sys, asyncio; sys.modules['sounddevice'] = None; "
            "from agent import runtime; from agent.worker import links; "
            "asyncio.run(links._load_voice()); print(runtime.voice_warm)")
    done = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]
    assert "'state': 'failed'" in done.stdout
    assert "voice's packages are not installed (sounddevice)" in done.stdout


# What only the add-on holds, by the names they import as: the local models
# and what runs them. Core never loads one (07-voice.md, section 9).
LOCAL_MODELS = ("faster_whisper", "ctranslate2", "kokoro_onnx", "onnxruntime")


def test_core_never_loads_a_local_model_on_its_way_to_a_turn():
    """Core's voice is OpenAI's, so a machine without the add-on must never
    need it. The other tests here hide the add-on and see the worker start;
    this one leaves it installed, as on a developer's checkout, and checks
    that nothing core does on its way to a push-to-talk turn imports it. A
    top-level import of Whisper anywhere on that path would pass every test
    that has the add-on, and break every install that does not."""
    code = "\n".join([
        "import sys",
        "import agent.server",
        "from agent.worker import links",
        "from voice.pipeline import VoiceSession",
        "async def reply(text): return '', {}",
        "VoiceSession(reply, streams=True, **links._local_voice())",
        f"print(sorted(n for n in {LOCAL_MODELS!r} if n in sys.modules))",
    ])
    env = {k: v for k, v in os.environ.items() if k != links.LOCAL_VOICE_ENV}
    done = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]
    assert done.stdout.strip().splitlines()[-1] == "[]"


def test_installing_the_add_on_does_not_choose_local_speech(monkeypatch):
    """A developer's checkout has every extra. Had the add-on's presence
    chosen local Whisper, session E would have timed it and reported the
    numbers as OpenAI's."""
    monkeypatch.delenv(links.LOCAL_VOICE_ENV, raising=False)
    assert links._local_voice() == {}


async def test_local_speech_chosen_without_the_add_on_fails_rather_than_use_openai(
        monkeypatch):
    """Someone who chose local speech chose that their voice stays on the
    machine. Without the add-on, voice is reported failed, naming what is
    missing; it never quietly sends the clip to OpenAI instead."""
    monkeypatch.setenv(links.LOCAL_VOICE_ENV, "1")
    for name in links.ADD_ON:
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setattr(runtime, "voice", None)
    monkeypatch.setattr(runtime, "voice_warm", {})
    await links._load_voice()
    assert runtime.voice is None
    assert runtime.voice_warm["state"] == "failed"
    assert "LocalVoiceMissing" in runtime.voice_warm["error"]
    assert "faster_whisper" in runtime.voice_warm["error"]


@needs_addon
def test_with_the_add_on_chosen_the_session_hears_and_speaks_locally(monkeypatch):
    from voice.stt import Transcriber
    from voice.tts import Speaker

    monkeypatch.setenv(links.LOCAL_VOICE_ENV, "1")
    parts = links._local_voice()
    assert isinstance(parts["transcriber"], Transcriber)
    assert isinstance(parts["speaker"], Speaker)
