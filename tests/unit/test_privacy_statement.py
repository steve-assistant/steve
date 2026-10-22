"""The privacy statement (PRIVACY.md), held to the security model's list of
what leaves the machine.

The statement is what a user reads before Steve starts, on Setup's first
page. Section 6 of docs/security-model.md is what the code is held to:
tests/gate/test_hosts_named.py fails on a host the shipped code names that
the section does not. Nothing tied the two, so a host added to the section
with its code would have been a destination the user was never told of.
"""

from __future__ import annotations

import json
import re

from core import paths
from tests.gate.test_hosts_named import MODEL, listed_in

STATEMENT = paths.SOURCE / "PRIVACY.md"
SETUP = paths.SOURCE / "ui" / "src" / "Setup.tsx"


def test_the_privacy_statement_names_every_host_the_security_model_lists():
    """Each host section 6 names, the statement names too: a new
    destination is told to the user in the same change that adds it."""
    said = set(re.findall(r"`([^`]+)`", STATEMENT.read_text(encoding="utf-8")))
    listed = listed_in(MODEL.read_text(encoding="utf-8"))
    assert listed
    missing = sorted(listed - said)
    assert not missing, f"name these in PRIVACY.md's 'Every host': {missing}"


def test_setup_shows_the_privacy_statement_itself():
    """Setup's first page shows the file, built into the app, rather than a
    summary of it that could drift, or a link that works only online."""
    source = SETUP.read_text(encoding="utf-8")
    assert re.search(r'^import \w+ from "\.\./\.\./PRIVACY\.md\?raw";$', source, re.MULTILINE)


def test_the_installer_shows_the_same_privacy_statement_as_setup():
    """A separate installer summary could omit a new destination or its
    off switch. Tauri's license page reads the statement itself, before
    installing, so there is only one text to keep current."""
    tauri = paths.SOURCE / "ui" / "src-tauri"
    config = json.loads((tauri / "tauri.conf.json").read_text(encoding="utf-8"))
    named = config["bundle"].get("licenseFile")
    assert named, "the installer must show PRIVACY.md before installing"
    assert (tauri / named).resolve() == STATEMENT.resolve()
    assert STATEMENT.is_file()
