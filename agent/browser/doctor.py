"""The saved logins' doctor, run by hand at the laptop.

    python -m agent.browser.doctor logins        # what each saved login holds -- never a value
    python -m agent.browser.doctor cookie-scope  # the 7 October mechanism, offline

On 7 October 2026 their LinkedIn login worked once and then looped, and the
investigation could not look at what the saved login held: decrypting it,
even to print cookie names and domains, was refused to a model, as it should
be. So the cause it named -- the sign-in window's import losing every domain
cookie's leading dot, leaving host-only twins the site could neither replace
nor delete -- was inferred from code and timing, not seen.

`logins` is that look, for the user to run: for each saved login, each
cookie's name, domain, whether it is host-only, when it expires, whether the
login's pages would be sent it, and whether it has a twin -- never a value,
and of the stored pages' storage only how many items, never what they say.

`cookie-scope` shows the mechanism itself with no site involved: in the
browser image, with no network at all, a local server forgets a session
cookie the way a site does -- by its own Domain -- and sends the browser
back to try again. Imported without its dot, the cookie survives and the
browser loops (ERR_TOO_MANY_REDIRECTS, as LinkedIn did); with it, the cookie
goes and the page loads. It uses whichever browser image is already built
and builds nothing.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time

from agent.browser.session import cookie_belongs
from core import images
from core.browser import sandbox

# ---------------------------------------------------------------------------
# What a saved login holds
# ---------------------------------------------------------------------------


def describe(state: dict, domains, now: float | None = None) -> list[dict]:
    """A saved state's cookies as rows of what they are, not what they say:
    name, domain, host-only, expiry, expired, belongs, twin. No value is
    copied into a row, whatever the state holds."""
    now = time.time() if now is None else now
    cookies = [c for c in (state or {}).get("cookies") or [] if isinstance(c, dict)]
    seen: dict[tuple, int] = {}
    for c in cookies:
        key = (str(c.get("name")), str(c.get("domain") or "").lower().lstrip("."),
               str(c.get("path") or "/"))
        seen[key] = seen.get(key, 0) + 1
    rows = []
    for c in cookies:
        domain = str(c.get("domain") or "")
        try:
            expires = float(c.get("expires", -1))
        except (TypeError, ValueError):
            expires = -1.0
        key = (str(c.get("name")), domain.lower().lstrip("."), str(c.get("path") or "/"))
        rows.append({
            "name": str(c.get("name"))[:80], "domain": domain[:80],
            "path": str(c.get("path") or "/")[:40], "host_only": not domain.startswith("."),
            "expires": ("session" if expires <= 0 else
                        dt.datetime.fromtimestamp(expires, dt.UTC).strftime("%Y-%m-%d %H:%M")),
            "expired": 0 < expires < now, "belongs": cookie_belongs(domain, domains),
            "twin": seen[key] > 1})
    return rows


def _origins(state: dict) -> list[str]:
    """Each stored page's origin and how many items it keeps -- never the
    items' names or contents."""
    found = []
    for o in (state or {}).get("origins") or []:
        if isinstance(o, dict):
            found.append(f"{str(o.get('origin'))[:80]} ({len(o.get('localStorage') or [])} "
                         "items)")
    return found


