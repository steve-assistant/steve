"""Finding models that might do a role better, or for less.

The user moved every role from GPT-5.6 Luna to GPT-6 Luna by hand on 25
September 2026: it was smarter and cost half as much, and they had to notice it
themselves. This is how the agent notices. It reads OpenAI's own model list,
and asks the frozen bounds (core/models_guard.py) which automatic roles each
model could play. It says nothing about quality -- only an evaluation on
their own turns can -- and it switches nothing.

OpenAI's list says what a model is called and when it was made, not what it
costs. The price is the guard's frozen table (`models_guard.PRICED`), so
nothing written here can make a model look cheaper than it is. A model not
in the table is reported as unpriced: a maintainer adds its line, and the
next scan judges it.

Until 12 October 2026 the scout read OpenRouter's catalog too, and could
propose DeepSeek or Gemini on OpenRouter's hosts. Models are reached on
OpenAI alone now, with OpenRouter kept for Jev.

    python -m agent.models_scout scan [--json]
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from collections import Counter

import httpx2

from agent import models
from agent.models import Role
from core import endpoints, secrets
from core.models_guard import PRICED, Candidate, blended, refusal
from core.policy import load_policy

log = logging.getLogger(__name__)

KEY = "OPENAI_API_KEY"
TIMEOUT_S = 30.0
# Older models are not news: anything better among them would have been
# picked by hand. A year still covers every model the lineup was chosen from.
MAX_AGE_DAYS = 365
# A dated snapshot ("gpt-6-luna-2026-09-22") is a model already on the list
# under its own name, pinned to one day's version.
SNAPSHOT = re.compile(r"-\d{4}-\d{2}-\d{2}$")


class ScoutError(RuntimeError):
    """OpenAI's model list could not be read."""


def _get(url: str, headers: dict | None = None) -> dict:
    """The one door to OpenAI's list; the test fences shut it."""
    try:
        response = httpx2.get(url, headers=headers or {}, timeout=TIMEOUT_S,
                              follow_redirects=True)
    except httpx2.HTTPError as exc:
        raise ScoutError(f"{url}: {type(exc).__name__}: {exc}") from exc
    if response.status_code != 200:
        raise ScoutError(f"{url}: HTTP {response.status_code}: {response.text[:200]}")
    return response.json()


def gather(fetch=None, now: float | None = None) -> tuple[list[tuple[Candidate, str]], int]:
    """Every model OpenAI made in the last year, with its release date, and
    how many models its list held in all."""
    key = secrets.get(KEY)
    if not key:
        raise ScoutError(f"{KEY} is not set, so OpenAI's list cannot be read")
    # Looked up now, not bound as a default, so the test fence on `_get` holds.
    listed = (fetch or _get)(f"{endpoints.openai()}/models", {"Authorization": f"Bearer {key}"})
    listed = listed.get("data") or []
    oldest = (now or time.time()) - MAX_AGE_DAYS * 86400
    found = []
    for m in listed:
        name = str(m.get("id") or "")
        try:
            created = int(m.get("created") or 0)
        except (TypeError, ValueError):
            created = 0
        # A ":" is a fine-tune or a tier, not the model itself.
        if not name or ":" in name or SNAPSHOT.search(name) or created < oldest:
            continue
        found.append((Candidate(f"openai/{name}"),
                      time.strftime("%Y-%m-%d", time.gmtime(created))))
    return found, len(listed)


def judge(candidates: list[tuple[Candidate, str]]) -> list[dict]:
    """Which automatic roles each model may play, against the lineup now."""
    roles = load_policy().models.auto_roles
    rows = []
    for candidate, released in candidates:
        verdicts: dict[str, str | None] = {}
        for name in roles:
            incumbent = models.MODELS[Role(name)]
            if candidate.slug == incumbent.slug:
                verdicts[name] = "it is the model the role uses now"
            else:
                verdicts[name] = refusal(candidate, name, incumbent)
        rows.append({"candidate": candidate, "released": released, "roles": verdicts})
    return rows


