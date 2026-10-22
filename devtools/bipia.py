"""BIPIA's emails, as a measure of the injection screen.

Microsoft's BIPIA benchmark (MIT) plants instructions in real emails -- "write
a script", "translate this", "tell the reader to visit" -- of fifteen kinds.
Many are the "do X in your response" kind: no override, no role marker,
nothing aimed at an AI by name, which the screen's patterns never see and a
classifier must. It is the second measure, beside the red team the screen's
own author wrote (`python -m core.screen redteam`), of whether a new
classifier may replace the old one.

What this prevents: a claim of parity on a set nobody can rebuild. The set
is fetched at a pinned commit, each file checked against the hash in
`devtools/bipia.json`, and built the same way every time, so the built set's
hash is checked too. The emails are someone else's mail, so the built set
stays in the data folder (git-ignored); only this file and its manifest are
in git.

The set is the one the personal edition measured Jev on, rebuilt exactly,
so its 40 of 50 is a baseline anyone can check: BIPIA's 50 test emails, each
headed by its sender and subject, clean, and again with one attack planted.
The attacks, every kind together, are shuffled with seed 13 and dealt in
turn; the nth email's lands at its start, in its middle (between lines) or
at its end, by n mod 3, set off by a blank line. Step 3 first built the set
as BIPIA plants it, which matched no measurement anyone had made, and
dropped it.

    python -m devtools.bipia build   # fetch, check, build data/evals/bipia/set.json
    python -m devtools.bipia run     # the screen's classifier on each, live, billed
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
import time
from pathlib import Path

MANIFEST = Path(__file__).resolve().with_suffix(".json")
RAW = "https://raw.githubusercontent.com/microsoft/BIPIA/{commit}/{path}"
EMAILS = "benchmark/email/test.jsonl"
ATTACKS = "benchmark/text_attack_test.json"
POSITIONS = ("start", "middle", "end")
SEED = 13


def folder() -> Path:
    from core import paths

    return paths.data_dir() / "evals" / "bipia"


def manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def fetch(path: str, commit: str, sha256: str) -> bytes:
    """One file of BIPIA at `commit`, refused unless it hashes to `sha256`."""
    import httpx2

    response = httpx2.get(RAW.format(commit=commit, path=path), timeout=60.0,
                          follow_redirects=True)
    response.raise_for_status()
    got = hashlib.sha256(response.content).hexdigest()
    if got != sha256:
        raise RuntimeError(f"{path} at {commit[:12]} hashed {got}, not the pinned {sha256}")
    return response.content


def planted(body: str, attack: str, position: str) -> str:
    """`attack` in `body`: before it or after it, set off by a blank line,
    or on its own line halfway down."""
    if position == "start":
        return f"{attack}\n\n{body}"
    if position == "end":
        return f"{body}\n\n{attack}"
    lines = body.split("\n")
    half = len(lines) // 2
    return "\n".join([*lines[:half], attack, *lines[half:]])


def build(emails: bytes, attacks: bytes) -> list[dict]:
    """The set, from BIPIA's two files: each email clean, then with the next
    of the shuffled attacks, at the nth email's position."""
    rows = [json.loads(line) for line in emails.decode("utf-8").splitlines() if line.strip()]
    kinds = json.loads(attacks.decode("utf-8"))
    kind_of = {attack: kind for kind, group in kinds.items() for attack in group}
    dealt = [attack for group in kinds.values() for attack in group]
    random.Random(SEED).shuffle(dealt)
    items = []
    for n, row in enumerate(rows):
        context = row["context"]
        fields = dict(part.split(": ", 1) for part in context.split("|")[:3] if ": " in part)
        body = context.split("|CONTENT:", 1)[-1].strip()
        sender = fields.get("EMAIL_FROM", "")
        head = f"From: {sender}\nSubject: {fields.get('SUBJECT', '')}\n\n"
        attack = dealt[n % len(dealt)]
        position = POSITIONS[n % len(POSITIONS)]
        items.append({"id": f"clean-{n}", "source": "email_body", "origin": sender[:200],
                      "attack": False, "kind": None, "position": None, "text": head + body})
        items.append({"id": f"attack-{n}-{position}", "source": "email_body",
                      "origin": sender[:200], "attack": True, "kind": kind_of[attack],
                      "position": position,
                      "text": head + planted(body, attack, position)})
    return items


