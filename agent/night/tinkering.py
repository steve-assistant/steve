"""The improvement block: modules the night builds for Steve.

Until step 6 the night proposed changes to Steve's own tree here, from the
failure ledger and the user's requests, and a gate merged the additive
ones unattended. That was self-upgrade v1, and it went in step 6
(docs/plan/06-modules.md, section 8): what Steve makes for itself is a
module now, built apart from the tree and waiting for the user's card like
any other (agent/modbuild.py). The night proposes module builds: fixes
from the ledger that a module can make, then features the user would
notice, chosen from what `agent/night/evidence.py` shows it.

The switch stays, and stays first: `self_upgrade.agent_initiated` in
core/policy.yaml, off by default. While it is off nothing is spent, and
the morning says why there were no builds. A build that would pass the
day's cap is not started: it waits for "Allow this build" in the app, as
one the user asked for does.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3

from pydantic import BaseModel, Field

from agent import issues, modbuild, offloop
from agent.night import evidence, model
from agent.night.report import Budget, NightReport

log = logging.getLogger("night")

# A ceiling, so a cheap night cannot turn into a long one: a build takes
# up to an attempt's budget and a quarter of an hour.
MAX_BUILDS = 2


class Proposal(BaseModel):
    """One module the night wants to build."""

    title: str = Field(description="One line naming the module, for the morning report.")
    fixes: int | None = Field(default=None, description="The ledger issue number this fixes, "
                                                        "or null for a new feature.")
    request: str = Field(description="What the module should do, said plainly as the user "
                                     "would ask for it, naming the site or source it reads.")
    why: str = Field(default="", description="Which of the user's requests or which issue it "
                                             "serves, and what they would notice.")


class Proposals(BaseModel):
    """Modules the night wants to build."""

    proposals: list[Proposal] = Field(default_factory=list)


async def improve(conn: sqlite3.Connection, budget: Budget, report: NightReport) -> None:
    """Build up to MAX_BUILDS modules, each ending at a card."""
    from core.policy import load_policy
    from core.sandbox import probe

    # Checked before anything is spent: a paused night pays for nothing.
    if (paused := load_policy().self_upgrade.refusal("night")) is not None:
        report.improvement_paused = paused
        return
    support = probe.verdict()
    if support is None or not support.ok:
        report.improvement_paused = ("this computer cannot run a module's code, so a module "
                                     "could not be tested")
        return
    if budget.exhausted(headroom=0.10):
        return
    try:
        known = await evidence.gather(conn)
        chosen, cost = await model.ask(
            Proposals,
            f"{known.brief()}\n\nChoose tonight's modules: up to {MAX_BUILDS}, or none if "
            "nothing is worth it.",
            model.prompt("night_proposals"))
        budget.add(cost)
    except Exception:  # noqa: BLE001 - the night goes on to its other blocks
        log.exception("could not choose tonight's modules")
        return
    offered = known.by_id()
    for proposal in chosen.proposals[:MAX_BUILDS]:
        if budget.exhausted(headroom=0.10):
            report.stopped_because = "the night's budget is spent"
            return
        issue = offered.get(proposal.fixes) if proposal.fixes is not None else None
        report.built.append(await _build(conn, proposal, issue, budget))


async def _build(conn: sqlite3.Connection, proposal: Proposal, issue: dict | None,
                 budget: Budget) -> str:
    """One proposal through `modbuild`, as the morning reads it."""
    title = " ".join(proposal.title.split())[:90] or "a module"
    came_from = "the night's proposal" + (f", for issue #{issue['id']}" if issue else "")
    try:
        row = await offloop.with_own_conn(conn, modbuild.request, proposal.request,
                                          origin="night", came_from=came_from)
    except modbuild.Refused as exc:
        return f"{title}: not started, {exc}"
    if row["state"] != "running":
        return f"#{row['id']} {title}: waits for you to allow it on the Extensions page"
    # The morning report says how it ended; nobody is in a conversation to
    # tell at three in the morning.
    db_file = offloop.db_path(conn)
    ended = await asyncio.to_thread(_run, db_file, row["id"])
    budget.add(ended["cost_usd"])
    if issue is not None:
        try:
            await offloop.with_own_conn(conn, issues.attempted, int(issue["id"]), row["id"],
                                        ended["state"])
        except Exception:  # noqa: BLE001 - bookkeeping, never the night's end
            log.warning("could not note the build for issue %s", issue["id"], exc_info=True)
    if ended["state"] == "carded":
        return f"#{row['id']} {title}: its card waits for you on the Extensions page"
    return f"#{row['id']} {title}: {ended['state']}, {ended['said'][:200]}"


def _run(store, build_id: int) -> dict:
    """The build, blocking, on a connection of its own, the computer kept
    awake: the night runs at three in the morning on a machine the wake
    task brought up."""
    from agent.wake import kept_awake
    from core import db, identity

    own = db.open_store(store) if store is not None else db.open_store()
    try:
        with kept_awake(f"{identity.NAME}: building a module"):
            return modbuild.build(own, build_id, notify=lambda text: None)
    finally:
        own.close()
