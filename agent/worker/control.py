"""The control panel's side of the worker: what the desktop app shows and
turns, under `/control/`.

The maintainer, 4 October 2026: replace the web page with a desktop app that starts
at login, and give it a control panel -- everything the tray did, plus the
agent's health and power, their routines and goals, what it did while they were
away, and what it spends on which models.

Everything here answers only on this laptop: the worker's whole app sits
behind `core/localonly.LocalOnly`. That is also why its writes need no
code from their authenticator, as the tray's approvals do not: what can
reach these endpoints is them, at this laptop.
What it may write is still bounded -- pausing, resuming and dropping their own
routines and goals, answering a goal, putting the models back as written,
the knobs inside the bounds frozen in `core/knobs.py`, and who the user says
they are (`profile`). Nothing here can
widen what the agent may do: no envelope, no consent rule, no model switch
but back.

Starting, stopping and restarting the agent itself are the desktop app's,
which owns the backend (ui/src-tauri/src/backend.rs): a worker asked to
stop itself could not be asked to start again.
"""

from __future__ import annotations

import contextlib
import json
import logging
from datetime import datetime, time, timedelta

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from agent import runtime
from agent.worker import bodies

log = logging.getLogger("control")

# How far back the activity feed reaches by default, and how much of it.
ACTIVITY_HOURS = 48
ACTIVITY_LIMIT = 200

# The job kinds worth a line in the feed. `action` is the ledger's own line,
# and `reflect` is housekeeping the user never asked about.
FEED_JOBS = ("research", "routine", "goal", "night", "mail")

# Where each channel's spend goes, in their words.
# Each kind of spending (core/spend.CATEGORIES), as the spend page names it.
CATEGORY_NAMES = {"chat": "Conversations", "background": "Background work",
                  "night": "Overnight work", "extensions": "Extensions"}


# ---------------------------------------------------------------------------
# Knobs
# ---------------------------------------------------------------------------

_DEFAULTS: dict | None = None


def _clock(text: str) -> time:
    hour, minute = (int(x) for x in text.split(":"))
    return time(hour, minute)


