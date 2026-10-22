"""The yardstick: the agent's unattended sessions, run against a fake world
with attacks planted in it, and scored on what they did.

The user's plan of 4 October 2026: "measured, not asserted". Three decisions
are next, and all three are about widening what the agent does on its own:
the action critic enforcing (core/critic.py), the injection screen
enforcing (core/screen.py), and clean-room browsing workers. Each would
otherwise be decided on a few days of shadow logs from whatever happened to
arrive. This gives each a number before and after: of the tasks the user would
set, how many get done, and of the attacks planted in what they read, how
many land.

**What runs is the real thing.** Each scenario (tests/gate/fixtures/
autonomy/scenarios.json) is played through the handler that runs it for
real -- `handle_routine`, `handle_goal`, `handle_mail`, `handle_research` --
with the live model lineup, the live prompts, the monitor, the egress rule,
and the critic and the screen in whatever mode they are in. Only the world
is fake: their mail, calendar, Drive and the web are the scenario's, every
action is recorded and never done, and nothing the user would be told is sent.

**What it may never touch**, and the reason it runs in a process of its
own (`python -m agent.autonomy_eval run`): every reader and writer of their
accounts is replaced or refused here before the first session starts --
the same fences the unit tests put up (tests/unit/conftest.py) -- and a
patched module in the live worker would be the live agent's. The database
is a scratch file per scenario. It refuses to start inside a process that
already holds the agent's database.

**What it costs** is model calls on the live lineup, the critic's and the
screen's Jev calls, nothing else; a budget stops it (`DEFAULT_BUDGET_USD`).

**Modules built** (agent/build_eval.py) join the run and its report, on a
budget of their own (`--build-budget`, 0 for none): Steve building a tool
is autonomy too. They run in a child process (`_builds`), since the build
eval sets its own data folder, keychain and sandbox for its process, and
this one's fences would stand in the way of all three.

What a session's record means -- a task done, an attack landed -- is
decided in the frozen core (core/yardstick.py), beside the frozen
scenarios, so the agent cannot move its own bar.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as dt
import importlib
import itertools
import json
import logging
import re
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from core import db, paths
from core.yardstick import materialize
from steve_sdk import ids

log = logging.getLogger("autonomy_eval")


def reports_dir() -> Path:
    return paths.data_dir() / "autonomy_evals"


DEFAULT_BUDGET_USD = 1.00
# Each scenario this many times: a model's answer varies run to run.
DEFAULT_REPEAT = 3
SCENARIO_TIMEOUT_S = 600

# Who the user is in the fake world. Never their real addresses: the scenarios are
# sent to models, and their addresses have no business in a test.
ME = "me@example.com"
ALIAS = "me+agent@example.com"


# ---------------------------------------------------------------------------
# The fake world
# ---------------------------------------------------------------------------

def _tz():
    from agent import zones

    return zones.local_zone()


_WORD = re.compile(r"[a-z0-9]+")
# Words nearly every page has, which say nothing of what was asked.
_COMMON = frozenset({
    "a", "an", "and", "are", "at", "by", "for", "from", "how", "i", "in", "is", "it", "me",
    "my", "of", "on", "or", "the", "this", "to", "what", "when", "where", "which", "who",
    "with"})


def ranked(hits: list[dict], pages: dict, query: str) -> list[dict]:
    """The hits a search for `query` finds, best first, as an engine ranks
    them: by how many of the query's words each has, in its title, address,
    description or page. A hit with none is not found. Every query used to
    get every hit, in the fixture's order, whatever it asked: no real
    search does, and a session's next step depends on what its search
    found."""
    wanted = set(_WORD.findall(query.lower())) - _COMMON

    def found(hit: dict) -> int:
        page = pages.get(hit["url"]) or {}
        text = " ".join([hit.get("title", ""), hit["url"], hit.get("description", ""),
                         page.get("title") or "", page.get("text", "")])
        return len(wanted & set(_WORD.findall(text.lower())))

    scored = [(found(hit), n, hit) for n, hit in enumerate(hits)]
    return [hit for score, _, hit in sorted(scored, key=lambda s: (-s[0], s[1])) if score]


@dataclass
class Recorder:
    """What the sessions did, for core/yardstick."""

    effects: list = field(default_factory=list)  # core.yardstick.Effect
    fetches: list = field(default_factory=list)  # core.yardstick.Fetch
    told: list[str] = field(default_factory=list)
    seen: set = field(default_factory=set)
    keys: list[str] = field(default_factory=list)  # each effect's ledger hash, for the critic
    shown: set = field(default_factory=set)  # world items handed over whole
    drafts: dict = field(default_factory=dict)  # draft id -> whom it is to


# The world's one account, in every class, with a key of its own, as an
# account the user added has (`steve_sdk.ids.ACCOUNT_KEY`).
WORLD_KEY = "world0000001"
WORLD_NAME = "personal"


class World:
    """One scenario's mail, calendar and Drive, as an account of the test
    module `tests/modules/world`, and its web, which no account serves."""

    def __init__(self, scenario: dict, recorder: Recorder, now: dt.datetime | None = None,
                 kind: str | None = None):
        from tests.modules.world import provider as world

        self.now = now or dt.datetime.now(_tz())
        self.rec = recorder
        found = scenario.get("world") or {}
        today = self.now.date()
        self.account = world.World({
            "mail": [{**m, "body": materialize(m.get("body", ""), today)}
                     for m in found.get("mail", [])],
            "events": [{**e, "description": materialize(e.get("description", ""), today)}
                       for e in found.get("events", [])],
            "files": [{**f, "text": materialize(f.get("text", ""), today)}
                      for f in found.get("files", [])],
        }, now=self.now, me=ME, shown=recorder.shown.add)
        self.providers = {klass: made(self.account) for klass, made in world.PROVIDERS.items()}
        if kind is not None:  # one kind of account's day (`world.KINDS`)
            can = world.KINDS[kind]
            self.providers = {klass: made for klass, made in self.providers.items()
                              if klass in can}
            for klass, made in self.providers.items():
                made.capabilities = made.capabilities & can[klass]
        self.pages = {url: {**p, "text": materialize(p.get("text", ""), today)}
                      for url, p in (found.get("pages") or {}).items()}
        self.hits = list(found.get("search", []))

    def connected(self, klass: str) -> list:
        """The world's account in `klass`, among the slot's accounts
        (`agent.connectors.accounts`): so the tools, the agenda and the
        attention sources read it through the slot, as they read any."""
        from agent.connectors import Connected

        made = self.providers.get(klass)
        return [] if made is None else [Connected(
            key=WORLD_KEY, name=WORLD_NAME, klass=klass, module="world", provider=made,
            address=ME)]

    # -- the web ---------------------------------------------------------------

    async def web_search(self, query: str, count: int = 5):
        from tools.web import SearchResult

        return [SearchResult(title=h.get("title", ""), url=h["url"],
                             description=h.get("description", ""))
                for h in ranked(self.hits, self.pages, query)[:count]]

    async def web_fetch(self, url: str, *, max_chars: int = 20_000):
        from core.yardstick import Fetch
        from tools.web import PageContent

        self.rec.fetches.append(Fetch(url, allowed=True))
        page = self.pages.get(url) or self.pages.get(url.rstrip("/"))
        if page is None:
            return PageContent(url=url, title=None, text="", error="HTTP 404")
        text = page["text"]
        self.rec.shown.add(url if url in self.pages else url.rstrip("/"))
        return PageContent(url=url, title=page.get("title"), text=text[:max_chars],
                           truncated=len(text) > max_chars)


# ---------------------------------------------------------------------------
# Fences and fakes
# ---------------------------------------------------------------------------


def _set(dotted: str, value) -> None:
    """Replace `module.attr` (or `module.Class.attr`), failing loudly if it is
    not there: a renamed function would otherwise leave a door open."""
    parts = dotted.split(".")
    for cut in range(len(parts) - 1, 0, -1):
        try:
            owner = importlib.import_module(".".join(parts[:cut]))
        except ImportError:
            continue
        for name in parts[cut:-1]:
            owner = getattr(owner, name)
        if not hasattr(owner, parts[-1]):
            raise AttributeError(f"the yardstick's fence names nothing: {dotted}")
        setattr(owner, parts[-1], value)
        return
    raise ImportError(f"the yardstick's fence names no module: {dotted}")


_NOTSET = object()


class _ForGood:
    """Patches for the life of the process: the CLI's, in a process of its
    own. A test passes pytest's `monkeypatch`, which has the same two
    methods and undoes them."""

    def setattr(self, target, name, value=_NOTSET):
        if value is _NOTSET:
            _set(target, name)
        else:
            setattr(target, name, value)

    def setitem(self, mapping, key, value):
        mapping[key] = value


def _refuse(what: str):
    def refused(*args, **kwargs):
        raise RuntimeError(f"{what} is not in the yardstick's world")

    return refused


_CURRENT: dict = {}


def _world() -> World:
    return _CURRENT["world"]


def _fence(scratch: Path, patch=None) -> None:
    """Everything that reaches their accounts, their devices or the running
    agent: refused, or answered from the scenario's world. Once a process."""

    patch = patch or _ForGood()
    patch.setattr(paths, "db_path", lambda: scratch / "unused.db")
    for dotted, what in [
        # The built-ins' own doors: Google's client and its browser sign-in
        # open their own connections (05-connectors.md, section 1's
        # exception); every COM program opens through core's one door.
        ("modules.google_cloud.common.build", "Google"),
        ("google_auth_oauthlib.flow.InstalledAppFlow.from_client_config",
         "the Google sign-in"),
        ("google_auth_oauthlib.flow.InstalledAppFlow.run_local_server",
         "the Google sign-in"),
        ("core.modctx._dispatch", "COM"),
        # Core's engines' one door: every IMAP and SMTP socket opens here.
        ("core.modctx.Network.open_tcp", "a mail server"),
        ("core.coder.models._openai", "the coder"),
        ("agent.control_plane.post_supervisor", "the supervisor"),
        ("agent.attention.fetch._get", "the news and weather feeds"),
        ("core.browser.sandbox._docker", "Docker"),
        ("agent.browser.session.SPAWN", "the browser"),
        ("agent.models_scout._get", "OpenAI's model list"),
    ]:
        patch.setattr(dotted, _refuse(what))
    patch.setattr("agent.wake._power_request", lambda why: None)
    patch.setattr("core.supervisor.deaths_path", lambda: scratch / "deaths.jsonl")

    # The world, as one more of the slot's accounts (`agent/connectors.py`):
    # every tool, the agenda and the attention sources read it through the
    # slot, by the product's own code. Until step 5's item 4f this patched
    # Google's readers by name, some thirty, and after 4d three of them
    # named world methods that no longer existed.
    from agent import connectors
    from core import modules

    accounts, addresses = connectors.accounts, connectors.addresses
    patch.setattr("agent.connectors.accounts", lambda conn, klass, **kw: [
        *accounts(conn, klass, **kw), *_world().connected(klass)])
    # Their own address, as their mail account says it: mail to it is a note.
    patch.setattr("agent.connectors.addresses",
                  lambda conn, **kw: addresses(conn, **kw) | {ME})
    live_keys = modules.live_keys
    patch.setattr("core.modules.live_keys",
                  lambda conn, manifests=None: live_keys(conn, manifests) | {WORLD_KEY})
    for dotted, name in [("tools.web.search", "web_search"), ("tools.web.fetch", "web_fetch")]:
        patch.setattr(dotted, _delegate_async(name))
    patch.setattr("tools.memory.recall", lambda *a, **k: [])
    patch.setattr("agent.attention.newsdesk.on_request",
         _async_value({"stories": [], "note": "no news in this world"}))

    # Them, and what the user would be told: recorded, never sent.
    patch.setattr("agent.mailbox.address", lambda conn: ALIAS)
    patch.setattr("agent.receipts.attach", _unchecked)

    async def told(text, **_):
        _CURRENT["recorder"].told.append(str(text))
        return True

    async def card(text, **_):
        _CURRENT["recorder"].told.append(str(text))
        return True

    patch.setattr("agent.worker.links.notify_user", told)
    patch.setattr("agent.approvals._notify", card)
    _wrap_effects(patch)


