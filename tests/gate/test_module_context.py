"""A module reaches the outside only as its manifest and account allow.

`ctx.http` is what a module uses for HTTPS, and core's engines connect
through the same context. Each rule here was a way a provider, or a module
of someone else's, could send the user's mail or password where it was not
meant to go: a redirect carrying a secret to another host, a header naming
another host, a URL holding the password, a public name that comes to
resolve to this computer, a COM object that runs programs
(05-connectors.md, section 1; 06-modules.md, section 4).

Only the network is faked (`Network`): DNS answers and an HTTP transport per
host. Every rule runs as it does live.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from core import credstore, egress
from core.modctx import ModuleContext, Network, Refused, expand
from steve_sdk.context import Account
from steve_sdk.errors import ConnectorError, Transient
from steve_sdk.manifest import parse

PUBLIC_IP = "93.184.216.34"


def _manifest(**extra):
    data = {
        "manifest_version": 1, "sdk": 1, "id": "webby", "label": "Webby", "for": "Tests",
        "classes": ["calendar"],
        "calendar": {"engine": "caldav", "url": "https://dav.webby.example/{address}/"},
        "hosts": ["api.webby.example", "*.cdn.webby.example"],
        "setup": {"steps": ["fields", "test"], "fields": [
            {"name": "token", "type": "secret",
             "bind": {"host": "api.webby.example", "header": "Authorization",
                      "form": "Bearer {}"}},
            {"name": "other", "type": "secret"}]},
    }
    data.update(extra)
    return parse(data)


class FakeNetwork(Network):
    def __init__(self, answers=None, handler=None):
        self.answers = answers or {}
        self.seen: list[httpx.Request] = []
        self._handler = handler or (lambda request: httpx.Response(200, text="ok"))

    def resolve(self, host, port):
        return self.answers.get(host, [PUBLIC_IP])

    def own_ports(self):
        return {54321}

    def transport_for(self, host):
        def handle(request):
            self.seen.append(request)
            return self._handler(request)

        return httpx.MockTransport(handle)


@pytest.fixture
def secrets_store():
    store = credstore.MemoryStore()
    credstore.use(store)
    yield store
    credstore.use(None)


def _ctx(network, *, fields=None, manifest=None, module_key="m0dkey0001", tmp_path=None):
    account = Account(key="acct0001", name="Mine", module="webby",
                      address="me@webby.example", fields=fields or {})
    return ModuleContext(manifest=manifest or _manifest(), module_key=module_key,
                         account=account, state_dir=tmp_path, network=network)


def _store_secret(name, value, module_key="m0dkey0001"):
    credstore.write(credstore.target(module_key, "acct0001", name), value)


def test_a_host_the_manifest_does_not_name_is_refused(secrets_store, tmp_path):
    net = FakeNetwork()
    with pytest.raises(Refused, match="not among the hosts"):
        _ctx(net, tmp_path=tmp_path).http.request("GET", "https://attacker.example/x")
    assert net.seen == []


def test_a_wildcard_host_covers_names_under_it_and_not_the_domain_itself(secrets_store,
                                                                         tmp_path):
    ctx = _ctx(FakeNetwork(), tmp_path=tmp_path)
    assert ctx.http.request("GET", "https://p01.cdn.webby.example/a").ok
    for host in ("cdn.webby.example", "cdn.webby.example.evil.example"):
        with pytest.raises(Refused):
            ctx.http.request("GET", f"https://{host}/a")


def test_a_public_name_that_resolves_to_this_computer_or_the_network_is_refused(
        secrets_store, tmp_path):
    """DNS is the attacker's: a name can be pointed at the router or at
    Steve's own port after the manifest was approved."""
    for address in ("127.0.0.1", "10.0.0.5", "169.254.169.254", "::1"):
        net = FakeNetwork({"api.webby.example": [address]})
        with pytest.raises(Refused):
            _ctx(net, tmp_path=tmp_path).http.request("GET", "https://api.webby.example/")
        assert net.seen == []