def record(conn, rows: list[dict]) -> list[str]:
    """Keep what was seen; returns the models never seen before."""
    known = {r[0] for r in conn.execute("SELECT slug FROM model_candidates")}
    for row in rows:
        c = row["candidate"]
        eligible = sum(1 for v in row["roles"].values() if v is None)
        conn.execute(
            "INSERT INTO model_candidates (slug, released, facts, roles, eligible)"
            " VALUES (?, ?, ?, ?, ?) ON CONFLICT(slug) DO UPDATE SET "
            "released = excluded.released, facts = excluded.facts, "
            "roles = excluded.roles, eligible = excluded.eligible, "
            "last_seen = datetime('now')",
            (c.slug, row["released"], json.dumps(c.to_dict()),
             json.dumps(row["roles"]), eligible))
    conn.commit()
    return [row["candidate"].slug for row in rows if row["candidate"].slug not in known]


def scan(conn=None, fetch=None) -> dict:
    """Read OpenAI's list, judge every model against the bounds, keep the
    result. Returns the report `main` prints."""
    candidates, listed = gather(fetch)
    rows = judge(candidates)
    new = record(conn, rows) if conn is not None else []
    return {"listed": listed, "rows": rows, "new": new}


# ---------------------------------------------------------------------------
# What is worth trying
#
# OpenAI's list carries no benchmark score, and the one OpenRouter's catalog
# carried did not predict their turns: DeepSeek V4.1 Flash scored above Luna
# (39.5 to 38.1) and lost 20 of 40 to it. A model only reaches the shortlist
# by having a price in the guard's frozen table, which a maintainer added
# because it looked worth a trial. So the shortlist takes what the bounds
# admit, newest first, and the trial's early stop (models_eval.hopeless) is
# the cheap test that is actually about them.
# ---------------------------------------------------------------------------

# A model tried for a role is not tried for it again for this long.
RETRY_AFTER_DAYS = 90
TRIALS_A_WEEK = 3


def shortlist(conn, limit: int = TRIALS_A_WEEK) -> list[dict]:
    """What is worth trying: a model the bounds admit for a role that can be
    tried, and not tried for it lately, newest first."""
    from agent.models_eval import TRIABLE
    from core.models_guard import Refused, admit

    rows = conn.execute("SELECT slug, released, facts, roles FROM model_candidates"
                        " WHERE eligible > 0 ORDER BY released DESC, slug").fetchall()
    picks = []
    for role in TRIABLE:
        incumbent = models.MODELS[role]
        for r in rows:
            if r["slug"] == incumbent.slug or json.loads(r["roles"] or "{}").get(role.value,
                                                                               "") is not None:
                continue
            if conn.execute("SELECT 1 FROM model_evals WHERE slug = ? AND role = ? AND"
                            f" started_at >= datetime('now', '-{RETRY_AFTER_DAYS} days')",
                            (r["slug"], role.value)).fetchone():
                continue
            try:
                route = admit(Candidate.from_dict(json.loads(r["facts"])), role.value,
                              models.BASE[role])
            except Refused:
                continue  # the bounds moved since the scan
            ratio = blended(route) / blended(incumbent)
            picks.append({"slug": r["slug"], "role": role,
                          "why": f"released {r['released']}, at {ratio:.2f}x the cost"})
    return picks[:limit]


def _route_note(candidate: Candidate, role: str) -> str:
    from core.models_guard import admit

    route = admit(candidate, role, models.MODELS[Role(role)])
    ratio = blended(route) / blended(models.MODELS[Role(role)])
    return (f"{route.input_usd:g}/{route.output_usd:g} per M, {ratio:.2f}x, "
            f"{route.effort or 'no reasoning'}")