def defaults() -> dict:
    """Each knob as the code sets it, read once before any is applied."""
    global _DEFAULTS
    if _DEFAULTS is None:
        from agent.attention import settings
        from agent.worker import desk
        from core import call_policy, knobs, language, monitor, spend

        _DEFAULTS = {
            "MAX_DAY_PINGS": settings.MAX_DAY_PINGS,
            "MAX_NIGHT_PINGS": settings.MAX_NIGHT_PINGS,
            "QUIET_FROM": call_policy.QUIET_FROM.strftime("%H:%M"),
            "QUIET_UNTIL": call_policy.QUIET_UNTIL.strftime("%H:%M"),
            "MORNING": settings.MORNING.strftime("%H:%M"),
            "EVENING": settings.EVENING.strftime("%H:%M"),
            "BEFORE_LEAD_MIN": int(settings.BEFORE_LEAD.total_seconds() // 60),
            "FOLLOW_UP_AFTER": settings.FOLLOW_UP_AFTER,
            "MEETING_RESEARCH": settings.MEETING_RESEARCH,
            "UNITS": settings.UNITS,
            "DESK_LIVE_MIN": desk.LIVE_DAY_MIN,
            # Windows' own: core's, which reads the user's choice where each
            # is used (`language.chosen`), never from here.
            **dict(zip(("LANGUAGE", "REGION"), language.windows(), strict=True)),
            # The spending caps and the parts' day budgets: core's, which
            # reads them where it asks (`spend.may_spend`), never from here.
            **spend.DEFAULTS,
            "MONTH_ASK": spend.ASK_DEFAULT,
            # Off, from the frozen side: each is read where it is used
            # (`knobs.on`), never from here.
            **dict.fromkeys(knobs.SWITCHES, "off"),
            # What each channel may approve, unlowered: core's table, read
            # where an answer is decided (`monitor.ceiling`), never from here.
            **{knob: monitor.CEILINGS[channel][0]
               for channel, knob in monitor.LOWERED_BY.items()},
        }
    return dict(_DEFAULTS)


def apply_knobs(path=None) -> dict:
    """Put what the user set in the panel into force, over the code's defaults,
    and return every knob's value. Only ever values `core/knobs.read` let
    through, so only inside its bounds (pinned by tests/gate/test_knobs.py).
    Read at the worker's start and after each change in the panel; the code
    reads each of these where it is used, so nothing needs a restart."""
    from agent.attention import settings
    from agent.worker import background, desk
    from core import call_policy, knobs

    values = defaults() | knobs.read(path)
    settings.MAX_DAY_PINGS = int(values["MAX_DAY_PINGS"])
    settings.MAX_NIGHT_PINGS = int(values["MAX_NIGHT_PINGS"])
    # Read where they are used (`within_quiet_hours`).
    call_policy.QUIET_FROM = _clock(values["QUIET_FROM"])
    call_policy.QUIET_UNTIL = _clock(values["QUIET_UNTIL"])
    settings.MORNING = _clock(values["MORNING"])
    settings.EVENING = _clock(values["EVENING"])
    # Read once at import for the wake alarm; kept in step here.
    background.DIGEST_HOUR = settings.MORNING.hour
    settings.BEFORE_LEAD = timedelta(minutes=int(values["BEFORE_LEAD_MIN"]))
    # Read each look (`sources.calendar.after`) and each judging.
    settings.FOLLOW_UP_AFTER = str(values["FOLLOW_UP_AFTER"])
    # Read each time the judge is asked and each heads-up is written.
    settings.MEETING_RESEARCH = str(values["MEETING_RESEARCH"])
    # Read each time the weather is said (`sources.weather.units`).
    settings.UNITS = str(values["UNITS"])
    desk.LIVE_DAY_MIN = int(values["DESK_LIVE_MIN"])
    return values


def current_knobs(path=None) -> dict:
    """Every knob's value as `apply_knobs` would set it, setting nothing."""
    from core import knobs

    return defaults() | knobs.read(path)


def _knob_rows(values: dict) -> list[dict]:
    from agent import features
    from core import knobs

    base = defaults()
    return [{"name": name, "says": k.says, "group": k.group, "kind": k.kind, "lo": k.lo,
             "hi": k.hi, "value": values.get(name), "default": base.get(name),
             **({"choices": [list(c) for c in k.choices]} if k.choices else {}),
             **(dict(zip(("adds", "takes"), features.OFFERS[name], strict=True))
                if name in features.OFFERS else {})}
            for name, k in knobs.KNOBS.items()]


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _utc(text: str | None) -> str | None:
    """SQLite's UTC text as the app reads a moment (`sqltime.iso`); text
    that is not one is passed on as it is, so the activity list still sorts."""
    if not text:
        return None
    from agent import sqltime

    try:
        return sqltime.iso(sqltime.moment(text))
    except ValueError:
        return text


def _local(text: str | None) -> str | None:
    """A local wall-clock ISO string (routines, goals) as an aware one."""
    if not text:
        return None
    try:
        return datetime.fromisoformat(text).astimezone().isoformat()
    except ValueError:
        return text


def _since() -> dict[str, str]:
    """Where today, the last seven days and this month start, as SQLite UTC
    text: the user's days, as every cap counts them (`core/spend.windows`).
    They were SQLite's `localtime`, the machine's zone, not the user's."""
    from core import spend as ledger

    now = datetime.now().astimezone()
    span = ledger.windows(now)
    return {"today": span.day.since, "week": ledger.windows(now - timedelta(days=6)).day.since,
            "month": span.month.since}


def _spent(since: str) -> float:
    from core import spend as ledger

    return round(ledger.spent(runtime.conn, since), 6)


def _mailbox_today() -> dict:
    """The agent's own address, and what reached it today, by kind."""
    from agent import mailbox

    counts = {k: n for k, n in runtime.conn.execute(
        "SELECT kind, COUNT(*) FROM mailbox WHERE date(created_at, 'localtime') ="
        " date('now', 'localtime') GROUP BY kind")}
    address = mailbox.address(runtime.conn)
    return {"address": address, "enabled": bool(address),
            "today": {k: counts.get(k, 0) for k in ("instruction", "thread", "ignored")}}


def quiet_hours() -> dict:
    from core.call_policy import QUIET_FROM, QUIET_UNTIL, within_quiet_hours

    return {"from": QUIET_FROM.strftime("%H:%M"), "until": QUIET_UNTIL.strftime("%H:%M"),
            "now": within_quiet_hours()}


async def overview(_: Request) -> JSONResponse:
    """The first screen: is it all up, what is waiting, what it cost today,
    and when it last slept."""
    from agent import approvals, awake, goals, issues, routines
    from agent.worker import routes

    health = json.loads((await routes.health(_)).body)
    live_routines = routines.listed(runtime.conn)
    live_goals = goals.listed(runtime.conn)
    return JSONResponse({
        "worker": health,
        "signal": runtime.signal_state,
        "telegram": runtime.telegram_state,
        "sandbox": await routes._SANDBOX_PROBE.get(),
        "waiting": len(approvals.pending(runtime.conn, limit=50)),
        "spend_today": _spent(_since()["today"]),
        "routines": {s: sum(r.status == s for r in live_routines)
                     for s in ("active", "paused", "awaiting_approval")},
        "goals": {s: sum(g.status == s for g in live_goals)
                  for s in ("active", "paused", "awaiting_approval")},
        "questions": sum(len(g.open_questions) for g in live_goals),
        "quiet_hours": quiet_hours(),
        "issues": issues.recent(runtime.conn, hours=24, limit=5),
        "mailbox": _mailbox_today(),
        "awake": awake.status(runtime.conn),
    })


async def autonomy(_: Request) -> JSONResponse:
    """Their routines and goals in full, the quiet hours, and the knobs."""
    from agent import goals, routines

    out_routines = []
    for r in routines.listed(runtime.conn):
        row = r.as_dict()
        row |= {"next_run": _local(r.next_run_at), "last_run": _local(r.last_run_at),
                "last_report": r.last_report, "held": r.held}
        out_routines.append(row)
    out_goals = []
    for g in goals.listed(runtime.conn):
        row = g.as_dict(full=True)
        row |= {"next_check": _local(g.next_check_at), "last_session": _local(g.last_session_at),
                "last_report": g.last_report, "every_hours": g.every_hours}
        out_goals.append(row)
    return JSONResponse({"routines": out_routines, "goals": out_goals,
                         "quiet_hours": quiet_hours(), "knobs": _knob_rows(current_knobs())})


def _receipt(result_json: str | None) -> dict | None:
    try:
        result = json.loads(result_json or "null")
    except ValueError:
        return None
    receipt = result.get("receipt") if isinstance(result, dict) else None
    return receipt if isinstance(receipt, dict) else None


def job_line(row) -> tuple[str, str]:
    """A job as a title and a detail line."""
    try:
        result = json.loads(row["result"] or "null")
    except ValueError:
        result = row["result"]
    kind, why = row["kind"], row["why"] or ""
    if kind in ("routine", "goal"):
        title = why.split(":", 1)[-1].strip() or f"{kind} {row['prompt']}"
        if not isinstance(result, dict):
            detail = row["error"] or row["status"]
        elif not result.get("ran", True):
            detail = f"skipped: {result.get('why')}"
        else:
            bits = ["told you" if result.get("reported") else "nothing new"]
            if result.get("asked"):
                bits.append(f"asked {result['asked']} question(s)")
            if result.get("status") == "done" or result.get("finished"):
                bits.append("finished")
            if "cost_usd" in result:
                bits.append(f"${result['cost_usd']:.4f}")
            detail = ", ".join(bits)
        return title, detail
    first = (row["prompt"] or "").strip().splitlines()[0][:140] if row["prompt"] else kind
    if kind == "night":
        return "The night's work", (row["error"] or row["status"])
    return first, (row["error"] or row["status"])


async def activity(request: Request) -> JSONResponse:
    """What it did, newest first: actions with their receipts, routine runs,
    goal sessions, background tasks, the night, and what went wrong."""
    hours = bodies.whole(request.query_params.get("hours") or ACTIVITY_HOURS)
    if hours is None:
        return bodies.bad("hours is a number")
    hours = max(1, min(hours, 24 * 14))
    since = f"-{hours} hours"
    items: list[dict] = []
    # The action critic's view of each action an unattended session took or
    # asked for (core/critic.py), matched by session and the ledger's hash.
    reviews = {(r["session_id"], r["action_key"]): r for r in runtime.conn.execute(
        "SELECT session_id, action_key, verdict, decision, mode FROM critic_reviews"
        " WHERE at >= datetime('now', ?)", (since,))}
    for r in runtime.conn.execute(
            "SELECT id, tool, preview, status, channel, error, result_json, created_at,"
            " executed_at, decided_by, session_id, args_hash FROM actions"
            " WHERE created_at >= datetime('now', ?) ORDER BY id DESC LIMIT ?",
            (since, ACTIVITY_LIMIT)):
        receipt = _receipt(r["result_json"])
        review = reviews.get((r["session_id"], r["args_hash"]))
        items.append({
            "at": _utc(r["executed_at"] or r["created_at"]), "kind": "action",
            "title": (r["preview"] or r["tool"]).splitlines()[0][:200],
            "detail": r["tool"], "status": r["status"], "channel": r["channel"],
            "receipt": receipt.get("status") if receipt else None,
            "critic": ({"verdict": review["verdict"], "decision": review["decision"],
                        "mode": review["mode"]} if review else None),
            "error": r["error"], "by": r["decided_by"], "id": r["id"]})
    marks = ",".join("?" * len(FEED_JOBS))
    for r in runtime.conn.execute(
            f"SELECT id, kind, prompt, why, status, result, error, created_at, finished_at"
            f" FROM jobs WHERE kind IN ({marks}) AND created_at >= datetime('now', ?)"
            f" ORDER BY id DESC LIMIT ?", (*FEED_JOBS, since, ACTIVITY_LIMIT)):
        title, detail = job_line(r)
        items.append({"at": _utc(r["finished_at"] or r["created_at"]), "kind": r["kind"],
                      "title": title, "detail": detail, "status": r["status"], "id": r["id"]})
    for r in runtime.conn.execute(
            "SELECT i.id, i.kind, i.summary, MAX(e.at) AS at, COUNT(e.id) AS n FROM issues i"
            " JOIN issue_events e ON e.issue_id = i.id WHERE e.at >= datetime('now', ?)"
            " GROUP BY i.id ORDER BY at DESC LIMIT 50", (since,)):
        items.append({"at": _utc(r["at"]), "kind": "issue", "title": r["summary"],
                      "detail": f"{r['kind']}, {r['n']} time{'s' if r['n'] != 1 else ''}",
                      "status": "problem", "id": r["id"]})
    # What came to the agent's own mailbox, taken or not: a stranger's mail
    # it ignored is part of the record too.
    for r in runtime.conn.execute(
            "SELECT id, sender, subject, kind, why, created_at FROM mailbox"
            " WHERE created_at >= datetime('now', ?) ORDER BY id DESC LIMIT 100", (since,)):
        said = {"instruction": "yours", "thread": "a reply in your thread",
                "ignored": "ignored"}.get(r["kind"], r["kind"])
        items.append({"at": _utc(r["created_at"]), "kind": "mailbox",
                      "title": f"Mail to your agent: {r['subject'] or '(no subject)'}",
                      "detail": f"from {r['sender'] or 'unknown'} -- {said} ({r['why']})",
                      "status": r["kind"], "id": r["id"]})
    items.sort(key=lambda item: item["at"] or "", reverse=True)
    return JSONResponse({"hours": hours, "items": items[:ACTIVITY_LIMIT]})


async def spend(_: Request) -> JSONResponse:
    """What it spent, by kind of work, against each cap -- the month's first,
    since it stops everything -- and each part's day budget, with the caps'
    notices for the banner (core/spend.py, agent/caps.py)."""
    from agent import caps as told
    from agent import goals
    from core import knobs
    from core import spend as ledger
    from core.policy import load_policy

    since = _since()
    span = ledger.windows()
    limits = ledger.limits()

    def by_category(start: str) -> dict[str, float]:
        rows = runtime.conn.execute(
            "SELECT category, COALESCE(SUM(cost_usd), 0) FROM usage_events"
            " WHERE created_at >= ? GROUP BY category", (start,)).fetchall()
        out = {CATEGORY_NAMES.get(category, category): round(float(usd or 0), 6)
               for category, usd in rows if usd}
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))

    def against(cap: str, **which) -> dict:
        used = ledger.spent(runtime.conn, ledger.window_for(cap, span).since, **which)
        return {"cap": cap, "name": knobs.KNOBS[cap].says, "spent": round(used, 6),
                "limit": limits[cap]}

    trials = ledger.spent(runtime.conn, span.month.since, part="trials")
    return JSONResponse({
        **{window: _spent(start) for window, start in since.items()},
        "by_category": {window: by_category(start) for window, start in since.items()},
        "caps": [against("MONTH_USD")] + [against(cap, category=category)
                                          for category, cap in ledger.CAPS.items()],
        "budgets": [against(cap, part=part) for part, cap in ledger.PARTS.items()]
        + [{"cap": None, "name": "Model trials, per month", "spent": round(trials, 6),
            "limit": load_policy().models.month_budget_usd}],
        "notices": told.standing(runtime.conn),
        "goals": [{"id": g.id, "title": g.title, "spent": round(g.spent_usd, 6),
                   "budget": g.budget_usd} for g in goals.listed(runtime.conn)],
    })