def test_a_secret_is_filled_only_into_its_header_for_its_host(secrets_store, tmp_path):
    _store_secret("token", "s3cr3t")
    net = FakeNetwork()
    ctx = _ctx(net, tmp_path=tmp_path)
    ctx.http.request("GET", "https://api.webby.example/me",
                     headers={"Authorization": "{{secret:token}}"})
    assert net.seen[-1].headers["Authorization"] == "Bearer s3cr3t"
    for url, header in (("https://p1.cdn.webby.example/", "Authorization"),
                        ("https://api.webby.example/", "X-Token")):
        with pytest.raises(Refused, match="not bound"):
            ctx.http.request("GET", url, headers={header: "{{secret:token}}"})
    with pytest.raises(Refused, match="not bound"):
        ctx.http.request("GET", "https://api.webby.example/",
                         headers={"Authorization": "{{secret:openai_api_key}}"})
    assert len(net.seen) == 1


def test_a_connection_that_fails_is_said_in_the_sdks_own_errors(secrets_store, tmp_path):
    """A module imports only `steve_sdk`, so it cannot catch httpx's errors
    by name, and one let through is no error core can say or heal. A host
    that does not answer is tried again later; a certificate that is not
    trusted would fail the next try too, so it is not."""
    for raised, kind, said in (
            (httpx.ConnectError("refused"), Transient, "could not reach api.webby.example"),
            (httpx.ReadTimeout("slow"), Transient, "did not answer in time"),
            (httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"),
             ConnectorError, "certificate is not trusted")):
        def fail(request, raised=raised):
            raise raised

        ctx = _ctx(FakeNetwork(handler=fail), tmp_path=tmp_path)
        with pytest.raises(ConnectorError, match=said) as caught:
            ctx.http.request("GET", "https://api.webby.example/")
        assert type(caught.value) is kind


def test_a_secret_is_dropped_on_a_redirect_to_another_host(secrets_store, tmp_path):
    """A server that answers 302 to its own collector must not receive the
    key the user gave for the first host."""
    _store_secret("token", "s3cr3t")

    def handler(request):
        if request.url.host == "api.webby.example":
            return httpx.Response(302, headers={"Location": "https://p9.cdn.webby.example/c"})
        return httpx.Response(200, text="landed")

    net = FakeNetwork(handler=handler)
    answer = _ctx(net, tmp_path=tmp_path).http.request(
        "GET", "https://api.webby.example/me", follow=True,
        headers={"Authorization": "{{secret:token}}", "Cookie": "a=b"})
    assert answer.text == "landed"
    landed = net.seen[-1]
    assert "Authorization" not in landed.headers and "Cookie" not in landed.headers
    assert "s3cr3t" not in str(landed.headers)


def test_a_redirect_to_an_undeclared_host_or_down_to_http_is_refused(secrets_store, tmp_path):
    for location in ("https://attacker.example/", "http://api.webby.example/"):
        net = FakeNetwork(handler=lambda r, loc=location: httpx.Response(
            301, headers={"Location": loc}) if r.url.path == "/" else httpx.Response(200))
        with pytest.raises(Refused):
            _ctx(net, tmp_path=tmp_path).http.request("GET", "https://api.webby.example/",
                                                      follow=True)
        assert len(net.seen) == 1


def test_redirects_are_not_followed_unless_asked(secrets_store, tmp_path):
    net = FakeNetwork(handler=lambda r: httpx.Response(302, headers={"Location": "/next"}))
    answer = _ctx(net, tmp_path=tmp_path).http.request("GET", "https://api.webby.example/")
    assert answer.status == 302 and len(net.seen) == 1


