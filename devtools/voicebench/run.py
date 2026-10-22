"""Run the bench on a folder of sessions, and write its report beside them.

    uv run --no-sync --with onnxruntime --with faster-whisper \\
        python -m devtools.voicebench.run private/voice

The report (`report.md`, `report.json`) goes into the folder it scored. A
report on private recordings is private too, but its numbers hold nothing
personal and may be quoted. Everything runs on the CPU: the plain
onnxruntime above has no GPU provider, and CUDA is hidden from CTranslate2
before it loads, so a number here is what a laptop without a GPU gets.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

os.environ["CUDA_VISIBLE_DEVICES"] = ""  # before CTranslate2 or onnxruntime load

from devtools.voicebench import judges, sets, turns, wake  # noqa: E402

SMART_TURN = Path("models/smart-turn/smart-turn-v3.2-cpu.onnx")

# The turn candidates: today's policy as the desk runs it; the same with the
# judge fed as its reference feeds it; the timer alone, as the desk ran
# before 7 October; and section 7's "speak late", the judge asked only once
# the pause is as long as the commit's.
TURN_CANDIDATES = (
    ("today", turns.TODAY, "end"),
    ("today, judge front-padded", turns.TODAY, "front"),
    ("timer 0.8 s, no judge", turns.Policy("timer"), None),
    ("commit 0.8 s, longest 2.0 s, front-padded", turns.Policy("c0.8", ask_s=(0.8,)), "front"),
    ("commit 1.0 s, longest 2.0 s, front-padded", turns.Policy("c1.0", ask_s=(1.0,)), "front"),
)
# The checks, smallest first: faster-whisper's names, run int8 on the CPU.
CHECKS = ("tiny", "base", "small", "large-v3-turbo")
# A proposal's clip in the negatives: this much from the first speech of a
# request said without the phrase, about what a detector hands on.
CLIP_S = 2.5


def turn_rows(root: Path, sessions: list[sets.Session], vad) -> dict:
    clips = [c for s in sessions for c in s.clips]
    out: dict = {"clips": len(clips), "candidates": {}}
    for name, policy, pad in TURN_CANDIDATES:
        judge = None if pad is None else judges.smart_turn(root / SMART_TURN, front=pad == "front")
        if judge is not None and not judge.available:
            out["candidates"][name] = {"skipped": f"no model at {SMART_TURN}"}
            continue
        started = time.perf_counter()
        results = [turns.run(c, vad=vad, judge=judge, policy=policy) for c in clips]
        row = turns.rows(results)
        row["seconds"] = round(time.perf_counter() - started, 1)
        row["early_takes"] = [t.name for t in results if t.early]
        out["candidates"][name] = row
        out.setdefault("pauses", {str(k): v for k, v in turns.pauses(results).items()})
    return out


def _clip_from_speech(clip: sets.Clip, vad) -> sets.Clip:
    heard = turns.speech(clip.audio, vad, clip.enter_s)
    start = 0 if heard.first_s is None else max(0, int((heard.first_s - turns.LEAD_S) * wake.SR))
    return sets.Clip(clip.name, clip.audio[start:start + int(CLIP_S * wake.SR)])


def check_rows(a: list[sets.Session], b: list[sets.Session], vad, *, models, limit: int,
               threads: int) -> dict:
    """Each check on session A's wake takes and on the opening of session
    B's requests, said without the phrase: what it rejects, what it lets
    through, and how long it takes on this CPU. `limit` caps the clips per
    side for the large models, whose minutes would otherwise be the run."""
    positives = [c for s in a for c in s.clips if c.section != "near"]
    negatives = [_clip_from_speech(c, vad) for s in b for c in s.clips]
    out = {}
    for name in models:
        transcribe = wake.whisper(name, threads=threads)
        transcribe(positives[0].audio)  # the first call loads; it is not a check's time
        n = limit if "large" in name else len(positives) + len(negatives)
        row = wake.check_rows(wake.check(transcribe, positives[:n]),
                              wake.check(transcribe, negatives[:n]))
        row["threads"] = threads
        out[name] = row
        print(name, json.dumps(row), flush=True)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m devtools.voicebench.run")
    parser.add_argument("root", type=Path, help="the folder of sessions, e.g. private/voice")
    parser.add_argument("--only", default="", help="session kinds to score, e.g. b; all by default")
    parser.add_argument("--checks", default="tiny,base,small",
                        help=f"the checks to time, of {','.join(CHECKS)}; none with ''")
    parser.add_argument("--limit", type=int, default=15, help="clips per side for a large check")
    parser.add_argument("--threads", type=int, default=4, help="CPU threads for a check")
    args = parser.parse_args(argv)
    from voice.listen import Vad

    found = sets.find(args.root, kinds=args.only)
    report: dict = {"sessions": {k: [s.folder.name for s in v] for k, v in found.items()}}
    if "b" in found:
        report["turns"] = turn_rows(args.root, found["b"], Vad())
    models = [m for m in args.checks.split(",") if m]
    if models and "a" in found and "b" in found:
        report["checks"] = check_rows(found["a"], found["b"], Vad(), models=models,
                                      limit=args.limit, threads=args.threads)
    (args.root / "report.json").write_text(json.dumps(report, indent=2) + "\n",
                                           encoding="utf-8", newline="\n")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
