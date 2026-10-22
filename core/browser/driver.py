"""The browser's driver: the only thing that runs inside the sandbox besides
Chromium, and the only way the agent touches a page.

It reads one JSON command per line on stdin and answers one JSON line on
stdout -- over `docker exec`, so there is no port to reach it by -- and its
commands are a fixed list: start, open, read, click, scroll, back,
screenshot, search, describe, submit, close. There is no command that runs
a script the model wrote, sets a cookie, downloads or uploads a file.
Dialogs are dismissed, downloads refused, file choosers left unanswered,
every permission (location, camera, microphone, clipboard, notifications)
denied, service workers blocked, and a page that opens a new window has it
closed and its address reported. Every request goes through the session's
gate (`core/browser/gate.py`), with nothing exempt, loopback included.

**What the agent may type is decided here, deterministically.** A page's
scripts see keystrokes as they happen, so typing is disclosure, not a draft:

- `search` types at most `SEARCH_CHARS` characters into a field
  `is_search_field` recognises as a search box -- the same exposure as a
  `web_search` query -- and presses Enter.
- Anything else is a *submit plan*: fields and their values, and the button
  to press. `describe` reports what the plan would touch, so the agent can
  put it on an approval card *before a character reaches the page*; only an
  approved plan is carried out, by `submit`, which first checks the page is
  still the one the user approved (`check_plan`) and refuses anything that looks
  like spending money (`looks_like_payment`) -- payments are not enabled.

The pure checks at the top import nothing outside the standard library, so
the gate tests run them on the host; the browser library is imported only in
`main`, through `load_engine`: Patchright when the image has it, Playwright
when it does not, so an older image keeps working. On 7 October 2026 LinkedIn
loaded PerimeterX's fingerprinting beside their replayed login and answered it
with redirect loops, a blank /feed/ and HTTP 999; stock Playwright talks to
the page through `Runtime.enable`, which a page can detect, and Patchright
(a patched Playwright, same API) does not. Nothing here sets a persistent
profile (`launch_persistent_context`, a `user_data_dir`) though Patchright's
README recommends one: a profile kept between sessions would hold their session
on the laptop's disk in the clear, where the vault keeps it under DPAPI.
Every session starts empty and gets its saved login only through
`storage_state`. Frozen, with the rest of `core/`.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import re
import sys
from urllib.parse import urlsplit

try:
    from core import egress
except ImportError:  # inside the browser image, egress.py sits beside this file
    import egress  # type: ignore[no-redef]

SEARCH_CHARS = 200
TEXT_CHARS = 8_000
# Downloads stay in the container's own /tmp until the worker asks for one
# (core/browser/files.py quarantines it). Capped in size and number, so a page
# that starts a hundred downloads fills nothing.
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
MAX_DOWNLOADS = 10
DOWNLOADS = "/tmp/downloads"
MAX_ELEMENTS = 150
MAX_FIELDS = 20
VALUE_CHARS = 2_000
COMMAND_TIMEOUT_S = 60.0
NAV_TIMEOUT_MS = 30_000
# How a logged-in page is waited for (`Driver._settle_on_content`): its main
# part's text at least this long and unchanged for two looks in a row, looked
# at every quarter second, for five seconds at most -- no longer than the
# networkidle wait it replaces, so a slow page is given what it was given.
CONTENT_MIN_CHARS = 300
CONTENT_POLL_S = 0.25
CONTENT_POLLS = 20
CONTENT_STEADY_POLLS = 2
CONTENT_SIZE = ("() => { const part = document.querySelector('main') || document.body; "
                "return part ? part.innerText.length : 0; }")
REF = re.compile(r"^e\d{1,4}$")

# ---------------------------------------------------------------------------
# The checks: pure, on element descriptors as `MARK` reports them
# ---------------------------------------------------------------------------

_SEARCH_WORDS = re.compile(r"(^|[^a-z])(q|query|search|searchbox|keywords?|find|lookup)([^a-z]|$)",
                           re.IGNORECASE)


def is_search_field(el: dict) -> bool:
    """A field the agent may type a search into on its own. Deterministic:
    the field calls itself a search box (role `searchbox`, `type=search`), or
    is a plain text box whose name, id, label or placeholder says search --
    and is not in a form with a password in it, and does not ask the browser
    to autofill something personal."""
    tag = (el.get("tag") or "").lower()
    kind = (el.get("type") or "").lower()
    role = (el.get("role") or "").lower()
    if el.get("form_has_password") or el.get("disabled"):
        return False
    autocomplete = (el.get("autocomplete") or "").lower().strip()
    if autocomplete not in ("", "off", "on"):
        return False
    if role == "searchbox" or (tag == "input" and kind == "search"):
        return True
    if tag == "input":
        # Plain text only: email, password, tel, number and the rest are
        # never a search box, whatever they are called.
        if kind not in ("", "text"):
            return False
    elif not (tag == "textarea" or role in ("textbox", "combobox")):
        return False
    words = " ".join(str(el.get(k) or "") for k in ("name", "id", "label", "placeholder"))
    return bool(_SEARCH_WORDS.search(words))


_PAYMENT_FIELD = re.compile(
    r"card.?(number|num|no\b)|credit.?card|debit.?card|\bcc.?(num|number|exp|csc|cvv)|"
    r"\bcvv|\bcvc|\bcsc\b|security.?code|expir(y|ation)|exp.?(date|month|year)|\biban\b|"
    r"routing.?number|account.?number|sort.?code", re.IGNORECASE)
_PAYMENT_BUTTON = re.compile(
    r"\b(pay|purchase|buy|place.?(my.?)?order|complete.?(my.?)?(order|purchase)|"
    r"confirm.?(and.?pay|order|purchase|payment)|check.?out|donate|subscribe|upgrade|"
    r"start.?(free.?)?trial|book.?(and|&).?pay)\b", re.IGNORECASE)
PAYMENT_HOSTS = ("stripe.com", "stripe.network", "braintreegateway.com", "braintree-api.com",
                 "adyen.com", "paypal.com", "paypalobjects.com", "checkout.com", "squareup.com",
                 "klarna.com", "affirm.com", "recurly.com", "chargebee.com", "authorize.net")


def looks_like_payment(fields: list[dict], button: dict | None, frames: list[str]) -> str | None:
    """Why a submit looks like spending money, or None. `fields` is every
    field on the page, not only the plan's: a card number anywhere means
    the page takes payment. Payments in the browser are not enabled."""
    for el in fields:
        if (el.get("autocomplete") or "").lower().startswith("cc-"):
            return "the page has a payment-card field"
        words = " ".join(str(el.get(k) or "") for k in ("name", "id", "label", "placeholder"))
        if _PAYMENT_FIELD.search(words):
            return "the page has a payment-card field"
    for url in frames:
        host = (urlsplit(url).hostname or "").lower()
        if any(host == h or host.endswith("." + h) for h in PAYMENT_HOSTS):
            return "the page holds a payment provider's frame"
    label = str((button or {}).get("label") or "")
    if _PAYMENT_BUTTON.search(label):
        return f"the button says {label[:60]!r}"
    return None


_FILLABLE = ("input", "textarea", "select")


def check_plan(plan: dict, current: dict[str, dict]) -> str | None:
    """Why an approved plan may not be carried out on the page as it is now,
    or None. `current` is the page's elements, freshly described, by ref."""
    fields = plan.get("fields") or []
    button = plan.get("button") or {}
    if not isinstance(fields, list) or not fields or len(fields) > MAX_FIELDS:
        return f"a plan has between 1 and {MAX_FIELDS} fields"
    for item in [*fields, button]:
        ref = str(item.get("ref") or "")
        if not REF.match(ref):
            return f"{ref!r} is not an element reference"
        el = current.get(ref)
        if el is None:
            return f"{ref} is no longer on the page"
        if _label(el) != _label(item):
            return f"{ref} is now {_label(el)!r}, not {_label(item)!r}"
    for item in fields:
        el = current[item["ref"]]
        kind = (el.get("type") or "").lower()
        if (el.get("tag") or "").lower() not in _FILLABLE and not el.get("editable"):
            return f"{item['ref']} is not a field"
        if kind in ("file", "password", "hidden"):
            return f"{item['ref']} is a {kind} field, which a plan may not fill"
        value = str(item.get("value") or "")
        if len(value) > VALUE_CHARS:
            return f"{item['ref']}'s value is longer than {VALUE_CHARS} characters"
        # What a box or a list will do must be exactly what the card said.
        # On 1 October a card read "Medium: Medium" and "Extra Cheese: Extra
        # Cheese"; "Medium" was not a word this took for ticked, so both were
        # left unticked and the order went without them.
        if kind in ("checkbox", "radio") and value not in CHECK_VALUES:
            return (f"{item['ref']} is a {kind}: its value is 'checked' or 'unchecked', "
                    f"not {value[:40]!r}")
        options = el.get("options")
        if (el.get("tag") or "").lower() == "select" and options is not None \
                and value not in options:
            return f"{item['ref']} has no option {value[:60]!r}"
    return None


