"""The browser sandbox's doctor, run by hand at the laptop.

    python -m core.browser setup    # pull the bases and build both images
    python -m core.browser check    # prove a live session holds, then tear it down
    python -m core.browser reap     # remove sessions whose worker is gone

`check` starts a real session and, from inside the browser container, tries
what a hostile page would (`core/browser/probe.py`): an outside name, a
public address directly, the laptop's supervisor, and the gate asked for
somewhere private -- each must fail -- and the gate asked for example.com,
which must work. Then the driver opens https://example.com and reads its
title, httpbin.org must be told ordinary Chrome rather than HeadlessChrome,
and teardown must leave nothing behind. The browser stays off
(`browser.enabled` in core/policy.yaml) until this has passed.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

from core import localapi
from core.browser import sandbox


def _setup() -> int:
    if not sandbox.available():
        print("Docker is not running: start Docker Desktop first.")
        return 1
    print("Building the browser images (the first time pulls about 2 GB)...")
    built = sandbox.build()
    for kind in ("gate", "browser"):
        tag = sandbox.image(kind)
        print(f"  {kind:8} {tag}  {'built' if tag in built else 'already there'}")
    return 0


def _driver(box: sandbox.Sandbox, commands: list[dict]) -> list[dict]:
    lines = "".join(json.dumps({"id": i, **c}) + "\n" for i, c in enumerate(commands))
    done = subprocess.run(sandbox.driver_argv(box), input=lines, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=180)
    said = []
    for line in done.stdout.splitlines():
        try:
            said.append(json.loads(line))
        except ValueError:
            continue
    if not said:
        said.append({"ok": False, "error": done.stderr.strip()[-400:] or "no answer"})
    return said


def _check() -> int:
    if not sandbox.ready():
        print("Not set up: run `python -m core.browser setup` first.")
        return 1
    box = sandbox.start(sandbox.this_process())
    failures = 0
    try:
        print(f"Session {box.session}: gate at {box.proxy}\n")
        # The supervisor's port is chosen at each start; with none running,
        # there is no port of its to probe.
        backend = localapi.read()
        if backend is None:
            print("  (Steve's backend is not running, so its port is not probed.)")
        probed = subprocess.run(
            ["docker", "exec", box.browser, "python", "/opt/agent/probe.py",
             box.proxy.removeprefix("http://").rsplit(":", 1)[0],
             *([str(backend.port)] if backend else [])],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
        try:
            results = json.loads(probed.stdout)
        except ValueError:
            print(f"The probe did not run: {probed.stderr.strip()[-400:]}")
            return 1
        for r in results:
            failures += not r["pass"]
            mark = "ok  " if r["pass"] else "FAIL"
            got = "reached" if r["reached"] else "blocked"
            print(f"  {mark} {r['probe']:44} expected {r['expect']:7} got {got:7} "
                  f"{r.get('detail', '')}")

        said = _driver(box, [{"cmd": "start", "proxy": box.proxy},
                             {"cmd": "open", "url": "https://example.com/"},
                             {"cmd": "read"}, {"cmd": "close"}])
        read = next((s for s in said if s.get("id") == 2), said[-1])
        title_ok = read.get("ok") and "Example Domain" in str(read.get("title"))
        failures += not title_ok
        print(f"\n  {'ok  ' if title_ok else 'FAIL'} the driver opened example.com: "
              f"{read.get('title') or read.get('error')}")
        # Which library drove it (8 October 2026: Patchright, for LinkedIn's
        # sake). An image that fell back to Playwright still opens pages, so
        # nothing above would say it was not the one built for.
        engine = str(next((s for s in said if s.get("id") == 0), {}).get("engine") or "playwright")
        failures += engine != "patchright"
        print(f"  {'ok  ' if engine == 'patchright' else 'FAIL'} the driver runs on patchright: "
              f"{engine}")

        # Logins (core/browser/vault.py): their password typed only on its own
        # site, into a real password box; a session saved only from its
        # site; a logged-in browser kept on its site.
        said = _driver(box, [
            {"cmd": "start", "proxy": box.proxy},
            {"cmd": "open", "url": "https://httpbin.org/forms/post"},
            {"cmd": "read"},
            {"cmd": "fill_login", "user_ref": "e1", "pass_ref": "e2", "username": "u",
             "password": "p", "domains": ["example.org"]},
            {"cmd": "fill_login", "user_ref": "e1", "pass_ref": "e2", "username": "u",
             "password": "p", "domains": ["httpbin.org"]},
            {"cmd": "export_state", "domains": ["example.org"]},
            {"cmd": "frame"},
            {"cmd": "close"}])
        by_id = {s.get("id"): s for s in said}
        checks = [
            ("a login is not typed on another site's page",
             "not one of the login's own sites" in str(by_id.get(3, {}).get("error"))),
            ("a login is not typed into a field that is not a password box",
             "not a password box" in str(by_id.get(4, {}).get("error"))),
            ("a session is not saved from another site",
             by_id.get(5, {}).get("ok") is False and "state" not in by_id.get(5, {})),
            ("the live view gets the screen", bool(by_id.get(6, {}).get("jpeg"))),
        ]
        # What a site is told the browser is (7 October 2026: "HeadlessChrome"
        # was refused on the first request by two sites): ordinary Chrome.
        said = _driver(box, [{"cmd": "start", "proxy": box.proxy},
                             {"cmd": "open", "url": "https://httpbin.org/headers"},
                             {"cmd": "read"}, {"cmd": "close"}])
        told = str(next((s for s in said if s.get("id") == 2), {}).get("text") or "")
        agent = next((line for line in told.splitlines() if '"User-Agent"' in line), "")
        checks.append(("a site is told ordinary Chrome, not HeadlessChrome",
                       "Chrome/" in agent and "Headless" not in agent))
        # The client hints are Chromium's own; reported, not failed, so the
        # containment checks above keep their meaning.
        hints = [line.strip() for line in told.splitlines() if "Sec-Ch-Ua" in line]
        if any("Headless" in line for line in hints):
            print("  note the client hints still say Headless: " + "; ".join(hints)[:200])
        said = _driver(box, [
            {"cmd": "start", "proxy": box.proxy, "storage_state": {"cookies": [], "origins": []},
             "site_domains": ["example.com"]},
            {"cmd": "open", "url": "https://example.com/"},
            {"cmd": "open", "url": "https://httpbin.org/get"},
            {"cmd": "close"}])
        by_id = {s.get("id"): s for s in said}
        checks.append(("a logged-in browser opens its own site",
                       bool(by_id.get(1, {}).get("ok"))))
        # Refused, in the driver's words for any browser kept to its sites
        # -- a login's, or a browsing worker's (4 October 2026).
        left_site = by_id.get(2, {})
        checks.append(("a logged-in browser does not leave its site",
                       left_site.get("ok") is False
                       and "is kept to" in str(left_site.get("error"))))
        for name, passed in checks:
            failures += not passed
            print(f"  {'ok  ' if passed else 'FAIL'} {name}")
    finally:
        sandbox.stop(box)
    left = sandbox.leftovers(box.session)
    failures += bool(left)
    print(f"  {'ok  ' if not left else 'FAIL'} teardown left "
          f"{', '.join(left) if left else 'nothing behind'}")
    print("\nThe sandbox holds." if not failures else f"\n{failures} check(s) failed.")
    return 0 if not failures else 1


def _reap() -> int:
    gone = sandbox.reap()
    print(f"Removed: {', '.join(gone)}" if gone else "Nothing to remove.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="The browser sandbox's doctor.")
    parser.add_argument("action", choices=["setup", "check", "reap"])
    return {"setup": _setup, "check": _check, "reap": _reap}[parser.parse_args().action]()


if __name__ == "__main__":
    sys.exit(main())
