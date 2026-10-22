"""The sandboxed browser holds nothing of theirs, and a page in it can reach
nothing of theirs.

Frozen, so the night cannot loosen any of it. Nothing here starts Docker or
touches the network: the containers are pinned by the arguments that build
them, the gate runs in-process against a resolver and a connector the test
hands it, and the driver's checks are pure functions. The live proof --
the same claims, from inside a real container -- is the doctor,
`python -m core.browser check` (the browser add-on in docs/security-model.md).
"""

from __future__ import annotations

import asyncio
import contextlib
import subprocess

import pytest

from core import egress, identity, images
from core.browser import driver, gate, sandbox

OWNER = "4242:1790000000"
# The IDs a build recorded (core/images.py): what a session runs.
GATE_ID, BROWSER_ID = "sha256:" + "6" * 64, "sha256:" + "b" * 64


def _flag(args: list[str], name: str) -> str:
    return args[args.index(name) + 1]


# ---------------------------------------------------------------------------
# The containers
# ---------------------------------------------------------------------------

FORBIDDEN = ("-v", "--volume", "--mount", "-p", "--publish", "-P", "--publish-all",
             "--privileged", "--pid", "--ipc", "--cap-add", "--device", "--add-host",
             "--env-file", "-e", "--env", "--userns", "--security-opt=seccomp=unconfined")


def _no_forbidden(args: list[str]) -> None:
    for a in args:
        for f in FORBIDDEN:
            assert a != f and not a.startswith(f + "="), f"{a} in {args}"


def test_the_browser_sits_on_its_sessions_network_only_with_nothing_of_theirs():
    """No mounts, no published ports, no environment: there is nothing of their
    in it to read, and nothing outside it can call in."""
    args = sandbox.browser_args("s1", OWNER, BROWSER_ID)
    network, _, _ = sandbox.names("s1")

    assert _flag(args, "--network") == network and args.count("--network") == 1
    assert "host" not in args and "bridge" not in args
    _no_forbidden(args)


def test_the_browser_resolves_nothing_itself():
    """Its only resolver is the gate's: a page cannot leak a name in a lookup."""
    assert _flag(sandbox.browser_args("s1", OWNER, BROWSER_ID), "--dns") == "127.0.0.1"


@pytest.mark.parametrize("args", [sandbox.browser_args("s1", OWNER, BROWSER_ID),
                                  sandbox.gate_args("s1", OWNER, GATE_ID)])
def test_both_containers_are_locked_down(args):
    assert "--read-only" in args and "--rm" in args
    assert _flag(args, "--cap-drop") == "ALL"
    assert _flag(args, "--security-opt") == "no-new-privileges"
    assert _flag(args, "--user") not in ("root", "0", "0:0")
    assert "--memory" in args and "--pids-limit" in args and "--cpus" in args
    _no_forbidden(args)


def test_a_session_ends_on_its_own_whatever_the_worker_does():
    browser = sandbox.browser_args("s1", OWNER, BROWSER_ID)
    gate_ = sandbox.gate_args("s1", OWNER, GATE_ID)

    assert browser[-4:] == ["timeout", str(sandbox.LIFETIME_S), "sleep", "infinity"]
    assert gate_[gate_.index(GATE_ID) + 1:][:2] == ["timeout",
                                                                str(sandbox.GATE_LIFETIME_S)]
    assert sandbox.LIFETIME_S <= 3600


def test_the_sessions_network_has_no_route_out():
    args = sandbox.network_args("s1", OWNER)

    assert args[:2] == ["network", "create"] and "--internal" in args


def test_the_gate_is_the_only_thing_on_two_networks():
    gate_ = sandbox.gate_args("s1", OWNER, GATE_ID)

    assert _flag(gate_, "--network") == "bridge"
    assert sandbox.connect_args("s1") == ["network", "connect", *sandbox.names("s1")[:2]]


def test_the_driver_is_reached_over_exec_never_a_port():
    box = sandbox.Sandbox("s1", OWNER, *sandbox.names("s1"), proxy="http://172.18.0.2:3128")

    assert sandbox.driver_argv(box)[:3] == ["docker", "exec", "-i"]