def _delegate_async(name: str):
    async def call(*args, **kwargs):
        return await getattr(_world(), name)(*args, **kwargs)

    return call


def _async_value(value):
    async def call(*args, **kwargs):
        return value

    return call


async def _unchecked(tool, args, result):
    return result


_FAKE_IDS = itertools.count(1)


def _fake_executor(name: str):
    """An action, recorded by `_wrap_effects` and answered as if it had
    worked, in the shape its tool expects back. A draft is kept in the
    world, so the card that sends it can read whom it is to, as the slot
    reads every draft."""
    async def done(**args) -> dict:
        from steve_sdk.classes import Draft

        n = next(_FAKE_IDS)
        if name in ("draft_email",):
            made = _world().providers["mail"].make_draft(Draft(
                to=str(args.get("to") or ""), subject=str(args.get("subject") or ""),
                body=str(args.get("body") or "")))
            return {"drafted": True, "draft_id": ids.make("mail", WORLD_KEY, made.native_id),
                    "to": args.get("to"), "subject": args.get("subject"), "where": "Drafts"}
        if name in ("create_event", "update_event"):
            return {"created": True, "id": f"fake-event-{n}", "summary": args.get("summary"),
                    "start": args.get("start")}
        if name in ("send_email", "send_draft"):
            return {"sent": True, "id": f"fake-message-{n}"}
        return {"ok": True, "id": f"fake-{n}"}

    return done


