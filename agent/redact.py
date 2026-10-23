"""Stripping personal detail out of a transcript before a judge model sees it.

Reflection sends conversations to a model to be scored. Those conversations
are about the user's mail, calendar and whereabouts, and the scoring is worth
very little compared to the cost of leaking any of it, so redaction happens
before the call rather than as a setting on it.

Two decisions shape this:

**Placeholders are numbered, not collapsed.** Presidio's default rewrites
every name to `<PERSON>`, which turns "ask Alice to forward Bob's note" into
"ask <PERSON> to forward <PERSON>'s note" -- a sentence the judge can no
longer reason about. Each distinct value instead gets a stable index within
the document, so structure survives and identity does not. The mapping is
never persisted.

**The threshold is deliberately low.** Presidio scores a bare phone number at
0.4. Over-redaction costs a slightly vaguer rubric score; under-redaction
costs a home address sent to a third party. Those are not comparable, so this
errs hard in one direction.
"""

from __future__ import annotations

import importlib.util
import re
import threading
import time
from dataclasses import dataclass, field

# Below this, a detection is ignored. Low on purpose -- see the module note.
MIN_SCORE = 0.3

# Per-entity overrides, for recognisers noisy enough that the blanket
# threshold does more harm than good.
#
# US_DRIVER_LICENSE is the one that matters: its weak patterns match any short
# alphanumeric token, so a conversation about earnings came back with "Q3" and
# "Q4" replaced by placeholders -- which costs the judge the meaning of the
# sentence and protects nothing. With a context word ("license") a real number
# scores 0.65 and bare tokens score 0.3, so the split is clean.
THRESHOLDS: dict[str, float] = {"US_DRIVER_LICENSE": 0.5}

# Tokens that are never personal data, whatever a recogniser says. Kept
# deliberately tiny: this is an escape hatch for detections that are both
# confidently wrong and destroy meaning, not a general vocabulary.
#
# Quarter labels earn their place because they hit twice over -- the
# driver-licence patterns match "Q3" as an alphanumeric, and spaCy tags "Q4"
# as a LOCATION at 0.85, which no threshold can separate from a real city.
# Add to this only with a case from a real transcript.
NEVER_REDACT = re.compile(r"^(?:Q[1-4]|FY\d{2,4}|H[12])$", re.IGNORECASE)

# What to look for. Presidio ships more recognisers than this; these are the
# ones that can actually appear in a personal assistant's transcript.
ENTITIES = [
    "PERSON",
    "EMAIL_ADDRESS",
    "PHONE_NUMBER",
    "CREDIT_CARD",
    "IBAN_CODE",
    "US_SSN",
    "US_PASSPORT",
    "US_DRIVER_LICENSE",
    "LOCATION",
    "IP_ADDRESS",
    "MEDICAL_LICENSE",
    "CRYPTO",
    "DATE_TIME",
]

# Entities that are structure rather than identity. Kept because reflection is
# largely about *timing* -- "did it check the calendar before answering?" is
# unanswerable once every date is a placeholder.
KEEP = {"DATE_TIME"}

_engine_lock = threading.Lock()
_analyzer = None
_used_at = 0.0

# How long the NER pipeline stays loaded after its last use. It is ~740 MB
# of the worker's memory (spaCy's large English model under Presidio) and
# loads in under three seconds; reflection, its main user, comes in bursts.
# Kept for good after the first reflection of the day, it took the worker
# from 330 MB to 930 MB (measured 6 October 2026).
IDLE_RELEASE_S = 600


@dataclass
class Redacted:
    """A transcript with the personal detail taken out."""

    text: str
    found: dict[str, int] = field(default_factory=dict)

    @property
    def count(self) -> int:
        return sum(self.found.values())

    @property
    def clean(self) -> bool:
        return self.count == 0


def _street_address_recogniser():
    """Presidio has no address recogniser; LOCATION catches "Pittsburgh" and
    walks straight past "12 Maple Street"."""
    from presidio_analyzer import Pattern, PatternRecognizer

    return PatternRecognizer(
        supported_entity="STREET_ADDRESS",
        patterns=[
            Pattern(
                name="street address",
                regex=(
                    r"\b\d{1,5}\s+(?:[A-Z][a-z]+\.?\s+){1,4}"
                    r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|"
                    r"Drive|Dr|Court|Ct|Place|Pl|Terrace|Ter|Way|Circle|Cir)\b\.?"
                ),
                score=0.7,
            )
        ],
    )


# Any address, whatever its ending. presidio's own email recogniser keeps
# only those whose ending is on the public-suffix list, so an address on an
# office's private domain (`.internal`, `.corp`), and the test persona's
# `maya@contoso.example`, went to the judge verbatim: CI's first run with
# presidio installed (22 October 2026) found it. Where both match, the
# longest-first pass in `redact` keeps one.
EMAIL_PATTERN = r"\b[\w.%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+\b"


def _email_recogniser():
    from presidio_analyzer import Pattern, PatternRecognizer

    return PatternRecognizer(
        supported_entity="EMAIL_ADDRESS",
        patterns=[Pattern(name="any email", regex=EMAIL_PATTERN, score=0.9)],
    )


