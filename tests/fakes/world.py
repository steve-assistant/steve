"""The fake world a real backend runs in, and the canaries that watch it.

The end-to-end tests (tests/e2e/) start Steve's backend as the app starts
it, in its own processes, which reach the services only over sockets. A
world starts each fake the test asks for on this computer and gives the
environment that points the backend at them: `STEVE_OPENAI_URL`,
`STEVE_OPENROUTER_URL`, `STEVE_TELEGRAM_URL` (core/endpoints.py, honoured
in a checkout only) and Signal's program and folder.

Every secret is a canary: each key, the bot's token and the
authenticator's secret get a value of their own, made for the run, and
the world's folder holds them as the keychain (`keychain.json`, which the
child fence reads when `STEVE_TEST_WORLD` names the folder:
tests/gate/childfence/sitecustomize.py). So a secret found anywhere was
put there by the backend, and `leaks` says where: every file in the data
folder, the logs and the database's pages among them, and every request
every fake received, headers and body. Each secret has the one place it
may go (`ALLOWED`); anywhere else is a leak. The fixed keys the fakes
were given before (`GOOD`, the fenced dummies) would hide one: a leak of
a value every test shares looks like any other test's.

The world's `world.json` holds its settings for the backend: statements to
run in a module as it is imported (`after_import`, applied by the child
fence). Some of the product's pace is a constant with no knob, the hold
before a yes runs above all, and a scenario would wait it out each time.
"""

from __future__ import annotations

import base64
import contextlib
import json
import secrets as random
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tests.fakes import signal_cli
from tests.fakes.jev import FakeJev
from tests.fakes.openai import FakeOpenAI
from tests.fakes.served import served_tls
from tests.fakes.telegram import FakeBotApi

WORLD_ENV = "STEVE_TEST_WORLD"
# Steve's keychain service (core/identity.py), as the child fence keys it.
SERVICE = "steve"
# The local API's token, from the run file: a secret the backend makes.
TOKEN = "the local API's token"

# What every world's backend is given (`world.json`'s `after_import`, run
# in each module as the child fence sees it imported): a yes runs 2 s after
# it is given, not 30; reminders, routines, the outbox and the briefing's
# hour are looked at every second, not every 20 s to 5 minutes. A scenario
# waits for what it checks, so the product's own pace would only make it
# slower. The outbox's matters most: the worker wakes it for a message it
# writes itself, but not for one a test writes into its database.
BRISK = {"agent.approvals": "HOLD_SECONDS = 2",
         "agent.reminders": "TICK_S = 1",
         "agent.worker.background": "ROUTINE_TICK_S = 1; OUTBOX_EVERY_S = 1; BRIEFING_CHECK_S = 1",
         "core.engines.imap": "SENT_WAIT_S = 0.5"}

# The world's mail provider, as setup's "Other" takes it: the servers a
# user types, and the calendar's address. And a calendar feed's host. Each
# is served on this computer, under its name (tests/fakes/worldnet.py).
IMAP, SMTP, CALDAV, FEEDS = ("imap.mail.example", "smtp.mail.example", "caldav.mail.example",
                             "feeds.school.example")
ADDRESS = "maya@mail.example"
# Setup's Fastmail preset, whose servers it fixes, is one whose mail a
# plus-address reaches (modules/imap/manifest.toml), as the agent's own
# mailbox needs (core/mailbox.py `choose`). The world serves its mail
# under those names too, so a test can choose the mailbox as the app does.
PLUS_IMAP, PLUS_SMTP = "imap.fastmail.com", "smtp.fastmail.com"
# What each world's backend runs as `core.modctx` is imported, when the
# world serves mail, a calendar or a feed.
ATTACH = "from tests.fakes import worldnet as _worldnet\n_worldnet.attach(Network, egress)"
# What a world's backend runs as `core.sandbox.probe` is imported, when the
# world lets it run modules: the probe at its start tries this computer's
# address as found from its host name (`worldnet.own_address`).
PROBE = "from tests.fakes import worldnet as _worldnet\nown_address = _worldnet.own_address"

# The one place each secret may be seen: (where, what part). A fake's
# requests are scanned header by header, and the body whole.
ALLOWED: dict[str, set[tuple[str, str]]] = {
    "OPENAI_API_KEY": {("the fake OpenAI", "header authorization")},
    "OPENROUTER_API_KEY": {("the fake Jev", "header authorization")},
    TOKEN: {("the data folder", "run/backend.json")},
}


