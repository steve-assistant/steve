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

import pytest

from core import paths
from devtools import package
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
    off switch. Tauri's license page shows, before installing, the
    statement's own text made plain, and only as PRIVACY.md now says it."""
    tauri = paths.SOURCE / "ui" / "src-tauri"
    config = json.loads((tauri / "tauri.conf.json").read_text(encoding="utf-8"))
    named = config["bundle"].get("licenseFile")
    assert named, "the installer must show PRIVACY.md before installing"
    assert (tauri / named).resolve() == package.PRIVACY_PAGE.resolve()
    assert package.PRIVACY == STATEMENT
    shown = package.PRIVACY_PAGE.read_text(encoding="utf-8")
    assert shown == package.privacy_page(STATEMENT.read_text(encoding="utf-8")), (
        "PRIVACY.md changed: run python -m devtools.package privacy")


def test_the_installers_privacy_page_shows_no_markdown():
    """NSIS's page shows a file as it is. The first build showed PRIVACY.md
    itself, with every # and ** (22 October 2026)."""
    shown = package.privacy_page(STATEMENT.read_text(encoding="utf-8"))
    assert "WHAT LEAVES YOUR COMPUTER" in shown.splitlines()
    assert not re.search(r"^#|\*\*|`", shown, re.MULTILINE)
    assert "- OpenAI: api.openai.com" in shown.splitlines()


def test_markdown_the_page_cannot_show_is_refused_not_shown_with_its_marks():
    """A link or a table added to the statement would reach every
    installer as raw markup; the page is refused instead, naming the line."""
    for line in ("See [the terms](https://example.com/terms).", "| host | why |",
                 "> quoted", "```", "<b>bold</b>"):
        with pytest.raises(package.BuildError, match="line 3"):
            package.privacy_page(f"# Title\n\n{line}\n")
