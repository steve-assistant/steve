"""What a pytest run said, read from its junit file instead of its dots.

For ten days (28 September to 8 October 2026) the suite was run 135 times,
and the output was usually piped through `tail`, which keeps the summary
line and drops the `E` lines above it. Four of those runs ended with the
same 17 errors in tests/gate/test_zero_downtime.py, and nothing on screen
said why; each cost a rerun to find out. A junit file keeps what the dots
and `tail` lose: for every test, whether it passed and, if not, the
exception line that says why.

`read` turns one into outcomes with pytest's own node ids, so a failure can
be rerun by pasting the line. Nothing here runs pytest.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

# What reading a junit file can raise when pytest died before finishing it.
UNREADABLE = (OSError, ValueError, ET.ParseError)


@dataclass(frozen=True)
class Outcome:
    node: str  # tests/unit/test_x.py::test_y[param], pytest's own id
    status: str  # passed, failed, error or skipped
    seconds: float
    reason: str = ""  # a failure or error: the first `E` line, else the message


# What xdist's `loadgroup` adds to a grouped test's id (`test_x@supervisor`,
# devtools/check.DIST), which pytest given the id back would not find. An `@`
# inside a parameter's brackets is the parameter's.
_GROUP = re.compile(r"@[^@\[\]]*$")


def plain(node: str) -> str:
    """A test's id without the xdist group it ran in."""
    return _GROUP.sub("", node)


def node_id(classname: str, name: str, root: Path) -> str:
    """pytest's node id from junit's `classname` and `name`.

    junit writes the module as a dotted path with any class after it
    (`tests.unit.test_x.TestThing`), and the dots do not say which part is
    the file, so the longest prefix that is a file under `root` is. A
    collection error has no classname and the module's dotted path as its name.
    """
    name = plain(name)
    parts = (classname or name).split(".")
    for i in range(len(parts), 0, -1):
        if root.joinpath(*parts[:i]).with_suffix(".py").is_file():
            module = "/".join(parts[:i]) + ".py"
            return "::".join([module, *parts[i:], name]) if classname else module
    return "::".join([classname.replace(".", "/") + ".py", name]) if classname else name


def reason(element: ET.Element) -> str:
    """The line a human needs: the first `E ` line of the traceback, else the
    first line of the message, else the first line there is."""
    text = element.text or ""
    for line in text.splitlines():
        if line.startswith("E "):
            return line[1:].strip()[:240]
    for source in (element.get("message") or "", text):
        for line in source.splitlines():
            if line.strip():
                return line.strip()[:240]
    return element.get("type") or "(no reason given)"


def read(path: Path, root: Path) -> list[Outcome]:
    """Every test case in a junit file, in the order pytest wrote them."""
    outcomes: list[Outcome] = []
    for case in ET.parse(path).getroot().iter("testcase"):
        node = node_id(case.get("classname") or "", case.get("name") or "", root)
        seconds = float(case.get("time") or 0)
        failure, error = case.find("failure"), case.find("error")
        if failure is not None:
            outcomes.append(Outcome(node, "failed", seconds, reason(failure)))
        elif error is not None:
            outcomes.append(Outcome(node, "error", seconds, reason(error)))
        elif case.find("skipped") is not None:
            outcomes.append(Outcome(node, "skipped", seconds))
        else:
            outcomes.append(Outcome(node, "passed", seconds))
    return outcomes


def bad(outcomes: list[Outcome]) -> list[Outcome]:
    return [o for o in outcomes if o.status in ("failed", "error")]


def slowest(outcomes: list[Outcome], count: int = 5) -> list[Outcome]:
    return sorted(outcomes, key=lambda o: o.seconds, reverse=True)[:count]
