"""A rehearsal's script: what the user says and when, what answers them, the room, and
what must have happened.

A scenario is written the way sound tests are described -- "say hey
Steve, wait for the tick, ask the weather", "six seconds into the answer,
say hey Steve, stop" -- so a line's time can hang on what the desk did
rather than on a guess at its latency: `voice+6` is six seconds after the
desk's voice was first heard after the user's previous line. Times guessed in
seconds broke the first time Whisper was slow, and a rehearsal that says
"hey Steve, stop" before the answer has started proves nothing about
stopping one.

    scenario stop-long-answer
    about "hey Steve, stop" over a long answer, at normal volume
    length 45
    reply /history of tea/ 3.0 research: Tea was first drunk in China ...
      ... a line that starts with a space carries on the one before
    0.5      user: Hey Steve, tell me about the history of tea.
    voice+6  user!: Hey Steve, stop.
    expect cut-in <= 1.0

Statements, one a line (`#` starts a comment):

    scenario <name>            about <text>             length <seconds>
    voice <who> <kokoro voice> level <who> <dB>
    room <knob> <value>        (devtools/rehearse/room.Acoustics' fields)
    stall <at> <seconds>       the speaker stops calling back
    reply /<regex>/ <delay> [<tool>]: <text>     the assistant's answer
    live <knob> <value>        the fake GPT-Live: says, first, noise, delegate
    expect <check> [<args>]    (CHECKS)
    <when> <who>[!]: <text>    who is user, other or video; ! marks the
                               line the latency checks measure from
    <when> key[!]: press       push-to-talk's key, pressed or let go
    <when> key[!]: release     (devtools/rehearse/keys.py rehearses it)

When: `12.5` from the start, `+1.5` after the user's previous line ended, or
`<anchor>+<s>` after the desk did something following the user's previous line
(ANCHORS).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

WHO = ("user", "other", "video", "key")
KEY = ("press", "release")
# What a line's time can hang on, once the user's previous line has ended:
#   voice  -- the desk's own voice (a turn's) is first audible
#   live   -- a live conversation's voice is first audible
#   idle   -- the desk is back waiting for the phrase
#   follow -- the follow-up window has opened
#   quiet  -- nothing of the desk's has been audible for half a second
ANCHORS = ("voice", "live", "idle", "follow", "quiet")
ROOM_KNOBS = ("echo", "delay", "rt60", "reverb", "drive", "highpass", "noise")
LIVE_KNOBS = ("says", "first", "noise", "delegate", "vad")
# Each check and how many arguments it takes: a number, or "text" for the
# rest of the line, or a comparison "<op> <number>".
CHECKS = {
    "asked": "text",          # the assistant was asked something matching
    "not-asked": 0,           # ...or nothing at all
    "outcome": 1,             # a turn ended so: a|b|c
    "no-outcome": 1,          # no turn ended so
    "first-audio": "cmp",     # from the marked line's end to the desk's voice
    "cut-in": "cmp",          # from the marked line's wake to the desk's silence
    "live": 1,                # a live conversation ended for a|b
    "no-live": 0,             # none opened
    "live-voice": "cmp",      # seconds a live conversation was audible
    "one-voice": 0,           # no sound outside the floor, nothing said twice
    "end-mode": 1,            # where the desk was at the end: a|b
    "wakes": "cmp",           # wakes the ear counted as the user's
    "laptop-wakes": "cmp",    # wakes it heard and took for the laptop's
    "transcribed": "cmp",     # captures Whisper was given
    "silent-after": "cmp",    # nothing of the desk's audible this long after the marked line
    # Push-to-talk's (devtools/rehearse/keys.py):
    "heard": "text",          # OpenAI heard something matching
    "silenced": "cmp",        # from the marked press to the silence of the reply it cut
    "mic-live": "cmp",        # from a press to its live microphone, the slowest
    "mic-shut": "cmp",        # from the marked release to the microphone closed
    "tick-live": 0,           # each press's tick sounded once its microphone was live
}
OPS = ("<=", ">=", "<", ">", "==")


class ScriptError(ValueError):
    def __init__(self, line: int, message: str) -> None:
        super().__init__(f"line {line}: {message}")
        self.line = line


@dataclass(frozen=True)
class When:
    """A line's time: `anchor` None for seconds from the start, "+" for after
    the user's previous line, or one of ANCHORS."""

    anchor: str | None
    offset: float


@dataclass
class Line:
    when: When
    who: str
    text: str
    mark: bool = False


@dataclass
class Reply:
    match: str
    delay: float
    tool: str | None
    text: str


@dataclass
class Expect:
    check: str
    args: tuple
    line: int


@dataclass
class Scenario:
    name: str = ""
    about: str = ""
    length: float = 30.0
    lines: list[Line] = field(default_factory=list)
    replies: list[Reply] = field(default_factory=list)
    expects: list[Expect] = field(default_factory=list)
    room: dict = field(default_factory=dict)
    stalls: list[tuple[float, float]] = field(default_factory=list)
    live: dict = field(default_factory=dict)
    voices: dict = field(default_factory=dict)
    levels: dict = field(default_factory=dict)

    @property
    def marked(self) -> Line | None:
        return next((line for line in self.lines if line.mark), None)


_TIMED = re.compile(r"^(?P<when>[+]?\d+(?:\.\d+)?|(?P<anchor>[a-z]+)\+(?P<off>\d+(?:\.\d+)?))\s+"
                    r"(?P<who>[a-z]+)(?P<mark>!?):\s*(?P<text>.*)$")
