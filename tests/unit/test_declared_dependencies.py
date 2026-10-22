"""Everything imported is declared.

This exists because `qrcode` was not. It had been installed by hand while
getting Signal linking working, so the laptop imported it happily and nothing
ever noticed -- until a fresh environment on another machine tried,
mid-deploy, at the exact step that needs it.

That is the shape of the bug worth guarding: a package that is present by
accident works perfectly on the machine where the accident happened. No amount
of running the test suite locally finds it, because locally it is installed.
So the check is not "does the import work" but "does the lockfile know about
it" -- and the lockfile only knows what was declared.
"""

from __future__ import annotations

import ast
import re
import sys
from importlib.metadata import packages_distributions
from pathlib import Path

import pytest

from tests.gate import repo_files

# First-party packages. An import of one of these is not a dependency.
OURS = {"agent", "channels", "core", "tools", "voice", "tests", "ui", "steve_sdk", "modules"}

LOCKFILE = Path("uv.lock")


def _top_level_imports(rel: str) -> set[str]:
    """Every top-level module name imported by a file, including inside
    functions -- lazy imports are still dependencies."""
    names: set[str] = set()
    for node in ast.walk(repo_files.parsed(rel)):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import, so first-party
                continue
            if node.module:
                names.add(node.module.split(".")[0])
    return names


@pytest.fixture(scope="module")
def locked_distributions() -> set[str]:
    """Distribution names uv resolved, normalised."""
    text = LOCKFILE.read_text(encoding="utf-8")
    return {
        re.sub(r"[-_.]+", "-", name).lower()
        for name in re.findall(r'^name = "([^"]+)"', text, re.M)
    }


@pytest.fixture(scope="module")
def module_to_distribution() -> dict[str, list[str]]:
    return packages_distributions()


def test_the_lockfile_is_readable(locked_distributions):
    assert len(locked_distributions) > 50, "uv.lock looks empty or unparsed"


def test_every_imported_package_is_in_the_lockfile(
    locked_distributions, module_to_distribution
):
    """The check that would have caught qrcode before a fresh install did."""
    undeclared: dict[str, set[str]] = {}

    for path in repo_files.python():
        for module in _top_level_imports(path):
            if module in OURS or module in sys.stdlib_module_names:
                continue
            distributions = module_to_distribution.get(module)
            if not distributions:
                # Not installed at all here. Either genuinely absent, or an
                # optional import guarded at runtime; the import test below
                # is the one that speaks to that.
                continue
            normalised = {
                re.sub(r"[-_.]+", "-", d).lower() for d in distributions
            }
            if not (normalised & locked_distributions):
                undeclared.setdefault(module, set()).add(str(path))

    assert not undeclared, (
        "these are imported but no declared dependency provides them -- they "
        "are installed here by accident and will be missing anywhere else:\n"
        + "\n".join(f"  {m}: {sorted(f)}" for m, f in sorted(undeclared.items()))
    )


def test_qrcode_specifically_is_declared(locked_distributions):
    """Named on its own because of how it failed: not at import time, not in
    any test, but on a cloud machine at the one step that needs it -- linking
    a Signal device, which is interactive and not something you retry
    casually."""
    assert "qrcode" in locked_distributions
