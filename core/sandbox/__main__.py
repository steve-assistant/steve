"""`python -m core.sandbox build`: make the module runtime in a checkout.
`python -m core.sandbox probe`: say whether modules can run here (D6)."""

from __future__ import annotations

import argparse
import sys

from core.sandbox import probe, runtime


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m core.sandbox")
    parser.add_argument("command", choices=["build", "probe"])
    args = parser.parse_args(argv)
    if args.command == "build":
        runtime.build()
        return 0
    found = probe.run()
    mode = f" ({found.mode})" if found.mode else ""
    reason = f": {found.reason}" if found.reason else ""
    print(f"{'supported' if found.ok else 'not supported'}{mode}{reason}")
    return 0 if found.ok else 1


if __name__ == "__main__":
    sys.exit(main())