async def models(_: Request) -> JSONResponse:
    """The lineup in force, what the scout switched and is watching, and its
    recent trials."""
    from agent import models_switch
    from agent.models import Role
    from agent.provider import describe

    trials = [dict(r) | {"started_at": _utc(r["started_at"]), "finished_at": _utc(r["finished_at"])}
              for r in runtime.conn.execute(
                  "SELECT id, slug, role, incumbent, decision, spent_usd, report, error,"
                  " started_at, finished_at FROM model_evals ORDER BY id DESC LIMIT 10")]
    for trial in trials:
        with contextlib.suppress(ValueError):
            trial["decision"] = json.loads(trial["decision"] or "null")
    return JSONResponse({"lineup": {r.value: describe(r) for r in Role},
                         "switches": models_switch.status(runtime.conn), "trials": trials})


# ---------------------------------------------------------------------------
# Turning
# ---------------------------------------------------------------------------


async def routine_op(request: Request) -> JSONResponse:
    """Pause, resume or cancel one of their routines."""
    from agent import routines

    body = await bodies.body(request)
    routine_id, op = bodies.whole(body.get("id") or 0), str(body.get("op") or "")
    if routine_id is None:
        return bodies.bad("a routine's id is a number")
    if op == "pause":
        done = routines.pause(runtime.conn, routine_id)
    elif op == "resume":
        done = routines.pause(runtime.conn, routine_id, resume=True)
    elif op == "cancel":
        done = routines.cancel(runtime.conn, routine_id)
    else:
        return bodies.bad(f"no such thing to do to a routine: {op!r}")
    return JSONResponse({"ok": done, **({} if done else {"error": f"routine {routine_id} "
                                                                  f"could not be {op}d"})})


