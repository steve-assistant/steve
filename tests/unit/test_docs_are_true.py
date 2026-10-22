"""The documents name real files and real functions.

The top-level documents (`README.md`, `CLAUDE.md`, `SECURITY.md`...) and
those in `docs/` are how a reader --
the user, the assistant working here, or the agent reading its own code --
finds their way in, and they are written in file paths, function names and
commands. Code moves: `agent/loop.py` and `agent/server.py` were split
in one night, and every name the documents used for them went stale at once.
A map that points at things that are not there is worse than no map, so the
suite checks the map. The README's commands are the first thing a new user
types: the audit of 12 October 2026 found it still the personal edition's.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DOCS = [*sorted(REPO.glob("*.md")), *sorted((REPO / "docs").glob("*.md"))]

# `agent/worker/answering.py`, `tests/gate/test_x.py`, `core/policy.yaml`,
# `modules/imap/manifest.toml`, `ui/src/Accounts.tsx`...
PATH = re.compile(
    r"`((?:agent|core|relay|channels|tools|tests|docs|voice|auth|db|devtools|modules|steve_sdk"
    r"|ui)/[\w/.-]+\.(?:py|md|yaml|sql|toml|ts|tsx|rs|json))`"
)
# `agent/worker/answering.intercept_approval`, `worker/duties.take_up_duties`,
# `modules/google_cloud/auth.scopes`, `steve_sdk/manifest.Field`
QUALIFIED = re.compile(
    r"`((?:agent/)?(?:worker|toolset)/\w+|agent/\w+|relay/\w+|core/\w+|modules/\w+/\w+"
    r"|steve_sdk/\w+)\.(?!(?:py|md|yaml|sql|lock|json|toml|ts|tsx|rs)`)(\w+)`"
)
# `python -m core.secrets set OPENAI_API_KEY`, `python -m core.supervisor`
COMMAND = re.compile(
    r"python -m ((?:agent|core|relay|channels|tools|voice|auth|devtools)(?:\.\w+)+)"
)


def _defined_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _module_file(ref: str) -> Path | None:
    ref = ref if ref.startswith(("agent/", "relay/", "core/", "modules/", "steve_sdk/")) \
        else f"agent/{ref}"
    for candidate in (REPO / f"{ref}.py", REPO / ref / "__init__.py"):
        if candidate.exists():
            return candidate
    return None


def _runnable(module: str) -> bool:
    """Whether `python -m module` finds something to run."""
    path = REPO / module.replace(".", "/")
    return path.with_suffix(".py").exists() or (path / "__main__.py").exists()


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_every_path_a_document_names_exists(doc):
    text = doc.read_text(encoding="utf-8")
    missing = sorted({p for p in PATH.findall(text) if not (REPO / p).exists()})

    assert not missing, f"{doc.name} names files that are not there: {missing}"


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_every_function_a_document_names_is_where_it_says(doc):
    text = doc.read_text(encoding="utf-8")
    wrong = []
    for module, name in QUALIFIED.findall(text):
        path = _module_file(module)
        if path is None:
            wrong.append(f"{module}.{name} (no module {module})")
        elif name not in _defined_names(path):
            wrong.append(f"{module}.{name}")

    assert not wrong, f"{doc.name} names things that are not where it says: {wrong}"


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_every_command_a_document_gives_runs_a_module_that_is_there(doc):
    text = doc.read_text(encoding="utf-8")
    missing = sorted({m for m in COMMAND.findall(text) if not _runnable(m)})

    assert not missing, f"{doc.name} gives commands for modules that are not there: {missing}"


def test_the_check_finds_references_at_all():
    """A pattern that matches nothing passes forever."""
    flows = (REPO / "docs" / "flows.md").read_text(encoding="utf-8")

    assert len(PATH.findall(flows)) > 10
    assert len(QUALIFIED.findall(flows)) > 5
    # The modules, the SDK and the app, which the patterns skipped until
    # step 5 (05-connectors.md, section 11).
    model = (REPO / "docs" / "security-model.md").read_text(encoding="utf-8")
    assert {p.split("/")[0] for p in PATH.findall(model)} >= {"modules", "ui"}
    assert any(module.startswith("modules/") for module, _ in QUALIFIED.findall(model))
    architecture = (REPO / "docs" / "architecture.md").read_text(encoding="utf-8")
    assert any(p.startswith("steve_sdk/") for p in PATH.findall(architecture))
    # Build and test commands live in the contributor's guide; the README
    # now takes an installer user to a first answer without a terminal.
    contributing = (REPO / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert len(COMMAND.findall(contributing)) > 5
    assert not _runnable("agent.nothing_here")
    # The documents a stranger reads first at the top of the repository,
    # which only README.md and CLAUDE.md of were read until step 11.
    assert {REPO / "PRIVACY.md", REPO / "SECURITY.md"} <= set(DOCS)