# The two values a checkbox or radio button may be given in a plan.
CHECK_VALUES = ("checked", "unchecked")


def _label(el: dict) -> str:
    return " ".join(str(el.get("label") or "").split())[:120]


def same_page(a: str, b: str) -> bool:
    return egress.normal(a) == egress.normal(b)


def download_name(suggested: str) -> str:
    """A download's name inside the container: its last path part, safe
    characters only (core/browser/files.quarantine_name does the same on
    the laptop side)."""
    base = re.split(r"[\\/]", suggested or "")[-1]
    base = re.sub(r"[^A-Za-z0-9._ -]+", "_", base).strip(" .")
    return (base or "download")[:100]


def upload_refusal(el: dict | None, label: str) -> str | None:
    """Why a file may not be put into this element: it must be the file
    field the user was shown on the card."""
    if el is None:
        return "the file field is no longer on the page"
    if (el.get("tag") or "") != "input" or (el.get("type") or "") != "file":
        return "that is not a file field"
    if _label(el) != _label({"label": label}):
        return f"the file field is now {_label(el)!r}, not {_label({'label': label})!r}"
    return None


def on_site(url: str, domains) -> bool:
    """Whether `url`'s host is one of `domains` or below one, on a label
    boundary: cas.contoso.example is contoso.example's; evilcontoso.example,
    and contoso.example.x.com, are not. Where a saved login may be used
    (`core/browser/vault.py`)."""
    host = (urlsplit(url or "").hostname or "").rstrip(".").lower()
    return bool(host) and any(
        host == d or host.endswith("." + d)
        for d in (str(x).strip().lower().rstrip(".") for x in domains or ()) if d)


