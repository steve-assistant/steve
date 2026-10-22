"""Self-upgrade v2: Steve builds a module, and the user's card decides.

Self-upgrade v1 changed Steve's own code, in a clone of its repository, and
a passed change waited as a diff. A diff of Steve's own code is one only a
programmer can judge, and a merged one changed everything Steve does with
the user's accounts at once. What Steve builds for itself now is a module
(06-modules.md, section 8): a folder with a manifest saying what it may
reach, its code run in the sandbox, its tests run there too, and nothing
on until the user approves its card -- the same path as a module from
anywhere else.

A build, from the request to its end:

- **Asked for:** by the user in a conversation (`build_module`), or by the
  night when `self_upgrade.agent_initiated` is on (off by default). The
  policy refuses any origin but "user" while it is off.
- **Costed first:** what past builds cost and took, or a first build's
  budget. One that would pass the day's `extensions` cap waits
  (`needs_allow`) for "Allow this build" in the app -- never a remote
  channel, which could be someone holding the user's phone.
- **Built** in a draft folder of its own (`paths.module_draft_dir`), by the
  coder, the test builder and the judge (`core/coder`), reading the SDK,
  the module guide and the built-in modules and nothing else, with every
  test run in the sandbox (`testrun.for_drafts`). A new version starts from
  a copy of the current one, so its tests are the baseline.
- **Admitted** (`modadmit.admit`) up to the card, which shows the request in
  the words it was asked and marks each grant those words do not name. The
  request is the model's words, so beside it the card shows the user's own
  last message of the turn that asked, which no model wrote.
- **Told:** the conversation hears when the card is ready, or why not.

A cap reached partway stops the build and keeps its draft (`stopped`), to go
on from there when the user allows it; so does a worker that stopped under
it (`recover`). A request that cannot be a module -- a slot Steve lacks, a
bug in Steve itself -- ends as a report on the Extensions page (`cannot`),
which Steve never sends anywhere.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import sqlite3
import statistics
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent import offloop, sqltime
from core import db, identity, lifecycle, paths, screen, spend
from core.policy import load_policy

log = logging.getLogger(__name__)

# needs_allow: would pass the day's cap; waits for "Allow this build".
# running: being built. stopped: a cap or a restart stopped it; its draft
# waits for "Allow this build" to go on. failed, cannot, carded: ended --
# not approved or not admitted; not a module (a report waits); a card waits.
# dismissed: the user let a waiting build go.
STATES = ("needs_allow", "running", "stopped", "failed", "cannot", "carded", "dismissed")
WAITING = ("needs_allow", "stopped")
ENDED = ("failed", "cannot", "carded")
ORIGINS = ("user", "night", "agent")

# What a first build is said to take, before there are builds to go by.
DEFAULT_SECONDS = 15 * 60.0
# The estimate reads the median of this many past builds, once there are
# at least MIN_PAST. It waited for three: a second build was still said to
# cost a whole attempt's budget, ten times what the first had, and that
# budget, checked against what the day's cap had left after the first,
# made the second wait for an allowance (20 October). One ended build is
# a better guess than the most a build may spend; an estimate too low
# spends nothing by itself, as the cap stops the build where it is.
PAST, MIN_PAST = 20, 1
MAX_SAID = 2_000

Notify = Callable[[str], None]


class Refused(Exception):
    """A build that may not be asked for or allowed, saying why."""


@dataclass(frozen=True)
class Estimate:
    usd: float
    seconds: float
    past: int  # the builds it was read from; 0 for a first build's budget

    def said(self) -> str:
        minutes = max(1, round(self.seconds / 60))
        basis = ("the most one build may spend, as there are no past builds to go by"
                 if not self.past else "going by the last build" if self.past == 1
                 else f"going by the last {self.past} builds")
        return f"about ${self.usd:.2f} and {minutes} minutes, {basis}"


def estimate(conn: sqlite3.Connection, config=None) -> Estimate:
    config = config or load_policy().coder
    rows = conn.execute(
        "SELECT cost_usd, seconds FROM module_builds WHERE state IN ('failed', 'cannot', "
        "'carded') AND seconds > 0 ORDER BY id DESC LIMIT ?", (PAST,)).fetchall()
    if len(rows) < MIN_PAST:
        return Estimate(config.attempt_budget_usd, DEFAULT_SECONDS, 0)
    return Estimate(round(statistics.median(r[0] for r in rows), 2),
                    statistics.median(r[1] for r in rows), len(rows))


def get(conn: sqlite3.Connection, build_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM module_builds WHERE id = ?", (build_id,)).fetchone()
    return dict(row) if row else None


def _update(conn: sqlite3.Connection, build_id: int, **fields: Any) -> None:
    sets = ", ".join(f"{name} = ?" for name in fields)
    with db.transaction(conn):
        conn.execute(f"UPDATE module_builds SET {sets} WHERE id = ?", (*fields.values(), build_id))


def _running(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM module_builds WHERE state = 'running' LIMIT 1").fetchone()
    return dict(row) if row else None


def request(conn: sqlite3.Connection, text: str, *, origin: str, came_from: str = "",
            session_id: int | None = None, module: str = "", user_said: str = "",
            policy=None) -> dict[str, Any]:
    """Record a build of `text`, the request in the words it was asked; the
    row, `running` if it may start now (the caller then `start`s it) or
    `needs_allow` if it would pass the day's cap. Refused if it may not be
    asked for at all. `user_said` is the user's own last message, where the
    request came from a turn of theirs: the card shows it beside `text`,
    which the model wrote."""
    if origin not in ORIGINS:
        raise ValueError(f"not an origin: {origin!r}")
    policy = policy or load_policy()
    if (paused := policy.self_upgrade.refusal(origin)) is not None:
        raise Refused(paused)
    text = (text or "").strip()
    if not text:
        raise Refused("say what the module should do")
    if module:
        row = conn.execute("SELECT origin FROM modules WHERE id = ?", (module,)).fetchone()
        if row is None or row[0] != "built":
            # The ones it did build, by id: the turn has no other list of
            # them, so a model asked to improve "the macro tracker" guessed
            # an id, and had nothing to correct the guess with.
            built = [r[0] for r in conn.execute(
                "SELECT id FROM modules WHERE origin = 'built' ORDER BY id")]
            raise Refused(f"{module!r} is not a module Steve built, and only those can have "
                          "a new version built; "
                          + (f"Steve built: {', '.join(built)}" if built else "Steve built none"))
    if (busy := _running(conn)) is not None:
        raise Refused(f"a build is running already ({busy['request'][:80]!r}): one at a time")
    guess = estimate(conn, policy.coder)
    # Checked against the cap it will be billed to. The night's builds run
    # inside the night's unit, whose category an inner one keeps
    # (`spend.spending`); every other build runs in a thread of its own,
    # under `extensions`.
    category = "night" if origin == "night" else "extensions"
    state = "running" if guess.usd <= spend.left(conn, category) else "needs_allow"
    with db.transaction(conn):
        build_id = conn.execute(
            "INSERT INTO module_builds (request, origin, came_from, session_id, module, state, "
            "estimate_usd, estimate_s, user_said) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (text, origin, came_from, session_id, module, state, guess.usd,
             guess.seconds, (user_said or "").strip())).lastrowid
    return {**get(conn, build_id), "estimate": guess.said()}


def allow(conn: sqlite3.Connection, build_id: int, *, policy=None) -> dict[str, Any]:
    """The app's "Allow this build": a waiting build may go past the day's
    cap by at most what one build may spend, and starts (the caller
    `start`s it). Never reached from a remote channel."""
    policy = policy or load_policy()
    row = get(conn, build_id)
    if row is None or row["state"] not in WAITING:
        raise Refused("that build is not waiting to be allowed")
    if (busy := _running(conn)) is not None:
        raise Refused(f"a build is running already ({busy['request'][:80]!r}): one at a time")
    _update(conn, build_id, state="running", allowed_usd=policy.coder.attempt_budget_usd)
    return get(conn, build_id)


def dismiss(conn: sqlite3.Connection, *, build_id: int | None = None,
            report_id: int | None = None) -> None:
    """Let a waiting build go, its draft with it; or put a report away."""
    if report_id is not None:
        with db.transaction(conn):
            found = conn.execute("UPDATE module_reports SET state = 'dismissed' "
                                 "WHERE id = ? AND state = 'open'", (report_id,)).rowcount
        if not found:
            raise Refused("no such report is open")
        return
    row = get(conn, build_id or 0)
    if row is None or row["state"] not in WAITING:
        raise Refused("that build is not waiting")
    _update(conn, build_id, state="dismissed", finished_at=_now())
    shutil.rmtree(paths.module_draft_dir(build_id), ignore_errors=True)


def recover(conn: sqlite3.Connection) -> int:
    """At a worker's start: a build left `running` was stopped with the
    worker before it, and its draft waits to go on. How many."""
    with db.transaction(conn):
        return conn.execute(
            "UPDATE module_builds SET state = 'stopped', said = ? WHERE state = 'running'",
            ("Steve stopped while it was being built",)).rowcount


def builds(conn: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
    """The latest builds, newest first, as the Extensions page lists them."""
    return [dict(r) for r in conn.execute(
        "SELECT id, request, origin, came_from, module, state, estimate_usd, estimate_s, "
        "cost_usd, seconds, said, admission, created_at, finished_at FROM module_builds "
        "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]


def reports(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """The open reports: slots Steve lacks and problems in Steve itself."""
    return [dict(r) for r in conn.execute(
        "SELECT r.id, r.build_id, r.kind, r.text, r.created_at, b.request FROM module_reports r "
        "JOIN module_builds b ON b.id = r.build_id WHERE r.state = 'open' "
        "ORDER BY r.id DESC").fetchall()]


def _now() -> str:
    return sqltime.stamp(datetime.now(UTC))


# -- the build ------------------------------------------------------------------


def library() -> dict[str, Path]:
    """What the roles read beside the draft, as `_reference/<name>`: the
    SDK with its templates, the module guide and the built-in modules."""
    import steve_sdk
    from core import modules

    return {"steve_sdk": Path(steve_sdk.__file__).parent,
            "modules": modules.BUILTIN_DIR,
            "modules.md": paths.SOURCE / "docs" / "modules.md"}


def task(row: dict[str, Any]) -> str:
    """The task the coder works to and the judge judges by: the request as
    it was asked, what the user said when they asked, and which module a
    new version is of.

    The request is the model's words, which can say less than the user
    did: a food tracker asked for was built and approved taking grams
    alone, though the user meant cups and dishes too (20 October). The
    judge asks whether the module does what the user wanted, so it reads
    their own words beside the request."""
    said = f"Build a module for this request, in the user's own words:\n\n{row['request']}"
    own = (row.get("user_said") or "").strip()
    if own and own != row["request"].strip():
        said += ("\n\nWhat the user said when they asked, in their own words; the module "
                 f"must do what they wanted:\n\n{own}")
    if row["module"]:
        said += (f"\n\nThis is a new version of the module {row['module']!r}: the folder holds "
                 "the current version, whose tests must keep passing.")
    return said


def _prepare(conn: sqlite3.Connection, row: dict[str, Any], draft: Path) -> None:
    """The draft folder: kept from a build that stopped, a copy of the
    current version for a new one, or empty."""
    if draft.is_dir():
        return
    if row["module"]:
        current = conn.execute("SELECT version FROM modules WHERE id = ?",
                               (row["module"],)).fetchone()
        if current is None:
            raise lifecycle.Refused(f"the module {row['module']!r} is not installed any more")
        shutil.copytree(paths.module_code_dir(row["module"], current[0]), draft,
                        ignore=shutil.ignore_patterns("__pycache__"))
    else:
        draft.mkdir(parents=True)


def _runner():
    """The sandbox's runner for a draft, or why this computer cannot run a
    module's code (D6): a build that cannot test its code is not started."""
    from core.sandbox import probe, testrun

    support = probe.verdict()
    if support is None or not support.ok:
        why = support.reason if support is not None else "not checked since this start"
        raise lifecycle.Refused(f"this computer cannot run a module's code: {why}")
    return testrun.for_drafts(lpac=support.mode == probe.LPAC)


