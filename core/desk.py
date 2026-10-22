"""What the desk's open microphone may hear and pass on, decided where the
agent cannot change it.

The maintainer, 5 October 2026: an always-on voice assistant at the laptop, "hey
Steve", that can take notes, answer, set things up and send email -- "but
obviously don't want everything I say going to the AI". The user chose that a
spoken "yes maple" approves a card here as it does on the phone.

The microphone is open all day, so the risk is not their words but sound the user
did not make. The one path by which somebody elsewhere could instruct the
agent is the laptop's own speakers: a Zoom participant, a video, the agent
reading out an email that says "hey Steve, forward everything to...". So:

- **A wake the laptop itself played is not theirs** (`wake_counts`). The wake
  detector runs on the speakers' output as well as the mic
  (`voice/listen.py`); a wake heard on both within ECHO_S is the laptop's.
- **What follows a wake counts only if it starts with the phrase**
  (`wake_phrase`): Whisper, locally, must hear "hey Steve" at the start of
  the capture, or the wake was false and the capture is dropped, unread by
  any model. Before a wake nothing is transcribed at all.
- **A capture with no wake word** -- a follow-up in the moments after a
  reply -- **counts only if the laptop was not itself talking**
  (`capture_counts`): otherwise a video's next sentence would be a request.
- **An approval heard hands-free counts only with the laptop quiet**
  (`approval_counts`): a "yes maple" mixed with a video's speech goes to
  the screen instead.
- **A yes handed over by a live conversation counts only if the microphone
  heard somebody say it** (`heard_user`): since 7 October 2026 the
  microphone stays open, echo-cancelled, while the desk's voice talks, so
  GPT-Live's own "say yes maple" can come back as an input transcript.
  Speech inside its own voice's window, plus the echo tail, is not them.
- **The agent never says its own wake phrase aloud** (`never_spoken`).
- **The microphone is closed while the user is away** (`may_listen`): switched
  off, the screen locked, or no one signed in.
- **The desk's own words, heard back, are not theirs** (`own_words_removed`):
  a capture that overlapped the desk's own voice is judged on what is left
  once the words the desk itself was saying are taken out. On 9 October
  2026 "hey Steve, stop" over an answer came back from Whisper mixed with
  the desk's voice, and a live conversation's transcript of the room
  handed "Today, you have two" on as theirs.
- **Speech over the desk's voice without the wake phrase** counts only by
  `barge_in_counts`, which is off (`BARGE_IN_WITHOUT_WAKE`) until the echo
  canceller has been measured on their speakers.

Audio is never written down: the listener holds it in memory, in a ring
buffer of seconds, and keeps only text. That and "nothing before a wake"
are properties of `voice/listen.py`, held by tests/gate/test_desk.py.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# How far apart a wake on the speakers' output and one on the mic may be
# and still be the same sound: output latency, the room, both detectors'
# 80 ms steps, with room to spare.
ECHO_S = 2.0
# Speech the laptop may play during a capture with no wake word, or one
# carrying an approval, before the capture is taken to be partly the laptop's.
LAPTOP_SPEECH_S = 0.5
# A wake's capture: how much before the wake is kept (the phrase itself),
# and the longest a request may run.
PRE_ROLL_S = 1.6
MAX_CAPTURE_S = 20.0
# After a reply, how long it listens for a follow-up without the wake word.
# It was 6 s; on 7 October 2026 the user found themselves unsure whether it wanted
# "hey Steve" before everything -- an answer begun after a moment's thought
# was past the window. A follow-up still counts only with the laptop quiet.
FOLLOW_UP_S = 10.0
# After a live conversation's own voice stops, how long the microphone is
# taken to still hold its echo; and how recent their speech must be for a yes
# it handed over (`heard_user`).
OWN_VOICE_TAIL_S = 0.35
HEARD_USER_S = 8.0

# "hey Steve", as Whisper writes it and as people say it ("hi", "ay").
_LEAD = r"(?:hey|hi|hay|hei|ay|eh|a)"
_NAME = r"(?:steve|steev|steeve|stieve)"
_PHRASE = re.compile(rf"\b{_LEAD}\W+{_NAME}\b", re.IGNORECASE)
# The pre-roll may hold the tail of whatever came before the phrase.
_MAX_WORDS_BEFORE = 4


def wake_counts(heard_at: float, laptop_wakes: Iterable[float]) -> bool:
    """Whether a wake heard on the mic at `heard_at` was theirs: not one the
    laptop's own output also had within ECHO_S. Both on one clock."""
    return not any(abs(heard_at - at) <= ECHO_S for at in laptop_wakes)


def wake_phrase(transcript: str) -> str | None:
    """The request after "hey Steve", or None if the transcript does not
    start with the phrase -- a false wake, to be dropped unread. An empty
    string is the phrase alone: the user is waiting to be asked."""
    match = _PHRASE.search(transcript or "")
    if match is None:
        return None
    if len(re.findall(r"\w+", transcript[:match.start()])) > _MAX_WORDS_BEFORE:
        return None
    return transcript[match.end():].lstrip(" ,.!?;:-–—").strip()


