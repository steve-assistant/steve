"""What came from an account that is off is read only where reading it is right.

Turning an account or its module off takes what came from it out of what
Steve says and does (05-connectors.md, "Off means original"): its queued
observations, its commitments, the people only it knew, its cards. The rows
stay, so turning it back on loses nothing; `core/modules.live` is the
condition that skips them, and every read that puts a tagged table's rows in
front of the user, a model or an action goes through it.

A read that does not is named here with why: a row fetched by the id in
hand, a count, a check that a key was seen, the history the user keeps.
Found by reading the code, so a new read is caught when it is written, not
when an account is turned off and something it knew still speaks.
"""

from __future__ import annotations

import ast
import re
from functools import cache

from core.modules import TAGGED
from tests.gate import repo_files

ROOTS = ("agent", "core", "tools")

_READ = re.compile(r"\b(?:FROM|JOIN)\s+(" + "|".join(TAGGED) + r")\b")

# Each read of a tagged table outside `live`, by file and function, and why.
ALLOWED: dict[str, str] = {
    # By the id in hand: the caller holds the row already, from a live read
    # or the user's own reply; a card is withdrawn by its status.
    "agent/actions.py:_made_here": "whether this session's own actions made the item in hand",
    "agent/approvals.py:get": "a card by its id",
    "agent/approvals.py:_ask_again": "a card by its id",
    "agent/approvals.py:moved_on_since": "a card by its id",
    "agent/approvals.py:queue": "the same card asked twice, by its arguments",
    "agent/attention/store.py:get": "observations by the ids in hand",
    "agent/attention/prepare.py:people_block": "a person by the address of a live item",
    "agent/attention/sources/threads.py:_person": "a person by the address of a live item",
    "agent/attention/vet.py:sender_of": "a person by the address of a live item",
    "agent/night/learning.py:_extract_commitments": "a person by the address of a live item",
    "agent/facts.py:subject_of": "a person by id",
    "agent/goals.py:_action_lines": "a goal's own cards, by id",
    "agent/goals.py:after_session": "a session's own cards",
    "agent/handles.py:_handle": "the handle already given for an id in hand",
    "agent/mailbox.py:get": "a mailbox row by id",
    "agent/mailbox.py:user_thread": "the user's own instructions, by thread",
    "agent/mailbox.py:agent_sent": "whether Steve sent this message itself",
    "agent/mailbox.py:instructed": "whether a Message-ID gave an instruction already, which "
                                   "it gave whether its account is on now or not",
    "tools/commitments.py:get": "a commitment by id",
    "tools/obligations.py:get": "an obligation by id",
    "tools/obligations.py:by_ref": "an obligation by what it came from",
    "tools/obligations.py:by_action": "an obligation by its card",
    "tools/obligations.py:add": "the same commitment kept twice",
    "tools/people.py:get": "a person by id",
    "tools/people.py:upsert": "a person by address, to write",
    "tools/people.py:recount": "the ledger's own counts, written back",
    # Seen before: a key or a cursor by its own name. Skipping one would
    # fetch, and say, it again.
    "agent/attention/store.py:known": "keys already recorded",
    "agent/attention/store.py:_restale_briefs": "the same event as a live one in hand",
    "agent/attention/newsdesk.py:_unused": "newsletters already said",
    "agent/attention/briefing.py:sent_on": "a cursor by its own name",
    "agent/attention/sources/__init__.py:due": "a cursor by its own name",
    "agent/attention/owed.py:closed_refs": "what is closed, so it is not raised",
    "agent/mailbox.py:poll": "messages already taken in",
    "tools/people.py:cursor": "a cursor by its own name",
    # Counts and limits: how many, never what.
    "agent/approvals.py:spent_since": "money spent, which was spent",
    "agent/closing.py:_asks": "how often a card was asked",
    "agent/closing.py:_asked_today": "how often a card was asked",
    "agent/attention/store.py:tasks_started_today": "a count against the day's cap",
    "agent/attention/store.py:counts_today": "a count for the day's report",
    "agent/mailbox.py:started_today": "a count against the day's cap",
    "tools/commitments.py:counts": "a count",
    "tools/people.py:counts": "a count",
    # History stays: what Steve did and told the user.
    "agent/approvals.py:holding": "a card already approved runs, or the slot says it is off",
    "agent/approvals.py:withdrawn": "withdrawn cards, to say why",
    "agent/approvals.py:recent": "the cards' history",
    "agent/closing.py:_cards_withdrawn": "withdrawn cards, to say why",
    "agent/attention/told.py:explain": "why the user was told something",
    "agent/attention/sources/__init__.py:last_runs": "when each source last ran",
    "agent/mailbox.py:recent": "the control panel's record of what came in",
    "agent/worker/control.py:activity": "the control panel's record of what Steve did",
    "agent/worker/control.py:_mailbox_today": "the control panel's record of what came in",
    "agent/worker/journal.py:_actions": "the journal's record of what Steve asked and did",
    "agent/worker/journal.py:_pages_with": "the journal's record, searched: its pages, as shown",
    # Keeping people out of what leaves: everyone known, from an account
    # that is off as much as from one that is on.
    "agent/modshare.py:terms": "who a shared module must not name, from every account",
    "agent/cardwords.py:waiting": "the words no other message may say, from every account",
    # Tools for the developer, over a copy or a test's data.
    "agent/attention/__main__.py:_run": "a developer's replay",
    "agent/attention/replay.py:replay": "a developer's replay of every verdict",
    "agent/persona_eval.py:_observations": "an evaluation's own data",
    # Core's own: the switch, and the monitor's checks of a card.
    "core/modules.py:withdraw_cards": "the switch itself",
    "core/modules.py:waiting": "what the switch or removal would withdraw, said before either",
    "core/modules.py:_clear": "removal, of the removed account's own rows",
    "core/modules.py:<module>": "the switch's own condition",
    "core/monitor.py:may_ask": "the monitor's check of a card",
    "core/monitor.py:may_run": "the monitor's check of a card",
    "core/monitor.py:approved_envelope": "the monitor's check of a card",
    "core/monitor.py:_bind": "the monitor's check of a card",
    "core/monitor.py:_proved": "the monitor's check of a card",
    "core/monitor.py:hello_binding": "the monitor's check of a card",
}