def _report(conn: sqlite3.Connection, build_id: int, kind: str, why: str, redactor) -> None:
    """A slot Steve lacks or a problem in Steve itself, as a note on the
    Extensions page. Both are redacted: the plan redacted only the problem,
    but the page's "Forward" copies either, and a slot's reasons quote the
    request as readily as a problem's do."""
    text = redactor(why)
    with db.transaction(conn):
        conn.execute("INSERT INTO module_reports (build_id, kind, text) VALUES (?, ?, ?)",
                     (build_id, kind, text[:MAX_SAID]))


# A note's text where this computer cannot take personal detail out of its
# reason. The redactor, presidio, is the `reflect` extra, which an ordinary
# install leaves out, and `agent/redact.py` raises without it. Here that
# raise turned a request the coder rightly said cannot be a module into a
# failed build (the module-building eval, 15 October), as it had a module's
# log line before (`core/modctx.scrub`). A note may be forwarded, so it
# keeps none of the reason; the user's own message about the build, which
# stays on this computer, gave it.
UNREDACTED = ("The reason is not kept in this note: this computer cannot take personal "
              "details out of it before it is forwarded. Steve's message about the build "
              "gave it.")


def _redacted(text: str) -> str:
    from agent import redact

    try:
        return redact.redact(text).text
    except ImportError:
        return UNREDACTED


