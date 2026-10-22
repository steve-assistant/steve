"""Gate tests for the frozen-core policy.

These are THE JUDGE. They live inside the frozen zone precisely so that a
self-modifying agent cannot weaken them: the documented number-one failure
mode of self-improving agents is editing the assertion instead of fixing the
code. If you find yourself wanting to relax something here, that is the signal
to stop and think, not to edit.
"""

from __future__ import annotations

import pytest

from core.policy import (
    REPO_ROOT,
    _sha256,
    build_manifest,
    load_policy,
    read_lock,
    verify_lock,
)


@pytest.fixture(scope="module")
def policy():
    return load_policy()


# --------------------------------------------------------------------------
# The globs mean what we think they mean
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "core/supervisor.py",
        "core/policy.py",
        "core/policy.yaml",
        "core/nested/deeper/thing.py",
        "tests/gate/test_frozen_core.py",
        "modules/google_cloud/auth.py",
        "pyproject.toml",
        ".gitignore",
    ],
)
def test_frozen_paths_are_frozen(policy, path):
    assert policy.is_frozen(path), f"{path} must be frozen but is not"


@pytest.mark.parametrize(
    "path",
    [
        "agent/loop.py",
        "agent/prompts/system.md",
        "tools/web_search.py",
        "voice/pipeline.py",
        "channels/signal.py",
        "tests/unit/test_router.py",
        "ui/src/App.tsx",
    ],
)
def test_modifiable_paths_are_not_frozen(policy, path):
    assert not policy.is_frozen(path), f"{path} must not be frozen but is"


def test_windows_separators_are_normalised(policy):
    """git on Windows can hand us backslashes; the policy must not care."""
    assert policy.is_frozen("core\\supervisor.py")
    assert policy.is_frozen(".\\core\\policy.yaml")


def test_dotfiles_are_not_silently_dropped(policy):
    """Regression: str.lstrip("./") strips characters, not a prefix.

    It ate the leading dot of ".gitignore", which quietly removed it from the
    frozen set -- a silent hole rather than a loud failure. Any dotfile we ever
    freeze depends on this staying fixed.
    """
    assert policy.is_frozen(".gitignore")
    assert policy.is_frozen("./.gitignore")


# --------------------------------------------------------------------------
# The manifest reflects reality
# --------------------------------------------------------------------------


def test_lock_file_exists_and_is_populated():
    lock = read_lock()
    assert lock, "core/policy.lock is missing or empty; run python -m core.policy --write-lock"


def test_every_frozen_file_on_disk_is_in_the_lock(policy):
    manifest = build_manifest(policy, REPO_ROOT)
    lock = read_lock()
    missing = sorted(set(manifest) - set(lock))
    assert not missing, f"frozen files absent from policy.lock: {missing}"


def test_frozen_core_is_intact(policy):
    """The check the supervisor runs at each start, and devtools.check before each commit."""
    assert verify_lock(policy, REPO_ROOT) == []


def test_manifest_ignores_line_endings_but_not_content(tmp_path):
    """The lock must mean the same thing whatever line endings a checkout has.

    If CRLF/LF conversion registered as tampering, every fresh checkout would
    fail the gate and you would learn to regenerate the lock without reading
    it -- which defeats the entire mechanism. Detecting real edits is the part
    that must not be traded away for that portability.
    """
    lf = tmp_path / "a.py"
    crlf = tmp_path / "b.py"
    edited = tmp_path / "c.py"

    body = b"import os\nprint(os.name)\n"
    lf.write_bytes(body)
    crlf.write_bytes(body.replace(b"\n", b"\r\n"))
    edited.write_bytes(body + b"print('extra')\n")

    assert _sha256(lf) == _sha256(crlf), "line endings must not affect the hash"
    assert _sha256(lf) != _sha256(edited), "a real content change must change the hash"


def test_binary_files_are_hashed_verbatim(tmp_path):
    """Normalisation must not corrupt a model file or an icon.

    A 0x0D 0x0A pair inside binary data is data, not a line ending.
    """
    import hashlib

    blob = tmp_path / "weights.onnx"
    payload = b"\x00\x01\r\n\x02\x03"
    blob.write_bytes(payload)

    assert _sha256(blob) == hashlib.sha256(payload).hexdigest()