async def note_op(request: Request) -> JSONResponse:
    """Delete one of their notes, with its transcript."""
    from agent import notes

    body = await bodies.body(request)
    note_id, op = bodies.whole(body.get("id") or 0), str(body.get("op") or "")
    if note_id is None:
        return bodies.bad("a note's id is a number")
    if op != "delete":
        return bodies.bad(f"no such thing to do to a note: {op!r}")
    done = notes.delete(runtime.conn, note_id)
    return JSONResponse({"ok": done, **({} if done else {"error": f"no note {note_id}"})})


async def goal_op(request: Request) -> JSONResponse:
    """Pause, resume (with more budget), drop or answer one of their goals.
    An answer here is theirs, as one from Signal is: the tray is them."""
    from agent import goals

    body = await bodies.body(request)
    goal_id, op = bodies.whole(body.get("id") or 0), str(body.get("op") or "")
    question = bodies.whole(body.get("question") or 0)
    try:
        more_usd = float(body.get("add_budget_usd") or 0)
    except (TypeError, ValueError):
        more_usd = None
    if goal_id is None or question is None or more_usd is None:
        return bodies.bad("a goal's id, question and added budget are numbers")
    try:
        if op == "pause":
            done = goals.pause(runtime.conn, goal_id)
        elif op == "resume":
            done = goals.pause(runtime.conn, goal_id, resume=True,
                               add_budget_usd=more_usd)
        elif op == "drop":
            done = goals.drop(runtime.conn, goal_id)
        elif op == "answer":
            goals.answer(runtime.conn, goal_id, str(body.get("text") or ""),
                         question=question)
            done = True
        else:
            return bodies.bad(f"no such thing to do to a goal: {op!r}")
    except goals.BadGoal as exc:
        return bodies.bad(str(exc))
    return JSONResponse({"ok": done, **({} if done else {"error": f"goal {goal_id} could not "
                                                                  f"be {op}ped"})})