def _secret_recogniser():
    """API keys are not PII, but a transcript is exactly where one ends up
    after the agent reads a config file out loud, and a judge model is exactly
    where it should not go next."""
    from presidio_analyzer import Pattern, PatternRecognizer

    return PatternRecognizer(
        supported_entity="SECRET",
        patterns=[
            Pattern(name="openai key", regex=r"\bsk-[A-Za-z0-9_\-]{16,}\b", score=0.9),
            Pattern(name="openrouter key", regex=r"\bsk-or-v1-[A-Za-z0-9]{16,}\b", score=0.9),
            Pattern(name="google key", regex=r"\bAIza[A-Za-z0-9_\-]{30,}\b", score=0.9),
            Pattern(name="bearer token", regex=r"\bBearer\s+[A-Za-z0-9._\-]{20,}", score=0.8),
            Pattern(name="private key", regex=r"-----BEGIN [A-Z ]*PRIVATE KEY-----", score=1.0),
        ],
    )


def _offline_suffixes() -> None:
    """Point tldextract's shared extractor at the public-suffix list it
    bundles. presidio's email check calls `tldextract.extract`, whose
    extractor fetches the list from publicsuffix.org on first use: each
    reflection would have told a third party that Steve was redacting, and
    under the test fences it raised instead, failing every email
    redaction. `core/browser/vault.py` builds its own extractor the same
    way; this one replaces the module's, which presidio reads at each call."""
    import tldextract.tldextract as tld

    tld.TLD_EXTRACTOR = tld.TLDExtract(suffix_list_urls=(), cache_dir=None)


# What `_get_analyzer` loads: the engine, and the spaCy model its default
# configuration names. Both are the `reflect` extra, which an install leaves out.
NEEDS = ("presidio_analyzer", "en_core_web_lg")


def can_redact() -> bool:
    """Whether this computer can redact at all, asked without loading the
    pipeline. Without the model, presidio would try to download it."""
    try:
        return all(importlib.util.find_spec(name) is not None for name in NEEDS)
    except (ImportError, ValueError):
        return False


def _get_analyzer():
    """Load the NER pipeline, or reuse it while it is loaded. Takes a few
    seconds and holds a spaCy model; `release_if_idle` lets it go."""
    global _analyzer, _used_at
    _used_at = time.monotonic()
    if _analyzer is not None:
        return _analyzer
    with _engine_lock:
        if _analyzer is None:
            from presidio_analyzer import AnalyzerEngine

            _offline_suffixes()
            engine = AnalyzerEngine()
            engine.registry.add_recognizer(_street_address_recogniser())
            engine.registry.add_recognizer(_email_recogniser())
            engine.registry.add_recognizer(_secret_recogniser())
            _analyzer = engine
    return _analyzer


def release_if_idle(idle_s: float = IDLE_RELEASE_S) -> bool:
    """Drop the NER pipeline if nothing has used it for `idle_s`; True if it
    was dropped. A redaction already running keeps its own reference and
    finishes; the next one loads it again."""
    global _analyzer
    with _engine_lock:
        if _analyzer is None or time.monotonic() - _used_at < idle_s:
            return False
        _analyzer = None
    import gc

    gc.collect()
    return True


def _entities() -> list[str]:
    return [*ENTITIES, "STREET_ADDRESS", "SECRET"]


def redact(text: str) -> Redacted:
    """Replace personal detail with stable numbered placeholders.

    Synchronous and slow to first call (the spaCy model loads). Callers on an
    event loop must push this to a thread.
    """
    if not text or not text.strip():
        return Redacted(text=text or "")

    results = [
        r
        for r in _get_analyzer().analyze(text=text, language="en", entities=_entities())
        if r.score >= THRESHOLDS.get(r.entity_type, MIN_SCORE)
        and r.entity_type not in KEEP
        and not NEVER_REDACT.match(text[r.start : r.end].strip())
    ]
    if not results:
        return Redacted(text=text)

    # Longest first at the same offset, so an overlapping shorter match (the
    # "URL" presidio finds inside an email address) loses to the better one.
    results.sort(key=lambda r: (r.start, -(r.end - r.start)))
    chosen = []
    consumed = -1
    for r in results:
        if r.start >= consumed:
            chosen.append(r)
            consumed = r.end

    labels: dict[tuple[str, str], str] = {}
    counts: dict[str, int] = {}
    found: dict[str, int] = {}

    out = []
    cursor = 0
    for r in chosen:
        value = text[r.start : r.end]
        key = (r.entity_type, value.casefold())
        if key not in labels:
            counts[r.entity_type] = counts.get(r.entity_type, 0) + 1
            labels[key] = f"<{r.entity_type}_{counts[r.entity_type]}>"
        out.append(text[cursor : r.start])
        out.append(labels[key])
        found[r.entity_type] = found.get(r.entity_type, 0) + 1
        cursor = r.end
    out.append(text[cursor:])

    return Redacted(text="".join(out), found=found)


_PLACEHOLDER = re.compile(r"<[A-Z_]+_\d+>")


def placeholders(text: str) -> list[str]:
    """Every placeholder in a redacted string. For tests and for assertions
    at the boundary, where 'did this actually get redacted?' is worth asking
    rather than assuming."""
    return _PLACEHOLDER.findall(text)
