"""Every data path is built by core/paths.py, and only there.

About forty modules each built `REPO_ROOT / "data" / ...`, bound when the
module was imported. An installed Steve has no checkout, so each would have
written into the code folder its next update replaces; and a test had to
patch every one of them, the database, the sign-in token, the memories, the
logs, or write to the real folder. The scan fails on the next one.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from core import paths
from tests.gate import repo_files

REPO = Path(__file__).resolve().parents[2]
# Not shipped, so not scanned: they may name the checkout's data/ to check
# that nothing else does.
NOT_SHIPPED = {".venv", ".git", "tests", "spikes", "data", "docs", "node_modules"}
PATHS = Path("core/paths.py")


def _is_data(node: ast.AST, *, joined: bool = False) -> bool:
    """A string that starts a path with the `data` folder. `joined`: a piece
    glued onto something before it (`+ "/data/x"`, f"{root}/data/x"), so its
    leading separator goes first."""
    if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
        return False
    text = node.value.replace("\\", "/")
    return (text.lstrip("/") if joined else text).split("/")[0] == "data"


# Calls that read a bare string as a path relative to the working directory,
# which is the checkout only when a process happens to start there.
_RELATIVE = {"Path", "PurePath", "PureWindowsPath", "open", "join", "makedirs", "mkdir",
             "abspath", "realpath"}


def _derived(expr: ast.AST, names: set[str], known: set[str]) -> bool:
    """Whether `expr` is built from the module's own location: `__file__`,
    a name bound to one, or another module's (`service.REPO_ROOT`) -- or from
    the working directory, which is the checkout's only by chance."""
    return _from(_parts(expr), names, known)


def _parts(expr: ast.AST) -> tuple[set[str], set[str]]:
    """The names and the attributes `expr` is built from. A call for the
    working directory counts as the name `__file__`."""
    used: set[str] = set()
    attrs: set[str] = set()
    for sub in ast.walk(expr):
        if isinstance(sub, ast.Name):
            used.add(sub.id)
        elif isinstance(sub, ast.Attribute):
            attrs.add(sub.attr)
        elif isinstance(sub, ast.Call) and _called(sub) in ("cwd", "getcwd"):
            used.add("__file__")
    return used, attrs


def _from(parts: tuple[set[str], set[str]], names: set[str], known: set[str]) -> bool:
    used, attrs = parts
    return "__file__" in used or bool(used & names) or "__file__" in attrs or bool(attrs & known)


def _called(node: ast.Call) -> str:
    func = node.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


def _roots(tree: ast.Module, known: set[str]) -> set[str]:
    """The names in a module bound to a path built from its location:
    assigned one, returning one, or imported from a module that has one."""
    # Each binding and what its values are built from, read once. Walking
    # the whole module, and every function's body again, on each round took
    # twenty seconds over the tree.
    bindings: list[tuple[set[str], list[tuple[set[str], set[str]]]]] = []
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign | ast.AnnAssign) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            bindings.append(({t.id for t in targets if isinstance(t, ast.Name)},
                             [_parts(node.value)]))
        elif isinstance(node, ast.FunctionDef):
            bindings.append(({node.name}, [_parts(r.value) for r in ast.walk(node)
                                           if isinstance(r, ast.Return) and r.value is not None]))
        elif isinstance(node, ast.ImportFrom):
            imported |= {a.asname or a.name for a in node.names if a.name in known}
    names = imported
    while True:
        found = set(names)
        for bound, values in bindings:
            if not bound <= found and any(_from(v, names, known) for v in values):
                found |= bound
        if found == names:
            return names
        names = found


def _offences(tree: ast.Module, known: set[str]) -> list[int]:
    names = _roots(tree, known)

    def derived(expr: ast.AST) -> bool:
        return _derived(expr, names, known)

    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            found = _is_data(node.right) and derived(node.left)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            found = _is_data(node.right, joined=True) and derived(node.left)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            found = (isinstance(node.left, ast.Constant) and isinstance(node.left.value, str)
                     and "data" in node.left.value.replace("\\", "/").split("/")[1:]
                     and derived(node.right))
        elif isinstance(node, ast.JoinedStr):
            found = any(isinstance(a, ast.FormattedValue) and derived(a.value)
                        and _is_data(b, joined=True)
                        for a, b in zip(node.values, node.values[1:], strict=False))
        elif isinstance(node, ast.Call) and any(_is_data(a) for a in node.args):
            others = [node.func, *(a for a in node.args if not _is_data(a))]
            found = (any(derived(o) for o in others)
                     or (_called(node) in _RELATIVE and _is_data(node.args[0])))
        else:
            found = False
        if found:
            lines.append(node.lineno)
    return lines


