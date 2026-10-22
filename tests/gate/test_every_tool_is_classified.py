"""No tool reaches the model without somebody having decided what it can do.

This is the keystone of the consent design, and it belongs in the gate rather
than in unit tests for the same reason the rest of `tests/gate/` does: the
agent must not be able to grade its own powers.

The hole it closes is bigger than any single tool. Until step 6 a
self-modification run could write `tools/whatever.py` at night, pass the
gate, and merge unattended. Gating `send_email` and calling it done would
have been decorative -- the next capability arrives through a door nobody
is watching. That door is gone, and a capability arrives now as a module's
tool, whose class its manifest states and core checks against its grants
(`core/lifecycle.needs_card`): the built-ins' manifests are read here too.

So the check is not "is send_email gated". It is: **enumerate everything the
model can actually call, and fail if any of it is unclassified.** An
unclassified tool is treated as gated at runtime, which is the safe direction,
but a silent fail-closed is still a thing nobody decided. This makes the
decision explicit and makes a nightly upgrade that adds an ungated capability
die in its worktree instead of landing.

Read by AST rather than by import: the same instinct as deriving a changeset
from git rather than from the model's own account of it. A list maintained by
hand drifts; a list derived from the source cannot.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from core.consent import Reach, Reversal, classify, load_rules
from tests import persona
from tests.gate import repo_files

# One module per domain, each with a `register(tool)` holding its `@tool`
# functions -- see agent/toolset/__init__.py. They were all nested in one
# `_register_tools` in agent/loop.py until that file was split up.
TOOLSET = Path("agent/toolset")
REPO = Path(__file__).resolve().parents[2]


def _every_manifest() -> dict[str, object]:
    """Every module manifest in the tree, by its path: the built-ins', the
    SDK's templates' and those of the modules under tests/, new files among
    them. Found by git rather than from a list of folders, so a manifest put
    anywhere is read; the built-ins alone held one module's tools."""
    from steve_sdk.manifest import load

    return {rel: load(REPO / rel) for rel in repo_files.listed()
            if rel.endswith("manifest.toml")}


def _module_tools() -> dict[str, tuple]:
    """The built-in modules' tools, by the name the model is offered, with
    their manifests.

    Counted as registered because they are. They arrive through a module's
    manifest rather than a literal `@tool` in the source, so the AST walk
    below cannot see them -- and a walk that cannot see them would let this
    file pass vacuously for exactly the tools nobody wrote into the toolset.
    """
    from agent import modtools
    from core import modules

    return {modtools.name_of(manifest.id, tool.name): (manifest, tool)
            for manifest in modules.builtins().values() for tool in manifest.tools}


def _hand_written_tools() -> list[str]:
    """Every function decorated `@tool` inside a toolset `register`."""
    return sorted(_tool_nodes())


def _registered_tools() -> list[str]:
    """Everything the model can call, however it got there."""
    return sorted(set(_hand_written_tools()) | set(_module_tools()))


def _tool_nodes() -> dict[str, ast.AsyncFunctionDef | ast.FunctionDef]:
    """Every `@tool` function in the toolset, by name, keeping the bodies.

    Two questions get asked of these -- is every tool classified, and does
    every gated one actually go through the gate -- and the second needs to
    look inside the function rather than at its name.

    The decorator is the local `tool` filter each `register` is handed, not
    `@agent.tool`, because registration is filtered by the `allow` set.
    """
    files = sorted(TOOLSET.glob("*.py"))
    assert files, f"no toolset modules under {TOOLSET}: it has moved"

    found: dict[str, ast.AsyncFunctionDef | ast.FunctionDef] = {}
    registers = 0
    for path in files:
        tree = repo_files.parsed(path.as_posix())
        for register in ast.walk(tree):
            if not (isinstance(register, ast.FunctionDef) and register.name == "register"):
                continue
            registers += 1
            for node in ast.walk(register):
                if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
                    continue
                for decorator in node.decorator_list:
                    plain = isinstance(decorator, ast.Name) and decorator.id == "tool"
                    attribute = (
                        isinstance(decorator, ast.Attribute) and decorator.attr == "tool"
                    )
                    if plain or attribute:
                        found[node.name] = node
                        break
    assert registers, "no toolset module has a `register(tool)`: the layout has changed"
    return found


