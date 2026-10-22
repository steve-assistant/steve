"""A module reaches Steve only through `steve_sdk`, and secrets only through
`core/credstore.py`.

Before modules, a provider's code imported whatever it liked: the keyring,
the database, the agent's state. A module that can import `core` can read
any secret and write any table, whatever its manifest says, so the manifest
would describe nothing (05-connectors.md, section 1; 06-modules.md,
section 2). The rules here are read from the source, before any module runs:

- `steve_sdk` imports only the standard library and itself, so a module
  author installs nothing of Steve's to build one;
- a module imports only the standard library, `steve_sdk`, its own files and
  the libraries its manifest declares;
- only `core/credstore.py` imports `win32cred`, so every module secret goes
  through its prefix checks.
"""

from __future__ import annotations

import sys
from pathlib import Path

from core.sandbox.imports import REACHES_PAST, imports
from steve_sdk.manifest import load
from tests.gate import repo_files

REPO = Path(__file__).resolve().parents[2]


def _imports(rel: str) -> list[tuple[str, int]]:
    return imports(repo_files.text(rel) or "", rel)


def test_the_sdk_imports_only_the_standard_library_and_itself():
    bad = [f"{rel}:{line} imports {name}"
           for rel in repo_files.python() if rel.startswith("steve_sdk/")
           for name, line in _imports(rel)
           if name not in sys.stdlib_module_names and name not in {"steve_sdk", "."}]
    assert not bad, bad


# Today's connectors, moved into their modules whole in step 5's item 4f so
# that git kept their history, and refitted on `ctx` by later items: each
# file's imports past the rule, with the item that ends them. A ratchet: an
# import not listed fails, and so does an entry no longer needed, until it
# is removed, so the list only shrinks.
NOT_YET_REFITTED: dict[str, tuple[set[str], str]] = {
}


def test_a_module_imports_only_the_stdlib_the_sdk_its_own_files_and_its_declared_libraries():
    bad, unrefitted = [], {}
    for rel in repo_files.python():
        parts = rel.split("/")
        if parts[0] != "modules" or len(parts) < 3:
            continue
        manifest_path = REPO / "modules" / parts[1] / "manifest.toml"
        declared = set(load(manifest_path).libraries) if manifest_path.is_file() else set()
        allowed = {"steve_sdk", "."} | declared
        excused = NOT_YET_REFITTED.get(rel, (set(), ""))[0]
        for name, line in _imports(rel):
            if name in excused:
                unrefitted.setdefault(rel, set()).add(name)
            elif name == "modules":
                bad.append(f"{rel}:{line} imports through modules. (use a relative import)")
            elif name in REACHES_PAST:
                bad.append(f"{rel}:{line} imports {name}, which reaches past the context")
            elif name not in sys.stdlib_module_names and name not in allowed:
                bad.append(f"{rel}:{line} imports {name}")
    assert not bad, bad
    stale = {rel: sorted(names - unrefitted.get(rel, set()))
             for rel, (names, _) in NOT_YET_REFITTED.items()
             if names - unrefitted.get(rel, set())}
    assert not stale, f"refitted: take these out of NOT_YET_REFITTED: {stale}"


def test_only_the_credential_store_imports_win32cred():
    # Only a file that names it can import it; parsing the rest took two
    # seconds of every check.
    users = sorted(rel for rel in repo_files.python() if not rel.startswith("tests/")
                   and "win32cred" in (repo_files.text(rel) or "")
                   and any(name == "win32cred" for name, _ in _imports(rel)))
    assert users == ["core/credstore.py"]


def test_the_guard_sees_an_import_hidden_in_a_function():
    """A check that only read the top of a file would pass a module that
    imports `core` inside the function that runs."""
    source = "def f():\n    from core import credstore\n    __import__('os')\n"
    assert [name for name, _ in imports(source, "m.py")] == ["core", "<dynamic>"]