def _bound_at_import(tree: ast.Module, asked: set[str]) -> list[int]:
    """Lines that call one of `core/paths.py`'s functions when the module is
    imported: a module or class constant, or a default argument. Bound then,
    the path is the data folder of whoever imported it first -- before a test,
    or a worktree, chose its own -- and stays that for the process."""
    lines = []

    def calls(expr: ast.AST | None) -> bool:
        return expr is not None and any(
            isinstance(sub, ast.Call) and _called(sub) in asked for sub in _eager(expr))

    bodies = [tree.body]
    while bodies:
        for node in bodies.pop():
            if isinstance(node, ast.ClassDef):
                bodies.append(node.body)
            elif isinstance(node, ast.Assign | ast.AnnAssign) and calls(node.value):
                lines.append(node.lineno)
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                defaults = [*node.args.defaults, *node.args.kw_defaults]
                if any(calls(d) for d in defaults):
                    lines.append(node.lineno)
    return lines


def _eager(expr: ast.AST):
    """The nodes of `expr` evaluated with it: not a lambda's body."""
    yield expr
    if not isinstance(expr, ast.Lambda):
        for child in ast.iter_child_nodes(expr):
            yield from _eager(child)


def _asked() -> set[str]:
    """`core/paths.py`'s functions that answer from the chosen data folder."""
    tree = ast.parse((REPO / PATHS).read_text(encoding="utf-8"))
    return {n.name for n in tree.body if isinstance(n, ast.FunctionDef)
            and not n.name.startswith("_")} - {"is_source_tree"}


def _shipped() -> dict[Path, ast.Module]:
    # The files git knows, tracked or new, not a walk of the folder: the walk
    # also read the copy of the tree in a worktree a tool had left under
    # .claude/, and failed on code that was not this checkout's.
    return {Path(name): repo_files.parsed(name) for name in repo_files.python()
            if Path(name).parts[0] not in NOT_SHIPPED}


def _module_level(tree: ast.Module, *, functions: bool = True) -> set[str]:
    top = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            top |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            top.add(node.target.id)
        elif isinstance(node, ast.FunctionDef) and functions:
            top.add(node.name)
    return top & _roots(tree, set())


def test_no_module_but_core_paths_finds_the_data_folder():
    trees = _shipped()
    assert {"agent", "channels", "core", "devtools", "modules", "tools", "voice"} \
        <= {rel.parts[0] for rel in trees}, "the scan must cover the shipped packages"
    # Another module's location reached as an attribute (`service.REPO_ROOT`)
    # or imported by name counts as its own: gather them all first.
    # core/paths.py's functions are the way to the data folder; only its
    # constant, the code's own location, counts.
    known: set[str] = set()
    for rel, tree in trees.items():
        known |= _module_level(tree, functions=rel != PATHS)
    offences = [f"{rel.as_posix()}:{line}"
                for rel, tree in trees.items() if rel != PATHS
                for line in _offences(tree, known)]
    assert offences == [], "build data paths with core/paths.py: " + ", ".join(offences)


def test_no_module_binds_a_data_path_when_it_is_imported():
    """`voice/wake_train.py` bound its folder as a constant: a test, or a
    worktree with its own STEVE_DATA_DIR, would have used whatever folder
    the first importer had, and written its gigabytes there."""
    asked = _asked()
    assert {"data_dir", "db_path", "scratch_dir"} <= asked
    bound = [f"{rel.as_posix()}:{line}" for rel, tree in _shipped().items()
             for line in _bound_at_import(tree, asked)]
    assert bound == [], "ask core/paths.py when the path is needed: " + ", ".join(bound)


@pytest.mark.parametrize("source", [
    'DB = paths.db_path()',
    'class Store:\n    ROOT = paths.data_dir() / "store"',
    'def open_db(path=paths.db_path()):\n    pass',
    'DATA = Path(os.environ.get("X") or paths.data_dir() / "wake")',
])
def test_the_scan_finds_a_data_path_bound_at_import(source):
    assert _bound_at_import(ast.parse(source), {"data_dir", "db_path"})


@pytest.mark.parametrize("source", [
    'def db():\n    return paths.db_path()',
    'DB = lambda: paths.db_path()',
    'def open_db(path=None):\n    path = path or paths.db_path()',
])
def test_the_scan_passes_a_data_path_asked_for_when_needed(source):
    assert _bound_at_import(ast.parse(source), {"data_dir", "db_path"}) == []