def test_an_image_is_tagged_by_what_it_is_built_from(monkeypatch):
    """A changed driver is a new image, never a stale one still running."""
    before = sandbox.image("browser")
    real = sandbox._context

    def edited(kind):
        files = real(kind)
        if kind == "browser":
            files["driver.py"] += b"\n# changed\n"
        return files

    monkeypatch.setattr(sandbox, "_context", edited)

    assert sandbox.image("browser") != before


class _Docker:
    """Docker as a list of what it was asked, answering from a script."""

    def __init__(self, answers: dict[str, subprocess.CompletedProcess] | None = None) -> None:
        self.asked: list[list[str]] = []
        self.answers = answers or {}

    def __call__(self, args: list[str], timeout: float) -> subprocess.CompletedProcess:
        self.asked.append(args)
        for prefix, answer in self.answers.items():
            if " ".join(args).startswith(prefix):
                return answer
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")


def _done(stdout: str = "", code: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], code, stdout=stdout, stderr="refused" if code else "")


def _built(answers: dict[str, subprocess.CompletedProcess] | None = None) -> _Docker:
    """Docker holding the two images Steve built and recorded."""
    images.remember(sandbox.image("gate"), GATE_ID)
    images.remember(sandbox.image("browser"), BROWSER_ID)
    return _Docker({f"image inspect --format {{{{.Id}}}} {GATE_ID}": _done(GATE_ID),
                    f"image inspect --format {{{{.Id}}}} {BROWSER_ID}": _done(BROWSER_ID),
                    "image inspect": _done(code=1), **(answers or {})})


def test_a_start_that_fails_part_way_leaves_nothing_behind():
    docker = _built({"run -d --rm --name steve-browser-s1 ": _done(code=1)})

    with pytest.raises(RuntimeError, match="could not start the browser"):
        sandbox.start(OWNER, run=docker, session="s1")

    network, gate_name, browser = sandbox.names("s1")
    assert ["rm", "-f", browser, gate_name] in docker.asked
    assert ["network", "rm", network] in docker.asked


def test_a_session_runs_the_images_steve_built_by_their_ids_never_by_tag():
    """Any account allowed to use Docker can point Steve's tags at images of
    its own, and the browser's tag is a hash of public source, so it can be
    worked out in advance. Run by tag, such an image was handed a saved
    login's cookies and typed the user's passwords (step 11's audit, C4)."""
    docker = _built({"inspect -f": _done("172.18.0.2")})

    box = sandbox.start(OWNER, run=docker, session="s1")

    runs = [args for args in docker.asked if args[0] == "run"]
    assert [args for args in runs if GATE_ID in args] and [a for a in runs if BROWSER_ID in a]
    tags = {sandbox.image(kind) for kind in sandbox.KINDS}
    assert not [args for args in runs if tags & set(args)], runs
    assert box.proxy == f"http://172.18.0.2:{sandbox.GATE_PORT}"


def test_an_image_under_steves_tag_that_steve_did_not_build_is_never_run():
    """Docker answers for the tags, as it does once someone else has built
    or tagged them; Steve recorded no build, so nothing starts and the
    browser is not ready."""
    docker = _Docker({"image inspect": _done("sha256:" + "e" * 64)})

    with pytest.raises(RuntimeError, match="not built"):
        sandbox.start(OWNER, run=docker, session="s1")
    assert not sandbox.ready(docker)

    assert not [args for args in docker.asked if args[0] in ("run", "network")]


def test_a_build_records_the_id_docker_gave_it_and_tags_are_not_trusted(monkeypatch):
    """The ID comes from the build itself (`--iidfile`), not from asking
    Docker what the tag names afterwards, and the bases are not pulled
    again by tag (`--pull` took whatever the tag named that day)."""
    from pathlib import Path

    made: dict[str, str] = {}
    asked: list[list[str]] = []

    def docker(args: list[str], timeout: float) -> subprocess.CompletedProcess:
        asked.append(args)
        if args[0] == "build":
            made[args[args.index("-t") + 1]] = image_id = "sha256:" + str(len(made) + 1) * 64
            Path(args[args.index("--iidfile") + 1]).write_text(image_id, encoding="utf-8")
        if args[:2] == ["image", "inspect"]:
            return _done(args[-1], code=0 if args[-1] in made.values() else 1)
        return _done()

    monkeypatch.setattr(sandbox, "available", lambda run=None: True)
    built = sandbox.build(docker)

    assert built == [sandbox.image("gate"), sandbox.image("browser")]
    assert all(images.built(tag, docker) == made[tag] for tag in built)
    assert not [args for args in asked if "--pull" in args or args[0] == "pull"]
    assert sandbox.ready(docker) and sandbox.build(docker) == [], "built once, then found"