_USER_TYPES = ("", "text", "email", "tel")


def login_refusal(page_url: str, user: dict | None, password: dict | None,
                  domains) -> str | None:
    """Why their username and password may not be typed into these two fields
    on this page, or None. The page is the login's own, over https, and the
    fields are what they claim: a plain or email box, and a real password
    box -- so a look-alike page, or a "password" field that is really a
    search box, gets nothing. Both are in the page's own frame (refs are
    only ever given there)."""
    if urlsplit(page_url or "").scheme != "https":
        return "a login is only typed into an https page"
    if not on_site(page_url, domains):
        host = urlsplit(page_url or "").hostname
        return f"this page ({host}) is not one of the login's own sites"
    if user is None or password is None:
        return "both fields must be on the page"
    if (password.get("tag") or "") != "input" or (password.get("type") or "") != "password":
        return "the password field is not a password box"
    if (user.get("tag") or "") != "input" or (user.get("type") or "") not in _USER_TYPES:
        return "the username field is not a text or email box"
    if user.get("disabled") or password.get("disabled"):
        return "a field is disabled"
    return None


_SIGN_IN = re.compile(r"^\s*(log\s?in|sign\s?in|log\s?on|sign\s?on|continue|next|submit|"
                      r"enter|go)\s*[.!>»→]*\s*$", re.IGNORECASE)


def sign_in_refusal(button: dict | None) -> str | None:
    """Why this element is not the login form's own sign-in button, or None.
    A button saying only "Log in", "Sign in", "Continue", "Next" and their
    like: the login itself, which `fill_login` may press. Anything else --
    "Sign in and post", "Delete account" -- is a click like any other."""
    if button is None:
        return "the sign-in button is not on the page"
    tag, kind = (button.get("tag") or ""), (button.get("type") or "")
    if not (tag == "button" or (tag == "input" and kind in ("submit", "button", "image"))
            or (button.get("role") or "") == "button"):
        return "that is not a button"
    if not _SIGN_IN.match(str(button.get("label") or "")):
        return f"that button says {str(button.get('label') or '')[:40]!r}, not sign in"
    return None


# ---------------------------------------------------------------------------
# The page side: what `read` and `describe` run in the page. Fixed text, the
# driver's own -- no command sends a script of the model's.
# ---------------------------------------------------------------------------

MARK = r"""
(maxElements) => {
  const pick = 'a[href], button, input, select, textarea, summary, [contenteditable=""], ' +
    '[contenteditable="true"], [role=button], [role=link], [role=searchbox], [role=textbox], ' +
    '[role=checkbox], [role=radio], [role=combobox], [role=tab], [role=menuitem], [role=option]';
  document.querySelectorAll('[data-agent-ref]').forEach(e => e.removeAttribute('data-agent-ref'));
  const out = [];
  let n = 0;
  for (const el of document.querySelectorAll(pick)) {
    if (out.length >= maxElements) break;
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    if (r.width === 0 || r.height === 0) continue;
    if (s.visibility === 'hidden' || s.display === 'none') continue;
    if (el.tagName === 'INPUT' && (el.type || '').toLowerCase() === 'hidden') continue;
    n += 1;
    const ref = 'e' + n;
    el.setAttribute('data-agent-ref', ref);
    const byFor = el.id ? document.querySelector('label[for="' + CSS.escape(el.id) + '"]') : null;
    const own = el.labels && el.labels[0] && el.labels[0].innerText;
    // A button's value is its caption. A field's value is what is in it --
    // a typed password, once logins exist -- and is never reported.
    const kind = (el.getAttribute('type') || '').toLowerCase();
    const caption = (el.tagName === 'BUTTON' || ['submit', 'button', 'reset'].includes(kind))
      ? el.value : '';
    const label = el.getAttribute('aria-label') || own
      || (byFor && byFor.innerText) || el.getAttribute('placeholder') || el.innerText
      || caption || el.getAttribute('title') || el.getAttribute('alt') || '';
    out.push({
      ref, tag: el.tagName.toLowerCase(), type: (el.getAttribute('type') || '').toLowerCase(),
      role: (el.getAttribute('role') || '').toLowerCase(),
      label: String(label).replace(/\s+/g, ' ').trim().slice(0, 120),
      name: el.getAttribute('name') || '', id: el.id || '',
      placeholder: el.getAttribute('placeholder') || '',
      autocomplete: el.getAttribute('autocomplete') || '',
      href: el.tagName === 'A' ? el.href : '',
      editable: el.isContentEditable || false,
      disabled: !!el.disabled,
      options: el.tagName === 'SELECT'
        ? Array.from(el.options).slice(0, 60).map(o => o.label.trim()) : undefined,
      form_has_password: !!(el.form && el.form.querySelector('input[type=password]')),
    });
  }
  return out;
}
"""