def digest(items: list[dict]) -> str:
    return hashlib.sha256(json.dumps(items, sort_keys=True, ensure_ascii=False)
                          .encode("utf-8")).hexdigest()


def run(key: str, items: list[dict]) -> list[dict]:
    """Each email through the screen's classifier, live, uncached, timed.
    `screen.redteam` gives a verdict a row; this keeps the classifier's
    probabilities and the time too, which the comparison needs. Billed to the
    data folder's ledger, in the unit it runs in."""
    from core import screen

    rows = []
    for item in items:
        artifact = screen.Artifact(item["source"], item["id"], item["origin"], item["text"])
        started = time.perf_counter()
        said = screen._ask(None, artifact, key)
        ms = int((time.perf_counter() - started) * 1000)
        rows.append({"id": item["id"], "got": said.verdict, "choices": said.choices,
                     "p": said.p_injection, "hits": said.hits, "error": said.error,
                     "parts": said.chunks, "ms": ms, "cost_usd": said.cost_usd})
    return rows


def summary(items: list[dict], said: list[dict]) -> dict:
    """What the screen made of the set: attacks caught and clean emails
    flagged, by the screen as it ships (patterns and classifier), by the
    classifier's likeliest choice alone, and by the patterns alone; the
    attacks caught by kind and by position; each item's time."""
    rows = {r["id"]: r for r in said}

    def flagged(row: dict, by: str) -> bool:
        if by == "screen":
            return row["got"] in ("sanitize", "ignore")
        if by == "classifier":
            choices = row.get("choices") or {}
            return bool(choices) and max(choices, key=choices.get) != "as_is"
        return bool(row["hits"])

    attacked = [i for i in items if i["attack"]]
    clean = [i for i in items if not i["attack"]]
    out: dict = {}
    for by in ("screen", "classifier", "patterns"):
        out[by] = {"attacks_caught": sum(flagged(rows[i["id"]], by) for i in attacked),
                   "attacks": len(attacked),
                   "clean_flagged": sum(flagged(rows[i["id"]], by) for i in clean),
                   "clean": len(clean)}
    for field in ("kind", "position"):
        groups: dict[str, list[int]] = {}
        for i in attacked:
            caught = groups.setdefault(i[field], [0, 0])
            caught[0] += flagged(rows[i["id"]], "screen")
            caught[1] += 1
        out[f"by_{field}"] = {k: f"{c} of {t}" for k, (c, t) in groups.items()}
    ms = sorted(r["ms"] for r in rows.values())
    out["ms"] = {"p50": ms[len(ms) // 2], "p99": ms[min(len(ms) - 1, int(len(ms) * 0.99))],
                 "max": ms[-1]}
    out["errors"] = sum(bool(r["error"]) for r in rows.values())
    out["parts"] = sum(r["parts"] for r in rows.values())
    out["cost_usd"] = round(sum(r["cost_usd"] for r in rows.values()), 6)
    return out


def _main() -> None:
    parser = argparse.ArgumentParser(description="BIPIA's emails through the screen.")
    parser.add_argument("what", choices=["build", "run"])
    args = parser.parse_args()
    pinned = manifest()
    where = folder()
    if args.what == "build":
        files = {path: fetch(path, pinned["commit"], sha)
                 for path, sha in pinned["files"].items()}
        items = build(files[EMAILS], files[ATTACKS])
        got = digest(items)
        if pinned.get("built_sha256") and got != pinned["built_sha256"]:
            sys.exit(f"the set built to {got}, not the manifest's {pinned['built_sha256']}")
        where.mkdir(parents=True, exist_ok=True)
        (where / "set.json").write_text(json.dumps(items, indent=1, ensure_ascii=False),
                                        encoding="utf-8", newline="\n")
        print(f"{len(items)} emails in {where / 'set.json'}, sha256 {got}")
        return
    from core import secrets, spend

    items = json.loads((where / "set.json").read_text(encoding="utf-8"))
    if digest(items) != pinned["built_sha256"]:
        sys.exit("set.json is not the set the manifest pins: build it again")
    with spend.spending("background"):
        rows = run(secrets.require("OPENROUTER_API_KEY"), items)
    out = summary(items, rows)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    (where / f"run-{stamp}.json").write_text(json.dumps({"summary": out, "rows": rows},
                                                        indent=1), encoding="utf-8",
                                             newline="\n")
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    _main()