def holds(text: str | bytes, value: str) -> bool:
    """Whether `text` holds `value`. A file's bytes are searched for the
    value as UTF-8, as Latin-1 and as UTF-16, Windows' own: the scan
    decoded every file as Latin-1 until step 11's audit, so a secret a
    program wrote in UTF-16 passed it. Text the fakes kept is searched as
    it is."""
    if isinstance(text, str):
        return value in text
    forms = {value.encode("utf-8"), value.encode("utf-16-le")}
    with contextlib.suppress(UnicodeEncodeError):
        forms.add(value.encode("latin-1"))
    return any(form in text for form in forms)


def on_disk(root: Path) -> Iterator[tuple[str, bytes | None]]:
    """Each file under `root`, by its path from there, with its bytes, or
    None if it could not be read. A file the scan cannot read is one it
    cannot say holds nothing: it was passed over until step 11's audit."""
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        try:
            raw = path.read_bytes()
        except OSError:
            raw = None
        yield path.relative_to(root).as_posix(), raw


def found_in(root: Path, watched: dict[str, str], where: str) -> list[str]:
    """Each watched secret in a file under `root`, and each file there that
    could not be read, as `World.leaks` says them."""
    found = []
    for part, raw in on_disk(root):
        if raw is None:
            found.append(f"could not read {where} ({part})")
            continue
        found += [f"{name} in {where} ({part})" for name, value in watched.items()
                  if value and holds(raw, value)]
    return found


def canaries() -> dict[str, str]:
    """A value of its own for each secret the keychain holds, in the shape
    the code checks it by (a bot token as BotFather's, the authenticator's
    secret as base32), and marked so a reader of a failure knows it."""
    tag = random.token_hex(6)
    return {
        "OPENAI_API_KEY": f"sk-proj-canary-{tag}-openai",
        "OPENROUTER_API_KEY": f"sk-or-v1-canary-{tag}-jev",
        "BRAVE_API_KEY": f"canary-{tag}-brave",
        "TELEGRAM_BOT_TOKEN": f"{100000000 + random.randbelow(899999999)}:"
                              f"canary{tag}{random.token_urlsafe(24)[:17]}",
        "APPROVAL_TOTP_SECRET": base64.b32encode(random.token_bytes(20)).decode().rstrip("="),
    }


