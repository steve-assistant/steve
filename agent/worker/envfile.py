"""The keys a `.env` file holds, offered to setup by name, never by value.

Someone running Steve from a clone often keeps their provider keys in a
`.env`: in the clone, or in the folder above it, shared by several
projects. Setup asked for each key to be pasted by hand, so the key was
copied out of that file into the page, where a stray character made it
wrong. Setup's key page now offers what such a file holds (agent/worker/
setup.py, `/setup/envfile`).

What it prevents:

- **A key read from a file and kept unchecked.** Each key found goes
  through the paste's own path (`setup.keep`): the same shape check, the
  same request to its service, the same refusal. A file is not a way
  round the check.
- **A value shown back, or logged.** What this module hands out is the
  file's path and the names it holds: never a value, nor its length or
  ending. Only `values`, called once the user has said yes, reads one.
- **Any name taken as Steve's.** Only the provider keys are read
  (`NAMES`, a subset of `core/secrets.KNOWN_KEYS`). The authenticator's
  secret is made by setup itself and never handed in, the Telegram token
  is checked on its own page, and anything else (another program's key)
  is not Steve's.
- **The file kept, or held.** It is read whole, once per question, and
  closed: never written, copied, or watched. The rest of Steve still
  reads keys only from the Credential Manager; an installed Steve never
  reads the environment for them (`core/secrets._from_environment`).

Where it looks: Steve's own folder, then the one above. In a checkout
that is the repository's root (`core/paths.SOURCE`, which an app built
with `STEVE_ROOT` runs its backend from); in an install, the install's
folder (`core/paths.install_dir`). Never the data folder: that one is
Steve's to write, not a place the user puts files.
"""

from __future__ import annotations

import re
from pathlib import Path

from core import logscrub, paths

NAME = ".env"
NAMES = ("OPENAI_API_KEY", "OPENROUTER_API_KEY", "BRAVE_API_KEY")
# Far above any real `.env`; a bigger file is not one, and is not read whole.
MOST_BYTES = 64 * 1024

_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def own_folder() -> Path:
    return paths.install_dir() if paths.is_installed() else paths.SOURCE


def folders() -> list[Path]:
    """Where a `.env` is looked for, nearest first."""
    own = own_folder()
    return [own, own.parent] if own.parent != own else [own]


def _parse(text: str) -> dict[str, str]:
    """The known names a file sets, with their values: `NAME=value`, an
    optional `export `, one pair of matching quotes, `#` comments. The
    last line naming a key wins, as in a shell."""
    held: dict[str, str] = {}
    for line in text.splitlines():
        if not (m := _LINE.match(line)) or m.group(1) not in NAMES:
            continue
        value = m.group(2)
        if value[:1] in ("'", '"') and (end := value.find(value[0], 1)) > 0:
            value = value[1:end]
        else:
            value = value.split(" #", 1)[0]
        held[m.group(1)] = value.strip()
    return held


def _read(file: Path) -> dict[str, str] | None:
    try:
        if not file.is_file() or file.stat().st_size > MOST_BYTES:
            return None
        text = file.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None
    return _parse(text)


def found() -> list[dict]:
    """Each `.env` that names a key Steve takes, nearest first: its path,
    the names set, and the names left empty. Never a value."""
    out = []
    for folder in folders():
        file = folder / NAME
        if (held := _read(file)) is None or not held:
            continue
        out.append({"file": str(file),
                    "keys": [n for n in NAMES if held.get(n)],
                    "empty": [n for n in NAMES if n in held and not held[n]]})
    return out


def values(file: str) -> dict[str, str] | None:
    """The keys set in `file`, if it is one `found` offers now; None for
    any other path, which is not read."""
    if file not in {f["file"] for f in found()}:
        return None
    held = _read(Path(file)) or {}
    return {name: logscrub.hold(value) for name, value in held.items() if value}
