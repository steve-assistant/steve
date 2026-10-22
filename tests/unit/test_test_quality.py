"""Brittle test patterns may only become fewer.

Most failed self-upgrades in the shadow week were not failed by the change
(tests-validity audit, 8 October 2026). Some of the rest were failed by
tests that pin how code is written rather than what it does, and those were
found only after they had killed an attempt or cost a rerun:

- A string looked for in a function's source (`"audit.is_revoked(" in
  inspect.getsource(...)`): a valid refactor fails it, a different route
  to the same mistake passes it. About a dozen existed; a coder that
  renamed a variable could only die, or loosen the test and be rejected.
- A wall-clock upper bound (three 0.3 s calls "together" in under 0.6 s):
  correct code fails it on a loaded laptop or in the 6 GB sandbox under
  -n 3. The mail-search concurrency test failed exactly so on 5 and 6
  October and helped sink attempt #84.
- A gate test patching non-frozen code by its dotted name: the coder cannot
  edit the gate test, so a change that moves the patched name can never
  pass. #87 died on tests/gate/test_cards.py this way.

This is a ratchet, not a ban. Today's instances are counted per file below;
a new one fails here with what to do instead, and a fixed one fails until
its count is lowered, so the numbers stay true and can only go down. To add
one anyway, raise its count in the same change and say why -- a judge
reading the diff sees a test getting more brittle on purpose.

The scanner is deliberately narrow, because a ratchet that cries wolf is one
more fake failure for the night's coder to be killed by. It sees a string
literal tested for membership in text that came from `getsource` or from
`read_text` on a named `.py` file (through any assignment, loop or slice),
and not what has been parsed first: `"request" in called`, where `called`
was collected by walking `ast.parse(getsource(...))`, is the *recommended*
form and is not counted. What it cannot see (a source read through a
helper, a prompt's wording) it does not claim.
"""

from __future__ import annotations

import ast
from collections import Counter

from tests.gate import repo_files

# Non-frozen code, by the prefix of a dotted name.
_NOT_FROZEN = ("agent.", "tools.", "channels.", "voice.")
_CLOCKS = {"perf_counter", "monotonic", "time", "perf_counter_ns", "monotonic_ns"}
# What comes out of these is structure or a token, no longer the text.
_STRUCTURE = {"parse", "walk", "finditer", "findall", "search", "match", "fullmatch",
              "compile"}
_LOCATORS = {"index", "find", "count"}
_UNDER_S = 1  # a bound below this many seconds is one a loaded machine can miss

ALLOWED: dict[str, dict[str, int]] = {
    # A string literal looked for (`in` / `not in`, or `.index(...)`) in a
    # test's copy of source text: `inspect.getsource`, or a .py file's text.
    # Instead: drive the code with a fake and assert what it did, or, where
    # it cannot be driven, check the syntax with `ast` -- call and keyword
    # names, independent of order and spelling (tests/unit/test_hearing.py).
    "source-text": {
    },
    # An upper bound under a second on elapsed time measured in the test, or
    # a `wait_for` under a second. Instead: count calls in flight, use an
    # Event the calls must reach, or bound generously ("not the 90 s
    # timeout") -- lower bounds are load-safe.
    "wall-clock-bound": {
    },
    # A gate test patching agent/, tools/, channels/ or voice/ by
    # dotted name. Instead: inject a fake through the code's own seam, or
    # move the fence into tests/gate/fences.py, which both trees share. The
    # gate is frozen, so these can only be repaired in a session with the user;
    # a change that moves a name patched here can only die until then.
    "gate-patches-unfrozen": {
        "tests/gate/test_desk.py": 1,
    },
}


# What to write instead, said in the failure itself: the night's coder reads
# the message, not this file's comments.
INSTEAD = {
    "source-text": ("drive the code with a fake and assert what it did, or check the syntax "
                    "with `ast` (call and keyword names), as tests/unit/test_hearing.py does"),
    "wall-clock-bound": ("count calls in flight, wait on an Event the calls must reach, or "
                         "bound generously (not the 90 s timeout); lower bounds are load-safe"),
    "gate-patches-unfrozen": ("inject a fake through the code's own seam, or put the fence in "
                              "tests/gate/fences.py, which both trees share"),
}


def _name(node: ast.AST) -> str:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _is_text(node: ast.AST) -> bool:
    return isinstance(node, ast.JoinedStr) or (
        isinstance(node, ast.Constant) and isinstance(node.value, str))


def _number(node: ast.AST) -> float | None:
    if (isinstance(node, ast.Constant) and isinstance(node.value, (int, float))
            and not isinstance(node.value, bool)):
        return float(node.value)
    return None