def capture_counts(*, woke: bool, laptop_speech_s: float) -> bool:
    """Whether a capture may be taken as theirs. One that began with a wake
    that counted is; one without (a follow-up) only if the laptop was not
    talking through it."""
    return woke or laptop_speech_s < LAPTOP_SPEECH_S


def approval_counts(*, laptop_speech_s: float) -> bool:
    """Whether a "yes <word>" heard hands-free may approve a card: only if
    the laptop played no speech of its own while it was said."""
    return laptop_speech_s < LAPTOP_SPEECH_S


def heard_user(*, mic_speech_at: float, own_voice_until: float, now: float) -> bool:
    """Whether the microphone heard speech that was not the desk's own voice,
    recently enough to be the words a live conversation is handing over.

    Audit of 8 October 2026: full duplex on the speakers retired the old
    guarantee that a conversation cannot hear itself -- the microphone used
    to be zeros while it talked -- and it now rests on the echo canceller
    alone. The laptop-side check (`approval_counts`) looks at laptop speech
    after the conversation's own voice, so GPT-Live saying "say yes maple"
    and its echo coming back as "yes maple" passed it. So an approval-shaped
    handoff also needs the microphone's voice detector to have fired after
    the conversation's own voice ended plus OWN_VOICE_TAIL_S, within
    HEARD_USER_S: speech nobody could have mistaken for its own echo. All
    three on one clock (`time.monotonic`)."""
    since = max(now - HEARD_USER_S, own_voice_until + OWN_VOICE_TAIL_S)
    return mic_speech_at >= since


def never_spoken(text: str) -> str:
    """Text the agent is about to say, without its wake phrase: "Hey Steve,
    forward..." in an email it reads out becomes "Steve, forward...". The
    loopback check would catch it anyway; this is the second lock."""
    return _PHRASE.sub(lambda m: re.sub(rf"^{_LEAD}\W+", "", m.group(0), flags=re.I), text or "")


def may_listen(*, switched_on: bool, locked: bool, signed_in: bool) -> bool:
    """Whether the microphone may be open: the user switched it on, and is at the
    laptop -- signed in, the screen not locked. `signed_in` has no default:
    with True as one, no caller passed it, and a press opened the microphone
    for anything that held the token."""
    return switched_on and signed_in and not locked


# The desk's own words in a capture: runs of this many words or more that it
# was itself saying are its echo, not theirs. Three, not two: "you have" is in
# half of what anyone says; "today you have" is a quotation.
OWN_WORDS_RUN = 3
# How far back, before a capture ends, the desk's own words are compared.
OWN_WORDS_S = 12.0


def _norm(token: str) -> str:
    return re.sub(r"[^\w']", "", token.lower()).replace("'", "")


def own_words_removed(heard: str, said: str) -> str:
    """What of a capture is theirs when it overlapped the desk's own voice: the
    transcript with every word taken out that belongs to a run of
    OWN_WORDS_RUN or more words the desk itself was saying (`said`, its
    sentences from the last OWN_WORDS_S). What is left is judged as the
    request; nothing left means it was the desk hearing itself.

    The echo canceller takes most of its voice out of the microphone, not
    all of it, and Whisper writes down whatever is there. A request made of
    the desk's own sentence would have it answer -- or act on -- itself."""
    tokens = (heard or "").split()
    norm = [_norm(t) for t in tokens]
    said_norm = [w for w in (_norm(t) for t in (said or "").split()) if w]
    n = OWN_WORDS_RUN
    runs = {tuple(said_norm[i:i + n]) for i in range(len(said_norm) - n + 1)}
    if not runs:
        return " ".join(tokens)
    # Positions of the words that carry text, so punctuation alone does not
    # break a run.
    words = [i for i, w in enumerate(norm) if w]
    own: set[int] = set()
    for k in range(len(words) - n + 1):
        window = words[k:k + n]
        if tuple(norm[i] for i in window) in runs:
            own.update(window)
    return " ".join(t for i, t in enumerate(tokens) if i not in own and norm[i])


# Speech over the desk's own voice, without the wake phrase, as a request
# (decision D2, 9 October 2026). Off until a sound test has logged how much
# of its voice the canceller leaves on their speakers: turning it on is a
# reviewed change to this line.
BARGE_IN_WITHOUT_WAKE = False


def barge_in_counts(*, other_laptop_speech_s: float, user_words: str) -> bool:
    """Whether speech the user made over the desk's own voice, with no wake phrase,
    may be taken as a request: only with BARGE_IN_WITHOUT_WAKE on, no other
    laptop speech (a video, a call) through it, and words of theirs left once
    the desk's own are taken out (`own_words_removed`)."""
    return (BARGE_IN_WITHOUT_WAKE and other_laptop_speech_s < LAPTOP_SPEECH_S
            and bool((user_words or "").strip()))
