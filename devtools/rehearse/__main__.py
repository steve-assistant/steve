"""Run the desk's rehearsals.

    python -m devtools.rehearse all
    python -m devtools.rehearse stop-long-answer talk-live --save-audio
    python -m devtools.rehearse --list
    python -m devtools.rehearse quiet-closes-live --live      # real GPT-Live, billed

Each scenario is a script in devtools/rehearse/scenarios/. The report of each
is printed and kept, with its numbers, in data/rehearsals/<when>/<scenario>/,
and the run's results in data/rehearsals/<when>/summary.json. Exit 1 if any
scenario failed, 2 if the desk could not be rehearsed at all.

`--live` uses the real GPT-Live, at about $0.05 a minute: the cost is said
before anything starts, and each run is capped (`rig.LIVE_CAP_S`).
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from devtools.rehearse import keys, script
from devtools.rehearse import rig as rigs

SCENARIOS = Path(__file__).resolve().parent / "scenarios"


def available() -> dict[str, script.Scenario]:
    """Every scenario script, by name. A script that does not parse is an
    error here, not a scenario silently skipped."""
    found: dict[str, script.Scenario] = {}
    for path in sorted(SCENARIOS.glob("*.txt")):
        try:
            scenario = script.parse(path.read_text(encoding="utf-8"))
        except script.ScriptError as exc:
            raise SystemExit(f"{path.name}: {exc}") from None
        if scenario.name in found:
            raise SystemExit(f"{path.name}: a second scenario named {scenario.name!r}")
        found[scenario.name] = scenario
    return found


def chosen(names: list[str], scenarios: dict[str, script.Scenario]) -> list[script.Scenario]:
    if not names or names == ["all"]:
        return list(scenarios.values())
    unknown = [n for n in names if n not in scenarios]
    if unknown:
        raise SystemExit(f"no scenario {', '.join(unknown)}; there are: "
                         + ", ".join(scenarios))
    return [scenarios[n] for n in names]


def head() -> str:
    """The commit rehearsed: the night rehearses again when the desk has
    changed since (agent/night/rehearsal.py)."""
    done = subprocess.run(["git", "-C", str(rigs.ROOT), "rev-parse", "HEAD"], capture_output=True,
                          text=True, check=False)
    return done.stdout.strip()


def live_cost(run: list[script.Scenario]) -> float:
    """The most a `--live` run can be billed: each scenario capped."""
    minutes = sum(min(s.length, rigs.LIVE_CAP_S) for s in run) / 60
    return round(minutes * rigs.LIVE_USD_PER_MIN, 2)


async def rehearse(run: list[script.Scenario], *, live: bool, save_audio: bool,
                   out: Path | None = None) -> tuple[int, Path | None]:
    rig = rigs.Rig(live=live, save_audio=save_audio, out=out)
    results: list[dict] = []
    # Push-to-talk's need no models (devtools/rehearse/keys.py); the room's
    # load the add-on's, so they are loaded only if a room scenario runs.
    room = [s for s in run if not keys.pushes(s)]
    for scenario in (s for s in run if keys.pushes(s)):
        with tempfile.TemporaryDirectory(prefix=f"{scenario.name}-") as tmp:
            result = await keys.rehearse(scenario, tmp=Path(tmp))
        print(keys.report(result), "\n", flush=True)
        results.append(keys.summary(result))
    try:
        if room:
            loaded = await rig.load()
            print(f"models warm in {loaded.get('load_s')} s", flush=True)
        for scenario in room:
            record = await rig.rehearse(scenario)
            print(rigs.report(record), "\n", flush=True)
            results.append(rigs.summary(record))
    finally:
        rig.close()
    rig.out.mkdir(parents=True, exist_ok=True)
    passed = sum(1 for r in results if r["passed"])
    (rig.out / "summary.json").write_text(json.dumps({
        "passed": passed, "of": len(results),
        "failed": [{"scenario": r["scenario"],
                    "checks": [c["check"] for c in r["checks"] if not c["ok"]]}
                   for r in results if not r["passed"]],
        "live": live, "sha": head(), "at": dt.datetime.now().isoformat(timespec="seconds"),
    }, indent=1) + "\n", encoding="utf-8", newline="\n")
    print(f"{passed}/{len(results)} passed; reports in {rig.out}")
    return (0 if passed == len(results) else 1), rig.out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m devtools.rehearse",
                                     description="Rehearse the desk's voice in software.")
    parser.add_argument("scenarios", nargs="*", help="scenario names, or all")
    parser.add_argument("--list", action="store_true", help="list the scenarios")
    parser.add_argument("--live", action="store_true",
                        help="real GPT-Live (billed, about $0.05 a minute, capped)")
    parser.add_argument("--save-audio", action="store_true",
                        help="keep the rehearsal's synthetic room as WAV files")
    args = parser.parse_args(argv)
    scenarios = available()
    if args.list:
        for name, scenario in scenarios.items():
            print(f"{name:24} {scenario.about}")
        return 0
    run = chosen(args.scenarios, scenarios)
    if args.live:
        print(f"real GPT-Live: at most ${live_cost(run):.2f} for {len(run)} scenario(s)",
              flush=True)
    try:
        code, _ = asyncio.run(rehearse(run, live=args.live, save_audio=args.save_audio))
    except RuntimeError as exc:
        print(f"could not rehearse: {exc}", file=sys.stderr)
        return 2
    return code


if __name__ == "__main__":
    sys.exit(main())