def _reads_project_source(call: ast.Call) -> bool:
    """`Path("agent/server.py").read_text()` or `(REPO / "tools" / "x.py")...`:
    a .py file of this project, named from the repository's root. A file in
    the test's own scratch repository (`tmp_path / "agent/calc.py"`) is a
    fixture's content, not the code under test."""
    receiver = getattr(call.func, "value", None)
    if receiver is None:
        return False
    names_py = any(isinstance(c, ast.Constant) and isinstance(c.value, str)
                   and c.value.endswith(".py") for c in ast.walk(receiver))
    from_root = any((isinstance(n, ast.Call) and _name(n.func) == "Path")
                    or (isinstance(n, ast.Name) and n.id.isupper())
                    for n in ast.walk(receiver))
    return names_py and from_root


def _holds_source(expr: ast.AST, tainted: set[str]) -> bool:
    """Is `expr` source text: a read of one, or a name that holds one?"""
    stack = [expr]
    while stack:
        node = stack.pop()
        if isinstance(node, ast.Call):
            name = _name(node.func)
            if name in _STRUCTURE:
                continue  # parsed: what comes out is no longer text
            if name in ("getsource", "getsourcelines"):
                return True
            if name in ("read_text", "read_bytes") and _reads_project_source(node):
                return True
        elif isinstance(node, ast.Name) and node.id in tainted:
            return True
        stack.extend(ast.iter_child_nodes(node))
    return False


def _tainted(fn: ast.AST) -> set[str]:
    """The names in a test that hold source text, followed through
    assignments, loops and comprehensions until nothing more is found."""
    held: set[str] = set()
    grown = True
    while grown:
        grown = False
        for node in ast.walk(fn):
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and node.value is not None:
                targets, value = [node.target], node.value
            elif isinstance(node, (ast.For, ast.comprehension)):
                targets, value = [node.target], node.iter
            else:
                continue
            if _holds_source(value, held):
                for target in targets:
                    for name in ast.walk(target):
                        if isinstance(name, ast.Name) and name.id not in held:
                            held.add(name.id)
                            grown = True
    return held


def _looks_for_text_in_source(node: ast.AST, held: set[str]) -> bool:
    """`"x" in source`, or `source.index("x")`, with `source` source text."""
    if isinstance(node, ast.Compare):
        return (_is_text(node.left)
                and any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops)
                and any(_holds_source(c, held) for c in node.comparators))
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr in _LOCATORS and bool(node.args) and _is_text(node.args[0])
            and _holds_source(node.func.value, held))


def _source_text(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    held = _tainted(fn)
    return sum(1 for node in ast.walk(fn) if _looks_for_text_in_source(node, held))


def _is_clock_delta(expr: ast.AST, elapsed: set[str]) -> bool:
    """`time.perf_counter() - start`, or a name assigned from one."""
    if isinstance(expr, ast.Name):
        return expr.id in elapsed
    return (isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Sub)
            and any(isinstance(n, ast.Call) and _name(n.func) in _CLOCKS
                    for n in ast.walk(expr)))