@pytest.mark.parametrize("headers", [
    {"Host": "attacker.example"}, {"X-Forwarded-For": "1.2.3.4"}, {"Forwarded": "for=x"},
    {"X-Original-URL": "/admin"}, {"Transfer-Encoding": "chunked"}, {"Connection": "close"},
    {"X-Note": "a\r\nHost: attacker.example"},
])
def test_a_module_may_not_set_a_header_that_names_another_host_or_splits_the_request(
        headers, secrets_store, tmp_path):
    net = FakeNetwork()
    with pytest.raises(Refused):
        _ctx(net, tmp_path=tmp_path).http.request("GET", "https://api.webby.example/",
                                                  headers=headers)
    assert net.seen == []


def test_plain_http_only_to_a_loopback_host_the_user_typed(secrets_store, tmp_path):
    fields = {"hosts": {"127.0.0.1": egress.LOOPBACK}}
    manifest = parse({
        "manifest_version": 1, "sdk": 1, "id": "bridge", "label": "Bridge", "for": "Tests",
        "classes": ["mail"], "mail": {"engine": "mcp"},
        "setup": {"steps": ["fields", "test"]}})
    net = FakeNetwork({"127.0.0.1": ["127.0.0.1"]})
    ctx = _ctx(net, fields=fields, manifest=manifest, tmp_path=tmp_path)
    assert ctx.http.request("POST", "http://127.0.0.1:7000/mcp", json={}).ok
    with pytest.raises(Refused, match="Steve's own port"):
        ctx.http.request("GET", "http://127.0.0.1:54321/")
    with pytest.raises(Refused, match="HTTPS only"):
        _ctx(net, tmp_path=tmp_path).http.request("GET", "http://api.webby.example/")


def test_a_loopback_host_is_ignored_for_a_module_not_on_the_mcp_or_imap_engine(
        secrets_store, tmp_path):
    net = FakeNetwork({"127.0.0.1": ["127.0.0.1"]})
    ctx = _ctx(net, fields={"hosts": {"127.0.0.1": egress.LOOPBACK}}, tmp_path=tmp_path)
    with pytest.raises(Refused):
        ctx.http.request("GET", "http://127.0.0.1:7000/")


def test_a_typed_host_pinned_private_is_refused_once_it_resolves_public(tmp_path):
    """A home server's name taken over, or pointed at the internet, is not
    the server the user set up."""
    assert egress.vet("nas.home", 8443, kind=egress.PRIVATE,
                      resolve=lambda h, p: ["192.168.1.20"]) == ["192.168.1.20"]
    assert "now resolves to a public" in egress.vet("nas.home", 8443, kind=egress.PRIVATE,
                                                    resolve=lambda h, p: [PUBLIC_IP])
    assert egress.pin_kind("nas.home", 8443, resolve=lambda h, p: ["192.168.1.20"]) == "private"
    assert egress.pin_kind("x", 1, resolve=lambda h, p: ["192.168.1.20", PUBLIC_IP]) is None


def test_a_url_template_takes_fields_into_its_path_only_and_encodes_them():
    url = expand("https://dav.webby.example/{address}/events/",
                 {"address": "me@webby.example/../../admin?x=1"})
    assert url.startswith("https://dav.webby.example/me@webby.example%2F..%2F..%2Fadmin%3Fx")
    with pytest.raises(Exception, match="scheme, host and port"):
        expand("https://{host}/", {"host": "attacker.example"})


class FakeOutlook:
    class Session:
        class Stores:
            items = []

        def GetDefaultFolder(self, n):
            return FakeOutlook.Folder()

    class Folder:
        Name = "Inbox"

        def Delete(self):
            raise AssertionError("deleted")

    def __init__(self):
        self.Session = FakeOutlook.Session()