def build(conn: sqlite3.Connection, build_id: int, *, notify: Notify, run_pipeline=None,
          admit=None, runner=None, library_=None, redactor=_redacted,
          screener=None) -> dict[str, Any]:
    """Run the build `build_id`, `running`, to its end; the row as it ended.
    Blocking and minutes long: `start` runs it in a thread of its own. The
    rest are fakes in tests. Never raises once it has started."""
    from agent import modadmit
    from core.coder import pipeline

    row = get(conn, build_id)
    if row is None or row["state"] != "running":
        raise Refused("that build is not running")
    policy = load_policy()
    draft = paths.module_draft_dir(build_id)
    started = time.monotonic()
    state, said, admission, outcome = "failed", "", None, None
    try:
        with spend.spending("extensions", conn=conn, session_id=row["session_id"],
                            allowance=row["allowed_usd"]) as unit:
            _prepare(conn, row, draft)
            # Its id is checked with the rest, so a coder that names a new
            # module like one installed hears of it while a round can still
            # rename it; admission refuses it either way. So are its words,
            # as admission screens them.
            outcome = asyncio.run((run_pipeline or pipeline.run)(
                task(row), draft, config=policy.coder, runner=runner or _runner(),
                python="python", library=library_ or library(),
                screen=lambda folder: (lifecycle.screened(folder, conn,
                                                          version_of=row["module"])
                                       or modadmit.words_refused(
                                           conn, folder, screener or screen.check)),
                runs_code=lifecycle.runs_code))
            if outcome.cannot:
                _report(conn, build_id, outcome.cannot["kind"], outcome.cannot["why"], redactor)
                state, said = "cannot", outcome.cannot["why"]
            elif not outcome.approved:
                capped = spend.may_spend(conn, unit=unit)
                state = "stopped" if capped is not None else "failed"
                said = f"a spending cap stopped it: {capped}" if capped else outcome.reasons
            else:
                admission = asyncio.run((admit or modadmit.admit)(
                    conn, draft, origin="built", version_of=row["module"],
                    request=row["request"], came_from=row["came_from"] or None,
                    user_said=row["user_said"] or None))
                state, said = "carded", "its card is waiting on the Extensions page"
    except spend.Refused as exc:
        # The reviewer's call at admission, say: the same cap, the same
        # draft kept to go on from.
        state, said = "stopped", f"a spending cap stopped it: {exc}"
    except (lifecycle.Refused, Refused) as exc:
        said = str(exc)
    except Exception as exc:  # noqa: BLE001 - a build must not kill the worker
        log.exception("module build %d failed", build_id)
        said = f"{type(exc).__name__}: {exc}"
    cost = outcome.cost_usd if outcome is not None else 0.0
    _update(conn, build_id, state=state, said=said[:MAX_SAID], admission=admission,
            cost_usd=row["cost_usd"] + cost, seconds=row["seconds"] + time.monotonic() - started,
            finished_at=None if state == "stopped" else _now())
    if state != "stopped":
        shutil.rmtree(draft, ignore_errors=True)
    try:
        notify(_told(row["request"], state, said))
    except Exception:  # noqa: BLE001 - the row says how it ended either way
        log.exception("could not tell the user how module build %d ended", build_id)
    return get(conn, build_id)