def _wall_clock(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    found = 0
    for call in (n for n in ast.walk(fn) if isinstance(n, ast.Call)):
        if _name(call.func) == "wait_for":
            timeouts = [k.value for k in call.keywords if k.arg == "timeout"]
            timeouts += call.args[1:2]
            if any((_number(t) or _UNDER_S) < _UNDER_S for t in timeouts):
                found += 1
    elapsed = {t.id for n in ast.walk(fn) if isinstance(n, ast.Assign)
               and _is_clock_delta(n.value, set()) for t in n.targets
               if isinstance(t, ast.Name)}
    for node in ast.walk(fn):
        if (isinstance(node, ast.Assert) and isinstance(node.test, ast.Compare)
                and len(node.test.ops) == 1 and isinstance(node.test.ops[0], (ast.Lt, ast.LtE))
                and _is_clock_delta(node.test.left, elapsed)
                and (_number(node.test.comparators[0]) or _UNDER_S) < _UNDER_S):
            found += 1
    return found


def _gate_patches(tree: ast.Module) -> int:
    return sum(1 for n in ast.walk(tree) if isinstance(n, ast.Call)
               and _name(n.func) in ("setattr", "setitem", "delattr")
               and isinstance(getattr(n.func, "value", None), ast.Name)
               and n.func.value.id == "monkeypatch"
               and n.args and isinstance(n.args[0], ast.Constant)
               and isinstance(n.args[0].value, str)
               and n.args[0].value.startswith(_NOT_FROZEN))


def counts() -> tuple[dict[str, Counter], dict[str, list[str]]]:
    """Each rule's instances per file, and where they are, for the message."""
    found: dict[str, Counter] = {rule: Counter() for rule in ALLOWED}
    where: dict[str, list[str]] = {rule: [] for rule in ALLOWED}
    for rel in repo_files.python("tests/"):
        if not rel.rsplit("/", 1)[-1].startswith("test_"):
            continue
        tree = repo_files.parsed(rel)
        for fn in ast.walk(tree):
            if not (isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and fn.name.startswith("test_")):
                continue
            for rule, count in (("source-text", _source_text(fn)),
                                ("wall-clock-bound", _wall_clock(fn))):
                if count:
                    found[rule][rel] += count
                    where[rule].append(f"{rel}:{fn.lineno} {fn.name}")
        if rel.startswith("tests/gate/") and (patched := _gate_patches(tree)):
            found["gate-patches-unfrozen"][rel] += patched
            where["gate-patches-unfrozen"].append(rel)
    return {rule: +c for rule, c in found.items()}, where  # `+` drops the zeros


def test_no_new_brittle_test_patterns_and_the_counts_stay_true():
    """Every count is exactly what is in the tree: above it is a new
    brittle test (the failure says what to write instead); below it is one
    fixed, and the allowance must come down with it."""
    found, where = counts()
    wrong: list[str] = []
    for rule, allowed in ALLOWED.items():
        for rel in sorted(set(allowed) | set(found[rule])):
            have, may = found[rule].get(rel, 0), allowed.get(rel, 0)
            if have > may:
                here = [w for w in where[rule] if w.startswith(rel)]
                wrong.append(f"{rule}: {rel} has {have}, allowed {may} -- a new brittle test: "
                             f"{here}. Instead: {INSTEAD[rule]}. (To keep it on purpose, raise "
                             "its count in ALLOWED and say why.)")
            elif have < may:
                wrong.append(f"{rule}: {rel} has {have}, allowed {may} -- fixed one; "
                             f"lower its count to {have}")
    assert not wrong, "\n".join(wrong)


def test_each_rule_finds_the_pattern_it_names_and_only_that():
    """The scanner is checked on samples, both ways, so a rule that
    silently matched nothing could not make the ratchet pass by being blind,
    and one that matched the recommended forms would not fail the night's
    coder for writing them."""
    sample = ast.parse(
        "import inspect, time, asyncio, re\n"
        "def test_a():\n"
        "    assert 'x(' in inspect.getsource(f)\n"
        "def test_b():\n"
        "    start = time.perf_counter()\n"
        "    elapsed = time.perf_counter() - start\n"
        "    assert elapsed < 0.6\n"
        "async def test_c():\n"
        "    await asyncio.wait_for(t, timeout=0.2)\n"
        "def test_d(monkeypatch):\n"
        "    monkeypatch.setattr('agent.x.y', 1)\n"
        "    monkeypatch.setattr('core.x.y', 1)\n"
        "def test_e():\n"
        "    assert done >= 0.85\n"
        "def test_f():\n"
        "    source = Path('agent/server.py').read_text()\n"
        "    body = source[source.index('async def go'):][:900]\n"
        "    assert 'minutes' in body.lower()\n"
        "def test_g():\n"
        "    tree = ast.parse(inspect.getsource(f))\n"
        "    called = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call)}\n"
        "    assert 'request' in called\n"
        "def test_h():\n"
        "    for m in re.finditer('x', Path('channels/signal.py').read_text()):\n"
        "        assert 'WINDOWS' in m.group(1)\n"
        "def test_i():\n"
        "    lines = Path('a.py').read_text().splitlines()\n"
        "    assert any('x' in line for line in lines)\n"
        "def test_j():\n"
        "    text = (root / 'agent/calc.py').read_text()\n"
        "    assert GOOD in text and 'x' in other\n"
        "def test_k():\n"
        "    elapsed = time.monotonic() - start\n"
        "    assert elapsed < 30\n"
        "    assert server.offline_for() < 0.5\n")
    fns = {n.name: n for n in sample.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

    assert _source_text(fns["test_a"]) == 1
    assert _source_text(fns["test_f"]) == 2, "the slice and the word looked for in it"
    assert _source_text(fns["test_i"]) == 1, "through a loop over the lines"
    assert _wall_clock(fns["test_b"]) == 1 and _wall_clock(fns["test_c"]) == 1
    assert _gate_patches(sample) == 1, "core is frozen; agent is not"
    for name in ("test_e", "test_k"):
        assert _wall_clock(fns[name]) == 0, "a lower bound, a generous one, a value from the code"
    for name in ("test_g", "test_h", "test_j"):
        assert _source_text(fns[name]) == 0, "parsed, matched, or a name that is not source"