def render(report: dict) -> str:
    rows = report["rows"]
    priced = [r for r in rows if r["candidate"].slug in PRICED]
    unpriced = [r for r in rows if r["candidate"].slug not in PRICED]
    lines = [f"{report['listed']} models on OpenAI's list; {len(rows)} from the last year; "
             f"{sum(1 for r in rows if None in r['roles'].values())} may play a role."]
    for role in load_policy().models.auto_roles:
        now = models.MODELS[Role(role)]
        lines.append(f"\n{role} -- now {now.slug} ({now.effort or 'no reasoning'})")
        able = [r for r in rows if r["roles"].get(role) is None]
        for r in sorted(able, key=lambda r: r["released"], reverse=True):
            lines.append(f"  {r['candidate'].slug:<40} {r['released']}  "
                         f"{_route_note(r['candidate'], role)}")
        if not able:
            lines.append("  nothing")
    reasons = Counter(re.sub(r"\d[\d.,]*", "#", v.split(":")[0])
                      for r in priced for v in r["roles"].values() if v)
    if reasons:
        lines.append("\nWhy the priced models may not, most often:")
        lines += [f"  {n:>4}  {why}" for why, n in reasons.most_common(8)]
    if unpriced:
        lines.append(f"\nNot judged, with no price in core/models_guard.PRICED "
                     f"({len(unpriced)}), newest first:")
        lines += [f"  {r['candidate'].slug:<40} {r['released']}"
                  for r in sorted(unpriced, key=lambda r: r["released"], reverse=True)[:12]]
    if report["new"]:
        lines.append(f"\nNew since the last scan: {', '.join(report['new'])}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agent.models_scout")
    sub = parser.add_subparsers(dest="command", required=True)
    scan_p = sub.add_parser("scan", help="list what the bounds would let each role switch to")
    scan_p.add_argument("--json", action="store_true")
    eval_p = sub.add_parser("eval", help="try a scanned model on the user's turns against a role's")
    eval_p.add_argument("slug")
    eval_p.add_argument("role", choices=[r.value for r in Role])
    eval_p.add_argument("--turns", type=int, default=40)
    eval_p.add_argument("--dry", action="store_true",
                        help="rebuild the turns and estimate the cost; call no model")
    switch_p = sub.add_parser("switch", help="switch a role to a scanned model, by hand")
    switch_p.add_argument("slug")
    switch_p.add_argument("role", choices=[r.value for r in Role])
    switch_p.add_argument("--anyway", action="store_true",
                          help="even though its last trial did not clear the bar")
    revert_p = sub.add_parser("revert", help="put roles back on the lineup as written")
    revert_p.add_argument("role", nargs="?", choices=[r.value for r in Role])
    sub.add_parser("status", help="what is switched, and what is being watched")
    sub.add_parser("shortlist", help="what the night would try next")
    args = parser.parse_args(argv)

    from core import db

    if refused := models.load_active():
        print(f"switches refused, those roles as written: {refused}", file=sys.stderr)
    if args.command in ("switch", "revert", "status", "shortlist"):
        with db.open_store() as conn:
            return _by_hand(conn, args)
    if args.command == "eval":
        import asyncio

        from agent import models_eval

        with db.open_store() as conn:
            try:
                out = asyncio.run(models_eval.trial(conn, args.slug, Role(args.role),
                                                    args.turns, dry=args.dry))
            except models_eval.TrialRefused as exc:
                print(f"refused: {exc}", file=sys.stderr)
                return 2
        print(json.dumps(out, indent=1))
        return 0
    with db.open_store() as conn:
        try:
            report = scan(conn)
        except ScoutError as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 2
    if args.json:
        print(json.dumps({**report, "rows": [
            {**r, "candidate": r["candidate"].to_dict()} for r in report["rows"]]}, indent=1))
    else:
        print(render(report))
    return 0


def _by_hand(conn, args) -> int:
    """The user at the laptop. A switch or a revert here is theirs, and is put into
    the running worker by a swap: this is a process of its own."""
    from agent import control_plane, models_switch
    from core.models_guard import BY_HAND

    if args.command == "status":
        print(json.dumps(models_switch.status(conn), indent=1, default=str))
        return 0
    if args.command == "shortlist":
        for pick in shortlist(conn):
            print(f"{pick['slug']:<40} {pick['role'].value:<9} {pick['why']}")
        return 0
    if args.command == "switch":
        role = Role(args.role)
        trial = models_switch.last_trial(conn, args.slug, role)
        decision = models_switch._decision(trial)
        if not (decision and decision.switch) and not args.anyway:
            print(f"refused: its last trial for {role.value} "
                  + (f"said no ({decision.reason})" if decision else "is missing")
                  + "; add --anyway to switch regardless", file=sys.stderr)
            return 2
        try:
            print(models_switch.switch(conn, args.slug, role, by=BY_HAND))
        except models_switch.SwitchRefused as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 2
    else:
        back = models_switch.revert(conn, [args.role] if args.role else None, by=BY_HAND,
                                    why="put back by hand")
        print(f"put back: {', '.join(back) or 'nothing was switched'}")
    print(json.dumps(control_plane.post_supervisor("/_sup/swap", {}), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
