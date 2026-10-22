"""The first guides must work after private planning documents leave the tree.

The release splits the README by reader. Broken links or a reference to a
private plan would leave a new user stranded even if every code name was
real. The plans leave the public tree before the first release, since they
name real people and say how the work went, so no shipped document may name
one: the code's map once did fourteen times ("06-modules.md, section 1").
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

from tests.gate import repo_files

ROOT = Path(__file__).resolve().parents[2]
GUIDES = ("README.md", "CONTRIBUTING.md", "docs/user-guide.md")
PRIVATE_PLAN = re.compile(
    r"(?:docs|private)[/\\]plan[/\\]|\b(?:\d{2}[a-z]?-[\w-]+|s\d+-[\w-]+"
    r"|handoff|building|later-steps|v01-sort)\.md\b", re.I)
LINK = re.compile(r"(?<!!)\[[^\]]+\]\(([^\s)]+)\)")


def test_no_shipped_document_names_a_private_plan():
    """The public copy has no planning folder, including bare plan names."""
    named = {rel: sorted(set(PRIVATE_PLAN.findall(repo_files.text(rel) or "")))
             for rel in repo_files.listed() if rel.endswith(".md")
             and not rel.startswith("private/")}

    assert {rel: found for rel, found in named.items() if found} == {}, (
        "a shipped document names a plan; say in place what it pointed to")


@pytest.mark.parametrize("name", GUIDES)
def test_each_local_link_in_a_release_guide_reaches_a_file_and_heading(name):
    """Moving a section must move its incoming links as well as its words."""
    guide = ROOT / name
    for link in LINK.findall(guide.read_text(encoding="utf-8")):
        parts = urlsplit(link)
        if parts.scheme or parts.netloc:
            continue
        target = (guide.parent / unquote(parts.path)).resolve() if parts.path else guide
        assert target.is_relative_to(ROOT), f"{name}: link leaves the checkout: {link}"
        assert target.is_file(), f"{name}: missing linked file: {link}"
        if parts.fragment and target.suffix == ".md":
            headings = re.findall(r"^#{1,6} (.+)$", target.read_text(encoding="utf-8"), re.M)
            anchors = {re.sub(r"[^\w\- ]", "", title.lower()).replace(" ", "-")
                       for title in headings}
            assert unquote(parts.fragment) in anchors, f"{name}: missing heading: {link}"


@pytest.mark.parametrize("reference", ["docs/plan/12-release.md", "DOCS\\PLAN\\file.md",
                                       "08-packaging.md", "10b-before-the-audit.md",
                                       "private/plan/report.md", "s1-appcontainer.md",
                                       "handoff.md", "building.md", "later-steps.md",
                                       "v01-sort.md"])
def test_the_private_plan_pattern_recognises_paths_and_bare_plan_names(reference):
    """A relative filename is just as unusable as the full private path."""
    assert PRIVATE_PLAN.search(reference)


def test_the_private_plan_pattern_recognises_every_current_plan_filename():
    """Scouting only numbered steps missed the spikes and shared handoff.
    The plans are in the maintainer's private folder, so a checkout without
    them has nothing to read."""
    plans = ROOT / "private" / "plan"
    if not plans.is_dir():
        pytest.skip("the plans are private, and this checkout has none")
    missed = sorted({path.name for path in plans.rglob("*.md")
                     if not PRIVATE_PLAN.search(path.name)})
    assert not missed, f"private plan names the guard misses: {missed}"