_REPLY = re.compile(r"^/(?P<match>.+)/\s+(?P<delay>\d+(?:\.\d+)?)(?:\s+(?P<tool>[\w]+))?:\s*"
                    r"(?P<text>.*)$")


def _number(text: str, where: int, what: str) -> float:
    try:
        return float(text)
    except ValueError:
        raise ScriptError(where, f"{what} must be a number, not {text!r}") from None


def _statements(text: str) -> list[tuple[int, str]]:
    """The script's statements with their first line's number: comments
    dropped, continuation lines (starting with a space) joined on."""
    out: list[tuple[int, str]] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].rstrip() if not raw.lstrip().startswith("#") else ""
        if not line.strip():
            continue
        if line[0].isspace():
            if not out:
                raise ScriptError(number, "a continuation line with nothing to continue")
            first, said = out[-1]
            out[-1] = (first, f"{said} {line.strip()}")
            continue
        out.append((number, line.strip()))
    return out


def parse(text: str) -> Scenario:
    """A scenario from its script. Raises ScriptError, naming the line."""
    scenario = Scenario()
    for number, statement in _statements(text):
        if timed := _TIMED.match(statement):
            scenario.lines.append(_line(timed, number))
            continue
        keyword, _, rest = statement.partition(" ")
        rest = rest.strip()
        if keyword == "scenario":
            scenario.name = rest
        elif keyword == "about":
            scenario.about = rest
        elif keyword == "length":
            scenario.length = _number(rest, number, "length")
        elif keyword in ("voice", "level"):
            who, _, value = rest.partition(" ")
            if who not in WHO or not value.strip():
                raise ScriptError(number, f"{keyword} wants one of {WHO} and a value")
            if keyword == "voice":
                scenario.voices[who] = value.strip()
            else:
                scenario.levels[who] = _number(value.strip(), number, "level")
        elif keyword == "room":
            knob, _, value = rest.partition(" ")
            if knob not in ROOM_KNOBS:
                raise ScriptError(number, f"no room knob {knob!r}: {ROOM_KNOBS}")
            scenario.room[knob] = _number(value.strip(), number, knob)
        elif keyword == "stall":
            parts = rest.split()
            if len(parts) != 2:
                raise ScriptError(number, "stall wants <at> <seconds>")
            scenario.stalls.append((_number(parts[0], number, "at"),
                                    _number(parts[1], number, "seconds")))
        elif keyword == "reply":
            reply = _REPLY.match(rest)
            if reply is None:
                raise ScriptError(number, "reply wants /regex/ <delay> [tool]: <text>")
            try:
                re.compile(reply["match"])
            except re.error as exc:
                raise ScriptError(number, f"bad regex: {exc}") from None
            scenario.replies.append(Reply(reply["match"], float(reply["delay"]), reply["tool"],
                                          reply["text"].strip()))
        elif keyword == "live":
            knob, _, value = rest.partition(" ")
            value = value.strip().lstrip(":").strip()
            if knob not in LIVE_KNOBS:
                raise ScriptError(number, f"no live knob {knob!r}: {LIVE_KNOBS}")
            if knob == "says":
                scenario.live.setdefault("says", []).append(value)
            elif knob == "delegate":
                scenario.live["delegate"] = value.lower() in ("yes", "true", "1")
            else:
                scenario.live[knob] = _number(value, number, knob)
        elif keyword == "expect":
            scenario.expects.append(_expect(rest, number))
        else:
            raise ScriptError(number, f"not a statement: {statement!r}")
    if not scenario.name:
        raise ScriptError(1, "a scenario needs a name (scenario <name>)")
    if sum(line.mark for line in scenario.lines) > 1:
        raise ScriptError(1, "only one line may be marked with !")
    return scenario


def _line(match: re.Match, number: int) -> Line:
    who = match["who"]
    if who not in WHO:
        raise ScriptError(number, f"who is one of {WHO}, not {who!r}")
    if match["anchor"]:
        if match["anchor"] not in ANCHORS:
            raise ScriptError(number, f"no anchor {match['anchor']!r}: {ANCHORS}")
        when = When(match["anchor"], float(match["off"]))
    elif match["when"].startswith("+"):
        when = When("+", float(match["when"][1:]))
    else:
        when = When(None, float(match["when"]))
    text = match["text"].strip()
    if not text:
        raise ScriptError(number, "a line needs words")
    if who == "key" and text not in KEY:
        raise ScriptError(number, f"the key is {' or '.join(KEY)}, not {text!r}")
    return Line(when=when, who=who, text=text, mark=bool(match["mark"]))


def _expect(rest: str, number: int) -> Expect:
    check, _, args = rest.partition(" ")
    args = args.strip()
    if check not in CHECKS:
        raise ScriptError(number, f"no check {check!r}: {sorted(CHECKS)}")
    kind = CHECKS[check]
    if kind == "text":
        if not args:
            raise ScriptError(number, f"{check} wants a pattern")
        return Expect(check, (args,), number)
    if kind == "cmp":
        parts = args.split()
        if len(parts) != 2 or parts[0] not in OPS:
            raise ScriptError(number, f"{check} wants <op> <number>, op one of {OPS}")
        return Expect(check, (parts[0], _number(parts[1], number, check)), number)
    parts = args.split() if args else []
    if len(parts) != kind:
        raise ScriptError(number, f"{check} takes {kind} argument(s)")
    return Expect(check, tuple(parts), number)


def compare(value: float | None, op: str, bound: float) -> bool:
    """`value op bound`; a value that never came is a failure."""
    if value is None:
        return False
    return {"<=": value <= bound, ">=": value >= bound, "<": value < bound,
            ">": value > bound, "==": value == bound}[op]
