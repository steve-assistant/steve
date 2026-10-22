"""The reference monitor: what decides whether an action is free, asked about,
refused, approved and run.

The rules were frozen (`core/consent.yaml`, `core/consent.py`), but the code
that applied them was not: `agent/actions.act`, `agent/approvals.py` and the
approval intercept could be changed by a self-upgrade a judge had passed
(docs/security-model.md, 1 October 2026). Those keep the plumbing -- the
database rows, the cards, the queue -- and ask this module every question
that decides anything; the frozen gate (tests/gate/test_high_risk_approval.py,
test_workers_stay_private.py) pins that they do.

**Each channel has a ceiling** (`CEILINGS`): what a yes from there may
approve, and what a high-risk yes there must carry. A channel not in the
table -- a text message, an unattended run, a reminder's words -- approves
nothing. The user may lower a channel's ceiling in the app (`LOWERED_BY`,
core/knobs.py), never raise it past the table. The yes is recorded as the
channel it came from: until step 4 a spoken yes at the desk was recorded as
Signal's.

**High-risk actions need a second factor.** The maintainer, 1 October 2026:
spending money (`tier: high` in consent.yaml).
- Their "YES MAPLE" comes over a remote channel, whose servers and linked
  devices could forge it, so there the yes must carry the six digits from
  their authenticator app (`core/totp.py`). A code is bound to one card and
  one 30-second step, and a step once used is never accepted again. Five
  wrong codes within a day and none is taken for 15 minutes, longer each
  time.
- In the app, the passphrase (`core/passphrase.py`). Until step 4 the app
  answered with a click alone (`LOCAL`, now gone): it was "this laptop". But whoever
  sits at an unlocked laptop, or drives its window, can click. Since step 9,
  Windows Hello's signature may stand in for the passphrase (`core/hello.py`).
- Either is bound to the card and to its arguments as they were approved
  (`approval_proofs`): changed afterwards, the action does not run.
- Only one high-risk card is open at a time, so a channel holding their
  code cannot point it at a different one.
- The check is made when the yes is decided *and* again when the action
  runs, so a change to the parsing in between cannot let one through.
"""

from __future__ import annotations

import re
import sqlite3
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime

from core import db, hello, knobs, passphrase, totp, unseen
from core.consent import Consent, Mine, Reach, Reversal, classify, high_risk

# Six digits in their reply, "482913" or "482 913": ASCII digits, after
# NFKC (`code_in`).
_CODE = re.compile(r"(?<!\d)([0-9]{3})\s?([0-9]{3})(?!\d)")

# What a yes may approve: nothing, an ordinary card, or a high-risk one too.
NEVER, ORDINARY, HIGH = "never", "ordinary", "high"
_RANK = {NEVER: 0, ORDINARY: 1, HIGH: 2}

# Each channel's ceiling, and the second factor a high-risk yes there
# carries: the app's passphrase, or the authenticator's code over a channel
# someone else's servers carry. Telegram's is here before Telegram is.
CEILINGS: dict[str, tuple[str, str]] = {
    "tray": (HIGH, "passphrase"),
    "signal": (HIGH, "code"),
    "telegram": (HIGH, "code"),
    "voice": (HIGH, "code"),
}
_NAMES = {"tray": "the app", "signal": "Signal", "telegram": "Telegram",
          "voice": "your voice at the desk"}

# The setting that lowers a channel's ceiling. The app's is not lowered: it
# is where every card can still be answered.
LOWERED_BY = {"signal": "APPROVE_ON_SIGNAL", "telegram": "APPROVE_ON_TELEGRAM",
              "voice": "APPROVE_BY_VOICE"}


@dataclass(frozen=True)
class Gate:
    """What a tool call needs: `free` (do it), `card` (ask them), `refused`.
    `waived`: free because Jev judged a card asked only for want of a
    condition unneeded (core/lostupdate.py), which alone lets core write
    without a condition and without a card (core/writes.py)."""

    kind: str
    consent: Consent
    reason: str = ""
    waived: bool = False