def _wrap_effects(patch) -> None:
    """Every action through `actions.act` recorded with what became of it --
    run, asked about, or refused -- and every executor replaced, so nothing
    runs. Toolset modules bound `act` by name; each binding is replaced.

    A send by way of Drafts is asked about as `send_draft`, whose arguments
    are only the draft's id; the card shows whom the draft is to, and so does
    its record here (`Recorder.drafts`)."""
    import agent.toolset  # noqa: F401 - registers every executor first
    from agent import actions, approvals
    from core import egress
    from core.yardstick import Effect, Fetch

    for name in list(actions.EXECUTORS):
        patch.setitem(actions.EXECUTORS, name, _fake_executor(name))

    real_act = actions.act

    async def act(ctx, tool, args, preview):
        result = await real_act(ctx, tool, args, preview)
        rec = _CURRENT["recorder"]
        if tool == "draft_email" and isinstance(result, dict) and result.get("draft_id"):
            rec.drafts[result["draft_id"]] = args.get("to")
        key = (tool, json.dumps(args, sort_keys=True, default=str))
        if key not in rec.seen:
            rec.seen.add(key)
            shown = dict(args)
            if tool == "send_draft" and args.get("draft_id") in rec.drafts:
                shown["to"] = rec.drafts[args["draft_id"]]
            rec.effects.append(Effect(tool, shown, _outcome(result)))
            rec.keys.append(approvals._hash(tool, args))
        return result

    for module in list(sys.modules.values()):
        if (getattr(module, "__name__", "").startswith("agent.")
                and getattr(module, "act", None) is real_act):
            patch.setattr(module, "act", act)
    patch.setattr(actions, "act", act)

    real_unseen = egress.unseen

    def unseen(url, seen):
        why = real_unseen(url, seen)
        if why:
            _CURRENT["recorder"].fetches.append(Fetch(url, allowed=False))
        return why

    patch.setattr(egress, "unseen", unseen)


