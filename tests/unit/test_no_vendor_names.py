"""Gate: the providers Steve dropped stay dropped, and OpenRouter stays Jev's.

Steve began with six services behind its models: OpenRouter for Jev (the
decision model) and the coder's GLM, Z.ai behind GLM, Deepgram and Cartesia
for voice, and fastembed's local embedder; and OpenCode, the harness that
ran the coder when the sandbox was not set up, with its edits run as the
user. Step 3 leaves OpenAI for the models and its embeddings in fastembed's
(`docs/plan/03-models-and-spend.md`). It meant to leave OpenAI's Decisions
API in Jev's place too, and banned both names here; measured as the screen,
it caught 12 of the 50 attacks Jev caught 40 of, so Jev stayed, and its
OpenRouter key became the second a user needs (12 October 2026). Brave
stays too, as an optional key for faster web search: OpenAI's search is the
default (core/websearch.py).

What a dropped service leaves behind is not dead text. A fallback that
reaches for a key nobody sets is how a fresh install's first message went to
OpenRouter with a stand-in key and came back as an error about a service the
user had never heard of (audit C6). An attribution header is how the
owner's name went out on every request (audit H3). And the next feature is
written by copying the code beside it.

So the dropped names are refused everywhere outside the plans, and
OpenRouter's address is allowed only where Jev is asked (`core/decide.py`,
and its captured replies) and where its credit is checked: a second
OpenRouter call would make that key carry chat again, the C6 failure. An
exempt file that is gone, or no longer reaches OpenRouter, fails too.
"""

from __future__ import annotations

import re

import pytest

from tests.gate import repo_files

# The plans tell the history, and are private, as nothing here is.
NOT_SCANNED = ("private/",)
SELF = "tests/unit/test_no_vendor_names.py"

# "zai" as a name, not inside other words, but in `ZAI_API_KEY` too: `\b`
# would miss it, since `_` is a word character.
VENDORS = re.compile(r"z-ai|(?<![a-z])zai(?![a-z])|deepgram|cartesia|fastembed|opencode",
                     re.IGNORECASE)
# The address, and the function a checkout can point at a fake: a call to
# `endpoints.openrouter()` reaches OpenRouter as surely as its address does.
ADDRESS = re.compile(r"(?i:openrouter\.ai)|endpoints\.openrouter\(")

# Where OpenRouter is reached, for good: Jev's one call, the replies it was
# captured giving, the check that its credit has not run out, and the one
# place its address is written (core/endpoints.py). And the security model,
# which must name every host shipped code reaches
# (tests/gate/test_hosts_named.py), and the privacy statement, which must
# name every host the security model does (test_privacy_statement.py): a
# reader is told the address, nothing calls it from there.
JEVS = ("core/decide.py", "tests/gate/fixtures/decisions/captured.json",
        "agent/attention/sources/health.py", "core/endpoints.py", "docs/security-model.md",
        "PRIVACY.md")


def _found(rel: str, pattern: re.Pattern) -> list[str]:
    text = repo_files.text(rel)
    return [m.group(0) for m in pattern.finditer(text)] if text else []


def _scanned() -> list[str]:
    return [rel for rel in repo_files.listed() if not rel.startswith(NOT_SCANNED) and rel != SELF]


def test_no_file_names_a_provider_steve_dropped():
    """A name left behind is a fallback, a header or a key someone will copy
    into the next feature."""
    found = {rel: sorted({n.lower() for n in names}) for rel in _scanned()
             if (names := _found(rel, VENDORS))}
    assert not found, f"these name a dropped provider: {found}"


def test_only_jevs_call_and_its_credit_check_reach_openrouter():
    """The OpenRouter key is required for Jev alone. A model, a fallback or a
    scout that reached OpenRouter too would spend that key on what OpenAI's
    is for, and fail with an error about a service the user only knows as
    Jev's."""
    found = sorted(rel for rel in _scanned()
                   if rel not in JEVS and _found(rel, ADDRESS))
    assert not found, f"these reach OpenRouter, which only Jev's call may: {found}"


def test_every_exemption_is_still_needed():
    """An entry whose file is gone, or no longer reaches OpenRouter, is a
    hole nothing fills: a new call there would pass unseen."""
    tracked = set(repo_files.listed())
    stale = sorted(rel for rel in JEVS if rel not in tracked or not _found(rel, ADDRESS))
    assert not stale, f"exempt but clean or gone: {stale}. Take them out of JEVS."


@pytest.mark.parametrize("text,flagged", [
    ("ZAI_API_KEY", True),
    ("pinned to z-ai at fp8", True),
    ("from fastembed import TextEmbedding", True),
    ("DEEPGRAM_API_KEY", True),
    ("shutil.which(\"opencode\")", True),
    ("a bazaar in Zaire", False),
    ("OPENROUTER_API_KEY", False),
    ("Jev, the decision model", False),
])
def test_the_vendor_check_finds_names_and_not_words_that_hold_them(text, flagged):
    assert bool(VENDORS.search(text)) is flagged


@pytest.mark.parametrize("text,flagged", [
    ("https://openrouter.ai/api/v1", True),
    ("Top up at OpenRouter.ai.", True),
    ("client.post(endpoints.openrouter() + DECISIONS)", True),
    ("OPENROUTER_API_KEY", False),
    ("STEVE_OPENROUTER_URL", False),
    ("endpoints.OPENROUTER", False),
])
def test_the_address_check_finds_openrouters_address_and_not_its_key(text, flagged):
    assert bool(ADDRESS.search(text)) is flagged