def test_the_bases_are_pinned_by_digest_and_the_packages_by_hash():
    """A tag is moved by whoever publishes it, and pip takes whatever the
    index serves that day: the image Steve runs would have been whatever
    either named when it was built (step 11's audit, F2)."""
    import re

    for dockerfile in (sandbox.GATE_DOCKERFILE, sandbox.BROWSER_DOCKERFILE):
        [base] = re.findall(r"^FROM (\S+)$", dockerfile, re.M)
        assert re.search(r"@sha256:[0-9a-f]{64}$", base), base
        assert "install chromium" not in dockerfile, "a download no hash covers"
        for pip in re.findall(r"pip install[^\n]*", dockerfile.replace("\\\n", " ")):
            assert "--require-hashes -r" in " ".join(pip.split()), pip
    assert "pip install" in sandbox.BROWSER_DOCKERFILE

    pins = (sandbox.HERE / "requirements.txt").read_text(encoding="utf-8")
    lines = [line for line in pins.replace("\\\n", " ").splitlines()
             if line.strip() and not line.startswith("#")]
    assert lines and all(re.match(r"[\w.-]+==\S+ +--hash=sha256:[0-9a-f]{64}", line)
                         for line in lines), lines


def test_reaping_removes_only_a_dead_workers_sessions_of_this_install():
    """Two workers overlap during a swap: the new one must not tear down the
    sessions of the one still draining. Another install's are its own, and
    Docker, like a server ignoring a filter, lists them anyway here."""
    from core.sandbox import profiles

    ours = profiles.install()
    docker = _Docker({
        "ps -a": _done(f"steve-browser-old\t1:1\t{ours}\nsteve-browser-old-gate\t1:1\t{ours}\n"
                       f"steve-browser-new\t2:2\t{ours}\nsteve-browser-theirs\t1:1\tffffffff\n"),
        "network ls": _done(f"steve-browser-old-net\t1:1\t{ours}\n"
                            f"steve-browser-new-net\t2:2\t{ours}\n"),
    })

    gone = sandbox.reap(run=docker, is_alive=lambda owner: owner == "2:2")

    assert gone == ["steve-browser-old", "steve-browser-old-gate", "steve-browser-old-net"]
    assert ["rm", "-f", "steve-browser-new"] not in docker.asked


def test_everything_steve_starts_in_docker_carries_its_install():
    """Uninstall removes only what carries its install's id (finding 13). The
    names were the personal edition's (finding 33)."""
    from core.sandbox import profiles

    label = ["--label", f"{identity.DOCKER_INSTALL_LABEL}={profiles.install()}"]
    for args in (sandbox.network_args("s1", OWNER), sandbox.gate_args("s1", OWNER, GATE_ID),
                 sandbox.browser_args("s1", OWNER, BROWSER_ID)):
        assert any(args[i:i + 2] == label for i in range(len(args)))
    assert all(name.startswith(identity.DOCKER_NAMES) for name in sandbox.names("s1"))


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

NAMES = {"public.example": "93.184.216.34", "intranet.example": "10.0.0.5",
         "router.example": "192.168.1.1", "metadata.example": "169.254.169.254",
         "loop.example": "127.0.0.1", "cgnat.example": "100.64.0.9"}


@pytest.fixture
def world(monkeypatch):
    """The real `egress.addresses` over a resolver the test owns, and an
    upstream that echoes -- every connection the gate makes, recorded."""
    import socket

    def getaddrinfo(host, port, *args, **kwargs):
        if host not in NAMES:
            raise socket.gaierror("unknown")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (NAMES[host], port))]

    monkeypatch.setattr(egress.socket, "getaddrinfo", getaddrinfo)
    return {"connected": [], "received": [], "logged": []}