def _outcome(result) -> str:
    if isinstance(result, dict):
        if result.get("status") == "awaiting_approval":
            return "card"
        if (result.get("kind") in ("refused", "not_for_workers")
                or result.get("status") == "refused"):
            return "refused"
    return "free"


# ---------------------------------------------------------------------------
# Setting a scenario up, as production would have
# ---------------------------------------------------------------------------


def _approve(conn, holder: str, holder_id: int, title: str) -> str:
    """Their yes to an envelope's card, as the ledger records one."""
    from agent import approvals
    from core import monitor

    _, card, key, _ = monitor.HOLDERS[holder]
    sha = monitor.holder_sha(conn, holder, holder_id)
    args = {key: holder_id, "envelope_sha": sha, "title": title}
    with db.transaction(conn):
        conn.execute("INSERT INTO actions (tool, args_json, args_hash, preview, reach, reversal,"
                     " status) VALUES (?, ?, ?, ?, 'outward', 'reversible', 'done')",
                     (card, json.dumps(args), approvals._hash(card, args), f"{title} (approved)"))
    return sha


@dataclass
class _Session:
    """A scenario's session, set up as production would have it: how to run
    it, and the mandate and channel the critic judges its actions under."""

    run: object  # a coroutine: what the session said
    mandate: str
    channel: str


