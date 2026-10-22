"""Imported first by a test module whose own imports need the voice extra:
skips that module where the extra is not installed (tests/gate/extras.py)."""

import pytest

from tests.gate.extras import VOICE

pytest.importorskip(VOICE, reason="needs the voice extra (the desk voice add-on)")