def fit_for_a_worker(consent: Consent) -> bool:
    """What a worker may do on its own: free, private and reversible. A draft
    or an event on their own calendar is; an event with guests is not."""
    return (not consent.gated and consent.reach == Reach.PRIVATE
            and consent.reversal == Reversal.REVERSIBLE)


def mailbox_refusal(conn: sqlite3.Connection, args: dict) -> str | None:
    """Why this call may not be made, if it would reach the agent's own
    mailbox (core/mailbox.py): mail, an invitation or a draft there would be
    the agent writing its own instructions in their name. Whoever asks.

    The address is read here, from the table only the app writes. Until
    step 5 it was a line of the frozen policy; a caller handing it in could
    hand in none."""
    from core import mailbox

    own = mailbox.own_address(conn)
    if own and mailbox.names_mailbox(args, own):
        return (f"{own} is the agent's own mailbox, which it never writes to: leave it out "
                "of the recipients")
    return None


def gate(conn: sqlite3.Connection, tool: str, args: dict, *, worker: bool = False,
         me: Mine = None, conditional: frozenset[str] | None = None) -> Gate:
    """The decision `actions.act` carries out. `me` and `conditional` are
    what consent knows of the user's accounts (`core.consent.classify`)."""
    consent = classify(tool, args, me=me, conditional=conditional)
    if why := mailbox_refusal(conn, args):
        return Gate("refused", consent, why)
    if worker and not fit_for_a_worker(consent):
        return Gate("refused", consent, "; ".join(consent.reasons) or tool)
    return Gate("card" if consent.gated else "free", consent)


def may_ask(conn: sqlite3.Connection, tool: str) -> str | None:
    """Why a card for `tool` may not be raised now, or None. One high-risk
    card open at a time: their code answers the card the user is looking at, and no
    other can be waiting for a channel to redirect it to."""
    if not high_risk(tool):
        return None
    open_ = conn.execute(
        "SELECT tool, preview FROM actions WHERE status = 'pending'").fetchall()
    for other_tool, preview in open_:
        if high_risk(other_tool):
            # By its first line, not its word: the model reads this, and a
            # waiting card's word is said only by the card (agent/cardwords.py).
            shows = ((preview or "").splitlines() or [""])[0][:80]
            return (f"a high-risk card is still waiting for the user's yes and code "
                    f"({shows}); a second high-risk action waits until that one is answered")
    return None


def code_in(text: str) -> str | None:
    """The authenticator code in a reply, or None. `\\d` took any
    script's digits, and the check then raised on them (step 11's third
    review): NFKC makes full-width digits ASCII, and others are no code."""
    match = _CODE.search(unicodedata.normalize("NFKC", text or ""))
    return match.group(1) + match.group(2) if match else None


def ceiling(channel: str, chosen: dict | None = None) -> str:
    """What a yes from `channel` may approve: the table's, or lower where
    the user chose (`chosen`, else what core/knobs.py holds)."""
    top = CEILINGS.get(channel, (NEVER, ""))[0]
    knob = LOWERED_BY.get(channel)
    if knob is None:
        return top
    picked = (knobs.read() if chosen is None else chosen).get(knob, top)
    return picked if picked in _RANK and _RANK[picked] < _RANK[top] else top


def _too_high(by: str, need: str) -> str | None:
    """Why a yes from `by` cannot approve a card that needs `need`, or None."""
    has = ceiling(by)
    if _RANK[has] >= _RANK[need]:
        return None
    if by not in CEILINGS:
        return f"a yes from {by or 'nowhere'} approves nothing"
    name = _NAMES[by]
    if has == NEVER:
        return f"you chose not to approve from {name}: answer it in the app"
    return (f"you chose to approve only ordinary cards from {name}: answer this one in "
            "the app")


_MISSING = {
    "code": "this one needs the 6-digit code from your authenticator app as well",
    "passphrase": "this one needs your passphrase",
}


