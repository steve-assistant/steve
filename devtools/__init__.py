"""The developer's test loop: tools a Claude session or a developer runs by hand.

Not the agent's tools (those are `tools/`) and not the user's setup scripts
(`scripts/`, frozen). Nothing here runs inside the live agent, and nothing
here may touch the live checkout's working tree: see docs/testing.md.
"""

from __future__ import annotations

import sys


def utf8_stdout() -> None:
    """Print whatever pytest printed. A failure line can hold a character the
    Windows console's code page cannot, and a tool that dies on the way to
    reporting a failure has lost the failure."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
