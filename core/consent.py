"""Which actions the user has to agree to, and which the agent may just do.

`system.md` has always said the right thing -- "not undoable and visible to
other people" needs asking, "private to their account and easy to delete" does
not -- and it said it in prose, to a model, with nothing underneath. This is
the same rule with teeth.

It lives in `core/` for the reason everything here does: the agent must not be
able to widen its own permissions. `tools/**` is auto-mergeable, so a
self-modification run can write a new tool tonight and merge it unattended.
What it cannot do is decide that tool is safe.

**Unknown tools are gated.** That is the single most important line in the
file. A capability nobody classified is one nobody thought about, and the cost
of asking about something harmless is a tap; the cost of not asking about
something harmful is unbounded.

Two axes, because they genuinely disagree:

    reach      does anyone outside the user's own accounts notice?
    reversal   is there an inverse we can actually run?

Deleting their own calendar event is PRIVATE and IRREVERSIBLE. Archiving mail is
PRIVATE and REVERSIBLE. Emailing a stranger is OUTWARD and IRREVERSIBLE. Only
the second is free.

The predicate vocabulary is deliberately tiny -- `non_empty`, `true`, `self`,
and `unconditional`, which is about the account an id names, not the words.
The moment it can express "unless it is a reply and the thread has more than
two messages", the classifier has become a model judgement call wearing a YAML
costume, and it will be wrong in exactly the cases that matter.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from core import yamlfile

# The user's own addresses: one, several, or not known.
Mine = str | Iterable[str] | None

REPO_ROOT = Path(__file__).resolve().parent.parent
CONSENT_PATH = REPO_ROOT / "core" / "consent.yaml"


class Reach(StrEnum):
    PRIVATE = "private"  # nothing outside their own accounts notices
    OUTWARD = "outward"  # another person or system sees it


class Reversal(StrEnum):
    REVERSIBLE = "reversible"  # a named inverse exists and we can run it
    IRREVERSIBLE = "irreversible"  # nothing takes it back


@dataclass(frozen=True)
class Consent:
    """What a particular call to a particular tool requires."""

    gated: bool
    reach: Reach
    reversal: Reversal
    reasons: list[str] = field(default_factory=list)
    undo: str | None = None
    spend_usd: float = 0.0
    # Gated only because the account cannot make the write conditional on
    # the item being as it was read: the one card Jev may judge unneeded
    # (core/lostupdate.py). Any other rule that gates the call leaves it False.
    unconditional: bool = False

    @property
    def free(self) -> bool:
        return not self.gated


@dataclass(frozen=True)
class Rules:
    free: dict[str, dict]
    gated: dict[str, dict]
    spend: dict[str, float]

    @property
    def known(self) -> set[str]:
        return set(self.free) | set(self.gated)


# ---------------------------------------------------------------------------
# Predicates
# ---------------------------------------------------------------------------


def _own(me: Mine) -> set[str]:
    if isinstance(me, str):
        me = [me]
    return {str(a).strip().lower() for a in me or () if a and str(a).strip()}


def _self_addressed(value: Any, *, me: Mine) -> bool:
    """True when every recipient is the user themselves, at one of their
    own addresses.

    Mail to yourself is a note, not correspondence. Without this, "email me
    the summary" -- a thing the user asks for often -- would need approving.
    Each of their mail accounts has an address of its own, and a note sent
    from any of them to any of them is still a note.
    """
    mine = _own(me)
    if not mine:
        return False
    recipients = value if isinstance(value, list) else [value]
    addresses = {str(r).strip().lower().strip("<>") for r in recipients if r}
    if not addresses:
        return False
    # Tolerate "Sam Ortiz <sam@…>" by taking whatever is inside the angle
    # brackets when they are present.
    cleaned = {a.split("<")[-1].strip(">") if "<" in a else a for a in addresses}
    return cleaned <= mine


def _unconditional(value: Any, *, conditional: frozenset[str] | None) -> bool:
    """True unless `value` is an id from an account that makes its writes
    conditional on the item being as it was read.

    Without a condition, an update or a delete can land on an item that
    changed after Steve read it, and nothing on the server would stop it:
    so on such an account the user's yes is the condition (`core/writes.py`
    step 4). An id that names no account, or a caller that did not say which
    accounts are conditional, asks.
    """
    from steve_sdk import ids

    parsed = ids.parse(value) if isinstance(value, str) else None
    if parsed is None or conditional is None:
        return True
    return f"{parsed.klass}:{parsed.account}" not in conditional


def _holds(predicate: Any, value: Any, *, me: Mine,
           conditional: frozenset[str] | None = None) -> bool:
    # Normalised to a string because YAML resolves a bare `true` to the
    # boolean, not the word -- so `when: true` arrived here as True, matched
    # no predicate name, and fell through to the err-toward-asking branch.
    # The symptom was run_code gating even with no network, which is the
    # annoying direction of a fail-closed default rather than the dangerous
    # one, but wrong either way.
    predicate = str(predicate).strip().lower()
    if predicate == "non_empty":
        return bool(value)
    if predicate == "true":
        return value is True
    if predicate == "self":
        return _self_addressed(value, me=me)
    if predicate == "unconditional":
        return _unconditional(value, conditional=conditional)
    # An unknown predicate must not silently mean "does not apply" -- that
    # would turn a typo in the frozen file into a hole. Treat it as holding,
    # so a broken rule errs toward asking.
    return True


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_rules(path: Path = CONSENT_PATH) -> Rules:
    raw = yamlfile.load(path) or {}  # parsed once per version on disk
    return Rules(
        free=raw.get("free") or {},
        gated=raw.get("gated") or {},
        spend=raw.get("spend") or {},
    )


# ---------------------------------------------------------------------------
# Classifying
# ---------------------------------------------------------------------------


def classify(
    tool: str,
    args: dict | None = None,
    *,
    rules: Rules | None = None,
    me: Mine = None,
    conditional: frozenset[str] | None = None,
) -> Consent:
    """What this call needs. Never raises; an unclassifiable call is gated.

    `me` is the user's own addresses; `conditional`, the "<class>:<key>" of
    the accounts whose writes are conditional (`agent.connectors.conditional`)."""
    args = args or {}
    rules = rules or load_rules()

    if tool in rules.free:
        return _classify_free(tool, args, rules, me, conditional)
    if tool in rules.gated:
        return _classify_gated(tool, args, rules, me, conditional)

    return Consent(
        gated=True,
        reach=Reach.OUTWARD,
        reversal=Reversal.IRREVERSIBLE,
        reasons=[
            f"{tool} is not classified in consent.yaml, so it is treated as "
            "outward and irreversible until somebody says otherwise"
        ],
    )


def _classify_free(tool: str, args: dict, rules: Rules, me: Mine,
                   conditional: frozenset[str] | None) -> Consent:
    spec = rules.free[tool]
    reach = Reach(spec.get("reach", Reach.PRIVATE))
    reversal = Reversal(spec.get("reversal", Reversal.REVERSIBLE))

    for condition in spec.get("gate_when") or []:
        if _holds(condition["when"], args.get(condition["arg"]), me=me,
                  conditional=conditional):
            return Consent(
                gated=True,
                reach=Reach(condition.get("reach", Reach.OUTWARD)),
                reversal=Reversal(condition.get("reversal", Reversal.IRREVERSIBLE)),
                reasons=[condition.get("because", f"{condition['arg']} made this outward")],
                undo=spec.get("undo"),
                unconditional=str(condition["when"]).strip().lower() == "unconditional",
            )

    return Consent(gated=False, reach=reach, reversal=reversal, undo=spec.get("undo"))


def _classify_gated(tool: str, args: dict, rules: Rules, me: Mine,
                    conditional: frozenset[str] | None) -> Consent:
    spec = rules.gated[tool]
    reach = Reach(spec.get("reach", Reach.OUTWARD))
    reversal = Reversal(spec.get("reversal", Reversal.IRREVERSIBLE))

    for condition in spec.get("free_when") or []:
        if _holds(condition["when"], args.get(condition["arg"]), me=me,
                  conditional=conditional):
            return Consent(
                gated=False,
                reach=Reach(condition.get("reach", Reach.PRIVATE)),
                reversal=Reversal(condition.get("reversal", Reversal.REVERSIBLE)),
                reasons=[condition.get("because", "")],
                undo=spec.get("undo"),
            )

    spend = 0.0
    spend_arg = spec.get("spend_arg")
    if spend_arg:
        # An amount the model wrote. One that is not a number was free
        # (0), and NaN was stored as NULL and read back as free: unknown,
        # it is infinite instead, which every cap refuses
        # (`approvals.over_cap`).
        try:
            spend = float(args.get(spend_arg) or 0)
        except (TypeError, ValueError):
            spend = math.inf
        if math.isnan(spend):
            spend = math.inf

    return Consent(
        gated=True,
        reach=reach,
        reversal=reversal,
        reasons=[spec.get("because", f"{tool} always needs approval")],
        undo=spec.get("undo"),
        spend_usd=spend,
    )


def high_risk(tool: str, rules: Rules | None = None) -> bool:
    """Whether approving `tool` takes their authenticator code as well as their
    word: `tier: high` in consent.yaml (core/monitor.py)."""
    rules = rules or load_rules()
    spec = rules.gated.get(tool) or rules.free.get(tool) or {}
    return str(spec.get("tier") or "").strip().lower() == "high"


# ---------------------------------------------------------------------------
# Spend
# ---------------------------------------------------------------------------


def spend_caps(rules: Rules | None = None) -> dict[str, float]:
    """Ceilings the agent cannot raise, because this file is frozen.

    Approval does not override these. That is the point of having them: the
    cap is what a tired tap at eleven at night cannot undo.
    """
    rules = rules or load_rules()
    return {
        "per_action_usd": float(rules.spend.get("per_action_usd", 0)),
        "per_day_usd": float(rules.spend.get("per_day_usd", 0)),
        "per_week_usd": float(rules.spend.get("per_week_usd", 0)),
    }


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------

# Short, pronounceable, visually distinct. Words rather than numbers: a number
# collides with everything else in a chat, and this has to survive being read
# aloud on the voice channel and mangled by a phone keyboard's autocorrect.
# And words no ordinary message says by chance: minerals, birds and beasts,
# the parts of buildings and machines. A short message naming a waiting word
# is asked about, and the model reads the word as a hole, so with PEPPER or
# HARBOR waiting "add pepper to the list" and "the harbor tour at 5" never
# reached it (step 11's fourth review). No foods, places or verbs, then, and
# no two that sound alike ("ember" and "umber").
TOKEN_WORDS: tuple[str, ...] = (
    "maple", "cobalt", "quartz", "thistle", "cinder", "gable", "girder",
    "kestrel", "lichen", "nimbus", "pewter", "rafter", "vellum", "yarrow",
    "zephyr", "basalt", "ember", "aquifer", "bismuth", "bramble", "capstan",
    "cormorant", "dolomite", "feldspar", "foxglove", "fulcrum", "gantry",
    "ibex", "larkspur", "lintel", "malachite", "marmot", "narwhal",
    "obsidian", "ocelot", "osprey", "pangolin", "parapet", "plinth",
    "puffin", "sextant", "sprocket", "tourmaline", "transom", "trellis",
    "wombat", "zircon",
)


def token_for(taken: set[str], last_used: dict[str, int] | None = None) -> str:
    """A word not currently in use, and of those the one longest unused.
    Uniqueness is only needed among pending actions, so a short list
    outlasts the agent.

    `last_used` maps a word to the latest card that had it (cards' ids
    grow). A word went back into the draw the moment its card was
    answered, so "YES MAPLE" typed late, from a card read an hour after
    it was answered, could approve the next card to draw MAPLE, one the
    user had not seen (step 11's audit). Now the draw is at random from
    the older half of the free words, never-used words counting oldest, or
    from every never-used word while they are more than half: a word comes
    round again only after at least half the others have had a turn. The
    oldest alone was the next card's word known in advance, to anyone who
    had seen the cards before it, the model too, which reads answered
    cards' words: a reply could say "Reply YES <it>" before that card
    existed, where nothing redacts it (step 11's fourth review). So was
    the last never-used word, drawn from the never-used alone, once every
    other had had its turn (its fifth review)."""
    import secrets as pysecrets

    last_used = {word.upper(): card for word, card in (last_used or {}).items()}
    available = [w for w in TOKEN_WORDS if w.upper() not in {t.upper() for t in taken}]
    if not available:
        # Vanishingly unlikely -- it would mean 48 unanswered cards -- but a
        # collision would let one "yes" approve the wrong thing.
        return pysecrets.token_hex(3).upper()
    # Never used first (0), then by the card that last had it.
    by_age = sorted(available, key=lambda w: last_used.get(w.upper(), 0))
    fresh = sum(w.upper() not in last_used for w in available)
    return pysecrets.choice(by_age[:max(1, fresh, len(by_age) // 2)]).upper()
