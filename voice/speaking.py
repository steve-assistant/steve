"""What the desk says while a reply is on its way, and what the user actually heard.

Three things voice assistants do that the desk did not (7 October 2026, from
how LiveKit, Pipecat, Retell and OpenAI's own voice guide describe them):

- **A reply is spoken as it is written.** The desk waited for the whole
  reply before saying a word. Now the model's text is streamed in, cut into
  sentences as they complete (`Sentences`), and each is spoken while the next
  is still being written. A sentence that claims something was done --
  "Added it to your calendar" -- is held back until the reply has been
  checked (agent/verify.py), since the check may correct it; everything
  after it waits with it.
- **A few words before a lookup, about the lookup.** "Checking your
  calendar." when the model reaches for the calendar (`preamble_for`), not a
  timer's "One sec." before an answer that needed no lookup at all. A short
  generic line remains for a slow reply with no tool.
- **What was cut off was not said.** When the user cuts in, only what the speaker
  played is kept as said (`heard`); the history and the record get that,
  and no more (agent/loop.Conversation.cut_short).
"""

from __future__ import annotations

import re

# The first tool a turn reaches for, and what is said as it does: short,
# specific, the voice guide's advice -- "I'll check that order now", never
# "I am now going to access the tool".
_PREAMBLES: tuple[tuple[str, frozenset[str]], ...] = (
    ("Checking your calendar.", frozenset({
        "calendar_agenda", "calendar_free", "calendar_search", "due_dates"})),
    ("Looking through your mail.", frozenset({"search_email", "read_email", "email_thread"})),
    ("Checking the forecast.", frozenset({"weather"})),
    ("Looking that up.", frozenset({
        "web_search", "read_page", "search_and_read", "research", "news_summary",
        "browse_open", "browse_search", "linkedin_profile", "linkedin_search_people",
        "linkedin_company", "linkedin_jobs"})),
    ("Checking your files.", frozenset({"search_drive", "read_drive_file",
                                         "read_spreadsheet"})),
    ("Let me check.", frozenset({"recall", "who_is", "my_commitments", "find_notes",
                                  "list_reminders", "list_tasks"})),
)
PREAMBLE_LINES = tuple(line for line, _ in _PREAMBLES)


def preamble_for(tool: str) -> str | None:
    """The line to say as `tool` is reached for, or None: a tool that acts
    (a draft, an event) is reported when it is done, not announced."""
    return next((line for line, tools in _PREAMBLES if tool in tools), None)


_END = re.compile(r"[.!?](?=\s)|\n\s*\n")


class Sentences:
    """Streamed text in, whole sentences out, at least `min_chars` long --
    the same rule `voice.tts.split_for_streaming` uses on a whole reply."""

    def __init__(self, min_chars: int = 24) -> None:
        self.min_chars = min_chars
        self._buffer = ""

    def feed(self, delta: str) -> list[str]:
        self._buffer += delta
        out: list[str] = []
        start = 0
        for end in _END.finditer(self._buffer):
            piece = self._buffer[start:end.end()]
            if len(piece.strip()) >= self.min_chars:
                out.append(piece.strip())
                start = end.end()
        self._buffer = self._buffer[start:]
        return out

    def rest(self) -> str:
        """What is left once the text has stopped coming."""
        left, self._buffer = self._buffer.strip(), ""
        return left


def claims_an_effect(sentence: str) -> bool:
    """Whether a sentence says something was done -- which the check after
    the reply (agent/verify.py) may yet correct."""
    from agent import verify

    if verify._NOT_A_CLAIM.search(sentence):
        return False
    return any(pattern.search(sentence) for pattern, _, _ in verify.EFFECTS)


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def rest_of(final: str, spoken: list[str]) -> str:
    """What of the final reply is still to be said, given the sentences
    already spoken from its stream. The reply as checked is the one that
    counts: when it begins with what was said, the rest of it; when the
    check changed it, whatever of it was not already said."""
    said = [w for s in spoken for w in _words(s)]
    if not said:
        return final
    matches = list(re.finditer(r"[a-z0-9']+", final.lower()))
    if [m.group(0) for m in matches[:len(said)]] == said:
        if len(matches) <= len(said):
            return ""
        return final[matches[len(said) - 1].end():].lstrip(" ,;:-—.!?").strip()
    from voice.tts import split_for_streaming

    # Sentence by sentence, however short: chunks merge short sentences, and
    # a merged chunk never matches what was said of it.
    already = {" ".join(_words(sentence)) for said_piece in spoken
               for sentence in split_for_streaming(said_piece, min_chars=1)}
    return " ".join(sentence for sentence in split_for_streaming(final, min_chars=1)
                    if " ".join(_words(sentence)) not in already)


def heard(said: list[tuple[str | None, float]], unplayed_s: float) -> str:
    """What the user heard of what was queued -- (text, seconds) in order, a line
    of its own as None text -- with `unplayed_s` still waiting when the user cut
    in. A sentence counts as heard once more than half of it was played."""
    played = max(0.0, sum(seconds for _, seconds in said) - max(0.0, unplayed_s))
    out, at = [], 0.0
    for text, seconds in said:
        if at + seconds / 2 > played:
            break
        if text:
            out.append(text)
        at += seconds
    return " ".join(out)