def logins_report(store=None, now: float | None = None) -> str:
    """Every saved login, and what its saved session holds, as text."""
    from core.browser import vault

    store = store if store is not None else vault.vault()
    lines = []
    sites = store.sites()
    if not sites:
        return "No saved logins."
    for site in sites:
        lines.append(f"{site.name}: {', '.join(site.domains)} ({site.tier}"
                     f"{', password saved' if site.has_password else ''})")
        state = store.load_state(site.name)
        if state is None:
            lines.append("  no saved session" if not site.has_session
                         else "  its saved session could not be read")
            continue
        rows = describe(state, site.domains, now)
        twins = sum(r["twin"] for r in rows)
        host_only = sum(r["host_only"] and r["belongs"] for r in rows)
        stray = sum(not r["belongs"] for r in rows)
        lines.append(f"  {len(rows)} cookies: {host_only} host-only, {twins} in twins, "
                     f"{stray} for other sites")
        for r in sorted(rows, key=lambda r: (r["domain"].lstrip("."), r["name"])):
            flags = [w for w, on in (("host-only", r["host_only"]), ("TWIN", r["twin"]),
                                     ("expired", r["expired"]),
                                     ("other site", not r["belongs"])) if on]
            lines.append(f"    {r['name']:32} {r['domain']:32} {r['path']:10} "
                         f"{r['expires']:16} {' '.join(flags)}")
        for origin in _origins(state):
            lines.append(f"  storage: {origin}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The mechanism, offline
# ---------------------------------------------------------------------------

# Run inside the browser image with `--network none`: a server on loopback
# that, seeing the stale session, deletes it by Domain=.www.example.test and
# redirects to itself -- what a site does with a session it no longer
# honours -- and Chromium resolving the test names to that server.
REPRODUCE = r'''
import asyncio, http.server, json, threading
from playwright.async_api import async_playwright

class Site(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if "sid=stale" in (self.headers.get("Cookie") or ""):
            self.send_response(302)
            self.send_header("Set-Cookie", "sid=; Domain=.www.example.test; Path=/; Max-Age=0")
            self.send_header("Location", "/")
            self.end_headers()
            return
        body = b"<title>signed out cleanly</title>home"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass

server = http.server.ThreadingHTTPServer(("127.0.0.1", 8080), Site)
threading.Thread(target=server.serve_forever, daemon=True).start()

async def attempt(pw, domain):
    browser = await pw.chromium.launch(args=[
        "--host-resolver-rules=MAP www.example.test 127.0.0.1, MAP example.test 127.0.0.1"])
    context = await browser.new_context(storage_state={"origins": [], "cookies": [{
        "name": "sid", "value": "stale", "domain": domain, "path": "/", "expires": -1,
        "httpOnly": True, "secure": False, "sameSite": "Lax"}]})
    page = await context.new_page()
    try:
        await page.goto("http://www.example.test:8080/", timeout=20000)
        outcome = "loaded: " + await page.title()
    except Exception as exc:
        outcome = str(exc).splitlines()[0][:200]
    left = [c["domain"] for c in await context.cookies() if c["name"] == "sid"]
    await browser.close()
    return {"imported_as": domain, "outcome": outcome, "sid_left_on": left}

async def main():
    async with async_playwright() as pw:
        found = [await attempt(pw, "www.example.test"), await attempt(pw, ".www.example.test")]
    print(json.dumps(found))

asyncio.run(main())
'''


def _built_image(run) -> str | None:
    """The browser image Steve built here, by its ID (`core/images.py`):
    this check builds nothing, and runs nothing it did not build."""
    return images.built(sandbox.image("browser"), run)


def reproduce_args(image: str) -> list[str]:
    """The throwaway container the reproduction runs in: no network at all,
    the sandbox's own hardening, capped, removed when done."""
    return ["run", "--rm", "--network", "none", *sandbox.HARDENING, "--user", "pwuser",
            "--memory", "1g", "--memory-swap", "1g", "--cpus", "1", "--pids-limit", "256",
            "--shm-size", "512m", "--tmpfs", "/tmp:rw,size=64m,mode=1777",
            "--tmpfs", "/home/pwuser:rw,size=256m,mode=1777",
            image, "timeout", "150", "python", "-c", REPRODUCE]


def cookie_scope(run=None) -> dict:
    """Run the reproduction; what happened, and whether it shows the
    mechanism: the dotless import loops and the dotted one loads."""
    run = run or sandbox._docker  # looked up when called: the unit tests fence it
    image = _built_image(run)
    if image is None:
        return {"ok": False, "error": "no browser image is built: `python -m core.browser setup`"}
    done = run(reproduce_args(image), 200)
    lines = [line for line in done.stdout.splitlines() if line.startswith("[")]
    try:
        results = json.loads(lines[-1])
    except (IndexError, ValueError):
        return {"ok": False, "image": image,
                "error": (done.stderr.strip()[-400:] or "the reproduction said nothing")}
    by = {r.get("imported_as"): r for r in results}
    dotless, dotted = by.get("www.example.test", {}), by.get(".www.example.test", {})
    confirmed = ("ERR_TOO_MANY_REDIRECTS" in str(dotless.get("outcome"))
                 and str(dotted.get("outcome", "")).startswith("loaded"))
    return {"ok": True, "image": image, "results": results, "confirmed": confirmed}


def _cookie_scope_text() -> int:
    said = cookie_scope()
    if not said["ok"]:
        print(f"Could not run it: {said['error']}")
        return 1
    print(f"In {said['image']}, with no network:")
    for r in said["results"]:
        print(f"  imported as {r['imported_as']!r:22} -> {r['outcome']}"
              f"  (sid left on: {', '.join(r['sid_left_on']) or 'nothing'})")
    print("\nConfirmed: a cookie imported without its dot outlives the site's delete and the "
          "browser loops; with its dot it is deleted and the page loads." if said["confirmed"]
          else "\nNot confirmed: the two imports did not behave as the 7 October account says.")
    return 0 if said["confirmed"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="The saved logins' doctor.")
    parser.add_argument("action", choices=["logins", "cookie-scope"])
    action = parser.parse_args().action
    if action == "logins":
        print(logins_report())
        return 0
    return _cookie_scope_text()


if __name__ == "__main__":
    sys.exit(main())