@pytest.mark.parametrize("source", [
    'ROOT = Path(__file__).resolve().parent.parent\nDB = ROOT / "data" / "agent.db"',
    'LOGS = Path(__file__).parents[1] / "data/logs"',
    'def here():\n    return Path(__file__).parent\n\nX = here() / "data"',
    'X = os.path.join(os.path.dirname(__file__), "..", "data")',
    'X = Path(__file__).parent.joinpath("data")',
    'X = Path(models.__file__).parent.parent / "data"',
    'from agent.service import REPO_ROOT\nX = REPO_ROOT / "data"',
    'X = service.REPO_ROOT / "data"',
    'X = f"{service.REPO_ROOT}/data/agent.db"',
    'X = str(service.REPO_ROOT) + "\\\\data\\\\agent.db"',
    'X = "%s/data/agent.db" % service.REPO_ROOT',
    'X = Path("data/agent.db")',
    'X = open("data/knobs.json")',
    'X = os.path.join("data", "logs")',
    'X = Path.cwd() / "data"',
])
def test_the_scan_finds_a_data_path_built_from_the_code(source):
    assert _offences(ast.parse(source), {"REPO_ROOT"})


@pytest.mark.parametrize("source", [
    'X = paths.data_dir() / "logs"',
    'X = signal_data() / "data" / "accounts.json"',
    'X = Path(__file__).parent / "prompts"',
    'X = paths.SOURCE / "db" / "migrations"',
    'X = reply.get("data")',
    'X = tar.extractall(out, filter="data")',
    'X = f"{prefix}data"',
    'X = f"{url}/data/feed"',
])
def test_the_scan_passes_paths_that_are_not_the_data_folder(source):
    assert _offences(ast.parse(source), {"SOURCE"}) == []


def test_steve_data_dir_comes_first(monkeypatch, tmp_path):
    monkeypatch.setenv("STEVE_DATA_DIR", str(tmp_path / "elsewhere"))
    assert paths.data_dir() == tmp_path / "elsewhere"
    assert paths.db_path() == tmp_path / "elsewhere" / "agent.db"


def test_a_checkout_keeps_its_data_beside_its_code(monkeypatch, tmp_path):
    monkeypatch.delenv("STEVE_DATA_DIR", raising=False)
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    monkeypatch.setattr(paths, "SOURCE", tmp_path)
    assert paths.data_dir() == tmp_path / "data"


def test_an_installed_steve_keeps_its_data_in_local_app_data(monkeypatch, tmp_path):
    """Its code folder (`backend\\app\\`, marked by `installed.json`) is
    replaced at every update; the data outlives it."""
    monkeypatch.delenv("STEVE_DATA_DIR", raising=False)
    code = tmp_path / "Programs" / "Steve" / "backend" / "app"
    code.mkdir(parents=True)
    (code / paths.INSTALLED_MARKER).write_text("{}", encoding="utf-8")
    monkeypatch.setattr(paths, "SOURCE", code)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    assert paths.data_dir() == tmp_path / "Local" / "Steve"


def test_memories_and_models_can_be_kept_apart(monkeypatch, tmp_path):
    monkeypatch.setenv("STEVE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("STEVE_MEMORIES_DIR", raising=False)
    monkeypatch.delenv("STEVE_MODEL_CACHE", raising=False)
    assert paths.memories_dir() == tmp_path / "data" / "memories"
    assert paths.models_dir() == tmp_path / "data" / "models"

    monkeypatch.setenv("STEVE_MEMORIES_DIR", str(tmp_path / "memories"))
    monkeypatch.setenv("STEVE_MODEL_CACHE", str(tmp_path / "models"))
    assert paths.memories_dir() == tmp_path / "memories"
    assert paths.models_dir() == tmp_path / "models"


@pytest.mark.parametrize("module_id, version", [
    ("webby\n", "1"), ("webby", "1\n"), ("webby", ".."), ("..", "1"), ("webby", "1/../.."),
])
def test_a_modules_folders_are_named_only_by_a_whole_id_and_version(module_id, version):
    """A module's id and version name its folders. A pattern ending in `$`
    also matches before a final line break, and `re.match` let "webby\n"
    through as an id other than "webby"."""
    with pytest.raises(ValueError, match="not a module"):
        paths.module_code_dir(module_id, version)
    assert paths.module_code_dir("webby", "1.0.2") == paths.module_dir("webby") / "code" / "1.0.2"


def test_a_test_never_has_the_checkouts_data_folder():
    """tests/gate/fences.py gives every test a data folder of its own."""
    checkout = (paths.SOURCE / "data").resolve()
    assert not paths.data_dir().resolve().is_relative_to(checkout)
    assert not paths.memories_dir().resolve().is_relative_to(checkout)