def _calls_the_gate(node: ast.AST) -> bool:
    return any(
        isinstance(call.func, ast.Name) and call.func.id == "act"
        for call in ast.walk(node)
        if isinstance(call, ast.Call)
    )


def _can_be_gated(name: str, rules) -> bool:
    """Either gated outright, or gated for some arguments."""
    if name in rules.gated:
        return True
    spec = rules.free.get(name) or {}
    return bool(spec.get("gate_when"))


@pytest.fixture(scope="module")
def registered() -> list[str]:
    return _registered_tools()


@pytest.fixture(scope="module")
def rules():
    return load_rules()


# --------------------------------------------------------------------------
# The check itself
# --------------------------------------------------------------------------


def test_the_toolset_is_found_at_all(registered):
    """If the AST walk silently found nothing, every other test here passes
    vacuously and the gate is decorative."""
    assert len(registered) > 10, f"only found {registered}"
    assert "send_email" in registered


def test_every_registered_tool_is_classified(registered, rules):
    """The one that matters. A tool the model can call and nobody classified
    is a capability nobody thought about.

    A module's tools are exempt from `core/consent.yaml`, and only those:
    each is classified by its manifest, checked against its grants below,
    and a call that needs asking is `module.tool`, which is classified here.
    """
    exempt = set(_module_tools())
    unclassified = [
        name for name in registered if name not in rules.known and name not in exempt
    ]
    assert not unclassified, (
        "these tools are callable by the model and absent from "
        "core/consent.yaml:\n  "
        + "\n  ".join(unclassified)
        + "\n\nClassify them. They are gated at runtime either way, but a "
        "silent fail-closed is still nobody having decided."
    )


def test_every_hand_written_tool_says_once_whether_its_results_carry_outside_text():
    """Text someone else wrote marks the conversation that reads it, and
    from then on `read_page` opens only addresses it was shown
    (agent/toolset/web.py `unshown`). Each tool marked it by hand, and
    nothing made a new one decide: step 11's audit found Drive's file names
    and the people ledger's names and subjects reaching a watched chat
    unscreened and unmarked. Now each tool is declared, and its wrapper
    marks for it (`screening.read_through`).

    So a tool in neither set is one nobody decided about; in both, one
    decided two ways; and a name in a set that no tool has is a decision
    about nothing -- often a tool renamed, whose new name went undeclared.
    A module's tools are outside text by what they are, and need no entry.
    """
    from agent import screening

    tools = set(_hand_written_tools())
    undeclared = sorted(tools - screening.OUTSIDE - screening.OWN)
    both = sorted(screening.OUTSIDE & screening.OWN)
    unknown = sorted((screening.OUTSIDE | screening.OWN | screening.LEFT_TO_THE_TOOL) - tools)

    assert not undeclared, (
        f"no declaration of whether their results carry outside text: {undeclared}. Add "
        "each to agent/screening.py OUTSIDE (it may return words someone other than the "
        "user wrote) or OWN."
    )
    assert not both, f"declared both outside text and their own: {both}"
    assert not unknown, f"declared, but no tool has the name: {unknown}"
    assert screening.LEFT_TO_THE_TOOL <= screening.OUTSIDE, (
        "only outside text has a mark to leave to the tool")


def test_every_module_tool_is_classified_by_its_manifest_and_asks_where_its_grants_say(
        rules):
    """A module's tool says what it is, and core decides from the grants
    whether that can be believed: a read whose module can read the user's
    accounts, or change what its hosts hold, asks first, every call. The
    rule is written out again here rather than called, so a change to
    `needs_card` that frees such a read fails this file.

    Every manifest in the tree, not the built-ins' alone: a template is what
    a module's author copies, and a module under tests/ is what the suite
    runs, and the built-ins' walk found one tool module's tools."""
    from agent import modtools
    from core import lifecycle
    from steve_sdk.manifest import READ_ONLY, TOOL_CLASSES

    manifests = _every_manifest()
    assert {"modules/countdown/manifest.toml", "steve_sdk/templates/mail/manifest.toml",
            "tests/modules/world/manifest.toml"} <= set(manifests), "a manifest has moved"
    found = [(modtools.name_of(manifest.id, tool.name), manifest, tool)
             for manifest in manifests.values() for tool in manifest.tools]
    assert "mod__countdown__days_until" in {name for name, _, _ in found}, \
        "the built-in tool module has moved"
    for name, manifest, tool in found:
        assert tool.klass in TOOL_CLASSES, name
        changes = any(m not in READ_ONLY for said in manifest.methods.values() for m in said)
        want = bool(manifest.reads) or (tool.klass == "read" and changes)
        assert lifecycle.needs_card(manifest, tool) is want, name
    # The cards they ask with, and the actions they propose, are gated, and
    # nothing lets them go free.
    for gated in ("module.tool", "module.perform"):
        assert gated in rules.gated and gated not in rules.free
        said = classify(gated, {"module": "x", "args": {"to": persona.EMAIL}}, rules=rules,
                        me=frozenset({persona.EMAIL}))
        assert said.gated and said.reach is Reach.OUTWARD, gated
        assert said.reversal is Reversal.IRREVERSIBLE, gated


