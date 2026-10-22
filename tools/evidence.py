"""Evidence grounding: label answer claims against supplied source excerpts.

Given the claims an answer makes and the excerpts/URLs it was drawn from, each
claim is labelled DIRECTLY SUPPORTED (a supplied source states it, near
verbatim), INFERRED (sources back the substance but not the wording), or
UNSUPPORTED (no supplied source provides lexical backing), and the result
comes back as citation-ready notes: one line per claim with a URL and a
trimmed quote from the best-matching excerpt.

Why lexical and deterministic rather than model-judged:

  - Grounding exists to *constrain* an answer, so the check itself must be
    inspectable. "The model said the quote supports the claim" is not
    evidence; a token-overlap match with the quoted span is.
  - It is cheap and pure, so it can run on every answer instead of a sample.

It cannot judge truth. A claim about Berlin against a Paris source still
shares half its words. The labels mean "the supplied sources say this", never
"this is correct", and INFERRED should read as "verify manually".

Fail-closed on purpose: when the lexical evidence is ambiguous the label
degrades toward inferred/unsupported, because a false "unsupported" sends a
human to check while a false "directly supported" is a fabricated citation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

DIRECT = "directly supported"
INFERRED = "inferred"
UNSUPPORTED = "unsupported"

# Direct support needs an unbroken verbatim run of at least this many claim
# tokens AND most of the claim's content words present. Four is about the
# shortest run that means something in prose ("the eiffel tower is" is
# coincidence; "tower is in paris, france" is a quotation).
DIRECT_MIN_RUN = 4
DIRECT_MIN_COVERAGE = 0.7

# A long verbatim run whose claim says substantially more than the quote is
# partial support, not quotation: label it inferred rather than direct.
INFERRED_MIN_RUN = 6
PARTIAL_MIN_COVERAGE = 0.35

# Paraphrase path: heavy content-word overlap with little or no verbatim span.
INFERRED_MIN_COVERAGE = 0.6

# Below this run length a match is a shared word, not a quotation: nothing to
# show in a note. Three tokens is the shortest phrase that means anything.
QUOTE_MIN_RUN = 3

# Guard against whole-page dumps: excerpts are supposed to be excerpts.
MAX_EXCERPT_TOKENS = 4000

_STOPWORD_TEXT = (
    "a an the is are was were be been being am do does did doing have has had "
    "having i you he she it we they me him her us them my your his its our "
    "their this that these those there here of in on at by for with from to "
    "as and or but not no nor so if then than too very can will would shall "
    "should could may might must about into over under after before between "
    "during also just only"
)
STOPWORDS = frozenset(_STOPWORD_TEXT.split())

_TOKEN_RE = re.compile(r"\S+")
_NON_WORD_RE = re.compile(r"[^\w]+")
_WHITESPACE_RE = re.compile(r"\s+")


@dataclass
class Source:
    url: str
    text: str
    title: str | None = None

    def as_dict(self) -> dict:
        return {"url": self.url, "title": self.title, "text": self.text}


@dataclass
class ClaimVerdict:
    claim: str
    label: str
    score: float  # rough match strength in [0, 1]; ranking aid, not a probability
    url: str | None = None  # best-matching source; None when nothing overlaps
    quote: str | None = None  # trimmed verbatim span, None when there is no span
    detail: str = ""  # one-line human note on why this label

    def as_dict(self) -> dict:
        return {
            "claim": self.claim,
            "label": self.label,
            "score": self.score,
            "url": self.url,
            "quote": self.quote,
            "detail": self.detail,
        }


@dataclass
class EvidenceReport:
    claims: list[str]
    sources: list[Source]
    verdicts: list[ClaimVerdict] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        counts = {DIRECT: 0, INFERRED: 0, UNSUPPORTED: 0}
        for v in self.verdicts:
            counts[v.label] += 1
        return counts

    def as_dict(self) -> dict:
        return {
            "claims": self.claims,
            "sources": [s.as_dict() for s in self.sources],
            "counts": self.counts,
            "verdicts": [v.as_dict() for v in self.verdicts],
        }

    def render(self) -> str:
        """Citation-ready notes: header with tally, then one entry per claim."""
        if not self.verdicts:
            return "Evidence notes: no claims to ground."
        counts = self.counts
        header = (
            f"Evidence notes - {len(self.verdicts)} claim(s) against "
            f"{len(self.sources)} source(s): {counts[DIRECT]} directly supported, "
            f"{counts[INFERRED]} inferred, {counts[UNSUPPORTED]} unsupported."
        )
        lines = [header, ""]
        for i, v in enumerate(self.verdicts, 1):
            lines.append(f"{i}. [{v.label}] {v.claim}")
            if v.quote:
                note = f' - {v.detail}' if v.label == UNSUPPORTED else ""
                lines.append(f'   {v.url} - "{v.quote}"{note}')
            elif v.url:
                lines.append(f"   {v.url} ({v.detail})")
            else:
                lines.append("   no supplied source matches - do not cite without checking.")
        return "\n".join(lines)


def _normalize(word: str) -> str:
    """Lowercase and strip punctuation, so quotes and hyphens do not break runs."""
    return _NON_WORD_RE.sub("", word.lower())


def _tokenize(text: str) -> list[tuple[str, int, int]]:
    """(normalized token, original start, original end), punctuation-only tokens dropped."""
    out: list[tuple[str, int, int]] = []
    for m in _TOKEN_RE.finditer(text):
        norm = _normalize(m.group())
        if norm:
            out.append((norm, m.start(), m.end()))
    return out[:MAX_EXCERPT_TOKENS]


def _clean(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text).strip()


def _longest_common_run(
    claim: list[str], source: list[str]
) -> tuple[int, int]:
    """Length and source-end index (1-based) of the longest contiguous token
    run shared by both sequences. Classic DP over two short token streams.
    Returns (0, 0) when nothing matches.
    """
    best_len = best_end = 0
    prev = [0] * (len(source) + 1)
    for _i, c in enumerate(claim, 1):
        cur = [0] * (len(source) + 1)
        for j, s in enumerate(source, 1):
            if s == c:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best_len:
                    best_len, best_end = cur[j], j
        prev = cur
    return best_len, best_end


def _make_quote(
    text: str,
    tokens: list[tuple[str, int, int]],
    run_start: int,
    run_len: int,
    *,
    pad: int,
    max_chars: int,
) -> str:
    """Quote the matching span plus a few tokens of context, ellipsized."""
    lo = max(0, run_start - pad)
    hi = min(len(tokens), run_start + run_len + pad)
    span = _clean(text[tokens[lo][1] : tokens[hi - 1][2]])
    if lo > 0:
        span = "... " + span
    if hi < len(tokens):
        span = span + " ..."
    if len(span) > max_chars:
        cut = span[: max_chars - 4]
        cut = cut.rsplit(" ", 1)[0] if " " in cut else cut
        span = cut.strip() + " ..."
    return span


def _match_one(
    claim_text: str,
    source: Source,
    *,
    direct_min_run: int,
    direct_min_coverage: float,
    inferred_min_run: int,
    partial_min_coverage: float,
    inferred_min_coverage: float,
    quote_pad: int,
    max_quote_chars: int,
) -> ClaimVerdict:
    claim_tokens = [t for t in (_normalize(w) for w in _TOKEN_RE.findall(claim_text)) if t]
    if not claim_tokens:
        return ClaimVerdict(
            claim=claim_text, label=UNSUPPORTED, score=0.0,
            detail="claim has no matchable words",
        )

    source_tokens = _tokenize(source.text)
    source_norms = [t[0] for t in source_tokens]
    content = [t for t in claim_tokens if t not in STOPWORDS]
    pool = {t for t in source_norms if t not in STOPWORDS}
    if not content:
        # An all-function-word claim has no content words to filter on;
        # compare every token rather than reporting zero coverage and
        # labelling the claim unsupported on a technicality.
        content = claim_tokens
        pool = set(source_norms)
    coverage = len(set(content) & pool) / len(content)

    # Every label path needs coverage above zero, so a zero-overlap pair can
    # skip the DP entirely and has nothing quotable to point at either.
    if coverage == 0.0:
        return ClaimVerdict(
            claim=claim_text, label=UNSUPPORTED, score=0.0,
            detail="no lexical overlap with this source",
        )

    run, run_end = _longest_common_run(claim_tokens, source_norms)

    if run >= direct_min_run and coverage >= direct_min_coverage:
        label = DIRECT
        detail = f"verbatim run of {run} tokens; coverage {coverage:.0%}"
    elif (run >= inferred_min_run and coverage >= partial_min_coverage) or (
        coverage >= inferred_min_coverage
    ):
        label = INFERRED
        if run >= inferred_min_run:
            detail = (
                f"verbatim span of {run} tokens but the claim says more; "
                f"coverage {coverage:.0%}"
            )
        else:
            detail = f"paraphrase-level overlap, no verbatim span; coverage {coverage:.0%}"
    else:
        label = UNSUPPORTED
        detail = f"closest excerpt below threshold; coverage {coverage:.0%}"

    run_share = run / len(claim_tokens)
    score = round(0.5 * run_share + 0.5 * coverage, 4)

    quote = None
    if run >= QUOTE_MIN_RUN:
        quote = _make_quote(
            source.text,
            source_tokens,
            run_end - run,
            run,
            pad=quote_pad,
            max_chars=max_quote_chars,
        )

    return ClaimVerdict(
        claim=claim_text,
        label=label,
        score=score,
        url=source.url,
        quote=quote,
        detail=detail,
    )


def ground_claims(
    claims: list[str],
    sources: list[Source],
    *,
    direct_min_run: int = DIRECT_MIN_RUN,
    direct_min_coverage: float = DIRECT_MIN_COVERAGE,
    inferred_min_run: int = INFERRED_MIN_RUN,
    partial_min_coverage: float = PARTIAL_MIN_COVERAGE,
    inferred_min_coverage: float = INFERRED_MIN_COVERAGE,
    quote_pad: int = 4,
    max_quote_chars: int = 200,
) -> EvidenceReport:
    """Label each claim against the supplied excerpts and return the report.

    The best source per claim is the one with the highest combined score
    (half verbatim run share, half content-word coverage), ties going to the
    earlier source. Thresholds are parameters so callers can loosen them for
    chatty excerpts without editing the module; they are validated because a
    threshold of 0 or 2 would silently relabel everything.
    """
    if direct_min_run < 1:
        raise ValueError(f"direct_min_run must be >= 1, got {direct_min_run}")
    for name, value in (
        ("direct_min_coverage", direct_min_coverage),
        ("partial_min_coverage", partial_min_coverage),
        ("inferred_min_coverage", inferred_min_coverage),
    ):
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"{name} must be in [0, 1], got {value}")

    verdicts = [
        max(
            (
                _match_one(
                    claim,
                    source,
                    direct_min_run=direct_min_run,
                    direct_min_coverage=direct_min_coverage,
                    inferred_min_run=inferred_min_run,
                    partial_min_coverage=partial_min_coverage,
                    inferred_min_coverage=inferred_min_coverage,
                    quote_pad=quote_pad,
                    max_quote_chars=max_quote_chars,
                )
                for source in sources
            ),
            key=lambda v: v.score,
            default=ClaimVerdict(
                claim=claim, label=UNSUPPORTED, score=0.0,
                detail="no sources supplied",
            ),
        )
        for claim in claims
    ]
    return EvidenceReport(claims=list(claims), sources=list(sources), verdicts=verdicts)
