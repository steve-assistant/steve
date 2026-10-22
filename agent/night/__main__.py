"""`python -m agent.night`: run a night now."""

from __future__ import annotations

import asyncio

from agent.night import DEFAULT_BUDGET_USD, digest, run_night


def _main() -> None:
    """Run a night now, from the command line.

    The point of being able to trigger it by hand is that "wait until 3am" is
    a terrible development loop, and a window nobody has ever watched run is
    a window nobody knows the behaviour of.
    """
    import argparse
    import json as _json

    from core import db

    parser = argparse.ArgumentParser(description="The agent's free time")
    parser.add_argument("--budget", type=float, default=DEFAULT_BUDGET_USD)
    parser.add_argument("--no-learn", action="store_true")
    parser.add_argument("--no-research", action="store_true")
    parser.add_argument("--no-improve", action="store_true")
    args = parser.parse_args()

    conn = db.open_store()
    report = asyncio.run(
        run_night(
            conn,
            budget_usd=args.budget,
            do_learn=not args.no_learn,
            do_research=not args.no_research,
            do_improve=not args.no_improve,
        )
    )
    print(_json.dumps(report.as_dict(), indent=2))
    print()
    print(digest(report, conn))


if __name__ == "__main__":
    _main()