def test_a_module_tool_that_calls_itself_a_read_is_a_card_when_its_grants_say_otherwise():
    """The exit criterion (06-modules.md): a module whose grants let it read
    the user's mail, or POST to its host, cannot make its tool free by
    calling it a read. Only a read with neither runs without a card; an
    action's call can only look, and its effects are cards of their own."""
    from core import lifecycle
    from steve_sdk.manifest import parse

    def module(**more):
        return parse({"manifest_version": 1, "sdk": 1, "id": "probe", "version": "1",
                      "label": "Probe", "for": "Trying the rule", "tier": "sandbox",
                      "code": "probe.py",
                      "hosts": ["api.example.com"],
                      "tools": [{"name": "look", "class": "read", "description": "Looks."},
                                {"name": "change", "class": "action",
                                 "description": "Changes."}],
                      **more})

    plain = module()
    reading = module(reads=["mail"])
    posting = module(methods={"api.example.com": ["GET", "POST"]})
    look, change = plain.tools

    assert not lifecycle.needs_card(plain, look)
    assert lifecycle.needs_card(reading, look) and lifecycle.needs_card(posting, look)
    assert lifecycle.needs_card(reading, change)
    assert not lifecycle.needs_card(plain, change) and not lifecycle.needs_card(posting, change)


def test_an_unknown_tool_is_gated(rules):
    """The runtime backstop behind the gate test: even if something slips
    past review, it asks before acting."""
    verdict = classify("tools_some_new_thing", {}, rules=rules)
    assert verdict.gated is True
    assert verdict.reach is Reach.OUTWARD
    assert verdict.reversal is Reversal.IRREVERSIBLE


# --------------------------------------------------------------------------
# Things that must never quietly become free
# --------------------------------------------------------------------------


def test_sending_mail_to_another_person_is_never_free(rules):
    verdict = classify("send_email", {"to": "someone@example.com"}, rules=rules)
    assert verdict.gated is True


def test_an_invitation_with_guests_is_never_free(rules):
    """The irreversible part of a calendar event is the notification, not the
    row -- so the gate splits on attendees, not on the function name."""
    assert classify("create_event", {"attendees": ["a@b.example"]}, rules=rules).gated
    assert classify("update_event", {"attendees": ["a@b.example"]}, rules=rules).gated


def test_code_with_a_network_is_never_free(rules):
    """`run_code` is the only tool that can make arbitrary outbound requests.
    Ungated, the whole queue is decorative."""
    assert classify("run_code", {"network": True}, rules=rules).gated


def test_spending_money_is_never_free(rules):
    verdict = classify("purchase", {"amount_usd": 12.5}, rules=rules)
    assert verdict.gated is True
    assert verdict.spend_usd == pytest.approx(12.5)


# --------------------------------------------------------------------------
# Things that must stay free, or the gate becomes noise
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool",
    ["web_search", "read_page", "recall", "search_email", "read_email",
     "calendar_agenda", "search_drive", "read_drive_file"],
)
def test_reading_never_asks(tool, rules):
    """"Reading is always fine" is already the stated rule. A gate that
    interrupts them to search their own mail teaches them to tap yes unread,
    which destroys the value of the ones that matter."""
    assert classify(tool, {}, rules=rules).free


def test_a_private_reversible_write_does_not_ask(rules):
    """Archiving has an inverse the agent can call itself."""
    assert classify("modify_email", {"archive": True}, rules=rules).free


def test_drafting_does_not_ask(rules):
    """A draft is the point of drafting: nothing leaves the machine, and
    asking to draft would make draft-first useless."""
    assert classify("draft_email", {"to": "x@y.example"}, rules=rules).free


