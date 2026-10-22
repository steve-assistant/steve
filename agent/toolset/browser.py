"""The sandboxed browser's tools: a real Chromium for pages `read_page`
cannot read -- ones that need their scripts, or turn away a plain fetcher --
and for the sites the user has saved a login for, which open logged in as them.

What a page did is said in words, not left for the model to guess
(`_plainly`). On 7 October 2026 their LinkedIn login looped -- Playwright's
"net::ERR_TOO_MANY_REDIRECTS" reached the model raw -- and it concluded that
"LinkedIn access isn't working reliably from this browser", when the answer
was that their saved login needed signing in again; a 403 page and CloudFront's
refusal came back as opens that went fine; a blank /feed/ came with no hint.
And the model was never told which logins exist: it tried read_page on
LinkedIn first, three times, and then closed the browser after every answer
-- five cold sandboxes in eight minutes for one conversation. So the saved
logins are named in `browse_open`'s description (`with_saved_logins`), a
refused `read_page` on one of their sites points here (`login_hint`), and
`browse_close` says it is rarely needed.

What holds the line is the frozen half (`core/browser/`, docs/security-
model.md section 6): the container, the gate, the driver's fixed commands
and its rules on typing. These tools are plumbing over it, with two checks
of their own, both before anything starts: the browser is off unless the
frozen policy says on (`core.browser.enabled`), and it is only for a
conversation the user is in -- never a worker, the doer, the night or a job
(`toolset/web.unattended`). The same two hide the tools from the model
altogether (`toolset.offered`), and the gate tests pin both. And once the
conversation has read outside text, the browser goes only where `read_page`
could (`toolset/web.unshown`), a click only to a link on a page the screen
passed or an address it was shown, and a site's search box takes only the
user's own words (`screening.theirs`).

Every step is recorded (`browser_steps`), and every page or download read
reaches the model as the injection screen allows, as `read_page`'s do
(`agent/screening.py`, `_screened`).
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import logging
import re
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import urlsplit

from pydantic_ai import RunContext

import core.browser
from agent import screening
from agent.actions import executable
from agent.browser import session as sessions
from agent.cut import cut
from agent.toolset.web import unattended, unshown
from agent.turn import Deps
from core import addons, db, egress, paths

log = logging.getLogger("toolset.browser")


def shots_dir() -> Path:
    return paths.artifacts_dir() / "browser"


# What the model is shown of a page: the text, capped by the driver, and the
# elements it can act on.
ELEMENTS_SHOWN = 120
NAMES = ("browse_open", "browse_read", "browse_click", "browse_scroll", "browse_back",
         "browse_screenshot", "browse_search", "browse_close", "browse_submit",
         "browse_use_login", "browse_login", "browse_files", "read_download", "browse_upload")
# How much of a download's text the model is shown.
DOWNLOAD_TEXT = 8_000


def refusal(deps: Deps) -> str | None:
    """Why the browser may not be used in this turn, or None.

    Their own conversations may; an unattended run may not -- with one
    exception, a clean-room browsing worker (agent/workers.py
    `Crew.start_browsing`), whose browser is kept to the sites it was given
    and who holds nothing of theirs to leak (`browse_sites`)."""
    from agent import features

    if not core.browser.enabled():
        return features.switched_off("BROWSER")
    if deps.browse_sites:
        if not core.browser.background():
            return features.switched_off("BACKGROUND_BROWSING")
        return None
    if unattended(deps):
        return "the browser is only for conversations the user is in"
    return None


def needs_user_yes(deps: Deps, site, otherwise: str) -> dict | None:
    """The answer for a page under one of their saved logins that the user approves
    each time (`ask`), when this conversation has not been allowed it
    (`browse_use_login`) -- or None, to go ahead: no login, a `free` one, or
    one they have said yes to. Shared by `browse_open` and the LinkedIn tools
    (`toolset/linkedin.py`), so a login is asked for by one rule. `otherwise`
    says what else the model may do, which differs by tool."""
    if site is None or site.tier == "free" or sessions.login_approved(deps.session_id, site.name):
        return None
    if why := refusal(deps):
        return {"ok": False, "error": why}
    return {"ok": False, "needs_user_yes": True,
            "error": f"this page is under the user's saved login '{site.name}', which they "
                     "approve each time: call browse_use_login(site="
                     f"'{site.name}', purpose=...) to ask them, {otherwise}"}


# Where the user signs in again to a login LinkedIn has turned away. Not the desktop
# app's sign-in window, which is where "sign in again" led (8 October 2026):
# a login imported from it was refused on its second page load and then on
# its first, and LinkedIn mailed them that a new device had signed in each time;
# only a login signed in to in the sandbox's own live view worked, twice.
LIVE_VIEW = ("in the sandbox's live view -- the desktop app, Logins, \"Use the sandbox view "
             "instead\" -- logging in to LinkedIn there and pressing Save, not in the sign-in "
             "window")
# What a model is told when it tries to drive LinkedIn through the general
# tools. The user's account is held to a pace, and a refusal rests the tools
# (toolset/linkedin.py); browse_open, a click on a LinkedIn page and the page
# fetcher bypassed both (8 October 2026 audit).
LINKEDIN_ONLY = ("LinkedIn is not read with the general browser or page tools: the user's "
                 "account is held to a pace there, and a refusal rests the tools. Use "
                 "linkedin_profile, linkedin_search_people, linkedin_company or linkedin_jobs, "
                 "which read it as them, paced")
# A search refused after outside text, for words the user did not write.
NOT_THEIR_SEARCH = ("this conversation has read text from outside, so a site's search box "
                    "takes only the user's own words from it: to type anything else, make it "
                    "a submit plan (browse_submit), which the user approves")
# A submit or a click refused on a page that takes payment, with the
# driver's reason (core/browser/driver.py `looks_like_payment`).
_NO_PAYMENTS = ("payments in the browser are not enabled ({}); tell the user to finish it "
                "themselves")


def _dead(session) -> bool:
    """Whether the browser died under this command (`BrowserSession.dead`). A
    stand-in session without the attribute -- the gate tests' -- has not."""
    return bool(getattr(session, "dead", ""))


def _on_linkedin(domains) -> bool:
    from core.browser import driver

    return bool(domains) and driver.on_site("https://www.linkedin.com/", tuple(domains))


# Said by anything a browsing worker must never do, should it be offered one.
CLEAN_ROOM =("a browsing worker only reads the sites it was given: it never logs in, "
              "fills in or submits anything, or handles files")


def _key(deps: Deps) -> int:
    """What the run's browsers are held under: a browsing worker's own key,
    never its lead's conversation, so two workers -- and the lead -- never
    share a browser."""
    return deps.browser_key if deps.browser_key is not None else deps.session_id