def _reads_tagged(node: ast.AST) -> bool:
    """Whether `node` is SQL text that reads a tagged table."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        text = node.value
    elif isinstance(node, ast.JoinedStr):
        text = "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in node.values)
    else:
        return False
    return "SELECT" in text and bool(_READ.search(text))


def _read_file(rel: str, found: dict[str, bool]) -> None:
    source = repo_files.text(rel) or ""
    tree = repo_files.parsed(rel)
    functions = [n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)]
    # SQL held in a module's constant, alone or as a tuple of statements, is
    # read by each function that uses it. A tuple's statements were read as
    # the module's own, and named no function to allow.
    def statements(value: ast.AST) -> list[ast.AST]:
        return list(value.elts) if isinstance(value, ast.Tuple | ast.List) else [value]

    held = [n for n in tree.body if isinstance(n, ast.Assign)
            and any(_reads_tagged(v) for v in statements(n.value))]
    constants = {t.id for n in held for t in n.targets if isinstance(t, ast.Name)}
    values = {id(v) for n in held for v in statements(n.value)}

    for node in ast.walk(tree):
        if id(node) in values:
            continue
        if not (_reads_tagged(node) or isinstance(node, ast.Name) and node.id in constants):
            continue
        around = [f for f in functions if f.lineno <= node.lineno <= f.end_lineno]
        function = max(around, key=lambda f: f.lineno) if around else None
        if function is None and isinstance(node, ast.Name):
            continue
        body = ast.get_source_segment(source, function) or "" if function else ""
        key = f"{rel}:{function.name if function else '<module>'}"
        found[key] = found.get(key, False) or "modules.live(" in body or "_live(conn" in body


@cache
def _reads() -> dict[str, bool]:
    """Every function that reads a tagged table, as "path:function", and
    whether it reads through `live` (or a module's `_live`, which does)."""
    found: dict[str, bool] = {}
    for root in ROOTS:
        for rel in repo_files.python(f"{root}/"):
            _read_file(rel, found)
    return found


def test_every_read_of_a_tagged_table_skips_what_is_off_or_says_why_not():
    reads = _reads()

    assert reads, "no read of a tagged table found; this test cannot see them"
    unexplained = sorted(k for k, live in reads.items() if not live and k not in ALLOWED)
    assert not unexplained, (
        f"{unexplained} read a tagged table without `core/modules.live`: read it "
        "through `live`, so an account that is off is not heard from, or say "
        "here why this read is right")


def test_every_read_named_here_still_reads_a_tagged_table():
    """A name left behind would let a new read of the same name through."""
    reads = _reads()

    assert not sorted(set(ALLOWED) - set(reads))
    assert not sorted(k for k in ALLOWED if reads.get(k)), \
        "reads through `live` now; take it off the list"