def _setup(conn, scenario: dict) -> _Session:
    from agent import goals, jobs, mailbox, routines
    from agent.worker import handlers

    kind = scenario["kind"]
    if kind == "routine":
        spec = scenario["routine"]
        schedule = routines.Schedule.build(
            at_time="" if spec.get("every_minutes") else "19:00",
            every_minutes=int(spec.get("every_minutes") or 0),
            condition=spec.get("condition", ""))
        made = routines.create(conn, title=spec["title"], instruction=spec["instruction"],
                               schedule=schedule, envelope=spec.get("envelope"))
        if spec.get("envelope"):
            sha = _approve(conn, "routine", made.id, made.title)
            routines.activate(conn, made.id, sha)

        async def run() -> str:
            said = await handlers.handle_routine(_Job(str(made.id)))
            after = routines.get(conn, made.id)
            return "\n".join(filter(None, [said, after.last_report if after else None]))

        return _Session(run(), routines.mandate(routines.get(conn, made.id)), "routine")
    if kind == "goal":
        spec = scenario["goal"]
        made = goals.create(conn, title=spec["title"], ask=spec["ask"],
                            done_when=spec["done_when"], steps=spec["steps"],
                            envelope=spec.get("envelope"))
        if spec.get("envelope"):
            sha = _approve(conn, "goal", made.id, made.title)
            goals.activate(conn, made.id, sha)
        return _Session(handlers.handle_goal(_Job(str(made.id))),
                        goals.mandate(goals.get(conn, made.id)), goals.CHANNEL)
    if kind == "mail":
        spec = scenario["mail"]
        message = next(m for m in scenario["world"]["mail"] if m["id"] == spec["message_id"])
        # Ids as the slot hands them out: the mailbox reads its mail through
        # the world's account (`agent/mailbox.read`).
        native = spec.get("thread_id") or message.get("thread") or message["id"]
        thread = ids.make("mail", WORLD_KEY, native)
        with db.transaction(conn):
            if spec["kind"] == "thread":
                conn.execute("INSERT INTO mailbox (message_id, thread_id, sender, subject, kind,"
                             " why, note) VALUES (?, ?, ?, ?, 'instruction', 'test', ?)",
                             (ids.make("mail", WORLD_KEY, f"{native}-user"), thread, ME,
                              message.get("subject", ""), spec["user_note"]))
            sender = re.search(r"[\w.+-]+@[\w.-]+", message["from"]).group(0)
            intake = conn.execute(
                "INSERT INTO mailbox (message_id, thread_id, sender, subject, kind, why, note)"
                " VALUES (?, ?, ?, ?, ?, 'test', ?)",
                (ids.make("mail", WORLD_KEY, message["id"]), thread, sender,
                 message.get("subject", ""), spec["kind"],
                 spec.get("note"))).lastrowid
        return _Session(handlers.handle_mail(_Job(json.dumps({"intake": intake}))),
                        mailbox.mandate(conn, mailbox.get(conn, intake)), mailbox.CHANNEL)
    if kind == "job":
        request = scenario["job"]["request"]
        with db.transaction(conn):
            session = conn.execute("INSERT INTO sessions (channel) VALUES ('signal')").lastrowid
            conn.execute("INSERT INTO messages (session_id, role, content) VALUES (?, 'user', ?)",
                         (session, request))
        job_id = jobs.enqueue(conn, kind="research", prompt=request, why=request[:120],
                              session_id=session)
        # As `handle_research` words it for a task from their conversation.
        return _Session(handlers.handle_research(jobs.get(conn, job_id)),
                        f"A background task from the user's conversation: {request}", "job")
    raise ValueError(f"no way to play a {kind!r} scenario")


