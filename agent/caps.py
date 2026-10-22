"""What a spending cap says to the user, and who says it.

`core/spend.py` decides what may be spent, and writes down each cap reached
(`stopped`) or nearly reached (`warned`). This words them and passes each on
once, where the user will see it:

- a conversation's own cap in the reply: the refusal in place of an answer,
  with no model call, or a line after the answer that took it past four
  fifths;
- the month's, which stops everything, as a message of its own, never in
  quiet hours;
- every other -- background work's, the night's, extensions' and the parts'
  budgets -- as a line in the next briefing, built with no model call, since
  the briefing's writer may be what the cap stopped.

OpenAI's balance running out (`spend.BALANCE`) is said as the month's cap
is, in the reply or by a message of its own, since it too stops everything.
Until 12 October 2026 a conversation heard only that its turn broke, with
OpenAI's error in it, and work no one was present for stopped unexplained.

Before step 3 the routines' and goals' day budgets said so by message, from
an in-memory set of days, so a restart said it again; the attention loop's
said nothing at all, and the user saw only that it had gone quiet.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta

from core import call_policy, clock, knobs, spend

# What each cap holds, as the user reads it.
WHAT = {
    "MONTH_USD": "everything",
    "CHAT_DAY_USD": "chat",
    "BACKGROUND_DAY_USD": "background work",
    "NIGHT_USD": "overnight work",
    "EXTENSIONS_DAY_USD": "extensions",
    "TRIAGE_BUDGET_USD": "judging what comes in",
    "WORK_BUDGET_USD": "preparing things unasked",
    "ROUTINES_DAY_USD": "routines",
    "GOALS_DAY_USD": "goals",
}
# Where in the app each is raised: its knob's group (ui/src/Autonomy.tsx).
WHERE = {"spend": "Autonomy → Spending caps", "budgets": "Autonomy → Daily budgets"}
# Told in the conversation's reply, and by a message of its own. The rest
# wait for the briefing.
IN_THE_REPLY = ("CHAT_DAY_USD",)
BY_MESSAGE = ("MONTH_USD",)
# Where OpenAI's balance is topped up: nothing in the app can raise it.
TOP_UP = "platform.openai.com, under Settings → Billing"
NO_CREDIT = ("I can't answer: OpenAI says the account's balance is used up. Once it's"
             f" topped up at {TOP_UP}, ask me again.")

# The tools that change a cap (agent/toolset/spending.py), each behind a
# card. A raise spends money, so it takes the high-risk yes (`tier: high` in
# core/consent.yaml, which goes by the tool's name); a lowering, a plain one.
RAISE = "raise_spending_cap"
LOWER = "lower_spending_cap"
# The caps as the tools and the user name them.
NAMED = {"month": "MONTH_USD", **{category: spend.CAPS[category]
                                  for category in spend.CATEGORIES}}
# "RAISE 25", the whole message: the month's cap raised to $25, asked in code
# before any model (`ask_to_raise`). At the cap the model is what is stopped,
# so "raise it to 25" could only be answered "I've reached the limit".
_RAISE = re.compile(r"\s*raise\s+\$?(\d+(?:\.\d+)?)\s*[.!]?\s*", re.IGNORECASE)
# How much higher the month's refusal suggests, in whole dollars.
SUGGEST_MORE_USD = 10


def refused(refusal: spend.Refused, *, partway: bool = False) -> str:
    """What a conversation says in place of an answer: which cap, how much,
    when it resets and where to raise it. At the month's, the reply that
    raises it, which works with no model."""
    cap = refusal.cap
    head = "I stopped partway: " if partway else ""
    resets = _resets(cap, refusal.window.period)
    if cap == "MONTH_USD" and (more := suggested(refusal.limit)) is not None:
        return (f"{head}I've reached {_named(refusal)}. {_reply_raise(more)}, or raise it"
                f" under {WHERE['spend']}. It resets {resets}.")
    return (f"{head}I've reached {_named(refusal)}. It resets {resets}, or you can raise it"
            f" under {WHERE[knobs.KNOBS[cap].group]}.")


def suggested(limit: float) -> float | None:
    """What the month's cap might be raised to: $10 more, in whole dollars,
    within its range. None at the top of it."""
    top = float(knobs.KNOBS["MONTH_USD"].hi)
    return None if limit >= top else min(top, float(int(limit) + SUGGEST_MORE_USD))


def _reply_raise(more: float) -> str:
    return f"Reply RAISE {more:g} to raise it to ${more:.2f}"


def raised_by_word(text: str) -> float | None:
    """The sum in "RAISE 25" when it is the whole message, or None."""
    found = _RAISE.fullmatch(text or "")
    return float(found.group(1)) if found else None


def _cap_words(cap: str) -> str:
    """'the month's spending cap', 'the daily cap for chat'."""
    return ("the month's spending cap" if cap == "MONTH_USD"
            else f"the {_per(cap)} cap for {WHAT[cap]}")


