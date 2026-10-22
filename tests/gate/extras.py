"""Tests of code that runs only with an optional extra installed.

Steve's base install has neither. The `voice` extra is the desk voice
add-on: Whisper, Kokoro and their CUDA libraries, gigabytes of them, and
hands-free's loopback and echo canceller. The `reflect` extra is presidio
and a spaCy model, which reflection's redaction needs and which fails closed
without them (tests/gate/test_redaction_present.py). A test that needs one
is skipped where it is not installed, rather than failing on the import.

`VOICE` is sounddevice: the microphone, which was the add-on's until
push-to-talk became core's (docs/plan/07-voice.md, item 5) and is a base
dependency now, so a test guarded by it runs everywhere. A test that needs
the add-on's own models takes `needs_addon`: guarded by sounddevice, the
Whisper test of the turn judge ran once sounddevice was core's, and failed.

A module whose imports need the extra calls `pytest.importorskip` with the
module named here, before them. A single test takes the marker.

Frozen, like the rest of tests/gate, because gate tests use it.
"""

from __future__ import annotations

from importlib.util import find_spec

import pytest

VOICE = "sounddevice"
ADDON = "faster_whisper"
REFLECT = "presidio_analyzer"

needs_voice = pytest.mark.skipif(find_spec(VOICE) is None,
                                 reason="needs the microphone's packages (sounddevice)")
needs_addon = pytest.mark.skipif(find_spec(ADDON) is None,
                                 reason="needs the voice extra (the desk voice add-on)")
needs_reflect = pytest.mark.skipif(find_spec(REFLECT) is None,
                                   reason="needs the reflect extra (presidio)")
