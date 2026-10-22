"""No secret Steve holds is written to a log.

Nothing filtered the logs. `log.exception` wrote the traceback of an error
whose text held what the failing call was given, and a worker's last lines
went on from its log into worker_deaths.jsonl and the issues table (step
11's audit). So every record is scrubbed as it is made, whatever logger and
handler carry it, in its message and its traceback: each secret Steve has
read or stored, by value (`hold`), and anything named or shaped as one
(`NAMED`).

Not the long runs of token characters `core/modctx.scrub` also takes from a
module's lines: in Steve's own logs they are commit ids, file hashes and
addresses, which a log is for.
"""

from __future__ import annotations

import logging
import re
import threading

MARK = "[secret]"

# A credential by its shape or its name: a JWT (Microsoft's and Google's
# access tokens), a bearer value, and a named token or secret.
NAMED = re.compile(
    r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*"
    r"|(?i:bearer)\s+[A-Za-z0-9._~+/=-]{8,}"
    r"|(?i:(?:access|refresh|id)_?token|client_?secret|password|secret|api_?key|code_verifier)"
    r"""["']?\s*[:=]\s*["']?[^\s"',;}]{4,}""")

# Shorter values would blank ordinary words wherever they appear.
SHORTEST = 6

_held: set[str] = set()
_lock = threading.Lock()
_formatter = logging.Formatter()


def hold(value: str) -> str:
    """`value`, returned as given, and kept out of every log line from now on."""
    if len(value) >= SHORTEST:
        with _lock:
            _held.add(value)
    return value


def scrub(text: str) -> str:
    """`text` without any secret held, or named or shaped as one."""
    with _lock:
        held = sorted(_held, key=len, reverse=True)  # a longer one whole, not around a shorter
    for value in held:
        text = text.replace(value, MARK)
    return NAMED.sub(MARK, text)


def install() -> None:
    """Scrub every log record this process makes from now on."""
    made = logging.getLogRecordFactory()
    if getattr(made, "scrubs", False):
        return

    def scrubbed(*args, **kwargs) -> logging.LogRecord:
        record = made(*args, **kwargs)
        # Whole, since a name in the format and its value in an argument are
        # one secret; and replaced only when it held one, since a formatter
        # may read the arguments.
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a bad format: its parts, as they came
            message = f"{record.msg} {record.args!r}"
        if (clean := scrub(message)) != message:
            record.msg, record.args = clean, None
        if record.exc_info and record.exc_info[0] is not None:
            record.exc_text = scrub(_formatter.formatException(record.exc_info))
        if record.stack_info:
            record.stack_info = scrub(record.stack_info)
        return record

    scrubbed.scrubs = True  # type: ignore[attr-defined]
    logging.setLogRecordFactory(scrubbed)