def accept_code(conn: sqlite3.Connection, action_id: int, tool: str, text: str, *,
                by: str, secret: str | None = None, when: float | None = None) -> str | None:
    """Check the code in their reply from `by` for this action, and bind it
    to it. Returns why not, or None when it is good -- or not needed.

    Wrong codes are counted, and five within a day stop every code, for
    longer each time (`totp.wrong`): uncounted, guessing at three good codes
    a step over a channel someone else holds was a matter of time (step
    11's audit). A yes with no code in it is not a guess, and is not
    counted. A right code does not start the count again: it did, and each
    code the user sent bought a guesser four more tries (step 11's second
    review)."""
    if not high_risk(tool):
        return None
    if why := _too_high(by, HIGH):
        return why
    now = time.time() if when is None else when
    with totp.TRYING:
        if why := totp.paused(now):
            return why
        code = code_in(text)
        if code is None:
            return _MISSING["code"]
        if secret is None:
            from core import secrets

            secret = secrets.get(totp.SECRET_KEY)
        if not secret:
            return ("no authenticator is set up yet, so nothing high-risk can be approved from "
                    "here: answer it in the app, or set one up there")
        step = totp.matching_step(secret, code, now)
        if step is None:
            return totp.wrong(now)
        if not _bind(conn, action_id, "code", step):
            return "that code was already used -- wait for the next one"
    return None


def accept_passphrase(conn: sqlite3.Connection, action_id: int, tool: str, given: str, *,
                      now: float | None = None) -> str | None:
    """Check the passphrase given in the app for this action, and bind it
    to it. Returns why not, or None when it is good -- or not needed."""
    if not high_risk(tool):
        return None
    if why := passphrase.check(given, now=now):
        return why
    _bind(conn, action_id, "passphrase", None)
    return None


def hello_binding(conn: sqlite3.Connection, action_id: int) -> str | None:
    """What a Hello signature for this card signs: its id and its
    arguments' digest as they are now. None for no such card."""
    row = conn.execute("SELECT args_hash FROM actions WHERE id = ?", (action_id,)).fetchone()
    return None if row is None else f"{action_id}:{row[0]}"


def accept_hello(conn: sqlite3.Connection, action_id: int, tool: str, nonce: str,
                 signature: str, *, now: float | None = None) -> str | None:
    """Check Windows Hello's signature given in the app for this action,
    and bind it to it, as `accept_passphrase` binds the passphrase. Returns
    why not, or None when it is good -- or not needed."""
    if not high_risk(tool):
        return None
    binding = hello_binding(conn, action_id)
    if binding is None:
        return "that card is gone"
    if why := hello.verify(nonce, signature, "yes", binding, now=now):
        return why
    _bind(conn, action_id, "hello", None)
    return None


def _bind(conn: sqlite3.Connection, action_id: int, factor: str, step: int | None) -> bool:
    """Record the proof for this card, against its arguments as they are
    now; False when the code's step was already used, on this card or
    another. A code's row is never rewritten: rewritten, its step would be
    free to spend again. A passphrase's or Hello's is, to the arguments as
    they are."""
    again = (" ON CONFLICT(action_id, factor) DO UPDATE SET args_hash = excluded.args_hash,"
             " at = datetime('now')") if factor != "code" else ""
    try:
        with db.transaction(conn):
            conn.execute(
                "INSERT INTO approval_proofs (action_id, factor, step, args_hash)"
                " SELECT id, ?, ?, args_hash FROM actions WHERE id = ?" + again,
                (factor, step, action_id))
    except sqlite3.IntegrityError:
        return False
    return True


# What proves each factor: the app's passphrase, or Hello in its place.
PROVEN_BY = {"passphrase": ("passphrase", "hello"), "code": ("code",)}