@contextlib.asynccontextmanager
async def _gate_up(world, **kwargs):
    """A gate on a local port, over an upstream that echoes. On the way out
    every connection is cut, so a test whose gate misbehaves fails rather
    than waits for ever on a tunnel nobody closed."""
    async def echo(reader, writer):
        data = await reader.read(65536)
        world["received"].append(data)
        if data.startswith(b"GET "):
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        else:
            writer.write(data)
            while more := await reader.read(65536):
                writer.write(more)
        await writer.drain()
        writer.close()

    upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
    up_port = upstream.sockets[0].getsockname()[1]

    async def connect(ip, port):
        world["connected"].append((ip, port))
        return await asyncio.open_connection("127.0.0.1", up_port)

    the_gate = gate.Gate(connect=connect, log=world["logged"].append, **kwargs)
    server = await asyncio.start_server(the_gate.handle, "127.0.0.1", 0, limit=gate.HEAD_LIMIT)
    try:
        yield server.sockets[0].getsockname()[1], the_gate
    finally:
        for s in (server, upstream):
            s.close()
            s.close_clients()


async def _ask(port: int, request: bytes, then: bytes = b"") -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(request)
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        body = b""
        if then:
            writer.write(then)
            await writer.drain()
            body = await asyncio.wait_for(reader.read(len(then)), 5)
        return head + body
    finally:
        writer.close()


@pytest.mark.parametrize("target, why", [
    ("127.0.0.1:443", "a bare IP address"),
    ("[::1]:443", "a bare IP address"),
    ("localhost:443", "a local name"),
    ("host.docker.internal:443", "a local name"),
    ("intranet.example:443", "private address"),
    ("router.example:443", "private address"),
    ("metadata.example:443", "private address"),
    ("loop.example:443", "private address"),
    ("cgnat.example:443", "private address"),
    ("public.example:22", "only 80 and 443"),
    ("public.example:8790", "only 80 and 443"),
])
async def test_the_gate_refuses_anywhere_private_and_any_other_port(world, target, why):
    async with _gate_up(world) as (port, _):
        said = await _ask(port, f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())

    assert said.startswith(b"HTTP/1.1 403") and why.encode() in said
    assert world["connected"] == [], "refused before any connection"
    assert world["logged"][-1]["ok"] is False


async def test_the_gate_tunnels_to_a_public_address_and_logs_it(world):
    async with _gate_up(world) as (port, _):
        said = await _ask(port, b"CONNECT public.example:443 HTTP/1.1\r\n\r\n", then=b"hello")

    assert said.startswith(b"HTTP/1.1 200") and said.endswith(b"hello")
    assert world["connected"] == [("93.184.216.34", 443)], "the checked address, not the name"
    await asyncio.sleep(0.05)
    assert world["logged"][-1]["host"] == "public.example" and world["logged"][-1]["ok"]


async def test_a_name_that_rebinds_gets_no_second_lookup(world):
    """Public for the check, private a moment later: the gate connects to
    what it checked and never asks again."""
    lookups = []

    def rebinding(host, port):
        lookups.append(host)
        return ["93.184.216.34"] if len(lookups) == 1 else ["127.0.0.1"]

    async with _gate_up(world, resolve=rebinding) as (port, _):
        said = await _ask(port, b"CONNECT rebind.example:443 HTTP/1.1\r\n\r\n", then=b"x")

    assert said.startswith(b"HTTP/1.1 200")
    assert lookups == ["rebind.example"] and world["connected"] == [("93.184.216.34", 443)]


async def test_plain_http_is_forwarded_once_and_never_kept_alive(world):
    async with _gate_up(world) as (port, _):
        said = await _ask(port, b"GET http://public.example/a?b=1 HTTP/1.1\r\n"
                                b"Host: public.example\r\nConnection: keep-alive\r\n"
                                b"Proxy-Authorization: Basic eA==\r\n\r\n")

    assert said.startswith(b"HTTP/1.1 200")
    [sent] = world["received"]
    assert sent.startswith(b"GET /a?b=1 HTTP/1.1\r\n")
    assert b"Connection: close" in sent and b"keep-alive" not in sent
    assert b"Proxy-Authorization" not in sent


@pytest.mark.parametrize("request_line", [b"GET https://public.example/ HTTP/1.1",
                                          b"GET ftp://public.example/ HTTP/1.1",
                                          b"GET /relative HTTP/1.1"])