def change_asked(tool: str, cap: str, to_usd: float) -> dict:
    """The card that changes a cap, as {"args", "preview"}, or {"error"}: no
    such cap, a sum outside its range, or a raise that would lower it. The
    card names the cap as it is now, so a yes changes it from that."""
    name = NAMED.get(cap, cap)
    if name not in spend.SETTABLE:
        return {"error": f"there is no {cap!r} cap; the caps are {', '.join(NAMED)}"}
    knob = knobs.KNOBS[name]
    now = spend.limits()[name]
    try:
        to = float(knobs.value(name, to_usd))
    except knobs.BadKnob:
        return {"error": f"{_cap_words(name)} can be from ${float(knob.lo):.2f}"
                         f" to ${float(knob.hi):.2f}"}
    if tool == RAISE and to <= now:
        return {"error": f"{_cap_words(name)} is ${now:.2f} already; a raise takes it higher"}
    if tool == LOWER and to >= now:
        return {"error": f"{_cap_words(name)} is ${now:.2f}; a lowering takes it lower"}
    verb = "Raise" if tool == RAISE else "Lower"
    return {"args": {"cap": name, "from_usd": now, "to_usd": to},
            "preview": f"{verb} {_cap_words(name)} from ${now:.2f} to ${to:.2f}"}


def change(cap: str, from_usd: float, to_usd: float) -> dict:
    """A yes to a card from `change_asked`: the cap set, if it is still what
    the card said. Changed since, on its page or by another card, the yes
    was to a change from a sum that is not there any more."""
    now = spend.limits()[cap]
    if round(now, 2) != round(from_usd, 2):
        return {"ok": False, "error": f"{_cap_words(cap)} is ${now:.2f} now, not the"
                                      f" ${from_usd:.2f} the card said; ask again"}
    return {"ok": True, "cap": cap, "from_usd": from_usd, "to_usd": spend.set_cap(cap, to_usd)}


def caps_now(conn: sqlite3.Connection, now: datetime | None = None) -> list[dict]:
    """Each cap a card may change: what it holds, its limit, what is spent
    and left, when it resets, and the range it may be set within."""
    limits = spend.limits()
    span = spend.windows(now)
    out = []
    for word, cap in NAMED.items():
        window = spend.window_for(cap, span)
        used = spend.spent(conn, window.since, category=None if cap == "MONTH_USD" else word)
        knob = knobs.KNOBS[cap]
        out.append({"cap": word, "holds": WHAT[cap], "limit_usd": limits[cap],
                    "spent_usd": round(used, 2),
                    "left_usd": round(max(0.0, limits[cap] - used), 2),
                    "resets": _resets(cap, window.period),
                    "range_usd": [float(knob.lo), float(knob.hi)]})
    return out


async def ask_to_raise(conn: sqlite3.Connection, usd: float, *, session_id: int | None,
                       channel: str) -> str:
    """"RAISE 25": the card raising the month's cap to $25, made with no
    model, and what the reply says. Asked when the turn ends, as every card
    a turn makes is (`approvals.held`)."""
    from agent import approvals
    from core import monitor
    from core.consent import classify

    asked = change_asked(RAISE, "month", usd)
    if "error" in asked:
        return asked["error"][:1].upper() + asked["error"][1:] + "."
    if monitor.may_ask(conn, RAISE):
        # One high-risk card at a time: their code answers the one in front of them.
        return ("Another card is waiting for your yes and your code. Answer it first, then"
                " send RAISE again.")
    receipt = await approvals.request(conn, tool=RAISE, args=asked["args"],
                                      preview=asked["preview"],
                                      consent=classify(RAISE, asked["args"]),
                                      session_id=session_id, channel=channel)
    if not approvals.waiting(receipt):
        return f"I couldn't ask for that: {receipt.get('error', 'it was refused')}."
    return f"I've asked for your yes: {asked['preview'][:1].lower()}{asked['preview'][1:]}."