def _proved(conn: sqlite3.Connection, action_id: int, by: str) -> bool:
    """Whether this card holds the second factor `by` carries, bound to its
    arguments as they still are."""
    factors = PROVEN_BY[CEILINGS[by][1]]
    marks = ", ".join("?" * len(factors))
    return conn.execute(
        "SELECT 1 FROM approval_proofs p JOIN actions a ON a.id = p.action_id"
        f" WHERE p.action_id = ? AND p.factor IN ({marks}) AND p.args_hash = a.args_hash",
        (action_id, *factors)).fetchone() is not None


def _may_approve(conn: sqlite3.Connection, action, by: str) -> str | None:
    need = HIGH if high_risk(action.tool) else ORDINARY
    if why := _too_high(by, need):
        return why
    if need == HIGH and not _proved(conn, action.id, by):
        return _MISSING[CEILINGS[by][1]]
    return None


def may_decide(conn: sqlite3.Connection, action, *, approve: bool, by: str) -> str | None:
    """Why this yes or no may not be recorded, or None. `action` is an
    `agent.approvals.Action`; `by` the channel the answer came from. A no
    may come from anywhere an answer can."""
    if action.status == "withdrawn":
        return (f"that was withdrawn: "
                f"{getattr(action, 'withdrawn_reason', None) or 'its account was turned off'}")
    if action.status != "pending":
        return f"action is {action.status}, not pending"
    if action.expires_at and datetime.fromisoformat(action.expires_at) < datetime.now():
        return "that expired; ask me again"
    return _may_approve(conn, action, by) if approve else None


def may_run(conn: sqlite3.Connection, action) -> str | None:
    """Why an approved action may not run, or None. The second check, on
    what was recorded: a yes from a channel that may approve it, as that
    channel's ceiling stands now, and for a high-risk action, its second
    factor still bound to the arguments it is about to run with."""
    row = conn.execute("SELECT decided_by FROM actions WHERE id = ?", (action.id,)).fetchone()
    if why := _may_approve(conn, action, (row[0] if row else "") or ""):
        return f"it does not run: {why}"
    return None


# ---------------------------------------------------------------------------
# Routines: approved once, run many times (agent/routines.py)
# ---------------------------------------------------------------------------
#
# The maintainer, 4 October 2026: a routine that acts outward -- "every Friday, email
# my mom a recap" -- is approved once, with its targets fixed; its runs act
# inside them without asking, and anything outside them asks. The targets are
# fixed from their words when the user creates it, and a run's untrusted reading can
# fill in a value (the recap's text) but never change who it reaches. That
# much is decided here: which tools an envelope may hold and how their
# targets are read (`ENVELOPE_TARGETS`), what the card must show
# (`describe_envelope`), that an envelope counts only if a card the user approved
# carries its exact hash (`approved_envelope`), and what a run's call needs
# (`routine_gate`). High-risk tools are not routinable: each such call is
# asked about, with their code, every time. A goal's work sessions
# (agent/goals.py) are held to the same rule (`goal_gate`).

# The outward tools an envelope may hold, and the arguments naming whom they
# reach. A call names its targets in these and nowhere else.
ENVELOPE_TARGETS: dict[str, tuple[str, ...]] = {
    "send_email": ("to", "cc", "bcc"),
    "create_event": ("attendees",),
    "update_event": ("attendees",),
}
MAX_TARGETS = 10
_ADDRESS = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")


def _addresses(value) -> set[str]:
    """Every address a target argument names, lower-cased: a list, or text
    with "Name <a@b>" and commas or semicolons between."""
    from email.utils import getaddresses

    if value is None or value == "":
        return set()
    items = value if isinstance(value, list | tuple) else [str(value)]
    found: set[str] = set()
    for item in items:
        for _, address in getaddresses([str(item).replace(";", ",")]):
            if address:
                found.add(address.strip().lower())
    return found


