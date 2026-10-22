"""Files in and out of the sandboxed browser: where a download may land, and
which files may be uploaded.

The maintainer, 1 October 2026: downloads go into a quarantine, and uploads happen
only for files they have granted. So:

- **A download** stays in the browser's container until asked for, then is
  copied to the quarantine (`quarantine_dir()/<session>/`) under a name made safe here
  (`quarantine_name`) -- no path, no surprises, never run or opened on the
  laptop. It is read only by extracting its text inside the no-network
  analysis container (`agent/toolset/browser.py`).
- **An upload** may come from three places and nowhere else
  (`upload_source`): the outbox, which only the user fills (the live view, or by
  hand); the quarantine; and what the agent itself made (`data/artifacts`).
  The path is resolved and must stay inside its root -- no `..`, no link
  out -- and every upload is a card all the same.

Both are capped at `MAX_FILE_BYTES`. Frozen, so the night cannot widen
where an upload may come from.
"""

from __future__ import annotations

import re
from pathlib import Path

from core import paths

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_DOWNLOADS = 20

_UNSAFE = re.compile(r"[^A-Za-z0-9._ -]+")


def quarantine_dir() -> Path:
    return paths.data_dir() / "browser" / "quarantine"


def outbox_dir() -> Path:
    return paths.data_dir() / "browser" / "outbox"


def roots() -> dict[str, Path]:
    """Where an upload may come from, by the name a tool uses for it."""
    return {"outbox": outbox_dir(), "quarantine": quarantine_dir(),
            "artifacts": paths.artifacts_dir()}


def quarantine_name(suggested: str) -> str:
    """A download's name as it is kept: its last path part, nothing but
    letters, digits, dots, dashes, underscores and spaces, no leading dot,
    at most 100 characters."""
    base = re.split(r"[\\/]", suggested or "")[-1]
    base = _UNSAFE.sub("_", base).strip(" .")
    return (base or "download")[:100]


def upload_source(spec: str, *, base: dict[str, Path] | None = None) -> Path | str:
    """The file `spec` names -- "outbox/cv.pdf", "quarantine/<session>/x.pdf",
    "artifacts/<run>/chart.png" -- or why it may not be uploaded."""
    places = base or roots()
    head, _, rest = str(spec or "").replace("\\", "/").strip("/").partition("/")
    root = places.get(head)
    if root is None or not rest:
        return f"a file to upload is outbox/..., quarantine/... or artifacts/..., not {spec!r}"
    try:
        path = (root / rest).resolve(strict=True)
    except (OSError, RuntimeError):
        return f"there is no {spec}"
    if not path.is_relative_to(root.resolve()):
        return f"{spec} is not inside {head}"
    if not path.is_file():
        return f"{spec} is not a file"
    if path.stat().st_size > MAX_FILE_BYTES:
        return f"{spec} is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB"
    return path