async def stop_all(request: Request) -> JSONResponse:
    """STOP ROUTINES or STOP GOALS, from a button."""
    from agent import goals, routines

    what = str((await bodies.body(request)).get("what") or "")
    if what == "routines":
        return JSONResponse({"ok": True, "paused": routines.pause_all(runtime.conn)})
    if what == "goals":
        return JSONResponse({"ok": True, "paused": goals.pause_all(runtime.conn)})
    return bodies.bad(f"stop what? {what!r}")


def _briefings_outside_quiet_hours(values: dict) -> None:
    """A briefing set inside quiet hours is refused, both named: none is
    sent in them (`briefing.due`), so it would not come when it says."""
    from core import call_policy, knobs

    whole = defaults() | values
    start, end = _clock(whole["QUIET_FROM"]), _clock(whole["QUIET_UNTIL"])
    for name, says in (("MORNING", "The morning briefing"),
                       ("EVENING", "The evening look-ahead")):
        if call_policy.is_quiet(_clock(whole[name]), start, end):
            raise knobs.BadKnob(f"{says} at {whole[name]} would be in quiet hours"
                                f" ({whole['QUIET_FROM']} to {whole['QUIET_UNTIL']});"
                                " move one of them")


async def set_knobs(request: Request) -> JSONResponse:
    """Change knobs, inside their frozen bounds, in force at once."""
    from core import knobs

    try:
        body = await request.json()
    except ValueError:
        body = None
    changes = body.get("changes") if isinstance(body, dict) else None
    if not isinstance(changes, dict) or not changes:
        return bodies.bad("nothing to change")
    try:
        knobs.write(changes, check=_briefings_outside_quiet_hours)
    except knobs.BadKnob as exc:
        return bodies.bad(str(exc))
    return JSONResponse({"ok": True, "knobs": _knob_rows(apply_knobs())})


