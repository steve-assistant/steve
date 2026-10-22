"""The relevance floors, measured: `python -m devtools.floors`.

Recall returns no note less similar to its query than
`memory.RELEVANCE_FLOOR`, and the facts lookup no fact less similar to the
message than `memory.FACT_FLOOR`. The numbers are the embedder's. Set at
0.72 for a local model whose unrelated texts scored above 0.5, the floor
kept 2 of 20 labelled hits on OpenAI's vectors, whose related texts score
lower: recall would have come back nearly empty, with nothing to say why.
So the floors are measured on labelled pairs
(`tests/unit/fixtures/floors/pairs.json`) and the scores kept beside them,
with the model and size that gave them. `tests/unit/test_relevance_floor.py`
holds the floors to them, and fails when the model changes until they are
measured again.

Each pair is embedded live, through `core/embed.py`, under the caps, for
a fraction of a cent. The scores are written into the fixture.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import date
from pathlib import Path

from core import db, embed, spend
from tools import memory

PAIRS = Path(__file__).resolve().parents[1] / "tests/unit/fixtures/floors/pairs.json"


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


def stored_text(fixture: dict, pair: dict) -> str:
    return fixture["facts" if pair["kind"] == "fact" else "memories"][pair["stored"]]


def measure(fixture: dict) -> dict:
    """The fixture with each pair's score, and the model and size that gave
    them."""
    texts = list(dict.fromkeys(t for p in fixture["pairs"] for t in (p["ask"],
                                                                      stored_text(fixture, p))))
    with spend.spending("background"):
        found = dict(zip(texts, embed.vectors(texts), strict=True))
    for pair in fixture["pairs"]:
        pair["score"] = round(cosine(found[pair["ask"]], found[stored_text(fixture, pair)]), 4)
    return fixture | {"model": db.EMBEDDING_MODEL, "dim": db.EMBEDDING_DIM,
                      "measured": date.today().isoformat()}


def floor_for(kind: str) -> float:
    return memory.FACT_FLOOR if kind == "fact" else memory.RELEVANCE_FLOOR


def report(fixture: dict) -> str:
    lines = []
    for kind in ("fact", "memory"):
        pairs = sorted((p for p in fixture["pairs"] if p["kind"] == kind),
                       key=lambda p: -p["score"])
        floor = floor_for(kind)
        lines.append(f"{kind}, floor {floor}:")
        for pair in pairs:
            lines.append(f"  {pair['score']:.4f}  {'HIT ' if pair['hit'] else 'miss'}"
                         f"  {'kept' if pair['score'] >= floor else '    '}"
                         f"  {pair['ask'][:60]!r} -> {pair['stored']}")
        hits = [p["score"] for p in pairs if p["hit"]]
        misses = [p["score"] for p in pairs if not p["hit"]]
        lines.append(f"  keeps {sum(h >= floor for h in hits)} of {len(hits)} hits, lets"
                     f" {sum(m >= floor for m in misses)} of {len(misses)} misses through; the"
                     f" lowest floor dropping every miss is above {max(misses):.4f} and keeps"
                     f" {sum(h > max(misses) for h in hits)}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry", action="store_true", help="report, and write nothing")
    args = parser.parse_args(argv)
    fixture = measure(json.loads(PAIRS.read_text(encoding="utf-8")))
    print(report(fixture))
    if not args.dry:
        PAIRS.write_text(json.dumps(fixture, indent=1, ensure_ascii=False) + "\n",
                         encoding="utf-8", newline="\n")
        print(f"written to {PAIRS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