@dataclass
class World:
    folder: Path
    secrets: dict[str, str]
    openai: FakeOpenAI | None = None
    jev: FakeJev | None = None
    telegram: FakeBotApi | None = None
    # signal-cli's folder (STEVE_SIGNAL_DATA), for `signal_cli.says` and `sent`.
    signal: Path | None = None
    env: dict[str, str] = field(default_factory=dict)
    # The mailbox's server store and what its SMTP server took; its
    # calendar (Radicale); the feed and its private link.
    mail: Any = None
    smtp: Any = None
    calendar: Any = None
    feed: Any = None
    feed_link: str = ""

    def mailbox(self, name: str = "Mailbox", *, plus: bool = False) -> dict:
        """The `/control/accounts` body that adds the world's mailbox, with
        its calendar when the world serves one, as setup's "Other" sends it.
        `plus`: as its Fastmail preset sends it instead, so the agent's own
        mailbox can be there; that preset fixes its calendar's address too,
        so the world's calendar is added apart (`calendar_account`)."""
        if plus:
            return {"op": "add", "module": "imap", "address": ADDRESS, "preset": "fastmail",
                    "fields": {"password": self.secrets["MAIL_PASSWORD"]},
                    "classes": ["mail"], "name": name}
        fields = {"password": self.secrets["MAIL_PASSWORD"], "imap": f"{IMAP}:993",
                  "smtp": f"{SMTP}:465"}
        classes = ["mail"]
        if self.calendar is not None:
            fields["url"] = self.calendar.calendar_url()
            classes.append("calendar")
        return {"op": "add", "module": "imap", "address": ADDRESS, "preset": "other",
                "fields": fields, "classes": classes, "name": name}

    def calendar_account(self, name: str = "Calendar") -> dict:
        """The `/control/accounts` body that adds the world's calendar alone,
        by its address, as setup's "Other" sends it."""
        return {"op": "add", "module": "imap", "address": ADDRESS, "preset": "other",
                "fields": {"password": self.secrets["MAIL_PASSWORD"],
                           "url": self.calendar.calendar_url()},
                "classes": ["calendar"], "name": name}

    def feed_account(self, name: str = "Course site") -> dict:
        """The `/control/accounts` body that adds the world's feed by its link."""
        return {"op": "add", "module": "ics", "address": "",
                "fields": {"link": self.feed_link}, "name": name}

    def keep(self, name: str, value: str) -> None:
        """A secret the test gives the backend another way (a passphrase
        typed in setup, an account's password): watched as the rest are."""
        self.secrets[name] = value

    def kept(self) -> dict[str, str]:
        """The keychain as the backend has left it."""
        held = json.loads((self.folder / "keychain.json").read_text(encoding="utf-8"))
        return {name.removeprefix(f"{SERVICE}/"): value for name, value in held.items()}

    def leaks(self, data: Path, *, also: dict[str, str] | None = None) -> list[str]:
        """Each secret seen where it may not be, as "NAME in WHERE (PART)".
        `also` adds secrets made outside the world (the local API's token).
        A file that could not be read is said too, as "could not read
        WHERE (PART)"."""
        watched = {**self.secrets, **(also or {})}
        found = []
        for where, part, text in self._places(data):
            if text is None:
                found.append(f"could not read {where} ({part})")
                continue
            for name, value in watched.items():
                if value and holds(text, value) and (where, part) not in ALLOWED.get(name, ()):
                    found.append(f"{name} in {where} ({part})")
        return found

    def _places(self, data: Path) -> Iterator[tuple[str, str, str | bytes | None]]:
        for part, raw in on_disk(data):
            yield "the data folder", part, raw
        for where, fake in (("the fake OpenAI", self.openai), ("the fake Jev", self.jev)):
            for kept in getattr(fake, "requests", ()):
                yield where, "path", kept.path
                for header, value in kept.headers.items():
                    yield where, f"header {header.lower()}", value
                yield where, "body", json.dumps(kept.body)
        if self.telegram is not None:
            for method, params in list(self.telegram.calls):
                yield "the fake Telegram", method, json.dumps(params)
        if self.signal is not None:
            for call in signal_cli.sent(self.signal):
                yield "the fake signal-cli", call["method"], json.dumps(call["params"])
        # Every message Steve sent or filed: its password goes to the server
        # at each login, which is not looked at, and never into a message.
        if self.smtp is not None:
            for sent in list(self.smtp.sent):
                yield "the fake mail server", "a message sent", bytes(sent.raw)
        if self.mail is not None:
            for box in list(self.mail.boxes.values()):
                for stored in list(box.items):
                    yield "the fake mail server", box.name, bytes(stored.raw)


def _settings(given: dict[str, str], ours: dict[str, str]) -> dict[str, str]:
    """The world's statements (`BRISK`, `ATTACH`) with a test's own, each
    module's run after the world's: a test that set one module's value
    used to replace the world's others for that module, and its backend
    ran at the product's pace there."""
    return {name: "\n".join(code for code in (ours.get(name), given.get(name)) if code)
            for name in {**ours, **given}}


def _feed(feed, path: str):
    """The feed's calendar at `path`, as a WSGI app; any other path is not
    found, as a server's is."""
    def app(environ, start_response):
        if environ.get("PATH_INFO") != path:
            start_response("404 Not Found", [("Content-Type", "text/plain")])
            return [b"no such feed"]
        start_response("200 OK", [("Content-Type", "text/calendar; charset=utf-8")])
        return [feed.text()]
    return app


def _network(made: World, served: contextlib.ExitStack, *, mail: bool, calendar: bool,
             feed) -> dict:
    """Start the mail, calendar and feed servers asked for, on loopback,
    each with a certificate for its name from one authority made for the
    run; what `world.json` tells the backend of them (tests/fakes/worldnet.py)."""
    import ssl

    import trustme

    ca = trustme.CA()
    tls = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ca.issue_cert(IMAP, SMTP, CALDAV, FEEDS, PLUS_IMAP, PLUS_SMTP).configure_cert(tls)
    network: dict[str, Any] = {"hosts": {}, "https": {},
                               "ca": ca.cert_pem.bytes().decode("ascii")}
    password = f"canary-{random.token_hex(6)}-mail"
    if mail:
        from tests.fakes.imap import STANDARD, ImapServer, ImapWorld
        from tests.fakes.smtp import SmtpServer, SmtpWorld

        made.keep("MAIL_PASSWORD", password)
        made.mail = ImapWorld(STANDARD, user=ADDRESS, password=password)
        made.smtp = SmtpWorld(user=ADDRESS, password=password, imap=made.mail)
        imap, smtp = ImapServer(made.mail, tls), SmtpServer(made.smtp, tls)
        served.callback(imap.close)
        served.callback(smtp.close)
        network["hosts"] = {name: {"993": imap.port} for name in (IMAP, PLUS_IMAP)}
        network["hosts"] |= {name: {"465": smtp.port} for name in (SMTP, PLUS_SMTP)}
    if calendar:
        from tests.fakes.caldav import STANDARD as PLAIN
        from tests.fakes.caldav import CalWorld, Habits

        store = made.folder / "caldav"
        store.mkdir(exist_ok=True)
        made.calendar = CalWorld(store, user=ADDRESS, password=password,
                                 base=f"https://{CALDAV}")
        habits = Habits(made.calendar.app, mode=PLAIN)
        network["https"][CALDAV] = served.enter_context(served_tls(habits, tls, "caldav"))
    if feed:
        from tests.fakes import ics

        made.feed = feed if isinstance(feed, ics.FeedWorld) else ics.FeedWorld()
        made.feed_link = f"https://{FEEDS}{ics.PATH}?token={random.token_hex(8)}"
        made.keep("FEED_LINK", made.feed_link)
        network["https"][FEEDS] = served.enter_context(
            served_tls(_feed(made.feed, ics.PATH), tls, "feed"))
    return network