SCROLLED = r"""
() => {
  const doc = document.documentElement;
  const height = Math.max(doc ? doc.scrollHeight : 0,
                          document.body ? document.body.scrollHeight : 0);
  return {height, at_end: window.innerHeight + window.scrollY >= height - 8};
}
"""

FIELDS = r"""
() => Array.from(document.querySelectorAll('input, select, textarea')).slice(0, 300).map(el => ({
  tag: el.tagName.toLowerCase(), type: (el.getAttribute('type') || '').toLowerCase(),
  name: el.getAttribute('name') || '', id: el.id || '',
  placeholder: el.getAttribute('placeholder') || '',
  autocomplete: el.getAttribute('autocomplete') || '',
  label: el.getAttribute('aria-label')
    || (el.labels && el.labels[0] && el.labels[0].innerText) || '',
}))
"""

# ---------------------------------------------------------------------------
# The driver itself (inside the container)
# ---------------------------------------------------------------------------

CHROMIUM_ARGS = [
    "--disable-background-networking", "--disable-component-update", "--disable-default-apps",
    "--disable-domain-reliability", "--disable-sync", "--no-first-run",
    "--disable-features=Translate,OptimizationHints,MediaRouter,DialMediaRouteProvider",
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--webrtc-ip-handling-policy=disable_non_proxied_udp",
    # `navigator.webdriver` false, as in their own browser. See `regular_user_agent`.
    "--disable-blink-features=AutomationControlled",
]
# Two of those Patchright takes out of Playwright's own defaults "to avoid
# detection as a Stealth Driver" (its README, "Command Flags Leaks"); passing
# them back by hand would undo that. Neither is a wall here: background
# networking, sync and domain reliability stay off above, and whatever the
# browser still reached for would go through the gate, which logs it.
PATCHRIGHT_DROPS = ("--disable-component-update", "--disable-default-apps")
ENGINES = ("patchright", "playwright")


def launch_args(engine: str) -> list[str]:
    """The browser's own flags for this engine: all of `CHROMIUM_ARGS`, less
    what Patchright documents leaving out (`PATCHRIGHT_DROPS`)."""
    if engine == "patchright":
        return [a for a in CHROMIUM_ARGS if a not in PATCHRIGHT_DROPS]
    return list(CHROMIUM_ARGS)


def load_engine() -> tuple[object, str]:
    """The browser library's `async_playwright` and its name: Patchright if
    the image has it, else Playwright -- the image before 8 October 2026
    has only that, and must go on working. Both are the same API for every
    call the driver makes; the page's scripts (`MARK`, `SCROLLED`) run in
    Patchright's isolated world, which shares the page's DOM and window
    geometry and sees none of the page's own variables, as they need."""
    try:
        from patchright.async_api import async_playwright
        return async_playwright, "patchright"
    except ImportError:
        from playwright.async_api import async_playwright
        return async_playwright, "playwright"


