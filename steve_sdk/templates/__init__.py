"""Working modules to copy, one per class, for a service with a web API of its own.

A provider that speaks IMAP, CalDAV or ICS, or an MCP server a profile can
map, needs only a manifest (05-connectors.md, section 6). Any other needs
code, and before step 5 each such provider was written from nothing, with
its own shapes, its own errors and its own write checks, or none: a bare
EXPUNGE for a delete, a calendar update with no etag. So a new one starts
here, from a module that already passes its class's contract suite:

1. Copy `<class>/` to `modules/<id>/`.
2. Fill each line of `manifest.toml` marked FILL: the id, the label, the
   address domains, the hosts, the methods its writes use, and where the
   service takes its token.
3. Rewrite `provider.py`'s calls for the service's API, keeping what each
   method returns and refuses; its docstring says what every copy keeps.
4. Rewrite `fake.py` to answer as the service does, its habits included,
   and run the class's contract suite against it.

Each folder holds:

- `manifest.toml`;
- `provider.py`: the class's interface over Example's API, a small JSON
  API at `api.example.com`, reached only through `ctx.http`, its token
  filled into a header by core and never read;
- `fake.py`: that API in memory (`steve_sdk.testing.api`), and a
  `steve_sdk.testing.World`, so the class's contract suite runs against it.

`tests/unit/test_templates.py` runs every template through its class's
contract suite and core's write path, on every run of the suite, so a
template cannot rot.
"""

from __future__ import annotations

from pathlib import Path

CLASSES = ("mail", "calendar", "files", "tasks")
HERE = Path(__file__).parent


def folder(klass: str) -> Path:
    """The template for `klass`."""
    if klass not in CLASSES:
        raise ValueError(f"no template for {klass!r}; the classes are {CLASSES}")
    return HERE / klass