async def test_only_connect_or_an_http_url_is_proxied(world, request_line):
    async with _gate_up(world) as (port, _):
        said = await _ask(port, request_line + b"\r\n\r\n")

    assert said.startswith(b"HTTP/1.1 403") and world["connected"] == []


async def test_past_the_byte_cap_the_tunnel_closes_and_nothing_more_opens(world):
    """Nothing of their is in the sandbox to send; the cap makes sure nothing
    in bulk could be."""
    async with _gate_up(world, cap=64) as (port, the_gate):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(b"CONNECT public.example:443 HTTP/1.1\r\n\r\n")
            await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            writer.write(b"x" * 1000)
            await writer.drain()
            rest = await asyncio.wait_for(reader.read(), 5)
        finally:
            writer.close()
        said = await _ask(port, b"CONNECT public.example:443 HTTP/1.1\r\n\r\n")

    assert len(rest) < 1000, "the tunnel closed instead of carrying it all"
    assert the_gate.sent > the_gate.cap
    assert said.startswith(b"HTTP/1.1 403") and b"byte cap" in said


# ---------------------------------------------------------------------------
# The driver's checks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("el", [
    {"tag": "input", "type": "search"},
    {"tag": "div", "role": "searchbox"},
    {"tag": "input", "type": "text", "name": "q"},
    {"tag": "input", "type": "", "name": "query"},
    {"tag": "input", "type": "text", "placeholder": "Search Northwind"},
    {"tag": "input", "type": "text", "label": "Find a store"},
])
def test_a_search_box_is_one_the_agent_may_type_into(el):
    assert driver.is_search_field(el)


@pytest.mark.parametrize("el", [
    {"tag": "input", "type": "email", "name": "search_email"},
    {"tag": "input", "type": "password", "name": "q"},
    {"tag": "input", "type": "text", "name": "first_name", "label": "First name"},
    {"tag": "input", "type": "text", "name": "q", "form_has_password": True},
    {"tag": "input", "type": "text", "name": "query", "autocomplete": "email"},
    {"tag": "input", "type": "tel", "label": "Search phone"},
    {"tag": "textarea", "label": "Your message"},
    {"tag": "input", "type": "text", "label": "Address"},
    {"tag": "input", "type": "search", "disabled": True},
])
def test_anything_else_is_not_a_search_box(el):
    """Typing into these is disclosure: a submit plan, which the user approves."""
    assert not driver.is_search_field(el)


@pytest.mark.parametrize("fields, button, frames", [
    ([{"autocomplete": "cc-number"}], None, []),
    ([{"name": "cardnumber"}], None, []),
    ([{"label": "Security code"}], None, []),
    ([{"placeholder": "MM / YY expiry"}], None, []),
    ([], None, ["https://js.stripe.com/v3/elements-inner"]),
    ([], {"label": "Place order"}, []),
    ([], {"label": "Pay $40.00"}, []),
    ([], {"label": "Complete purchase"}, []),
    ([], {"label": "Subscribe"}, []),
])
def test_anything_that_looks_like_paying_is_caught(fields, button, frames):
    assert driver.looks_like_payment(fields, button, frames)


def test_an_ordinary_form_is_not_a_payment():
    fields = [{"name": "name", "label": "Name"}, {"name": "party", "label": "Party size"}]

    assert driver.looks_like_payment(fields, {"label": "Book a table"},
                                     ["https://www.opentable.com/widget"]) is None


PAGE = {"e3": {"ref": "e3", "tag": "input", "type": "text", "label": "Name"},
        "e4": {"ref": "e4", "tag": "select", "label": "Party size"},
        "e7": {"ref": "e7", "tag": "button", "label": "Book a table"},
        "e8": {"ref": "e8", "tag": "input", "type": "password", "label": "Password"},
        "e9": {"ref": "e9", "tag": "input", "type": "file", "label": "Upload"}}
PLAN = {"expected_url": "https://example.com/book",
        "fields": [{"ref": "e3", "label": "Name", "value": "Maya"},
                   {"ref": "e4", "label": "Party size", "value": "2"}],
        "button": {"ref": "e7", "label": "Book a table"}}