def reached(refusal: spend.Refused) -> str:
    """Why work stopped, for a report: "tonight's $2.00 limit for overnight
    work was reached"."""
    return f"{_named(refusal)} was reached"


def _named(refusal: spend.Refused) -> str:
    cap = refusal.cap
    return (f"{_this(cap)} ${refusal.limit:.2f} limit"
            f"{'' if cap == 'MONTH_USD' else ' for ' + WHAT[cap]}")


def told_in_the_reply(conn: sqlite3.Connection, refusal: spend.Refused) -> None:
    """The refusal just said: no one needs to say it again."""
    spend.told(conn, spend.notices(conn, caps=[refusal.cap], untold=True))


def no_credit_told(conn: sqlite3.Connection) -> None:
    """`NO_CREDIT` just said in a reply: no message needs to follow it."""
    spend.told(conn, spend.notices(conn, caps=[spend.BALANCE], untold=True))


def warning_for_the_reply(conn: sqlite3.Connection) -> str:
    """The line after a conversation's answer when this turn took its cap
    past four fifths, or nothing. Told once: the next turn adds nothing."""
    found = [n for n in spend.notices(conn, caps=IN_THE_REPLY, untold=True, current_only=True)
             if n.kind == "warned"]
    if not found:
        return ""
    spend.told(conn, found)
    last = found[-1]
    return (f"\n\n(Spending on {WHAT[last.cap]} is at"
            f" {_of(last, f'{_this(last.cap)} ${last.limit:.2f} limit')}.)")


def for_the_briefing(conn: sqlite3.Connection) -> list[spend.Notice]:
    """The notices the next briefing passes on, not yet told: every cap's
    but the reply's, and of the month's only its four fifths, unless its ask
    or its stop at the same amount says more. The month's warning was a
    message of its own, and a $15 month pushed twice, $2 apart."""
    found = spend.notices(conn, caps=[c for c in WHAT if c not in IN_THE_REPLY], untold=True)
    pushed = {(n.period, n.limit) for n in found if n.cap in BY_MESSAGE and n.kind != "warned"}
    return [n for n in found if n.cap not in BY_MESSAGE
            or (n.kind == "warned" and (n.period, n.limit) not in pushed)]


def _in_force(notice: spend.Notice, limits: dict[str, float]) -> bool:
    """Whether a notice is about the cap as it stands: one written at an
    amount since raised past is about a cap that is not there any more."""
    return notice.cap not in limits or notice.limit >= limits[notice.cap] - 0.005


def lines(notices: list[spend.Notice], now: datetime | None = None) -> list[str]:
    """The notices as lines, a cap's stop standing for its warning."""
    stopped = {(n.cap, n.period) for n in notices if n.kind == "stopped"}
    return [_line(n, now) for n in notices
            if n.kind == "stopped" or (n.cap, n.period) not in stopped]


async def by_message(conn: sqlite3.Connection, notify: Callable[[str], Awaitable[bool]],
                     now: datetime | None = None) -> int:
    """Send the month's ask, $1 before its cap, or its stop, and OpenAI's
    balance spent, unless it is quiet hours, and mark them told once sent;
    the month's four fifths goes to the briefing (`for_the_briefing`) unless
    one of these says more. Returns how many notices went."""
    if call_policy.within_quiet_hours(now):
        return 0
    sent = 0
    spent = spend.notices(conn, caps=[spend.BALANCE], untold=True)
    if spent and await notify(_balance(spent[-1], now)):
        spend.told(conn, spent)
        sent += len(spent)
    found = spend.notices(conn, caps=BY_MESSAGE, untold=True)
    limits = spend.limits()
    # A stop or an ask at an amount since raised past is not said: the cap
    # it was about is not there any more.
    stale = [n for n in found if n.kind != "warned" and not _in_force(n, limits)]
    spend.told(conn, stale)
    pushed = [n for n in found if n.kind != "warned" and n not in stale]
    if not pushed:
        return sent
    stops = [n for n in pushed if n.kind == "stopped"]
    last = (stops or pushed)[-1]
    more = suggested(last.limit)
    resets = _resets(last.cap, last.period)
    if last.kind == "stopped":
        how = (f"{_reply_raise(more)}, or raise it under {WHERE['spend']}" if more is not None
               else f"It can't go higher than ${last.limit:.2f}")
        text = (f"I've reached this month's ${last.limit:.2f} limit, so everything is paused,"
                f" chat included. {how}. It resets {resets}.")
    elif more is not None:
        text = (f"This month's spending is at {_of(last, f'${last.limit:.2f}')}. Want to raise"
                f" the cap? Reply RAISE and the new cap, like RAISE {more:g}, or just tell me.")
    else:
        text = (f"This month's spending is at {_of(last, f'${last.limit:.2f}')}, as high as"
                f" the cap goes. It resets {resets}.")
    if not await notify(text):
        return sent
    # The four fifths too: the ask or the stop says more.
    spend.told(conn, [n for n in found if n not in stale])
    return sent + len(found) - len(stale)