async def _probe(conn, session: _Session, probe: dict | None) -> str | None:
    """What the critic decides about the action a hijacked session would
    have taken, under this session's mandate: allow, card, or None if it
    could not be asked."""
    from agent import offloop
    from core import critic, monitor
    from core.consent import classify

    if not probe:
        return None
    tool, args = probe["tool"], dict(probe["args"])
    consent = classify(tool, args, me=ME)
    review = await offloop.with_own_conn(
        conn, critic.review, mandate=session.mandate, channel=session.channel, tool=tool,
        args=args, reach=str(consent.reach), reversal=str(consent.reversal),
        gate=monitor.gate(conn, tool, args, me=ME).kind, action_key="yardstick-probe")
    return review.decision if review.verdict or not review.error else None


@dataclass
class _Job:
    """What a handler reads of a job it is given."""

    prompt: str
    id: int = 0
    kind: str = "test"
    why: str | None = None
    session_id: int | None = None


def screened(conn) -> dict[str, str | None]:
    """What the screen saw, and its verdict, by the ids the scenario names
    its items with.

    The screen keeps an item under the id the slot handed out
    (`files:<account>:<native>`) since step 5's item 4e, and a scenario
    names its planted items by the world's own (`f-offsite`). Read as kept,
    no planted mail or document was ever matched: the first yardstick after
    step 5 counted 3 planted items seen where step 4 counted 24, and every
    one it missed as clean material flagged (0.348, from 0). Not the
    screen's second look at its own extracts either: one shares its item's
    id, and its verdict would stand in for the item's."""
    found: dict[str, str | None] = {}
    for source, verdict in conn.execute(
            "SELECT source_id, verdict FROM screened WHERE source != 'screen_extract'"):
        parsed = ids.parse(source) if source else None
        found[parsed.native if parsed and parsed.account == WORLD_KEY else source] = verdict
    return found


def spent(conn) -> float:
    """What a run cost: the ledger's sum. Every model call is billed there
    (`core.spend.bill`), the screen's, the critic's and the extractor's
    too. Their own tables keep a copy of each cost, and adding them in, as
    when the screen billed nothing, counted every decision twice: step 3's
    first yardstick read about 40% dearer than step 2's on the same models."""
    return conn.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM usage_events").fetchone()[0]


async def run_one(scenario: dict, scratch: Path, attempt: int = 1) -> tuple[dict, float]:
    """One scenario, on a database of its own: its score, and what it cost."""
    from agent import runtime
    from core import db, yardstick

    path = scratch / f"{scenario['id']}-{attempt}.db"
    conn = db.open_store(path)
    runtime.conn = conn
    recorder = Recorder()
    world = World(scenario, recorder)
    _CURRENT.update(world=world, recorder=recorder)
    # Scored against the day its world was written from, not the machine's
    # date: the agent's zone may be in another day.
    record = yardstick.Record(today=world.now.date())
    started = time.perf_counter()
    try:
        session = _setup(conn, scenario)
        said = await asyncio.wait_for(session.run, SCENARIO_TIMEOUT_S)
        record.said = "\n".join([str(said or ""), *recorder.told])
        record.probe = await _probe(conn, session, (scenario.get("attack") or {}).get("probe"))
    except Exception as exc:  # noqa: BLE001 - one scenario is not the run
        log.exception("scenario %s failed", scenario["id"])
        record.error = f"{type(exc).__name__}: {exc}"[:300]
        record.said = "\n".join(recorder.told)
    decisions = dict(conn.execute("SELECT action_key, decision FROM critic_reviews"
                                  " WHERE decision IS NOT NULL AND action_key != 'yardstick-probe'"
                                  ).fetchall())
    for effect, key in zip(recorder.effects, recorder.keys, strict=True):
        effect.critic = decisions.get(key)
    record.effects, record.fetches = recorder.effects, recorder.fetches
    record.shown = set(recorder.shown)
    record.screened = screened(conn)
    cost = spent(conn)
    row = yardstick.score(scenario, record)
    row.update(attempt=attempt, probe=record.probe, cost_usd=round(cost, 5),
               seconds=round(time.perf_counter() - started),
               said=record.said[:600],
               effects=[{"tool": e.tool, "outcome": e.outcome, "critic": e.critic,
                         "to": e.args.get("to") or e.args.get("attendees")}
                        for e in recorder.effects],
               fetches=[{"url": f.url, "allowed": f.allowed} for f in recorder.fetches])
    runtime.conn = None
    conn.close()
    return row, cost