def envelope_refusal(conn: sqlite3.Connection, envelope) -> str | None:
    """Why `envelope` cannot be approved as written, or None. Shape:
    {tool: {"targets": [address, ...]}}."""
    if not isinstance(envelope, dict) or not envelope:
        return "an envelope names at least one tool and its targets"
    for tool, entry in envelope.items():
        if tool not in ENVELOPE_TARGETS:
            return f"{tool} cannot be done by a routine without asking"
        if high_risk(tool):
            return f"{tool} is high-risk: asked about, with the user's code, every time"
        targets = entry.get("targets") if isinstance(entry, dict) else None
        if not isinstance(targets, list) or not targets or len(targets) > MAX_TARGETS:
            return f"{tool} needs between 1 and {MAX_TARGETS} fixed targets"
        # A target is the one thing a model writes on an approve card's
        # first line, and is shown there as written. One with a character
        # the eye does not see (core/unseen.py; step 11's second review) is
        # refused, and so is one with a letter from another alphabet:
        # mom@exаmple.com, with a Cyrillic а, shows exactly as the real one
        # (its third review). An international domain has an xn-- form.
        bad = [t for t in targets if not isinstance(t, str) or not _ADDRESS.match(t.strip())
               or unseen.first_unseen(t)]
        if bad:
            return f"{tool}: not an address: {bad[0]!r}"
        if other := next((t for t in targets if not t.isascii()), None):
            return (f"{tool}: {other!r} has a letter that is not plain ASCII, and may only "
                    "look like the address meant: write it in plain letters, an "
                    "international domain in its xn-- form")
        if why := mailbox_refusal(conn, {"to": targets}):
            return why
    return None


def _canonical(envelope: dict) -> dict:
    return {tool: {"targets": sorted({t.strip().lower() for t in entry["targets"]})}
            for tool, entry in sorted(envelope.items())}


