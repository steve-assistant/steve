"""Core's engines: one class's provider for every module built on an engine.

A provider that speaks a standard (IMAP and SMTP, CalDAV, an ICS feed, an
MCP server) needs a manifest and nothing else (05-connectors.md, section
6): its hosts and flags are settings, and the code that talks to it is
here, in core, frozen, and tested once by the class's contract suite. A
module never brings its own copy of the protocol, so a fix to one is a fix
to every module on it.

`agent/connectors.py` builds an account's provider from `ENGINES`, by the
engine its manifest names for the class. Each arrives with its item in
step 5: IMAP with item 6, CalDAV with item 7, ICS with item 8, `code` (a
built-in's own providers) with item 9; MCP, with its profiles, with item 11.
`ical.py` is not an engine: it is the iCalendar reading CalDAV and ICS share.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core.engines import caldav, code, ics, imap, mcp

ENGINES: dict[str, Callable[[Any, Any], Any]] = {
    "imap": imap.engine,
    "caldav": caldav.engine,
    "ics": ics.engine,
    "code": code.engine,
    "mcp": mcp.engine,
}