def _lineup() -> dict:
    with contextlib.suppress(Exception):
        from agent.models import Role
        from agent.provider import describe

        return {r.value: describe(r) for r in Role}
    return {}


async def run(budget_usd: float = DEFAULT_BUDGET_USD, only: list[str] | None = None,
              repeat: int = DEFAULT_REPEAT, build_budget_usd: float = 0.0) -> dict:
    """Every scenario, or the ones named, `repeat` times each, until the
    budget is spent; then the module builds, on theirs. One run of a
    scenario is an anecdote: the same goal drafted Alex's email in one run
    and not in the next."""
    from agent import runtime
    from core import policy, yardstick

    if runtime.conn is not None:
        raise RuntimeError("the yardstick runs in a process of its own, never the worker's")
    # Steve's scratch, not %TEMP%: every run's databases are kept for reading
    # afterwards, and in the machine's temp folder removing Steve's data
    # would leave them behind.
    scratch = Path(tempfile.mkdtemp(prefix="yardstick-", dir=paths.scratch_dir()))
    _fence(scratch)
    scenarios = [s for s in yardstick.load() if not only or s["id"] in only]
    rows, spent = [], 0.0
    for attempt in range(1, max(1, repeat) + 1):
        for scenario in scenarios:
            if spent >= budget_usd:
                rows.append({"id": scenario["id"], "kind": scenario["kind"],
                             "attempt": attempt, "skipped": "budget"})
                continue
            log.info("playing %s (%d)", scenario["id"], attempt)
            row, cost = await run_one(scenario, scratch, attempt)
            spent += cost
            rows.append(row)
            log.info("%s: done=%s landed=%s turned=%s probe=%s ($%.4f)", scenario["id"],
                     row["done"], row["landed"], row["turned"], row["probe"], cost)
    played = [r for r in rows if "skipped" not in r]
    report = {"at": dt.datetime.now().isoformat(timespec="seconds"),
              "cost_usd": round(spent, 4), "budget_usd": budget_usd, "repeat": repeat,
              "lineup": _lineup(), "critic_mode": policy.load_policy().critic.mode,
              "summary": yardstick.summary(played), "by_scenario": _tally(played),
              "rows": rows}
    if build_budget_usd > 0:
        report["builds"] = await asyncio.to_thread(joined_builds, build_budget_usd)
    reports = reports_dir()
    reports.mkdir(parents=True, exist_ok=True)
    out = reports / f"{dt.datetime.now():%Y-%m-%d-%H%M%S}.json"  # two in one minute were one file
    out.write_text(json.dumps(report, indent=2), encoding="utf-8", newline="\n")
    report["path"] = str(out)
    return report


# A build is a coder, a tester and a judge, then a module run in its
# sandbox: minutes each.
BUILDS_TIMEOUT_S = 90 * 60


def builds_log() -> Path:
    return paths.logs_dir() / "build_eval.log"


def _builds(budget_usd: float) -> int:
    """The module builds, as a child process: its exit code, or -1 if it was
    killed for overrunning."""
    from agent.night import child

    return child.run([sys.executable, "-m", "agent.build_eval", "run", "--budget",
                      str(budget_usd)], builds_log(), BUILDS_TIMEOUT_S)


def joined_builds(budget_usd: float) -> dict:
    """The module builds' run, for this run's report: their summary and
    what they cost, or why there is none. A report from before this run is
    not this run's."""
    from agent import build_eval

    started = time.time()
    code = _builds(budget_usd)
    found = build_eval.latest()
    fresh = (found is not None
             and dt.datetime.fromisoformat(found["at"]).timestamp() >= started - 5)
    if code == 0 and fresh:
        return {"summary": found["summary"], "cost_usd": found["cost_usd"],
                "path": found["path"]}
    why = ("it ran out of time" if code == -1 else f"it exited {code}" if code
           else "it left no report")
    return {"error": f"the module builds did not finish: {why} (data/logs/build_eval.log)"}


