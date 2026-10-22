"""Imported first by a test module whose own imports need the reflect extra:
skips that module where the extra is not installed (tests/gate/extras.py)."""

import pytest

from tests.gate.extras import REFLECT

pytest.importorskip(REFLECT, reason="needs the reflect extra (presidio)")