def register(tool) -> None:
    """Attach the browser tools. `tool` is `register_tools`'s filter."""

    @tool
    async def browse_open(ctx: RunContext[Deps], url: str, use_login: bool = True) -> dict:
        """Open a web page in a sandboxed browser -- a real Chromium -- for a
        page read_page could not read (it needs scripts, or refuses plain
        fetchers). Then browse_read. On a site the user has saved a login
        for, the page opens logged in as them (use_login=False opens it
        without); a login they want to approve each time asks you to call
        browse_use_login first.
        """
        from core.browser import vault

        if ctx.deps.browse_sites:
            # A browsing worker's browser is never logged in: their saved
            # logins are not even looked up.
            return await _do(ctx, "open", url=url, site=None)
        if why := await linkedin_refusal(ctx.deps, url):
            return {"ok": False, "use_linkedin_tools": True, "error": why}
        # Where read_page could go, and no further (`unshown`): the browser
        # opened any address in a chat that had read an email until step
        # 11's audit, so the email's author could have it carry what the
        # chat held.
        if why := refusal(ctx.deps) or unshown(ctx.deps, url):
            return {"ok": False, "error": why}
        site = vault.vault().site_for(url) if use_login else None
        if held := needs_user_yes(ctx.deps, site, "or open it with use_login=False to read it "
                                                 "without their login"):
            return held
        return await _do(ctx, "open", url=url, site=site)

    @tool
    async def browse_use_login(ctx: RunContext[Deps], site: str, purpose: str) -> dict:
        """Ask the user, with a card, to let this conversation use one of their
        saved logins that they approve each time -- say in `purpose` what you
        want to do there, in a few words. Their yes lasts half an hour. Then
        browse_open the page.
        """
        from agent.actions import act
        from core.browser import vault

        deps = ctx.deps
        if deps.browse_sites:
            return {"ok": False, "error": CLEAN_ROOM}
        if why := refusal(deps):
            return {"ok": False, "error": why}
        found = vault.vault().site(site.strip().lower())
        if found is None:
            names = ", ".join(s.name for s in vault.vault().sites()) or "none"
            return {"ok": False, "error": f"no saved login called {site!r} (saved: {names})"}
        if found.tier == "free" or sessions.login_approved(deps.session_id, found.name):
            return {"ok": True, "note": "no approval needed: browse_open the page"}
        # Whole: what the user lets the login be used for (agent/approvals.py).
        preview = (f"Use your {found.name} login ({', '.join(found.domains)})"
                   f" to: {' '.join(purpose.split())}\n(This conversation, "
                   f"{sessions.LOGIN_APPROVAL_S // 60} minutes.)")
        return await act(ctx, "browse_use_login", {"session": deps.session_id,
                                                    "site": found.name}, preview)

    @tool
    async def browse_login(ctx: RunContext[Deps], user_ref: str, pass_ref: str,
                           submit_ref: str = "") -> dict:
        """On a saved login's own sign-in page, have the user's username and
        password typed into the fields with these refs (from browse_read)
        and, if given, its sign-in button pressed. You never see them; they
        are only typed on that site's own pages. If the site then asks for
        a code or a phone tap, tell the user.
        """
        deps = ctx.deps
        if deps.browse_sites:
            return {"ok": False, "error": CLEAN_ROOM}
        if why := refusal(deps):
            return {"ok": False, "error": why}
        session = sessions.current(deps.session_id)
        if session is None or not session.site:
            return {"ok": False, "error": "open the sign-in page of a saved login first"}
        said = await sessions.fill_login(session, user_ref, pass_ref, submit_ref)
        if (said.get("ok") and submit_ref and said.get("url")
                and not sessions.signed_out_page(str(said["url"]))):
            # Signed in afresh and off the sign-in page: what it holds now
            # is a login worth saving back, whatever came before it.
            session.stale = ""
        _record(deps, session, "login", {"ref": f"{user_ref}/{pass_ref}"}, said, None)
        return said

    @tool
    async def browse_read(ctx: RunContext[Deps], whole: bool = False, start: int = 0) -> dict:
        """Read the open page: its text, and the elements on it you can act
        on, each with a ref (e1, e2, ...) for browse_click or browse_search.
        whole=True scrolls to the end first, so sections that load as you
        scroll are there. A page longer than one read says truncated: read
        on with start=next_start.
        """
        if whole and (lost := await _scroll_to_end(ctx)):
            return lost
        return await _do(ctx, "read", **({"start": max(0, start)} if start else {}))

    @tool
    async def browse_click(ctx: RunContext[Deps], ref: str) -> dict:
        """Click an element by its ref from the last browse_read: a link, a
        button, a tab. Read the page again afterwards; refs change. Logged
        in as the user, a click that would do something -- send, post,
        follow, book, cancel, delete -- becomes a card they approve.
        """
        from agent.actions import act
        from core.browser import clicks

        deps = ctx.deps
        session = sessions.current(_key(deps))
        # After outside text a link goes where an open could: to a link on a
        # page the screen passed (`_do`), or an address it was shown. A
        # browsing worker's browser is kept to its sites by the driver.
        watched = screening.has_read() and not deps.browse_sites
        if session is None or refusal(deps) or not (session.site or watched):
            return await _do(ctx, "click", ref=ref)
        if session.site and _on_linkedin(getattr(session, "domains", ())):
            return {"ok": False, "use_linkedin_tools": True, "error": LINKEDIN_ONLY}
        seen = await session.ask("describe", fields=[], button=ref)
        if not seen.get("ok"):
            return {"ok": False, "error": plain_error(str(seen.get("error")))}
        element = seen["button"]
        href = str(element.get("href") or "")
        if (watched and urlsplit(href).scheme in ("http", "https")
                and (why := unshown(deps, href))):
            return {"ok": False, "error": why}
        if not session.site:
            return await _do(ctx, "click", ref=ref)
        ruled = clicks.rule(element, _tier(session.site))
        # Payments in the browser are not enabled, and a site the user is
        # logged in to often keeps their card: one click on its checkout
        # spends. So on a page that takes payment only a plain link goes,
        # before the judge is asked; even "Continue" can be the click that
        # pays. Until step 10 a submit there was refused and a click became
        # an everyday card.
        if ruled != "free" and seen.get("payment"):
            return {"ok": False, "error": _NO_PAYMENTS.format(seen["payment"])}
        p = await _judged(deps, session, element, seen) if ruled == "judge" else None
        if clicks.verdict(ruled, p) == "free":
            return await _do(ctx, "click", ref=ref)
        # The page's own label, held short and marked if cut: it is the
        # page's text, not theirs. Where it is, whole (agent/approvals.py).
        label = cut(" ".join(str(element.get("label") or ref).split()), 80, mark="…")
        parts = urlsplit(str(seen.get("url") or ""))
        preview = (f"Click \"{label}\" on {parts.hostname or ''}{parts.path}"
                   + f"\n(Logged in as you on {session.site}.)")
        result = await act(ctx, "browse_act", {"session": deps.session_id, "site": session.site,
                                                "url": seen.get("url"), "ref": ref,
                                                "label": element.get("label") or ""}, preview)
        if result.get("action_id"):
            sessions.hold(deps.session_id, sessions.MAX_AGE_S)
        return result

    @tool
    async def browse_scroll(ctx: RunContext[Deps], direction: str = "down") -> dict:
        """Scroll the open page up or down, for pages that load as you go."""
        return await _do(ctx, "scroll", direction=direction)

    @tool
    async def browse_back(ctx: RunContext[Deps]) -> dict:
        """Go back a page."""
        return await _do(ctx, "back")

    @tool
    async def browse_screenshot(ctx: RunContext[Deps]) -> dict:
        """Save a screenshot of the open page for the record; returns where."""
        return await _do(ctx, "screenshot")

    @tool
    async def browse_search(ctx: RunContext[Deps], ref: str, text: str) -> dict:
        """Type a search into the page's own search box (its ref from
        browse_read) and press Enter. Only a search box: typing anything
        else into a page is a form submission, which the user approves.
        """
        deps = ctx.deps
        # Typing sends the words to the site, and a page can call any field
        # its search box: after outside text, only the user's own words go,
        # as a fetch takes only addresses it was shown (`unshown`).
        if (not refusal(deps) and not deps.browse_sites and screening.has_read()
                and not screening.theirs(text)):
            return {"ok": False, "error": NOT_THEIR_SEARCH}
        return await _do(ctx, "search", ref=ref, text=text)

    @tool
    async def browse_files(ctx: RunContext[Deps]) -> dict:
        """What files there are: the open browser's downloads (read one with
        read_download), and the files that may be uploaded with
        browse_upload -- the user's outbox, quarantined downloads, and files
        you made -- each named the way browse_upload takes it.
        """
        deps = ctx.deps
        if deps.browse_sites:
            return {"ok": False, "error": CLEAN_ROOM}
        if why := refusal(deps):
            return {"ok": False, "error": why}
        session = sessions.current(deps.session_id)
        downloads = (await session.ask("downloads")) if session is not None else {}
        listed = await asyncio.to_thread(_uploadable)
        return {"ok": True, "downloads": downloads.get("downloads") or [],
                "download_notes": downloads.get("notes") or [], "uploadable": listed}

    @tool
    async def read_download(ctx: RunContext[Deps], name: str) -> dict:
        """Read a file the open browser downloaded (its name from
        browse_files). It is quarantined on this computer and its text taken
        out in a sealed container with no network -- PDF, Word, Excel,
        PowerPoint and plain text.
        """
        deps = ctx.deps
        if deps.browse_sites:
            return {"ok": False, "error": CLEAN_ROOM}
        if why := refusal(deps):
            return {"ok": False, "error": why}
        session = sessions.current(deps.session_id)
        if session is None:
            return {"ok": False, "error": "no browser is open"}
        fetched = await session.ask("fetch_download", name=name)
        if not fetched.get("ok"):
            return {"ok": False, "error": str(fetched.get("error"))}
        kept = await asyncio.to_thread(_quarantine, session.box.session, name, fetched["b64"])
        text = await asyncio.to_thread(_extract, kept, fetched["b64"])
        # What the model reads of it, screened (agent/screening.py).
        seen = await screening.shown(deps.conn, "browser_download", kept.name,
                                     session.site or "download", text[:DOWNLOAD_TEXT])
        _record(deps, session, "read_download", {"ref": name}, {"ok": True}, None)
        said = {"ok": True, "name": kept.name, "kept_as": _spec(kept), "text": seen.text,
                "truncated": len(text) > DOWNLOAD_TEXT}
        return said if seen.how == "text" else {**said, "screened": seen.how}

    @tool
    async def browse_upload(ctx: RunContext[Deps], ref: str, file: str) -> dict:
        """Put a file into the page's file field (its ref from browse_read)
        -- only once the user approves a card naming the file, its size, the
        site and the field. `file` is as browse_files lists it: outbox/...,
        quarantine/... or artifacts/...; nothing else can be uploaded.
        """
        from agent.actions import act
        from core.browser import driver, files

        deps = ctx.deps
        if deps.browse_sites:
            return {"ok": False, "error": CLEAN_ROOM}
        if why := refusal(deps):
            return {"ok": False, "error": why}
        session = sessions.current(deps.session_id)
        if session is None:
            return {"ok": False, "error": "open the page first"}
        found = await asyncio.to_thread(files.upload_source, file)
        if isinstance(found, str):
            return {"ok": False, "error": found}
        seen = await session.ask("describe", fields=[], button=ref)
        if not seen.get("ok"):
            return {"ok": False, "error": plain_error(str(seen.get("error")))}
        label = str(seen["button"].get("label") or "")
        if why := driver.upload_refusal(seen["button"], label):
            return {"ok": False, "error": why}
        digest = await asyncio.to_thread(addons.file_sha256, found)
        size_kb = max(1, found.stat().st_size // 1024)
        parts = urlsplit(str(seen.get("url") or ""))
        # A file field often has no label of its own; its name says which.
        shown = label or seen["button"].get("name") or seen["button"].get("id") or "the file field"
        preview = (f"Upload {found.name} ({size_kb:,} KB) to "
                   f"{(parts.hostname or '') + parts.path}"
                   + f' via "{cut(str(shown), 60, mark="…")}"?')
        result = await act(ctx, "browse_upload", {
            "session": deps.session_id, "site": session.site, "url": seen.get("url"),
            "ref": ref, "label": label, "file": file, "sha256": digest}, preview)
        if result.get("action_id"):
            sessions.hold(deps.session_id, sessions.MAX_AGE_S)
        return result

    @tool
    async def browse_close(ctx: RunContext[Deps]) -> dict:
        """Rarely needed: the browser closes itself after 3 idle minutes.
        Leave it open between the user's messages -- their next question is
        often about the same site, and a new one takes a while to start.
        Close it only when they ask.
        """
        closed = await sessions.close(_key(ctx.deps))
        return {"ok": True, "closed": closed}

    @tool
    async def browse_submit(ctx: RunContext[Deps], fields: list[dict[str, str]],
                            button: str) -> dict:
        """Fill in a form on the open page and press its button -- only once
        the user approves. `fields` is [{"ref": "e3", "value": "..."}, ...]
        with refs from browse_read, `button` the ref of the button to press.
        This asks them first with a card showing the site, every field and
        value, and the button; nothing is typed into the page until they say
        yes. Payments are not possible. Read the page first.
        """
        from agent.actions import act

        deps = ctx.deps
        if deps.browse_sites:
            return {"ok": False, "error": CLEAN_ROOM}
        planned = await _plan(deps, fields, button)
        if "error" in planned:
            return planned
        # Through `act`, here in the tool's own body where the gate tests read
        # it: what the user's card shows is exactly what will be typed and pressed.
        result = await act(ctx, "browse_submit", {"session": deps.session_id,
                                                   "site": getattr(planned["session"], "site",
                                                                   ""),
                                                   "plan": planned["plan"]},
                           planned["preview"])
        if result.get("action_id"):
            # Kept open for their answer -- as long as the sandbox itself lives.
            sessions.hold(deps.session_id, sessions.MAX_AGE_S)
        _record(deps, planned["session"], "submit-asked", {"url": planned["plan"]["expected_url"]},
                {"ok": "action_id" in result, "url": planned["plan"]["expected_url"],
                 "error": result.get("error")}, None, action_id=result.get("action_id"))
        return result


async def _plan(deps: Deps, fields: list, button: str) -> dict:
    """Describe the fields and the button, check them, and write the card --
    or say why not. Nothing is typed into the page."""
    from core.browser import driver

    if why := refusal(deps):
        return {"ok": False, "error": why}
    session = sessions.current(deps.session_id)
    if session is None:
        return {"ok": False, "error": "no page is open: browse_open it and browse_read it first"}
    try:
        wanted = [(str(_get(f, "ref")), str(_get(f, "value") or "")) for f in fields]
    except (KeyError, TypeError):
        return {"ok": False, "error": 'fields is a list of {"ref": ..., "value": ...}'}
    described = await session.ask("describe", fields=[ref for ref, _ in wanted], button=button)
    if not described.get("ok"):
        return described
    if described.get("payment"):
        return {"ok": False, "error": _NO_PAYMENTS.format(described["payment"])}
    labels = {el["ref"]: el for el in [*described["fields"], described["button"]]}
    plan = {"expected_url": described["url"],
            "fields": [{"ref": ref, "label": labels[ref].get("label") or "",
                        "value": _meant(labels[ref], value)} for ref, value in wanted],
            "button": {"ref": button, "label": labels[button].get("label") or ""}}
    if why := driver.check_plan(plan, labels):
        return {"ok": False, "error": why}
    # The user sees every value they are agreeing to, whole, however long the form: a
    # card is never cut (agent/approvals.py, "What a card may say"). Until 4
    # October 2026 cards were cut at 300 characters, and a longer form was
    # not asked about at all.
    preview = _preview(plan, {ref: (el.get("type") or "").lower() for ref, el in labels.items()})
    return {"plan": plan, "preview": preview, "session": session}


def _get(item, key: str):
    return item[key] if isinstance(item, dict) else getattr(item, key)


_TICKED = {"checked", "check", "yes", "true", "on", "1", "selected", "select", "tick",
           "ticked", "x"}
_UNTICKED = {"unchecked", "uncheck", "no", "false", "off", "0", "unselected", "unticked",
             "clear", "none"}


def _meant(el: dict, value: str) -> str:
    """A value as the driver takes it, from however a model put it. A
    checkbox or radio button is "checked" or "unchecked", and its own label
    means checked -- on 1 October the model gave the Medium button "Medium",
    which the driver did not take for ticked. A list takes the option whose
    label matches, whatever the case. Anything else is left as it was, for
    `driver.check_plan` to refuse in words."""
    said = value.strip()
    if (el.get("type") or "").lower() in ("checkbox", "radio"):
        low = said.lower()
        if low in _TICKED or low == " ".join(str(el.get("label") or "").split()).lower():
            return "checked"
        if low in _UNTICKED:
            return "unchecked"
        return said
    if (el.get("tag") or "").lower() == "select":
        for option in el.get("options") or []:
            if option.lower() == said.lower():
                return option
    return value


def done_text(preview: str, result: dict) -> str:
    """What the user is told once a form they approved has gone: that it went, and
    where the site took them -- its address and title, short. Not the page's
    text: this message joins the conversation a model reads next, and what a
    page says is not to be put in its mouth."""
    first = preview.splitlines()[0].removeprefix("Submit a form on ").rstrip(":")
    parts = urlsplit(str(result.get("url") or ""))
    landed = f"{parts.hostname or ''}{parts.path if parts.path not in ('', '/') else ''}"[:60]
    title = " ".join(str(result.get("title") or "").split())[:60]
    said = f"Done — submitted the form on {first}."
    if landed:
        said += f" The site went to {landed}" + (f' ("{title}")' if title else "") + "."
    return said + " Ask me to read the page if you want what it says."


def plain_error(error: str) -> str:
    """Playwright's failure, as something a model can act on. Its message
    carries a call log -- selectors, retries, the element's HTML -- and the
    first live click handed all of it to the model (1 October 2026: the
    search button on northwind.example, not yet clickable). What it needs is what
    failed and what to try instead."""
    head = error.split("\nCall log:", 1)[0].strip()
    if sessions.GONE.search(head):
        return CRASHED + "Open the page again if you still need it."
    # The call log says why a wait ran out. A button the site keeps
    # disabled is the form wanting something -- on 6 October 2026 Amtrak's
    # search, with a field still empty -- and the user was sent the whole log.
    if "Timeout" in head and ('aria-disabled="true"' in error or "not enabled" in error):
        return ("the button stayed disabled: the form still wants something -- a required "
                "field, a box to tick, or a step before this one. Nothing was submitted.")
    if "Page.goto: Timeout" in head:
        return ("the page took over 30 seconds to arrive; it may still come -- read it "
                "again in a moment")
    if "ERR_TOO_MANY_REDIRECTS" in head:
        return ("the site kept redirecting the browser in a loop, which is how a site turns "
                "a browser away; do not retry more than once")
    if "Timeout" in head and ("click" in head.lower() or "fill" in head.lower()):
        # The call log names what is in the way. Three of seven clicks timed
        # out by 7 October, and the model was told "something like a cookie
        # banner" while the log said which element it was.
        if blocker := _blocker(error):
            return (f"{head.split(':', 1)[0]}: {blocker} is in front of that element and takes "
                    "the click. Close or answer that first -- read the page for its button -- "
                    "then click again.")
        if "element is not visible" in error:
            return (f"{head.split(':', 1)[0]}: that element is hidden -- likely inside a menu, "
                    "tab or section that is closed. Open that first, or try another element.")
        return (f"{head.split(':', 1)[0]}: that element would not take the action in time -- "
                "it may be hidden, covered by something like a cookie banner, or still "
                "loading. Read the page again, or try another element (a search box can "
                "be typed into directly with browse_search).")
    return head[:300]


_INTERCEPTS = re.compile(r"(<[^\n]*?) intercepts pointer events")
# Whole attribute names: `data-agent-id` is not an id.
_ATTRIBUTE = re.compile(r'(?<![\w-])(id|aria-label|role|class)="([^"]{1,80})"')


def _blocker(error: str) -> str | None:
    """What Playwright's call log says covers an element, in a few words:
    `a div (id "onetrust-banner-sdk")`, `a button "Accept all"`. The
    page's own words, so short; None if the log does not say."""
    found = _INTERCEPTS.findall(error)
    if not found:
        return None
    html = found[-1]
    # "<span>Accept</span> from <div id="banner">…</div> subtree": the thing
    # to deal with is the one the span sits in.
    if " from <" in html:
        html = "<" + html.split(" from <", 1)[1].removesuffix(" subtree")
    tag = re.match(r"<([a-zA-Z][a-zA-Z0-9-]*)", html)
    kind = tag.group(1).lower() if tag else "something"
    attrs = dict((k, v) for k, v in _ATTRIBUTE.findall(html))
    words = re.search(r">([^<>…]{1,60})<", html)
    if "aria-label" in attrs:
        said = f'"{attrs["aria-label"][:60]}"'
    elif words and words.group(1).strip():
        said = f'"{" ".join(words.group(1).split())[:60]}"'
    elif "id" in attrs:
        said = f'(id "{attrs["id"][:60]}")'
    elif "class" in attrs:
        said = f'(class "{" ".join(attrs["class"].split()[:2])[:60]}")'
    else:
        said = ""
    return f"{'an' if kind[0] in 'aeiou' else 'a'} {kind} {said}".strip()


def _preview(plan: dict, kinds: dict[str, str] | None = None) -> str:
    """The card: the site, each field and its value in full, and the button
    -- what the user is agreeing to, whole and nothing paraphrased. A box reads as
    what will happen to it: "Medium: selected", "Extra Cheese: checked"."""
    kinds = kinds or {}
    parts = urlsplit(plan["expected_url"])
    where = f"{parts.hostname or ''}{parts.path if parts.path != '/' else ''}"
    lines = [f"Submit a form on {where}:"]
    # "Customer name:" is a common label; its own colon is not repeated.
    lines += [f"{(f['label'] or f['ref']).rstrip(': ')}: {_shown(f['value'], kinds.get(f['ref']))}"
              for f in plan["fields"]]
    lines.append(f"Press \"{plan['button']['label'] or plan['button']['ref']}\". "
                 f"(Page open ~{sessions.MAX_AGE_S // 60} min.)")
    return "\n".join(lines)


def _shown(value: str, kind: str | None) -> str:
    if kind == "radio":
        return {"checked": "selected", "unchecked": "not selected"}.get(value, value)
    return value


@executable("browse_submit")
async def _do_submit(*, session: int, plan: dict, site: str = "") -> dict:
    """Carry out a submit the user approved, on the page they approved it for. The
    driver checks that page again first -- the address, every field's label,
    the button's, and that it does not take payment -- and refuses if any of
    it changed (`core/browser/driver.check_plan`)."""
    from agent import runtime

    live = sessions.get(session, site)
    if live is None:
        return {"ok": False, "error": "the browser page this was for has closed (a page "
                                      "stays open about half an hour); ask me to open it "
                                      "again"}
    said = await live.ask("submit", plan=plan)
    sessions.release(session)
    if runtime.conn is not None:
        _record(Deps(conn=runtime.conn, session_id=session), live, "submit",
                {"url": plan.get("expected_url")}, said, None)
    # A failure reaches them as a notification ("Couldn't do it -- ..."): in
    # words, not Playwright's call log.
    if isinstance(said, dict) and said.get("error"):
        said = {**said, "error": plain_error(str(said["error"]))}
    return said


@executable("browse_upload")
async def _do_upload(*, session: int, site: str, url: str, ref: str, label: str, file: str,
                     sha256: str) -> dict:
    """A file the user approved, into the field they were shown -- the same file,
    byte for byte: a different one under the same name is refused."""
    from agent import runtime
    from core.browser import files

    live = sessions.get(session, site)
    if live is None:
        return {"ok": False, "error": "the browser page this was for has closed; ask me to "
                                      "open it again"}
    found = await asyncio.to_thread(files.upload_source, file)
    if isinstance(found, str):
        return {"ok": False, "error": found}
    if await asyncio.to_thread(addons.file_sha256, found) != sha256:
        return {"ok": False, "error": f"{file} changed since the user approved it"}
    data = await asyncio.to_thread(found.read_bytes)
    said = await live.ask("upload", expected_url=url, ref=ref, label=label, name=found.name,
                          b64=base64.b64encode(data).decode("ascii"))
    sessions.release(session)
    if said.get("error"):
        said["error"] = plain_error(str(said["error"]))
    if runtime.conn is not None:
        _record(Deps(conn=runtime.conn, session_id=session), live, "upload", {"ref": file},
                said, None)
    return said


def _spec(path: Path) -> str:
    """A file as browse_upload names it: its root's name and the rest."""
    from core.browser import files

    for name, root in files.roots().items():
        if path.resolve().is_relative_to(root.resolve()):
            return f"{name}/{path.resolve().relative_to(root.resolve()).as_posix()}"
    return path.name


def _uploadable(per_root: int = 10) -> list[dict]:
    """What may be uploaded, newest first within each place -- their outbox
    first -- named as browse_upload takes them."""
    from core.browser import files

    listed = []
    for root in files.roots().values():
        if not root.is_dir():
            continue
        found = [p for p in root.rglob("*")
                 if p.is_file() and p.stat().st_size <= files.MAX_FILE_BYTES]
        found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        listed += [{"file": _spec(p), "size": p.stat().st_size} for p in found[:per_root]]
    return listed


def _quarantine(session: str, name: str, b64: str) -> Path:
    """Keep a download on the laptop, under a safe name, to be read and
    never opened."""
    from core.browser import files

    folder = files.quarantine_dir() / session
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / files.quarantine_name(name)
    path.write_bytes(base64.b64decode(b64))
    return path


# Run in the analysis container -- no network, a read-only root, capped --
# never on the laptop: a stranger's PDF is parsed where it can reach nothing.
# The driver's code, not the model's.
EXTRACT = r'''
import base64, io, sys
from pathlib import Path
data = base64.b64decode(Path("blob.b64").read_text())
name = Path("name.txt").read_text().strip().lower()
out = ""
try:
    if name.endswith(".pdf"):
        from pypdf import PdfReader
        out = "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages[:50])
    elif name.endswith(".docx"):
        import docx
        out = "\n".join(p.text for p in docx.Document(io.BytesIO(data)).paragraphs)
    elif name.endswith((".xlsx", ".xlsm")):
        import openpyxl
        book = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        rows = []
        for sheet in book.worksheets[:5]:
            rows.append("# " + sheet.title)
            for row in sheet.iter_rows(max_row=200, values_only=True):
                rows.append("\t".join("" if v is None else str(v) for v in row))
        out = "\n".join(rows)
    elif name.endswith(".pptx"):
        from pptx import Presentation
        out = "\n".join(shape.text_frame.text for slide in Presentation(io.BytesIO(data)).slides
                        for shape in slide.shapes if getattr(shape, "has_text_frame", False))
    elif name.endswith((".txt", ".csv", ".tsv", ".md", ".json", ".xml", ".html", ".htm", ".ics")):
        out = data.decode("utf-8", "replace")
    else:
        out = "[this kind of file is not read: ." + name.rsplit(".", 1)[-1] + "]"
except Exception as exc:
    out = "[could not read it: " + type(exc).__name__ + "]"
sys.stdout.write(out[:20000])
'''


def _extract(path: Path, b64: str) -> str:
    """A quarantined download's text, taken out in the analysis container."""
    from tools import code_exec

    ran = code_exec.run_python(EXTRACT, files={"blob.b64": b64, "name.txt": path.name},
                               network=False, timeout_s=90, collect_artifacts=False)
    if not ran.ok:
        return f"[could not read it: {ran.error or ran.stderr[-200:] or 'the reader failed'}]"
    return ran.stdout


@executable("browse_use_login")
async def _do_use_login(*, session: int, site: str) -> dict:
    """Their yes to this conversation using an `ask` login, for half an hour."""
    sessions.approve_login(session, site)
    return {"ok": True, "site": site, "minutes": sessions.LOGIN_APPROVAL_S // 60}


@executable("browse_act")
async def _do_act(*, session: int, site: str, url: str, ref: str, label: str) -> dict:
    """A click the user approved, in the logged-in browser it was asked in, on the
    page and element they were shown (the driver's `act` checks both)."""
    from agent import runtime

    live = sessions.get(session, site)
    if live is None:
        return {"ok": False, "error": "the browser page this was for has closed; ask me to "
                                      "open it again"}
    said = await live.ask("act", expected_url=url, ref=ref, label=label)
    sessions.release(session)
    if said.get("error"):
        said["error"] = plain_error(str(said["error"]))
    if runtime.conn is not None:
        _record(Deps(conn=runtime.conn, session_id=session), live, "act", {"ref": ref}, said,
                None)
    return said


def _tier(site: str) -> str:
    from core.browser import vault

    found = vault.vault().site(site)
    return found.tier if found is not None else "ask"


# Jev's judgements of clicks, by (login, label), for this worker's life: the
# same button on the same site is the same question.
_judged_cache: dict[tuple[str, str], float] = {}


async def _judged(deps: Deps, session, element: dict, seen: dict) -> float | None:
    """Jev's probability that clicking `element` does something -- None if
    it could not be asked, which `clicks.verdict` treats as yes. Every
    judgement is recorded (`browser_judgements`)."""
    label = " ".join(str(element.get("label") or "").split())[:120]
    key = (session.site, label.lower())
    if key in _judged_cache:
        return _judged_cache[key]
    p, error = await asyncio.to_thread(_ask_jev, session.site, str(seen.get("title") or ""),
                                       element, label)
    if p is not None:
        _judged_cache[key] = p
    try:
        with db.transaction(deps.conn):
            deps.conn.execute(
                "INSERT INTO browser_judgements (site, label, url, p, error)"
                " VALUES (?, ?, ?, ?, ?)",
                (session.site, label, str(seen.get("url") or "")[:500], p, error or None))
    except Exception:  # noqa: BLE001 - the record failing is not the click failing
        log.warning("could not record a click judgement", exc_info=True)
    return p


def _ask_jev(site: str, title: str, element: dict, label: str) -> tuple[float | None, str]:
    """Blocking; never raises. Only the element and where it is go to Jev
    (`core/decide.py`) -- never the page's text or anything of theirs."""
    from core.browser import clicks
    from core.decide import Predicate, ask  # by name: agent/workers.py says why

    kind = element.get("tag") or "element"
    if element.get("type"):
        kind += f" ({element['type']})"
    href = str(element.get("href") or "")
    state = (f"Site: {site}\nPage title: {' '.join(title.split())[:120]}\n"
             f"Element: a {kind} labelled {label!r}"
             + (f" linking to {href[:200]}" if href else ""))
    said = ask(None, state, [Predicate("does", clicks.JUDGE)])
    answer = said.answers.get("does")
    if answer is None or answer.probability is None:
        return None, (said.error or "no answer")[:200]  # no answer is a card
    return answer.probability, ""


CRASHED = ("the browser crashed -- it ran out of memory or the page died -- and was closed. "
           "The site did not refuse it and nothing was lost or sent. ")


async def _after_crash(session, cmd: str, retried: bool) -> dict:
    """The answer for a browser that died: closed and out of the registry
    (`sessions.discard`), and said plainly. On 8 October 2026 Chromium was
    OOM-killed mid-read and the model was handed a raw `TargetClosedError`
    with half a profile; it went on to draft an unrelated email."""
    log.warning("the browser %s died during %s: %s", session.box.session, cmd,
                str(session.dead)[:120])
    await sessions.discard(session)
    return {"ok": False, "browser_crashed": True,
            "error": CRASHED + (
                "It crashed again on a second try: tell the user it failed, and do not carry on "
                "from what was read before it." if retried else
                "Open the page again if you still need it; it starts in a fresh browser.")}


async def _do(ctx: RunContext[Deps], cmd: str, *, site=None, retry: bool = True, **args) -> dict:
    """One browser command, screened. A page that crashes the
    browser is closed and said so; `retry` (an `open` only) tries the page
    once more in a fresh browser, as a caller that paces its own loads
    (`toolset/linkedin.py`) does for itself and turns off."""
    from core.browser import driver

    deps = ctx.deps
    if why := refusal(deps):
        return {"ok": False, "error": why}
    # Said before a sandbox is started for it; the gate would refuse it too.
    if cmd == "open" and (why := await asyncio.to_thread(egress.refusal, str(args["url"]))):
        return {"ok": False, "error": f"not opened: {why}"}
    # A browsing worker's sites, said here before the driver -- which keeps
    # its browser to them whatever a page links to -- would refuse it.
    if (cmd == "open" and deps.browse_sites
            and not driver.on_site(str(args["url"]), deps.browse_sites)):
        return {"ok": False, "error": "not opened: this browser is kept to "
                                      f"{', '.join(deps.browse_sites)}"}
    key = _key(deps)
    # Clicking, searching and going back each load a page. In their logged-in
    # LinkedIn browser that is a load with no pace and no pause, after the
    # LinkedIn tools have opened a page in it (8 October 2026 audit).
    here = sessions.current(key) if cmd in ("click", "search", "back") else None
    if (here is not None and getattr(here, "site", "")
            and _on_linkedin(getattr(here, "domains", ()))):
        return {"ok": False, "use_linkedin_tools": True, "error": LINKEDIN_ONLY}
    # A browsing worker's browser is kept to its sites from the start.
    kept = {"domains": deps.browse_sites} if deps.browse_sites else {}
    try:
        # A page is opened in the browser it belongs to -- logged in to its
        # saved login, or the public one; everything else acts on the one
        # last used.
        if cmd == "open":
            session = await sessions.session_for(key, site, **kept)
        else:
            session = sessions.current(key) or await sessions.session_for(key, **kept)
    except sessions.BrowserUnavailable as exc:
        return {"ok": False, "error": str(exc)}
    said = await session.ask(cmd, **args)
    if _dead(session):
        _record(deps, session, cmd, args, said, None)
        if not (cmd == "open" and retry):
            return await _after_crash(session, cmd, retried=False)
        await _after_crash(session, cmd, retried=False)
        try:
            session = await sessions.session_for(key, site, **kept)
        except sessions.BrowserUnavailable as exc:
            return {"ok": False, "error": str(exc)}
        said = await session.ask(cmd, **args)
        if _dead(session):
            _record(deps, session, cmd, args, said, None)
            return await _after_crash(session, cmd, retried=True)
    raw = str(said.get("error") or "")
    if cmd == "open" and "Download is starting" in raw:
        # An address that is a file: Chromium downloads it rather than
        # showing it. Not a failure -- it is waiting in browse_files.
        said = {"ok": True, "downloaded": True, "url": args.get("url"),
                "note": "that address is a file, and it was downloaded: browse_files lists "
                        "it and read_download reads it"}
    if said.get("error"):
        said["error"] = plain_error(str(said["error"]))
    shot = None
    if cmd == "screenshot" and said.get("png"):
        shot = await asyncio.to_thread(_save, session.box.session, session.steps,
                                       said.pop("png"))
        said["saved"] = str(shot)
    links: list[str] = []
    if cmd == "read" and said.get("ok"):
        page = str(said.get("url") or "")
        shown = (said.get("elements") or [])[:ELEMENTS_SHOWN]
        links = [str(el["href"]) for el in shown if isinstance(el, dict) and el.get("href")]
        said["elements"] = [_slim(el, page) for el in shown]
        if said.get("truncated"):
            said["next_start"] = int(args.get("start") or 0) + len(str(said.get("text") or ""))
    if said.get("ok") and (said.get("title") or said.get("text")):
        await _screened(deps, said)
        if not said.get("screened"):
            # Whole, as the page wrote them, on a page the screen passed.
            screening.came_whole(links)
    if cmd in ("open", "read"):
        _plainly(session, cmd, said, raw)
    _record(deps, session, cmd, args, said, shot)
    return said


# What of an element its page wrote, rather than what kind of thing it is.
_ELEMENT_WORDS = ("label", "placeholder", "options")


async def _screened(deps: Deps, said: dict) -> None:
    """What a page said, as the model may read it (agent/screening.py): its
    title, after any step that lands on a page, and, when it is read, its
    text and the words on its elements -- all the site's, screened as one
    item. A page the screen did not pass keeps none of them, and its
    elements only their refs, kinds and addresses. Before `_plainly`, so no
    note quotes a page the screen did not pass. The title an open, a click
    or a search landed on reached the model unscreened until 11 October
    2026: a title is as long as its site makes it."""
    page = str(said.get("url") or "")
    elements = said.get("elements") or []
    words = [str(el[k]) for el in elements if isinstance(el, dict)
             for k in _ELEMENT_WORDS if el.get(k)]
    seen = await screening.shown(
        deps.conn, "browser_page", page, urlsplit(page).hostname,
        "\n\n".join([str(said.get("title") or ""), str(said.get("text") or ""),
                     "\n".join(words)]))
    if seen.how == "text":
        return
    said.update(title=None, text=seen.text, screened=seen.how)
    if elements:
        said["elements"] = [{k: v for k, v in el.items() if k not in _ELEMENT_WORDS}
                            if isinstance(el, dict) else el for el in elements]


# How many screens `browse_read(whole=True)` scrolls at most, and how long it
# waits at the end for a section that has just started loading.
WHOLE_SCROLLS = 12
WHOLE_SETTLE_S = 1.0


async def _scroll_to_end(ctx: RunContext[Deps]) -> dict | None:
    """Scroll the open page down until its end stops moving, so what loads
    as it is scrolled is on it. On 7 October 2026 a LinkedIn profile read
    6,099 characters with no Experience section: LinkedIn renders it on
    scroll, and the start date in the answer came from an activity post.

    None, or -- if the browser died while it scrolled -- the answer to give
    instead of reading a page that is gone (`_after_crash`)."""
    deps = ctx.deps
    if refusal(deps):
        return None  # browse_read says why
    session = sessions.current(_key(deps))
    if session is None:
        return None
    height, waited = None, False
    for _ in range(WHOLE_SCROLLS):
        moved = await session.ask("scroll", direction="down")
        if _dead(session):
            _record(deps, session, "scroll", {"direction": "down"}, moved, None)
            return await _after_crash(session, "scroll", retried=False)
        if not moved.get("ok"):
            return None
        if moved.get("at_end") and moved.get("height") == height:
            if waited:
                return None
            # At the end, and nothing came yet: a section starting to load
            # gets a moment before the page is called whole.
            waited = True
            await asyncio.sleep(WHOLE_SETTLE_S)
        height = moved.get("height")
    return None


def _slim(element: dict, page: str) -> dict:
    """A link as the model needs it: its words and where it goes, the path
    alone on the page's own site. A read of 120 elements with full
    addresses ran to about 12,000 characters (7 October 2026)."""
    if not isinstance(element, dict) or not element.get("href"):
        return element
    href = str(element["href"])
    parts = urlsplit(href)
    if parts.scheme in ("http", "https"):
        short = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        if (parts.hostname or "") != (urlsplit(page).hostname or ""):
            short = f"{parts.hostname or ''}{short}"
    else:
        short = href
    slim = {k: v for k, v in element.items() if k not in ("name", "id", "href")}
    slim["href"] = cut(short, 120, mark="…")
    return slim


# What a page that turned the browser away says of itself: a status for a
# title, or a bot wall's own words. Measured from the start of the title, so
# a profile of someone at "429 Capital" is a profile.
_BLOCKED_TITLE = re.compile(
    r"^\s*(error\s*)?(401|403|429|999)\b|forbidden|access denied|could not be satisfied|"
    r"^just a moment|attention required|are you a (human|robot)|pardon our interruption|"
    r"request unsuccessful|unusual traffic", re.IGNORECASE)
REFUSED_STATUS = frozenset({401, 403, 429, 999})


def _plainly(session, cmd: str, said: dict, raw: str) -> None:
    """Say what a page did, where the result alone would mislead: a redirect
    loop or a sign-in wall in a logged-in browser is their saved login gone
    stale, a refusal is a refusal, and an empty page is not a page. A
    logged-in browser that met any of them is marked `stale`, so closing it
    does not save it over their login (agent/browser/session.kept_state)."""
    site = str(getattr(session, "site", "") or "")
    where = (LIVE_VIEW if _on_linkedin(getattr(session, "domains", ())) else
             "(the desktop app, Logins)")
    again = (f"the user's saved '{site}' login has most likely gone stale -- tell them to "
             f"sign in to it again {where}; do not keep retrying")
    if "ERR_TOO_MANY_REDIRECTS" in raw:
        if site:
            said["error"] = (f"the site keeps redirecting this browser logged in as the user: "
                             f"{again}. Its public pages open with use_login=False.")
            said["redirect_loop"] = True
            _mark_stale(session, "the site kept redirecting it")
        return
    if not said.get("ok"):
        return
    url = str(said.get("url") or "")
    notes = [str(said["note"])] if said.get("note") else []
    if site and sessions.signed_out_page(url):
        said["login_not_accepted"] = True
        notes.append(f"the site is asking who the user is ({urlsplit(url).path[:40]}): their "
                     f"saved '{site}' login was not accepted. If a password is saved for it, "
                     f"browse_login on this page signs them in; otherwise {again}.")
        _mark_stale(session, "it was sent to a sign-in page")
    title = " ".join(str(said.get("title") or "").split())
    status = said.get("status")
    if status in REFUSED_STATUS or _BLOCKED_TITLE.search(title):
        said["blocked"] = True
        notes.append(f"{urlsplit(url).hostname or 'the site'} turned this browser away "
                     f"({status or title[:60]}); do not retry -- say so, or find it elsewhere.")
        _mark_stale(session, "the site turned it away")
    if cmd == "read" and not str(said.get("text") or "").strip() and not said.get("elements"):
        said["empty"] = True
        notes.append("the page came back empty -- usually the site refusing this browser"
                     + (f"; here, likely because {again}" if site
                        else "; read it once more at most, then say so") + ".")
        _mark_stale(session, "a page came back empty")
    if notes:
        said["note"] = " ".join(notes)


def _mark_stale(session, why: str) -> None:
    if getattr(session, "site", "") and not getattr(session, "stale", ""):
        session.stale = why


# Their saved logins, as the tools tell the model of them: names, domains and
# tiers (`vault.listed`, never the keychain), read in a thread at most once a
# minute, and again when the vault itself is another -- a test's.
LOGINS_TTL_S = 60.0
_logins: dict = {"at": float("-inf"), "vault": None, "sites": []}


async def _saved_logins() -> list:
    from core.browser import vault

    saved = vault.vault()  # their saved logins, not a database
    now = time.monotonic()
    if _logins["vault"] is not saved or now - _logins["at"] > LOGINS_TTL_S:
        _logins.update(sites=await asyncio.to_thread(saved.listed), vault=saved, at=now)
    return list(_logins["sites"])


def logins_line(sites) -> str:
    """One line naming their saved logins -- names, domains and whether each
    asks them first; never a secret -- or "" when the user has none."""
    if not sites:
        return ""
    shown = "; ".join(
        f"{s.name} ({', '.join(s.domains[:4])}{', ...' if len(s.domains) > 4 else ''}; "
        f"{'free to use' if s.tier == 'free' else 'asks the user first'})" for s in sites[:12])
    line = (f"The user's saved logins: {shown}. Open those sites' pages here, logged in as "
            "them -- "
            "read_page and search_and_read cannot sign in, and those sites refuse them.")
    if any(_on_linkedin(s.domains) for s in sites):
        # Not this tool: `linkedin_refusal` would turn the model back at the
        # first page, after it had been told here to come.
        line += (" Except LinkedIn: that is read with linkedin_profile, "
                 "linkedin_search_people, linkedin_company and linkedin_jobs, never here.")
    return line


async def with_saved_logins(tools: list, deps: Deps) -> list:
    """`browse_open` as the model is shown it, with their saved logins named
    (`toolset.offered`). Not for a browsing worker, which never logs in."""
    if deps.browse_sites or not any(
            getattr(t, "name", "") == "browse_open" and dataclasses.is_dataclass(t)
            for t in tools):
        return tools
    line = logins_line(await _saved_logins())
    if not line:
        return tools
    return [dataclasses.replace(t, description=f"{t.description or ''}\n\n{line}")
            if t.name == "browse_open" and dataclasses.is_dataclass(t) else t for t in tools]


async def login_hint(deps: Deps, url: str) -> str:
    """For read_page and search_and_read when a site refuses them: if the user
    has a saved login for it and the browser is theirs to use here, where to
    go instead; else "". On 7 October 2026 LinkedIn answered read_page with
    HTTP 999 three times, and the model was told to look elsewhere."""
    if not url or deps.browse_sites or refusal(deps):
        return ""
    from core.browser import driver

    for site in await _saved_logins():
        if driver.on_site(url, site.domains):
            if driver.on_site(url, ("linkedin.com",)):
                # Not browse_open: that sent the model to LinkedIn with no pace
                # and no pause (8 October 2026 audit).
                return (f"The user has a saved login for LinkedIn ('{site.name}'): "
                        f"{LINKEDIN_ONLY}")
            return (f"The user has a saved login for this site ('{site.name}'): browse_open "
                    "the page instead, which opens it logged in as them")
    return ""


async def linkedin_refusal(deps: Deps, url: str) -> str:
    """Why `url` is not opened or fetched by the general tools, or "": it is
    on LinkedIn, the user has a saved login for LinkedIn, and the browser may be
    used in this run -- which is when the LinkedIn tools are there instead
    (`linkedin.offered`). A run where the browser may not be used is left to
    its own refusals."""
    if not url or deps.browse_sites or refusal(deps):
        return ""
    from agent.toolset import linkedin
    from core.browser import driver

    where = url if "://" in url else f"https://{url}"
    if driver.on_site(where, (linkedin.DOMAIN,)) and await linkedin.covered():
        return LINKEDIN_ONLY
    return ""


def linkedin_skip(deps: Deps) -> Callable[[str], Awaitable[str | None]]:
    """For `read_page` and `tools.web.search_and_read`: says why an address is
    not fetched when it is on LinkedIn (`linkedin_refusal`), so a search never
    loads linkedin.com pages from the laptop's own address, unpaced, to be
    refused. Nothing is looked up for an address that is not LinkedIn's."""
    from core.browser import driver

    async def skip(url: str) -> str | None:
        if not driver.on_site(url if "://" in url else f"https://{url}", ("linkedin.com",)):
            return None
        return f"not fetched: {why}" if (why := await linkedin_refusal(deps, url)) else None

    return skip


def _save(session: str, step: int, png: str) -> Path:
    folder = shots_dir() / session
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{step:03d}-{int(time.time())}.png"
    path.write_bytes(base64.b64decode(png))
    return path


def _record(deps: Deps, session, cmd: str, args: dict, said: dict, shot: Path | None, *,
            action_id: int | None = None) -> None:
    """One row per step: what was done, where it was, how it went. Never
    raises -- the record failing is not the step failing."""
    detail = str(args.get("url") or args.get("ref") or args.get("direction") or "")
    if cmd == "search":
        detail = f"{args.get('ref')}: {args.get('text')}"
    try:
        with db.transaction(deps.conn):
            deps.conn.execute(
                "INSERT INTO browser_steps (session_id, browser, step, action, detail, url, title,"
                " ok, error, screenshot, action_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (deps.session_id, session.box.session, session.steps, cmd, detail[:500],
                 str(said.get("url") or "")[:500], str(said.get("title") or "")[:300],
                 int(bool(said.get("ok"))), str(said.get("error") or "")[:300] or None,
                 str(shot) if shot else None, action_id))
    except Exception:  # noqa: BLE001
        log.warning("could not record a browser step", exc_info=True)