@contextlib.contextmanager
def world(folder: Path, *, openai: FakeOpenAI | bool = True, jev: FakeJev | bool = True,
          telegram: bool = False, signal: bool = False, kept: bool = True,
          mail: bool = False, calendar: bool = False, feed: Any = False,
          sandbox: bool = False,
          after_import: dict[str, str] | None = None) -> Iterator[World]:
    """The fakes asked for, served for the length of the block, and the
    environment that points a backend at them. `kept`: the keychain starts
    holding every secret, as an install that finished setup has it; without
    it, empty, as a fresh one's is. `mail`, `calendar`: a mailbox and its
    calendar at the world's provider, added by `World.mailbox`; `feed`: a
    calendar feed (a `FeedWorld`, or True for an empty one), added by
    `World.feed_account`. `sandbox`: the backend checks at its start that
    modules can run here, as shipped, in a real AppContainer, so it offers
    to build one; a test that asks for it deletes the profiles its backend
    makes. `after_import`: statements to run in a module of
    the backend's as it is imported, after the world's for the same module,
    so a test can change one of the world's and keep the rest."""
    folder.mkdir(parents=True, exist_ok=True)
    made = World(folder, canaries())
    (folder / "keychain.json").write_text(
        json.dumps({f"{SERVICE}/{name}": value for name, value in made.secrets.items()}
                   if kept else {}), encoding="utf-8", newline="\n")
    made.env[WORLD_ENV] = str(folder)
    with contextlib.ExitStack() as served:
        ours = dict(BRISK)
        settings: dict[str, Any] = {}
        if mail or calendar or feed:
            settings["network"] = _network(made, served, mail=mail, calendar=calendar,
                                           feed=feed)
            ours["core.modctx"] = ATTACH
        if sandbox:
            ours["core.sandbox.probe"] = PROBE
        settings["after_import"] = _settings(after_import or {}, ours)
        (folder / "world.json").write_text(json.dumps(settings), encoding="utf-8",
                                           newline="\n")
        if openai:
            made.openai = openai if isinstance(openai, FakeOpenAI) else FakeOpenAI()
            made.openai.keys.add(made.secrets["OPENAI_API_KEY"])
            made.env["STEVE_OPENAI_URL"] = served.enter_context(made.openai.running())
        if jev:
            made.jev = jev if isinstance(jev, FakeJev) else FakeJev(otherwise=0.1)
            made.jev.keys.add(made.secrets["OPENROUTER_API_KEY"])
            made.env["STEVE_OPENROUTER_URL"] = served.enter_context(made.jev.running())
        if telegram:
            made.telegram = FakeBotApi(token=made.secrets["TELEGRAM_BOT_TOKEN"])
            made.env["STEVE_TELEGRAM_URL"] = served.enter_context(made.telegram.running())
        if signal:
            made.signal = folder / "signal"
            signal_cli.linked(made.signal)
            # Run as the real one is, through a launcher, by the fenced
            # Python the test runs under (tests/gate/fences.py).
            launcher = folder / "signal-cli.bat"
            launcher.write_text(f'@"{sys.executable}" "{signal_cli.__file__}" %*\r\n',
                                encoding="utf-8")
            made.env["STEVE_SIGNAL_CLI"] = str(launcher)
            made.env["STEVE_SIGNAL_DATA"] = str(made.signal)
        yield made