def envelope_sha(envelope: dict, bound: dict[str, str]) -> str:
    """The hash their approval carries: of the targets as they will be
    checked, and of what the holder will do with them -- its `bound`
    columns, as stored (`HOLDERS`). Take it from `holder_sha`.

    It was the targets alone until step 11's audit (D10). The card showed a
    routine's title, schedule and targets, not its instruction, and a yes
    held while the instruction -- what each run acts on -- or the schedule
    changed: the same recipient sent something else, or sent to nightly
    where the user said Fridays."""
    import hashlib
    import json

    text = json.dumps({"envelope": _canonical(envelope), "bound": bound},
                      sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def describe_envelope(envelope: dict) -> str:
    """The envelope in words, for the card. A card for `approve_routine` must
    show exactly this (pinned by tests/gate/test_routines.py), so what the user
    approves is what is checked, whatever the plumbing writes around it."""
    said = {"send_email": "email", "create_event": "invite to new events",
            "update_event": "invite to changed events"}
    return "; ".join(f"{said[tool]}: {', '.join(entry['targets'])}"
                     for tool, entry in _canonical(envelope).items())


# What may hold an envelope: the table it is read from, the card that
# approves it, the argument on that card naming which one, and the columns
# its approval binds besides the envelope -- what it will do, and when. The
# card shows each whole. Goals (agent/goals.py) came second, 4 October 2026,
# and are held to exactly the rule routines are; a third kind is a line
# here, not a second copy of it.
HOLDERS: dict[str, tuple[str, str, str, tuple[str, ...]]] = {
    "routine": ("routines", "approve_routine", "routine_id", ("instruction", "schedule")),
    "goal": ("goals", "approve_goal", "goal_id", ("ask", "done_when")),
}


def _held(conn: sqlite3.Connection, holder: str, holder_id: int, *,
          active: bool) -> tuple[dict, str] | None:
    """This routine's or goal's envelope as stored, and the hash a card
    approving it must carry; None without one."""
    import json

    table, _, _, bound = HOLDERS[holder]
    row = conn.execute(f"SELECT envelope, {', '.join(bound)} FROM {table} WHERE id = ?"
                       + (" AND status = 'active'" if active else ""),
                       (int(holder_id),)).fetchone()
    if row is None or not row[0]:
        return None
    try:
        envelope = json.loads(row[0])
    except ValueError:
        return None
    return envelope, envelope_sha(envelope, dict(zip(bound, tuple(row)[1:], strict=True)))


def holder_sha(conn: sqlite3.Connection, holder: str, holder_id: int) -> str | None:
    """The hash a card approving this routine or goal carries, read from
    what is stored: the card is made, and its yes checked, against the same
    row a run will act on."""
    held = _held(conn, holder, holder_id, active=False)
    return held[1] if held else None


def approved_envelope(conn: sqlite3.Connection, holder_id: int, *,
                      holder: str = "routine") -> dict:
    """The envelope of this routine or goal, if an approval card the user
    approved, and that has run, carries its exact hash -- else nothing. Read
    from the database here rather than taken from the caller, so a run cannot
    present an envelope of its own, and a changed one, or a changed
    instruction or schedule, stops counting at once."""
    _, card, key, _ = HOLDERS[holder]
    held = _held(conn, holder, holder_id, active=True)
    if held is None:
        return {}
    envelope, sha = held
    if envelope_refusal(conn, envelope) is not None:
        return {}
    approved = conn.execute(
        "SELECT 1 FROM actions WHERE tool = ? AND status = 'done'"
        f" AND json_extract(args_json, '$.{key}') = ?"
        " AND json_extract(args_json, '$.envelope_sha') = ?",
        (card, int(holder_id), sha)).fetchone()
    return _canonical(envelope) if approved else {}


def within_envelope(envelope: dict, tool: str, args: dict) -> bool:
    """Whether a call reaches only targets its envelope fixed. A call naming
    nobody does not count as inside: an email must say whom it is to."""
    entry = envelope.get(tool)
    if not entry:
        return False
    named: set[str] = set()
    for arg in ENVELOPE_TARGETS.get(tool, ()):
        named |= _addresses(args.get(arg))
    return bool(named) and named <= set(entry["targets"])


def envelope_gate(conn: sqlite3.Connection, holder: str, holder_id: int, tool: str,
                  args: dict, *, me: Mine = None,
                  conditional: frozenset[str] | None = None) -> Gate:
    """What an unattended run of a routine or a goal needs for this call:
    free if a worker could do it or it is inside the approved envelope;
    otherwise a card. Never refused outright -- the user chose that anything
    outside the envelope asks."""
    consent = classify(tool, args, me=me, conditional=conditional)
    if why := mailbox_refusal(conn, args):
        return Gate("refused", consent, why)
    if fit_for_a_worker(consent):
        return Gate("free", consent)
    if not high_risk(tool) and within_envelope(
            approved_envelope(conn, holder_id, holder=holder), tool, args):
        return Gate("free", consent, f"inside the {holder}'s approved envelope")
    return Gate("card", consent, f"outside the {holder}'s approved envelope")


def routine_gate(conn: sqlite3.Connection, routine_id: int, tool: str, args: dict, *,
                 me: Mine = None, conditional: frozenset[str] | None = None) -> Gate:
    """A routine's run (agent/routines.py)."""
    return envelope_gate(conn, "routine", routine_id, tool, args, me=me,
                         conditional=conditional)


def module_gate(conn: sqlite3.Connection, tool: str, args: dict, *, me: Mine = None,
                conditional: frozenset[str] | None = None) -> Gate:
    """A run of a module's routine (agent/modroutines.py): no envelope and
    nothing free, not even what a worker may do, since its instruction is
    a module's words and no one watches it run: each action is a card.
    Refused where any run's would be."""
    consent = classify(tool, args, me=me, conditional=conditional)
    if why := mailbox_refusal(conn, args):
        return Gate("refused", consent, why)
    return Gate("card", consent, "a module's routine asks before each action")


def goal_gate(conn: sqlite3.Connection, goal_id: int, tool: str, args: dict, *,
              me: Mine = None, conditional: frozenset[str] | None = None) -> Gate:
    """A goal's work session (agent/goals.py): held to the routine's rule."""
    return envelope_gate(conn, "goal", goal_id, tool, args, me=me, conditional=conditional)
