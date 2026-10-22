"""What a module's code imports, read from its source before any of it runs.

The sandbox is what holds a module: in its container, `import socket`
reaches no network and `import os` no file of the user's. But a module that
imports them fails there at the user's first use, with an error that says
nothing of why, and a module built by Steve would be judged by tests that
fail on its first line. So admission reads every file first and refuses
with the reason (06-modules.md, section 4): a module imports only the
standard library, `steve_sdk`, and its own files, relatively or by its id.

What this cannot see, it does not pretend to: a name built at run time
(`__import__`, `importlib.import_module`) is refused as `<dynamic>`, and the
container holds whatever a read of the source misses. The same reading
checks the built-ins and the SDK (tests/gate/test_module_imports.py).
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

# Standard modules that reach past `ctx`: the network without its host
# rules, other processes, the environment (which holds the other install's
# variables), the registry, and other code loaded by name. A module's
# connection goes through `ctx.http` or `ctx.com`. Each comes with the C
# module beneath it, which does the same under a name of its own: a list of
# the public names alone let `import nt` stand in for `os`, and `_socket`
# for `socket`.
REACHES_PAST = frozenset({
    "<dynamic>", "os", "sys", "socket", "ssl", "http", "urllib", "smtplib", "imaplib",
    "poplib", "ftplib", "ctypes", "subprocess", "multiprocessing", "importlib", "runpy",
    "winreg", "_winapi", "msvcrt", "pickle", "marshal", "builtins", "code", "shutil",
    "nt", "posix", "_socket", "_overlapped", "_ssl", "_ctypes", "_posixsubprocess",
    "_multiprocessing", "_imp", "_frozen_importlib", "_frozen_importlib_external",
    "_pickle",
})


def imports(source: str, name: str = "<module>") -> list[tuple[str, int]]:
    """Each absolute import's top-level name, with a relative one as "." and
    a name built at run time as "<dynamic>"; and the line it is on.
    SyntaxError if `source` does not parse."""
    tree = ast.parse(source, name)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [(alias.name.split(".")[0], node.lineno) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                found.append((".", node.lineno))
            elif node.module and node.module != "__future__":
                found.append((node.module.split(".")[0], node.lineno))
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
              and node.func.id == "__import__") or (
                isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "import_module"):
            found.append(("<dynamic>", node.lineno))
    return found


# What a module's own tests may import besides: they run under pytest in
# the module's sandbox (core/sandbox/testrun.py), and the guide tells
# authors to write them in it. Refused like any other library, a test's
# first `pytest.raises` had the module turned away, for an import that
# only its test run makes, where pytest is installed.
TESTS = "tests"
FOR_TESTS = frozenset({"pytest"})


def problems(folder: Path, *, own: str) -> list[str]:
    """Why the code under `folder` may not be admitted as the module `own`:
    each import past the rule, and each file that does not parse, with its
    file and line. [] when there is none."""
    found = []
    for path in sorted(folder.rglob("*.py")):
        rel = path.relative_to(folder).as_posix()
        try:
            said = imports(path.read_text(encoding="utf-8"), rel)
        # A file well under the size limit can nest past what the parser
        # takes: `1 + 1 + ...` 5,000 times is a RecursionError, `-` 200,000
        # times a MemoryError. Each escaped this as a crash, not a refusal.
        except (SyntaxError, UnicodeDecodeError, ValueError, RecursionError, MemoryError) as exc:
            found.append(f"{rel}: does not parse ({type(exc).__name__})")
            continue
        allowed = {"steve_sdk", ".", own}
        if rel.split("/")[0] == TESTS:
            allowed |= FOR_TESTS
        for name, line in said:
            if name in REACHES_PAST:
                found.append(f"{rel}:{line} imports {name}, which reaches past the context")
            elif name not in sys.stdlib_module_names and name not in allowed:
                found.append(f"{rel}:{line} imports {name}, which is neither the standard "
                             f"library nor steve_sdk")
    return found