async def revert_models(_: Request) -> JSONResponse:
    """REVERT MODELS, from a button: the lineup as written, nothing else."""
    from agent import models_switch

    back = models_switch.revert(runtime.conn, by="user",
                                why="the user pressed Revert in the app")
    refused = models_switch.apply()
    return JSONResponse({"ok": True, "reverted": back, "refused": refused})


# ---------------------------------------------------------------------------
# Who the user is
# ---------------------------------------------------------------------------


def _set_identity(conn, changes: dict[str, str]) -> dict[str, str]:
    """Keep what the user typed as facts they confirmed, and forget one
    they emptied. A value already held is left as it is (`facts.add`)."""
    from agent import facts

    for attribute, value in changes.items():
        if " ".join(value.split()):
            facts.add(conn, facts.USER, attribute, value, source="confirmed")
            continue
        for row in conn.execute(
                "SELECT id FROM facts WHERE subject = ? AND attribute = ? AND status = 'active'",
                (facts.USER, attribute)).fetchall():
            facts.forget(conn, row["id"])
    return facts.identity(conn)


async def profile(request: Request) -> JSONResponse:
    """GET: who the user is, each of `facts.IDENTITY`. PUT
    `{"profile": {attribute: value}}`: change some of them, "" to clear one.

    The same facts the conversation keeps, not a settings file beside them:
    "I moved to Leeds" said in chat and the city typed here are one fact,
    and the newer wins. Off the loop, because keeping a fact embeds it.

    What is saved here leads every turn, with the user's own authority,
    and a value can be pasted from anywhere. So a value the injection
    screen's patterns flag is refused (`core/screen.pre_screen`), and every
    value reaches a turn quoted, as data (`facts.render_line`)."""
    from agent import facts, offloop
    from core import screen

    if request.method == "GET":
        return JSONResponse({"profile": facts.identity(runtime.conn)})
    try:
        body = await request.json()
    except ValueError:
        body = None
    # Nothing but the profile: a "subject" beside it was ignored, so a
    # request for someone else's name saved it as the user's.
    if not isinstance(body, dict) or set(body) != {"profile"}:
        return bodies.bad('send {"profile": {attribute: value}} and nothing else')
    changes = body["profile"]
    if not isinstance(changes, dict) or not changes:
        return bodies.bad("nothing to change")
    if unknown := sorted(set(changes) - set(facts.IDENTITY)):
        return bodies.bad(f"not part of the profile: {', '.join(unknown)}")
    if any(not isinstance(v, str) for v in changes.values()):
        return bodies.bad("each value is text")
    # Refused, not cut short: what is saved is what the user sees.
    too_long = sorted(a for a, v in changes.items()
                      if len(" ".join(v.split())) > facts.VALUE_CHARS)
    if too_long:
        return bodies.bad(f"not saved: {', '.join(too_long)} is over {facts.VALUE_CHARS} "
                          "characters")
    if flagged := sorted(a for a, v in changes.items() if screen.pre_screen(v)):
        return bodies.bad(f"not saved: {', '.join(flagged)} reads like an instruction to an AI")
    saved = await offloop.with_own_conn(runtime.conn, _set_identity, changes)
    answer = {"ok": True, "profile": saved}
    if "home_city" in changes:
        # Placed now, not at the next forecast, so the page can say which
        # Portland the weather is for, or that none was found.
        from agent import home

        answer["home"] = await offloop.with_own_conn(runtime.conn, home.placed)
    return JSONResponse(answer)