def regular_user_agent(version: str) -> str:
    """The user agent of ordinary Chrome on Linux at this browser's version,
    reduced as Chrome reduces its own ("141.0.0.0").

    The headless browser called itself "HeadlessChrome", and on 7 October
    2026 sites refused it on the first request: reserveamerica.com a 403,
    destateparks.com CloudFront's "request could not be satisfied", both
    from one host and before any script ran, while LinkedIn loaded its
    PerimeterX fingerprinting beside their replayed login. It stays Linux,
    which it is -- a Windows agent over a Linux platform and fonts is a
    louder mismatch than the one it replaces. What a site may do with an
    account it decides is automated is a risk taken when its login is saved."""
    major = (version or "").split(".", 1)[0]
    major = major if major.isdigit() else "140"
    return (f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{major}.0.0.0 Safari/537.36")


class Driver:
    def __init__(self, playwright, engine: str = "playwright") -> None:
        self.pw = playwright
        self.engine = engine if engine in ENGINES else "playwright"
        self.browser = None
        self.context = None
        self.page = None
        self.popups: list[str] = []
        self.elements: dict[str, dict] = {}
        self.domains: list[str] = []
        self.blocked: list[str] = []
        self.downloads: list[dict] = []
        self.download_notes: list[str] = []
        # Started from a saved login's session (`_settle`).
        self.logged_in = False

    async def run(self, msg: dict) -> dict:
        cmd = str(msg.get("cmd") or "")
        handler = getattr(self, f"cmd_{cmd}", None)
        if handler is None:
            return {"ok": False, "error": f"no such command: {cmd[:40]!r}"}
        if cmd != "start" and self.page is None:
            return {"ok": False, "error": "the browser is not started"}
        return await handler(msg)

    async def cmd_start(self, msg: dict) -> dict:
        proxy = str(msg.get("proxy") or "")
        if not proxy.startswith("http://"):
            return {"ok": False, "error": "start needs the gate's address"}
        state = msg.get("storage_state")
        self.domains = [str(d) for d in (msg.get("site_domains") or [])]
        if state is not None and not self.domains:
            return {"ok": False, "error": "a saved login is only opened on its own site"}
        self.logged_in = bool(self.domains) and "storage_state" in msg
        launch = {"headless": True, "args": launch_args(self.engine),
                  # Everything through the gate, loopback included.
                  "proxy": {"server": proxy, "bypass": "<-loopback>"}}
        try:
            # Chromium's own headless mode, the whole browser rather than the
            # stripped headless shell Playwright uses by default: what a page
            # can measure of it is much nearer their Chrome. The shell if the
            # image has no full Chromium.
            self.browser = await self.pw.chromium.launch(channel="chromium", **launch)
        except Exception:  # noqa: BLE001 - the shell is the browser this always used
            self.browser = await self.pw.chromium.launch(**launch)
        self.context = await self.browser.new_context(
            accept_downloads=True, service_workers="block", permissions=[],
            # One person, in the user's own zone and language (the worker
            # sends both: this runs in the container, whose clock is UTC and
            # whose language is none), with Chrome's own way of asking for
            # it. The language was en-US for everyone.
            viewport={"width": 1280, "height": 900}, locale=str(msg.get("locale") or "en-US"),
            timezone_id=str(msg.get("timezone") or "UTC"),
            extra_http_headers={"Accept-Language":
                                str(msg.get("accept_language") or "en-US,en;q=0.9")},
            user_agent=regular_user_agent(self.browser.version), storage_state=state)
        if self.domains:
            # A logged-in session stays on its site: the page itself may not
            # be taken anywhere else -- by a link, a script or a redirect --
            # so nothing it shows can be carried off to another site.
            await self.context.route("**/*", self._stay_on_site)
        self.page = await self.context.new_page()
        self.page.on("dialog", lambda dialog: asyncio.ensure_future(dialog.dismiss()))
        self.page.on("download", lambda d: asyncio.ensure_future(self._download(d)))
        self.context.on("page", self._popup)
        # Which library is driving, for the doctor: an image that quietly fell
        # back to Playwright would look the same from outside.
        return {"ok": True, "engine": self.engine}

    async def _download(self, download) -> None:
        """Keep a download in the container, if there is room for it."""
        import os

        name = download_name(download.suggested_filename)
        if len(self.downloads) >= MAX_DOWNLOADS:
            await download.cancel()
            self.download_notes.append(f"{name}: not kept, {MAX_DOWNLOADS} already")
            return
        os.makedirs(DOWNLOADS, exist_ok=True)
        path = f"{DOWNLOADS}/{len(self.downloads) + 1}-{name}"
        try:
            await download.save_as(path)
        except Exception as exc:  # noqa: BLE001 - a failed download is a note
            self.download_notes.append(f"{name}: {type(exc).__name__}")
            return
        size = os.path.getsize(path)
        if size > MAX_DOWNLOAD_BYTES:
            os.remove(path)
            self.download_notes.append(f"{name}: not kept, larger than 20 MB")
            return
        self.downloads.append({"name": f"{len(self.downloads) + 1}-{name}", "size": size,
                               "url": download.url[:300], "path": path})

    async def _stay_on_site(self, route) -> None:
        request = route.request
        if (request.is_navigation_request() and request.frame.parent_frame is None
                and not on_site(request.url, self.domains)):
            self.blocked.append(urlsplit(request.url).hostname or request.url[:80])
            await route.abort("blockedbyclient")
            return
        await route.continue_()

    def _popup(self, page) -> None:
        if page is self.page:
            return
        self.popups.append(page.url or "(a new window)")
        asyncio.ensure_future(page.close())

    async def _settle(self) -> None:
        """Give a page a moment to draw itself after it has been asked for.

        An ordinary page: until the network goes quiet, for five seconds at
        most. A logged-in one waits for its content instead
        (`_settle_on_content`): LinkedIn never goes quiet -- tracking and
        telemetry beacons run for as long as the page is open -- so on 8
        October 2026 about five seconds of every 8.5-second page load was this
        wait running out."""
        if self.logged_in:
            await self._settle_on_content()
            return
        with contextlib.suppress(Exception):  # a busy page is still a page
            await self.page.wait_for_load_state("networkidle", timeout=5_000)

    async def _settle_on_content(self) -> None:
        """Until the text of the page's main part has some substance and has
        stopped growing for `CONTENT_STEADY_POLLS` looks, or `CONTENT_POLLS`
        looks have passed. A page that is slow to draw, or that never settles,
        is read as it is -- as the networkidle wait it replaces did."""
        last, steady = -1, 0
        for _ in range(CONTENT_POLLS):
            with contextlib.suppress(Exception):  # a page mid-navigation has no size yet
                size = int(await self.page.evaluate(CONTENT_SIZE))
                if size >= CONTENT_MIN_CHARS and size == last:
                    steady += 1
                    if steady >= CONTENT_STEADY_POLLS:
                        return
                else:
                    steady = 0
                last = size
            await asyncio.sleep(CONTENT_POLL_S)

    async def _brief(self) -> dict:
        popups, self.popups = self.popups, []
        blocked, self.blocked = self.blocked, []
        said = {"ok": True, "url": self.page.url, "title": await self.page.title()}
        if popups:
            said["closed_popups"] = popups[:5]
        if blocked:
            said["kept_on_site"] = (f"the page tried to leave the sites this browser is kept "
                                    f"to, for {', '.join(dict.fromkeys(blocked))}; it was "
                                    "kept here")
        return said

    async def cmd_open(self, msg: dict) -> dict:
        url = str(msg.get("url") or "").strip()
        if urlsplit(url).scheme.lower() not in ("http", "https"):
            return {"ok": False, "error": "only http and https"}
        if self.domains and not on_site(url, self.domains):
            # A logged-in browser, or a browsing worker's (agent/workers.py):
            # either way kept to its sites, whatever a page links to.
            return {"ok": False, "error": "this browser is kept to "
                                          f"{', '.join(self.domains)}; other sites are not "
                                          "opened in it"}
        response = await self.page.goto(url, wait_until="domcontentloaded",
                                        timeout=NAV_TIMEOUT_MS)
        await self._settle()
        said = await self._brief()
        # The answer's status: a 403 page has a title too, and on 7 October
        # "403 Forbidden" reached the model as an open that went fine.
        status = getattr(response, "status", None)
        if isinstance(status, int):
            said["status"] = status
        return said

    async def cmd_read(self, msg: dict) -> dict:
        # A slow page is read once it has arrived, or said to be loading: an
        # empty read of a page still on its way read as a blank page
        # (1 October, a test site that took over 30 s to answer).
        with contextlib.suppress(Exception):
            await self.page.wait_for_load_state("domcontentloaded", timeout=15_000)
        # Where in the page's text to start: past TEXT_CHARS a long page was
        # unreadable, however far it was scrolled.
        start = max(0, int(msg.get("start") or 0))
        found = await self.page.evaluate(MARK, MAX_ELEMENTS)
        self.elements = {el["ref"]: el for el in found}
        text = await self.page.evaluate("() => document.body ? document.body.innerText : ''")
        said = await self._brief()
        said.update(text=text[start:start + TEXT_CHARS],
                    truncated=len(text) > start + TEXT_CHARS, chars=len(text),
                    elements=[{k: v for k, v in el.items() if v not in ("", False, None)}
                              for el in found],
                    frames=[f.url for f in self.page.frames[1:20]])
        if await self.page.evaluate("() => document.readyState") == "loading":
            said["still_loading"] = "the page is still arriving; read it again in a moment"
        return said

    def _locate(self, ref: str):
        if not REF.match(ref or ""):
            raise ValueError(f"{ref!r} is not an element reference")
        return self.page.locator(f'[data-agent-ref="{ref}"]')

    async def cmd_click(self, msg: dict) -> dict:
        ref = str(msg.get("ref") or "")
        el = self.elements.get(ref)
        if el and (el.get("type") or "") == "file":
            return {"ok": False, "error": "uploads are not enabled"}
        await self._locate(ref).click(timeout=10_000)
        await self._settle()
        return await self._brief()

    async def cmd_scroll(self, msg: dict) -> dict:
        down = str(msg.get("direction") or "down").lower() != "up"
        await self.page.mouse.wheel(0, 800 if down else -800)
        await asyncio.sleep(0.3)
        said = await self._brief()
        # How tall the page is and whether this is its end: a page that loads
        # as it is scrolled is whole once its end stops moving (the tools'
        # `browse_read(whole=True)`).
        with contextlib.suppress(Exception):
            where = await self.page.evaluate(SCROLLED)
            if isinstance(where, dict):
                said.update(height=int(where.get("height") or 0),
                            at_end=bool(where.get("at_end")))
        return said

    async def cmd_back(self, msg: dict) -> dict:
        await self.page.go_back(timeout=NAV_TIMEOUT_MS)
        await self._settle()
        return await self._brief()

    async def cmd_screenshot(self, msg: dict) -> dict:
        png = await self.page.screenshot(type="png")
        said = await self._brief()
        said["png"] = base64.b64encode(png).decode("ascii")
        return said

    async def _fresh(self) -> dict[str, dict]:
        """The page's elements described again, refs kept where they still
        stand -- a plan is checked against the page as it is now."""
        found = await self.page.evaluate(MARK, MAX_ELEMENTS)
        self.elements = {el["ref"]: el for el in found}
        return self.elements

    async def cmd_search(self, msg: dict) -> dict:
        ref, text = str(msg.get("ref") or ""), str(msg.get("text") or "")
        if not text.strip() or len(text) > SEARCH_CHARS or "\n" in text or "\r" in text:
            return {"ok": False, "error": f"a search is one line of at most {SEARCH_CHARS} "
                                          "characters"}
        el = (await self._fresh()).get(ref)
        if el is None or not is_search_field(el):
            return {"ok": False, "error": f"{ref} is not a search box; typing anything else "
                                          "into a page is a submit plan, which the user "
                                          "approves"}
        target = self._locate(ref)
        await target.fill(text, timeout=10_000)
        await target.press("Enter", timeout=10_000)
        await self._settle()
        return await self._brief()

    async def cmd_describe(self, msg: dict) -> dict:
        refs = [str(r) for r in (msg.get("fields") or [])][:MAX_FIELDS]
        button = str(msg.get("button") or "")
        current = await self._fresh()
        missing = [r for r in [*refs, button] if r not in current]
        if missing:
            return {"ok": False, "error": f"not on the page: {', '.join(missing)}"}
        everything = await self.page.evaluate(FIELDS)
        frames = [f.url for f in self.page.frames]
        said = await self._brief()
        said.update(fields=[current[r] for r in refs], button=current[button],
                    payment=looks_like_payment(everything, current[button], frames))
        return said

    async def cmd_submit(self, msg: dict) -> dict:
        plan = msg.get("plan") or {}
        if not same_page(self.page.url, str(plan.get("expected_url") or "")):
            return {"ok": False, "error": "the page changed since the user approved it"}
        current = await self._fresh()
        if why := check_plan(plan, current):
            return {"ok": False, "error": f"the page changed since the user approved it: {why}"}
        everything = await self.page.evaluate(FIELDS)
        frames = [f.url for f in self.page.frames]
        if why := looks_like_payment(everything, current[plan["button"]["ref"]], frames):
            return {"ok": False, "error": f"payments are not enabled ({why})"}
        for item in plan["fields"]:
            el, value = current[item["ref"]], str(item.get("value") or "")
            target = self._locate(item["ref"])
            kind = (el.get("type") or "").lower()
            if el.get("tag") == "select":
                await target.select_option(label=value, timeout=10_000)
            elif kind in ("checkbox", "radio"):
                await target.set_checked(value == "checked", timeout=10_000)
            else:
                await target.fill(value, timeout=10_000)
        await self._locate(plan["button"]["ref"]).click(timeout=10_000)
        await self._settle()
        said = await self._brief()
        text = await self.page.evaluate("() => document.body ? document.body.innerText : ''")
        said["text"] = text[:2_000]
        return said

    async def cmd_act(self, msg: dict) -> dict:
        """A click the user approved: carried out only on the page they approved it
        for, on the element with the label they were shown."""
        if not same_page(self.page.url, str(msg.get("expected_url") or "")):
            return {"ok": False, "error": "the page changed since the user approved it"}
        ref = str(msg.get("ref") or "")
        el = (await self._fresh()).get(ref)
        if el is None or _label(el) != _label({"label": msg.get("label")}):
            return {"ok": False, "error": "the page changed since the user approved it: "
                                          f"{ref} is not {str(msg.get('label'))[:60]!r} now"}
        # Looked at again, as a submit is: a page can turn into a checkout
        # between the card and the yes, and payments are not enabled.
        everything = await self.page.evaluate(FIELDS)
        if why := looks_like_payment(everything, el, [f.url for f in self.page.frames]):
            return {"ok": False, "error": f"payments are not enabled ({why})"}
        await self._locate(ref).click(timeout=10_000)
        await self._settle()
        return await self._brief()

    # -- files ---------------------------------------------------------------

    async def cmd_downloads(self, msg: dict) -> dict:
        notes, self.download_notes = self.download_notes, []
        return {"ok": True, "downloads": [{k: d[k] for k in ("name", "size", "url")}
                                          for d in self.downloads], "notes": notes}

    async def cmd_fetch_download(self, msg: dict) -> dict:
        """A kept download's bytes, for the worker to quarantine."""
        found = next((d for d in self.downloads if d["name"] == msg.get("name")), None)
        if found is None:
            return {"ok": False, "error": f"no download called {str(msg.get('name'))[:80]!r}"}
        with open(found["path"], "rb") as f:
            data = f.read(MAX_DOWNLOAD_BYTES + 1)
        if len(data) > MAX_DOWNLOAD_BYTES:
            return {"ok": False, "error": "larger than 20 MB"}
        return {"ok": True, "name": found["name"], "b64": base64.b64encode(data).decode("ascii")}

    async def cmd_upload(self, msg: dict) -> dict:
        """A file the user approved, into the file field they were shown, on the page
        they approved it for."""
        if not same_page(self.page.url, str(msg.get("expected_url") or "")):
            return {"ok": False, "error": "the page changed since the user approved it"}
        ref = str(msg.get("ref") or "")
        if why := upload_refusal((await self._fresh()).get(ref), str(msg.get("label") or "")):
            return {"ok": False, "error": why}
        data = base64.b64decode(str(msg.get("b64") or ""))
        if len(data) > MAX_DOWNLOAD_BYTES:
            return {"ok": False, "error": "larger than 20 MB"}
        await self._locate(ref).set_input_files(
            {"name": download_name(str(msg.get("name") or "file")),
             "mimeType": "application/octet-stream", "buffer": data}, timeout=10_000)
        return await self._brief()

    # -- logins ------------------------------------------------------------
    # Sent by the worker's session layer and the live view, never by a tool
    # a model calls (tests/gate/test_browser_logins.py reads the toolset for
    # it). What is typed here is theirs, from the vault or their own keyboard.

    async def cmd_fill_login(self, msg: dict) -> dict:
        """Type their username and password into two fields of this page, if
        `login_refusal` allows. Says nothing back of what was typed."""
        current = await self._fresh()
        user_ref, pass_ref = str(msg.get("user_ref") or ""), str(msg.get("pass_ref") or "")
        submit_ref = str(msg.get("submit_ref") or "")
        if why := login_refusal(self.page.url, current.get(user_ref), current.get(pass_ref),
                                msg.get("domains")):
            return {"ok": False, "error": why}
        if submit_ref and (why := sign_in_refusal(current.get(submit_ref))):
            return {"ok": False, "error": why}
        await self._locate(user_ref).fill(str(msg.get("username") or ""), timeout=10_000)
        await self._locate(pass_ref).fill(str(msg.get("password") or ""), timeout=10_000)
        if submit_ref:
            await self._locate(submit_ref).click(timeout=10_000)
            await self._settle()
        return {"ok": True, "url": self.page.url}

    async def cmd_export_state(self, msg: dict) -> dict:
        """The session's cookies and storage, for the vault -- only from a
        session that is on the site it is being saved for."""
        domains = msg.get("domains") or self.domains
        if not on_site(self.page.url, domains):
            return {"ok": False, "error": "the page is not on the login's site"}
        return {"ok": True, "url": self.page.url, "state": await self.context.storage_state()}

    # -- the live view: their hands, at the laptop (agent/browser/live.py) -------

    async def cmd_frame(self, msg: dict) -> dict:
        quality = max(30, min(90, int(msg.get("quality") or 60)))
        jpeg = await self.page.screenshot(type="jpeg", quality=quality)
        said = await self._brief()
        said["jpeg"] = base64.b64encode(jpeg).decode("ascii")
        return said

    async def cmd_human_click(self, msg: dict) -> dict:
        await self.page.mouse.click(float(msg.get("x") or 0), float(msg.get("y") or 0))
        await asyncio.sleep(0.2)
        return {"ok": True}

    async def cmd_human_type(self, msg: dict) -> dict:
        await self.page.keyboard.type(str(msg.get("text") or "")[:500])
        return {"ok": True}

    async def cmd_human_key(self, msg: dict) -> dict:
        key = str(msg.get("key") or "")
        if not re.fullmatch(r"[A-Za-z0-9+]{1,30}", key):
            return {"ok": False, "error": "not a key name"}
        await self.page.keyboard.press(key)
        return {"ok": True}

    async def cmd_human_scroll(self, msg: dict) -> dict:
        await self.page.mouse.wheel(0, max(-3000, min(3000, float(msg.get("dy") or 0))))
        return {"ok": True}

    async def cmd_close(self, msg: dict) -> dict:
        if self.browser is not None:
            await self.browser.close()
        return {"ok": True}


def _say(message: dict) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


async def main() -> None:
    async_playwright, engine = load_engine()

    async with async_playwright() as playwright:
        driver = Driver(playwright, engine)
        while True:
            line = await asyncio.to_thread(sys.stdin.readline)
            if not line:
                break
            try:
                msg = json.loads(line)
            except ValueError:
                _say({"ok": False, "error": "not JSON"})
                continue
            try:
                said = await asyncio.wait_for(driver.run(msg), COMMAND_TIMEOUT_S)
            except TimeoutError:
                said = {"ok": False, "error": f"timed out after {COMMAND_TIMEOUT_S:.0f}s"}
            except Exception as exc:  # noqa: BLE001 - the page's failure is an answer
                said = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:400]}
            said["id"] = msg.get("id")
            _say(said)
            if msg.get("cmd") == "close":
                break


if __name__ == "__main__":
    asyncio.run(main())