def test_com_opens_only_listed_progids_and_never_a_denied_one(tmp_path):
    manifest = _manifest(com=["Outlook.Application"],
                         com_members=["Session", "Session.GetDefaultFolder",
                                      "Session.GetDefaultFolder.*"])
    opened = []
    ctx = ModuleContext(
        manifest=manifest, module_key="k0001",
        account=Account(key="acct0001", name="Mine", module="webby"), state_dir=tmp_path,
        network=FakeNetwork(), dispatch=lambda p: opened.append(p) or FakeOutlook())
    root = ctx.com.open("outlook.application")
    assert root.Session.GetDefaultFolder(6).Name == "Inbox"
    with pytest.raises(Refused, match="never opened"):
        ctx.com.open("WScript.Shell")
    with pytest.raises(Refused, match="not among the programs"):
        ctx.com.open("Excel.Application")
    with pytest.raises(Refused, match="com_actions"):
        root.Session.GetDefaultFolder(6).Delete()
    with pytest.raises(Refused):
        root.Session.Stores  # noqa: B018 - reaching it is the test
    assert opened == ["Outlook.Application"]


class _Tree:
    """A folder with a folder under it, five deep, each with one message."""

    def __init__(self, depth: int = 0):
        self.Name = f"level {depth}"
        self.Folders = [_Tree(depth + 1)] if depth < 5 else []
        self.Message = SimpleNamespace(Subject=f"hello from {depth}", Session=self,
                                       CreateObject=lambda progid: "a shell")

    def GetRootFolder(self):
        return self


def test_a_listed_path_is_the_end_of_the_path_so_a_folder_walk_is_one_line(tmp_path):
    """Outlook's folders nest as deep as the user filed them. Listed from the
    root, each depth needed its own line; listed by its end, `Folders` once
    reaches them all, and every step on the way is still checked."""
    manifest = _manifest(com=["Outlook.Application"],
                         com_members=["GetRootFolder", "Folders", "Name",
                                      "Message", "Message.*"])
    ctx = ModuleContext(
        manifest=manifest, module_key="k0001",
        account=Account(key="acct0001", name="Mine", module="webby"), state_dir=tmp_path,
        network=FakeNetwork(), dispatch=lambda p: _Tree())
    top = ctx.com.open("Outlook.Application").GetRootFolder()

    deepest, names = top, []
    while True:
        names.append(deepest.Name)
        children = list(deepest.Folders)
        if not children:
            break
        deepest = children[0]
    assert names == [f"level {n}" for n in range(6)]
    assert deepest.Message.Subject == "hello from 5"

    # A wildcard reads a message's values, never an object: its session, the
    # whole profile, is refused, however deep the message was found.
    with pytest.raises(Refused):
        deepest.Message.Session.Name  # noqa: B018 - reaching it is the test
    with pytest.raises(Refused):
        top.Message.CreateObject("WScript.Shell")
    # Nor does it write one: a value set is listed as an action, unless
    # Steve knows it changes only a view.
    with pytest.raises(Refused):
        top.Message.Subject = "changed"
    assert top.Message.Subject == "hello from 0"
    assert ctx.com.refused == [
        "GetRootFolder.Folders[].Folders[].Folders[].Folders[].Folders[].Message.Session",
        "GetRootFolder.Message.CreateObject", "GetRootFolder.Message.Subject="]


def test_a_secret_is_read_only_by_its_declared_name_and_scrubbed_from_the_log(
        secrets_store, tmp_path, caplog):
    _store_secret("other", "pa55word")
    ctx = _ctx(FakeNetwork(), tmp_path=tmp_path)
    assert ctx.secret("other") == "pa55word"
    with pytest.raises(Refused):
        ctx.secret("OPENAI_API_KEY")
    with caplog.at_level("INFO"):
        ctx.log("login with pa55word failed")
    assert "pa55word" not in caplog.text and "[secret]" in caplog.text


def test_another_modules_key_reaches_none_of_this_modules_secrets(secrets_store, tmp_path):
    """Secrets are keyed by the key the install made, so a module that took
    another's id still reads under its own."""
    _store_secret("other", "pa55word", module_key="m0dkey0001")
    ctx = _ctx(FakeNetwork(), module_key="impostor01", tmp_path=tmp_path)
    assert ctx.secret("other") is None