def standing(conn: sqlite3.Connection) -> list[dict]:
    """This period's notices, told or not, for the app's banner and the
    stopped note: a cap's warning and its stop, as the cap stands. Raised
    since, the stop's note is gone; the ask was a message, not a state."""
    limits = spend.limits()
    found = [n for n in spend.notices(conn, current_only=True)
             if n.kind != "asked" and _in_force(n, limits)]
    stopped = {(n.cap, n.period) for n in found if n.kind == "stopped"}
    return [{"kind": n.kind, "cap": n.cap, "period": n.period, "at": n.at, "text": _line(n)}
            for n in found if n.kind == "stopped" or (n.cap, n.period) not in stopped]


def _this(cap: str) -> str:
    if cap == "MONTH_USD":
        return "this month's"
    return "tonight's" if cap == spend.CAPS["night"] else "today's"


def _per(cap: str) -> str:
    if cap == "MONTH_USD":
        return "monthly"
    return "nightly" if cap == spend.CAPS["night"] else "daily"


def _resets(cap: str, period: str) -> str:
    """When a cap's period ends, from its name: "2026-10", or a day's or a
    night's date."""
    if cap == "MONTH_USD":
        first = date.fromisoformat(f"{period}-01")
        following = (first + timedelta(days=32)).replace(day=1)
        return f"on {following.day} {following:%B}"
    return "tomorrow night" if cap == spend.CAPS["night"] else "at midnight"


def _line(notice: spend.Notice, now: datetime | None = None) -> str:
    """'Background work paused at 3:10pm: its $2.50 daily limit was reached.',
    or 'At 2:40pm, background work had spent $2.00 of its $2.50 daily limit.'"""
    if notice.cap == spend.BALANCE:
        return (f"At {_when(notice.at, now)}, OpenAI refused a request: the account's"
                f" balance was used up. It is topped up at {TOP_UP}.")
    what = WHAT[notice.cap]
    limit = f"its ${notice.limit:.2f} {_per(notice.cap)} limit"
    when = _when(notice.at, now)
    if notice.kind == "stopped":
        return f"{what[0].upper()}{what[1:]} paused at {when}: {limit} was reached."
    if notice.cap == "MONTH_USD":
        return f"At {when}, this month's spending was at {_of(notice, limit)}."
    return f"At {when}, {what} had spent {_of(notice, limit)}."


def _balance(notice: spend.Notice, now: datetime | None = None) -> str:
    """The message when OpenAI's balance ran out, with when: it may have been
    in the night, and topped up since."""
    return (f"OpenAI refused a request at {_when(notice.at, now)} because the account's"
            f" balance is used up. Until it's topped up at {TOP_UP}, I can't answer or"
            f" do anything else that needs a model.")


def _of(notice: spend.Notice, limit: str) -> str:
    """"$2.00 of its $2.50 daily limit", or "$2.60, past its $1.00 daily limit".

    A warning can be written past its limit: one bill can carry a cap from
    under four fifths to beyond it, and a part's cap can be passed while its
    category's stopped the work first, so no stop of its own follows. "$2.60
    of its $1.00 daily limit" is what the live check of 12 October 2026 read.
    """
    return f"${notice.spent:.2f}{' of ' if notice.spent < notice.limit else ', past '}{limit}"


def _when(at: str, now: datetime | None = None) -> str:
    """A moment as the user's clock reads it: "3:10pm", and "yesterday" or
    the day when it was not today."""
    zone = clock.local_zone()
    moment = clock.moment(at).astimezone(zone)
    now = now or datetime.now(zone)
    # A naive moment is the user's wall clock, as the briefing's is.
    today = (now.replace(tzinfo=zone) if now.tzinfo is None else now.astimezone(zone)).date()
    said = moment.strftime("%I:%M%p").lstrip("0").lower()
    if moment.date() == today:
        return said
    if moment.date() == today - timedelta(days=1):
        return f"{said} yesterday"
    return f"{said} on {moment:%A}"
