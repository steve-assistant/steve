"""The frozen YAML files, parsed once per version of each on disk.

`load_policy` ran on every model request (the browser tools ask whether the
browser is on) and the consent rules four to six times for every action a
turn takes, each a pure-Python parse on the event loop: 10 ms for
policy.yaml and 19 ms for consent.yaml, measured on 5 October 2026 -- a tenth
of a second of a research turn spent re-reading files that had not changed.

A file is parsed again whenever its size or modification time differs from
the parse kept, so an edit is seen on the next read, as before. A file
changed without either moving would keep its earlier, legitimate contents,
which is the safe direction for files that say what may not happen. A parse
that fails is not kept: it raises, every time, as before.

Each caller gets its own copy, so nothing a caller does to the rules it was
handed can change them for the next. libyaml's loader where it is installed:
the same documents, ten times faster (both are compared in
tests/unit/test_yamlfile.py).
"""

from __future__ import annotations

import copy
import threading
from pathlib import Path
from typing import Any

import yaml

LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)

_parsed: dict[Path, tuple[tuple[int, int], Any]] = {}
_lock = threading.Lock()


def load(path: Path) -> Any:
    """`path` parsed as YAML: a fresh copy of the parse kept for its current
    version on disk, or a new parse."""
    path = Path(path)
    stat = path.stat()
    version = (stat.st_mtime_ns, stat.st_size)
    with _lock:
        kept = _parsed.get(path)
    if kept is None or kept[0] != version:
        kept = (version, yaml.load(path.read_text(encoding="utf-8"), Loader=LOADER))  # noqa: S506 - a safe loader
        with _lock:
            _parsed[path] = kept
    return copy.deepcopy(kept[1])
