"""Gate test: no credential ever becomes a tracked file.

This exists because it already happened once. A Google OAuth client secret was
downloaded straight into the repo root, where it sat untracked and one
`git add -A` away from being committed. The self-modification loop runs
`git add` on the agent's behalf, so "remember not to do that" is not a control.

Two independent checks: filenames that look like credentials, and file
*contents* that look like credentials. The second matters because the first is
trivially defeated by an innocuous filename.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from tests.gate import repo_files

REPO_ROOT = Path(__file__).resolve().parents[2]

# Filename shapes that are almost never legitimately tracked.
FORBIDDEN_NAME_PATTERNS = [
    r"^\.env$",
    r"^\.env\..*",
    r".*client_secret.*\.json$",
    r"^credentials\.json$",
    r"^token\.json$",
    r"^.*\.pem$",
    r"^.*\.pfx$",
    r"^.*\.p12$",
    r"^id_rsa.*",
    r"^.*\.keytab$",
]

# Live-credential prefixes for every provider this project talks to.
# Written as split literals so this file does not itself trip a secret scanner.
SECRET_CONTENT_PATTERNS = [
    (r"sk-or-v1-[A-Za-z0-9]{32,}", "OpenRouter API key"),
    (r"sk-proj-[A-Za-z0-9_\-]{32,}", "OpenAI project key"),
    (r"\bBSA[A-Za-z0-9_\-]{20,}", "Brave Search API key"),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "private key"),
    (r'"client_secret"\s*:\s*"GOCSPX-', "Google OAuth client secret"),
    (r"\bghp_[A-Za-z0-9]{30,}", "GitHub personal access token"),
    (r"\bxox[baprs]-[A-Za-z0-9\-]{10,}", "Slack token"),
]

# Text this file and the docs legitimately contain: the *names* of keys, never
# their values. Skip the files whose whole job is to talk about credentials.
SELF_REFERENTIAL = {
    "tests/gate/test_no_secrets_tracked.py",
    "core/secrets.py",
}


def test_git_tracks_no_credential_filenames():
    # New files too, which the next `git add` takes: this read what git
    # tracked alone, so a key file made before a commit passed the suite.
    offenders = []
    for rel in repo_files.listed():
        name = Path(rel).name
        for pattern in FORBIDDEN_NAME_PATTERNS:
            if re.match(pattern, name, re.IGNORECASE):
                offenders.append(rel)
                break
    assert not offenders, f"credential-shaped files are tracked: {offenders}"


def test_no_tracked_file_contains_a_live_credential():
    offenders: list[str] = []
    for rel in repo_files.listed():
        if rel in SELF_REFERENTIAL:
            continue
        text = repo_files.text(rel)
        if text is None:  # not UTF-8: read what text it holds
            try:
                text = (REPO_ROOT / rel).read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
        for pattern, label in SECRET_CONTENT_PATTERNS:
            if re.search(pattern, text):
                offenders.append(f"{rel}: looks like a {label}")
    assert not offenders, "live credentials found in tracked files:\n  " + "\n  ".join(offenders)


def test_data_directory_is_ignored():
    """Everything secret-adjacent lives under data/. Prove git cannot see it."""
    probe = REPO_ROOT / "data" / "secrets" / ".gitignore-probe"
    probe.parent.mkdir(parents=True, exist_ok=True)
    probe.write_text("probe", encoding="utf-8")
    try:
        result = subprocess.run(
            ["git", "check-ignore", "-q", str(probe)],
            cwd=REPO_ROOT,
            capture_output=True,
        )
        assert result.returncode == 0, "data/ is NOT gitignored; credentials are exposed"
    finally:
        probe.unlink(missing_ok=True)


@pytest.mark.parametrize(
    "sample,should_match",
    [
        ("sk-or-v1-" + "a" * 40, True),
        ("-----BEGIN RSA PRIVATE KEY-----", True),
        ('"client_secret": "GOCSPX-abc123"', True),
        ("OPENROUTER_API_KEY", False),  # the *name* must stay allowed
        ("sk-or-v1-short", False),
    ],
)
def test_content_patterns_discriminate(sample, should_match):
    """The scanner must not fire on key names, or it will be disabled."""
    hit = any(re.search(p, sample) for p, _ in SECRET_CONTENT_PATTERNS)
    assert hit is should_match, f"{sample!r} -> {hit}, expected {should_match}"