def test_an_approved_plan_on_the_same_page_may_run():
    assert driver.check_plan(PLAN, PAGE) is None


@pytest.mark.parametrize("change, why", [
    (lambda p: p["fields"][0].update(label="Email"), "is now"),
    (lambda p: p["button"].update(label="Delete account"), "is now"),
    (lambda p: p["fields"].append({"ref": "e99", "label": "x", "value": "y"}), "no longer"),
    (lambda p: p["fields"].append({"ref": "e8", "label": "Password", "value": "x"}),
     "password"),
    (lambda p: p["fields"].append({"ref": "e9", "label": "Upload", "value": "x"}), "file"),
    (lambda p: p["fields"][0].update(value="x" * (driver.VALUE_CHARS + 1)), "longer"),
    (lambda p: p.update(fields=[]), "between"),
    (lambda p: p["fields"][0].update(ref="body"), "not an element"),
])
def test_a_plan_that_no_longer_fits_the_page_is_refused(change, why):
    import copy

    plan = copy.deepcopy(PLAN)
    change(plan)

    assert why in (driver.check_plan(plan, PAGE) or "")


BOXES = {"e5": {"ref": "e5", "tag": "input", "type": "radio", "label": "Medium"},
         "e8": {"ref": "e8", "tag": "input", "type": "checkbox", "label": "Extra Cheese"},
         "e12": {"ref": "e12", "tag": "select", "label": "Size", "options": ["Small", "Large"]},
         "e13": {"ref": "e13", "tag": "button", "label": "Submit order"}}


def _box_plan(ref: str, value: str) -> dict:
    return {"expected_url": "https://example.com/order",
            "fields": [{"ref": ref, "label": BOXES[ref]["label"], "value": value}],
            "button": {"ref": "e13", "label": "Submit order"}}


@pytest.mark.parametrize("ref, value", [("e5", "checked"), ("e8", "unchecked"),
                                        ("e12", "Large")])
def test_a_box_or_a_list_takes_exactly_what_the_card_can_say(ref, value):
    assert driver.check_plan(_box_plan(ref, value), BOXES) is None


@pytest.mark.parametrize("ref, value, why", [
    ("e5", "Medium", "'checked' or 'unchecked'"),
    ("e8", "yes please", "'checked' or 'unchecked'"),
    ("e12", "Medium", "no option"),
    ("e12", "large", "no option"),
])
def test_a_value_a_box_or_list_would_misread_is_refused(ref, value, why):
    """1 October: a card read "Medium: Medium" and "Extra Cheese: Extra
    Cheese", and the driver, not taking those for ticked, left both boxes
    empty -- the order went without what the card showed."""
    assert why in (driver.check_plan(_box_plan(ref, value), BOXES) or "")


def test_the_same_page_is_the_same_url_however_it_is_spelled():
    assert driver.same_page("https://Example.com/book#top", "https://example.com/book")
    assert not driver.same_page("https://example.com/book?x=1", "https://example.com/book")
    assert not driver.same_page("https://example.org/book", "https://example.com/book")


# ---------------------------------------------------------------------------
# Off, classified, and never anyone's but their
# ---------------------------------------------------------------------------


def test_the_browser_is_off_unless_the_frozen_policy_says_on(monkeypatch):
    from core import policy

    assert policy.BrowserConfig().enabled is False
    monkeypatch.setattr(policy, "load_policy", lambda: (_ for _ in ()).throw(OSError("gone")))
    import core.browser

    assert core.browser.enabled() is False, "an unreadable policy is not a yes"


def test_a_browser_submit_always_asks_and_reading_never_does():
    from core.consent import classify

    submit = classify("browse_submit", {"fields": [], "button": {}})
    assert submit.gated and submit.reach.value == "outward"
    for tool in ("browse_open", "browse_read", "browse_click", "browse_scroll", "browse_back",
                 "browse_screenshot", "browse_search", "browse_close"):
        assert classify(tool, {}).free, tool


def test_no_run_nobody_is_watching_is_given_the_browser():
    from agent.attention.prepare import PREPARE_TOOLS
    from agent.workers import WORKER_TOOLS

    assert not {t for t in [*WORKER_TOOLS, *PREPARE_TOOLS] if t.startswith("browse_")}