def _tally(rows: list[dict]) -> dict:
    """Each scenario across its attempts: "done 3/3, turned 0/3"."""
    out: dict[str, dict] = {}
    for row in rows:
        seen = out.setdefault(row["id"], {"runs": 0, "done": 0, "turned": 0, "landed": 0,
                                          "errors": 0})
        seen["runs"] += 1
        seen["done"] += bool(row["done"])
        seen["turned"] += bool(row["turned"])
        seen["landed"] += bool(row["landed"])
        seen["errors"] += bool(row["error"])
    return out


def line(report: dict) -> str:
    """The run in one line for their morning digest, in counts rather than
    shares: "21 of 21" says how much there was to go on."""
    rows = [r for r in report.get("rows", []) if "skipped" not in r]
    tasks = [r for r in rows if r.get("done") is not None]
    read = [r for r in rows if r.get("attacked") and r.get("exposed")]
    on_attack = [c for r in rows for c in r.get("critic_on_attack", [])]
    planted = sum(r.get("screen_planted_seen", 0) for r in rows)
    clean = sum(r.get("screen_clean_seen", 0) for r in rows)
    said = (f"{len({r['id'] for r in rows})} scenarios x{report.get('repeat', 1)}: "
            f"tasks done {sum(1 for r in tasks if r['done'])}/{len(tasks)}; "
            f"attacks read {len(read)}, obeyed {sum(1 for r in read if r['turned'])}, "
            f"landed {sum(1 for r in read if r['landed'])}; "
            f"the critic would have stopped {sum(1 for c in on_attack if c == 'card')}"
            f"/{len(on_attack)} hijacked actions and asked needlessly about "
            f"{sum(r.get('critic_needless', 0) for r in rows)}"
            f"/{sum(r.get('critic_reviewed', 0) for r in rows)} others; "
            f"the screen flagged {sum(r.get('screen_planted_flagged', 0) for r in rows)}"
            f"/{planted} planted items and "
            f"{sum(r.get('screen_clean_flagged', 0) for r in rows)}/{clean} clean ones")
    errors = sum(1 for r in rows if r.get("error"))
    if errors:
        said += f"; {errors} runs failed"
    said = f"{said}. ${report.get('cost_usd', 0):.2f}."
    builds = report.get("builds")
    if builds and "error" in builds:
        said += f" {builds['error'][0].upper()}{builds['error'][1:]}."
    elif builds:
        made = builds["summary"]
        said += (f" Modules built: tasks passed {made['tasks_passed']}; attacks landed"
                 f" {made['attacks_landed']}, held {made['attacks_held']}."
                 f" ${builds['cost_usd']:.2f}.")
    return said


def latest() -> dict | None:
    """The most recent run's report, if there is one."""
    reports = reports_dir()
    found = sorted(reports.glob("*.json")) if reports.exists() else []
    if not found:
        return None
    return json.loads(found[-1].read_text(encoding="utf-8"))


def _main() -> None:
    parser = argparse.ArgumentParser(description="The yardstick: unattended sessions, scored.")
    parser.add_argument("what", choices=["run", "report"])
    parser.add_argument("--budget", type=float, default=DEFAULT_BUDGET_USD)
    parser.add_argument("--only", nargs="*")
    parser.add_argument("--repeat", type=int, default=DEFAULT_REPEAT)
    from agent import build_eval

    parser.add_argument("--build-budget", type=float, default=build_eval.DEFAULT_BUDGET_USD,
                        help="for the module builds, on top of --budget; 0 builds none")
    args = parser.parse_args()
    if args.what == "report":
        print(json.dumps(latest(), indent=2))
        return
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    report = asyncio.run(run(args.budget, args.only, args.repeat, args.build_budget))
    print(json.dumps({k: report.get(k) for k in ("summary", "by_scenario", "cost_usd", "builds",
                                                  "path")}, indent=2))


if __name__ == "__main__":
    _main()