def test_mail_to_themselves_does_not_ask(rules):
    """"Email me the summary" is a note, not correspondence."""
    me = persona.GMAIL
    assert classify("send_email", {"to": me}, rules=rules, me=me).free


def test_mail_to_any_of_their_own_addresses_is_a_note_and_to_anyone_else_as_well_is_not(rules):
    """Since step 5 a note to themselves goes at once on every account that
    can send, and they have an address at each. Until then only Gmail's was
    known, and a note sent from work asked."""
    mine = persona.ADDRESSES
    assert all(classify("send_email", {"to": a}, rules=rules, me=mine).free for a in mine)
    both = [persona.WORK_EMAIL, f"Maya <{persona.GMAIL.upper()}>"]
    assert classify("send_email", {"to": both}, rules=rules, me=mine).free
    also = [persona.WORK_EMAIL, "ann@example.org"]
    assert not classify("send_email", {"to": also}, rules=rules, me=mine).free


def test_a_delete_on_an_account_without_conditional_writes_asks_and_a_create_or_flag_does_not(
        rules):
    """Step 5's exit criterion. Without a condition, a delete can land on an
    item changed since Steve read it, and nothing on the server stops it: the
    user's yes is the condition (`core/writes.py`, step 4). An id that names
    no account, or no word on which accounts are conditional, asks."""
    from steve_sdk import ids

    key = "0123456789ab"
    draft = ids.make("mail", key, "d1")
    on_condition = frozenset({f"mail:{key}"})

    def free(tool, args, conditional):
        return classify(tool, args, rules=rules, conditional=conditional).free

    assert not free("delete_draft", {"draft_id": draft}, frozenset())
    assert free("delete_draft", {"draft_id": draft}, on_condition)
    assert not free("delete_draft", {"draft_id": "r-before-step-5"}, on_condition)
    assert not free("delete_draft", {"draft_id": draft}, None)
    assert free("draft_email", {"to": "x@y.example"}, frozenset())
    assert free("modify_email", {"message_id": draft, "archive": True, "star": True},
                frozenset())


# --------------------------------------------------------------------------
# The rules file itself
# --------------------------------------------------------------------------


def test_no_tool_is_both_free_and_gated(rules):
    """Ordering in `classify` would silently pick one. Better to have neither
    than to have whichever the code happened to check first."""
    assert not (set(rules.free) & set(rules.gated))


def test_the_predicate_vocabulary_stays_tiny(rules):
    """The moment this can express "unless it is a reply and the thread has
    more than two messages", the classifier has become a model judgement call
    in a YAML costume -- and it will be wrong in exactly the cases that
    matter."""
    # `unconditional` is the one condition on the account rather than the
    # arguments (step 5's plan, section 1): an item on an account that cannot
    # write on condition is updated or deleted only through a card.
    allowed = {"non_empty", "true", "self", "unconditional"}
    for group in (rules.free, rules.gated):
        for tool, spec in group.items():
            for key in ("gate_when", "free_when"):
                for condition in spec.get(key) or []:
                    predicate = str(condition["when"]).strip().lower()
                    assert predicate in allowed, (
                        f"{tool}.{key} uses predicate {predicate!r}; "
                        f"allowed: {sorted(allowed)}"
                    )


def test_every_gate_condition_explains_itself(rules):
    """The reason is rendered on the card the user reads. "This needs approval" is
    not a reason."""
    for group in (rules.free, rules.gated):
        for tool, spec in group.items():
            for condition in spec.get("gate_when") or []:
                assert condition.get("because"), f"{tool} gates without saying why"


def test_spend_ceilings_exist_and_are_positive(rules):
    from core.consent import spend_caps

    caps = spend_caps(rules)
    assert caps["per_action_usd"] > 0
    assert caps["per_day_usd"] >= caps["per_action_usd"]
    assert caps["per_week_usd"] >= caps["per_day_usd"]


# --------------------------------------------------------------------------
# Classified is not the same as gated
# --------------------------------------------------------------------------