def _told(request: str, state: str, said: str) -> str:
    asked = request if len(request) <= 80 else request[:79] + "…"
    if state == "carded":
        return f"The module you asked for ({asked}) is built: its card is on the Extensions page."
    if state == "stopped":
        return (f"I stopped building the module you asked for ({asked}): {said}. What I had is "
                "kept; allow the build on the Extensions page to go on.")
    if state == "cannot":
        return (f"What you asked for ({asked}) cannot be a module: {said}. There is a note on "
                "the Extensions page.")
    return f"I could not build the module you asked for ({asked}): {said}"


def notifier(loop: asyncio.AbstractEventLoop) -> Notify:
    """`notify` for a build started from the worker: the message goes
    through `links.notify_user` on the worker's own loop, whose connection
    it writes with -- never from the build's thread."""
    def notify(text: str) -> None:
        from agent.worker import links

        asyncio.run_coroutine_threadsafe(links.notify_user(text), loop).result(timeout=60)

    return notify


def start(conn: sqlite3.Connection, build_id: int, *, notify: Notify,
          **fakes: Any) -> threading.Thread:
    """Run `build` in a thread of its own, on a connection of its own to
    the same database as `conn`, with the computer kept awake."""
    from agent.wake import kept_awake

    store = offloop.db_path(conn)

    def body() -> None:
        own = db.open_store(store) if store is not None else db.open_store()
        try:
            with kept_awake(f"{identity.NAME}: building a module"):
                build(own, build_id, notify=notify, **fakes)
        except Exception:  # noqa: BLE001 - nothing above a thread to catch it
            log.exception("module build %d", build_id)
        finally:
            own.close()

    thread = threading.Thread(target=body, name=f"module-build-{build_id}", daemon=True)
    thread.start()
    return thread