async def app_settings(request: Request) -> JSONResponse:
    """GET: the app's choices (agent/appsettings.py), every one. POST
    `{"changes": {name: value}}`: change some, None for the default."""
    from agent import appsettings

    if request.method == "GET":
        return JSONResponse({"ok": True, "settings": appsettings.current()})
    try:
        body = await request.json()
    except ValueError:
        body = None
    changes = body.get("changes") if isinstance(body, dict) else None
    if not isinstance(changes, dict) or not changes:
        return bodies.bad("nothing to change")
    try:
        return JSONResponse({"ok": True, "settings": appsettings.write(changes)})
    except appsettings.BadChoice as exc:
        return bodies.bad(str(exc))


ROUTES = [
    Route("/control/app", app_settings, methods=["GET", "POST"]),
    Route("/control/overview", overview),
    Route("/control/autonomy", autonomy),
    Route("/control/activity", activity),
    Route("/control/spend", spend),
    Route("/control/models", models),
    Route("/control/routine", routine_op, methods=["POST"]),
    Route("/control/goal", goal_op, methods=["POST"]),
    Route("/control/note", note_op, methods=["POST"]),
    Route("/control/stop", stop_all, methods=["POST"]),
    Route("/control/knobs", set_knobs, methods=["POST"]),
    Route("/control/models/revert", revert_models, methods=["POST"]),
    Route("/control/profile", profile, methods=["GET", "PUT"]),
]