def test_every_gatable_tool_actually_routes_through_the_gate(rules):
    """The hole the rest of this file does not cover.

    `core/consent.yaml` can say `create_event` with attendees needs asking,
    and the tool can go straight to Google anyway -- the classifier is a
    function nobody is obliged to call. Every classification here was correct
    while invitations went out ungated, which is precisely the kind of
    agreement between two files that reads as safety and is not.

    So this reads the tool bodies: anything that can classify as gated must
    hand its arguments to `act`, which is the one place that decides between
    doing and asking.
    """
    # Hand-written tools only: a module's has no body here, and its calls
    # are routed through `act` by agent/modtools.py, checked below.
    unrouted = sorted(
        name
        for name, node in _tool_nodes().items()
        if _can_be_gated(name, rules) and not _calls_the_gate(node)
    )
    assert not unrouted, (
        "these tools are classified as needing approval and then perform "
        f"the action themselves: {unrouted}. "
        "Route them through `act`, or the classification is decorative."
    )


def test_the_routing_check_can_tell_the_difference(rules):
    """Guards the guard: if `_calls_the_gate` silently stopped matching, the
    test above would pass for a codebase with no gate at all."""
    nodes = _tool_nodes()
    assert _calls_the_gate(nodes["send_email"])
    assert not _calls_the_gate(nodes["web_search"])


# --------------------------------------------------------------------------
# The prompt and the toolset describe the same agent
# --------------------------------------------------------------------------

# Offered only on top of the browser, and only with a saved LinkedIn login
# (agent/toolset/linkedin.py `offered`), so their guidance is in their own
# descriptions: a section in the house prompt would name LinkedIn to every
# user with the browser on, set up or not.
SELF_DESCRIBED = {"linkedin_profile", "linkedin_search_people", "linkedin_company",
                  "linkedin_jobs"}


def _house_prompt() -> str:
    """The house prompt with every feature on, as agent/features.py
    assembles it: what the model reads, rather than every file under
    `agent/prompts/features/` whether or not a feature includes it."""
    from agent import features

    return features.assemble(features.everything())


def _setups() -> list[tuple[str, dict[str, str] | None]]:
    """Nothing set up, everything set up, and each feature alone. Alone is
    where a section that leans on another feature's tool shows."""
    from agent import features

    every = features.everything()
    alone = [(f.name, {f.name: every[f.name]}) for f in features.all_features()]
    return [("nothing", {}), ("everything", None), *alone]


@pytest.mark.parametrize(("name", "on"), _setups(), ids=[n for n, _ in _setups()])
def test_every_name_in_the_prompt_is_a_tool_the_user_has_or_one_of_its_arguments(
        name, on, registered):
    """This mismatch has caused three separate failures: draft-first
    promised before drafts existed, a reduced toolset reporting that mail
    search had "failed" when it simply had no such tool, and a hardcoded claim
    about the calendar's timezone that had quietly stopped being true.
    Each time both halves were written correctly for the machine their
    author had in mind, which is why it is checked mechanically.

    The check before this one looked only at lines shaped "- `tool`", and
    once the prompt stopped listing tools as bullets it matched nothing and
    passed on anything. So every backticked name is checked now, against
    the tools this setup is offered (agent/features.py withholds the rest)
    and their arguments, read from the source like the tools themselves.
    """
    from agent import features

    on = features.everything() if on is None else on
    offered = set(registered) - features.tools_off(on)
    nodes = _tool_nodes()
    arguments = {
        a.arg for tool in offered if tool in nodes
        for a in (*nodes[tool].args.args, *nodes[tool].args.kwonlyargs)
    }
    named = set(re.findall(r"`(\w+)`", features.assemble(on)))
    unoffered = sorted(named - offered - arguments)
    assert not unoffered, (
        f"with {name} set up, the prompt names {unoffered}, which is not a "
        "tool this user has nor an argument of one. The model will try it "
        "and report a failure, which reads as a broken tool rather than an "
        "absent one."
    )


def test_every_tool_is_described_somewhere_the_model_can_read(registered):
    """A registered tool absent from the prompt still works -- pydantic-ai
    sends its docstring -- but it misses the curated guidance, which is
    where the caps and the warnings live."""
    named = set(re.findall(r"`(\w+)`", _house_prompt()))

    # A module's tools are described by their manifests, and the house
    # prompt cannot name a tool that leaves when its module is turned off.
    undocumented = sorted(set(registered) - named - SELF_DESCRIBED - set(_module_tools()))
    assert not undocumented, (
        f"registered and described nowhere: {undocumented}"
    )
