"""Every template passes its class's contract suite, on every run.

A template is what a new module starts from (`steve_sdk/templates`), for a
developer and for step 6's builder. One that no test ran would rot
quietly: an interface changed, a suite grew a check, and the next module
copied from it would start broken, or start without the write checks the
suite exists for. So each runs here as a module would: built from its
manifest, through core's own context (`core/modctx.py`, which fills its
token in and holds it to its hosts), and written through core's write path.
"""

from __future__ import annotations

import ast
import datetime as dt
import importlib
import json
import sys
from typing import Any

import httpx
import pytest

from core import credstore, modctx, writes
from steve_sdk import classes, templates
from steve_sdk.context import Account
from steve_sdk.errors import WrongPassword
from steve_sdk.manifest import load
from steve_sdk.testing import api, calendar, files, mail, tasks
from tests.gate.test_module_imports import REACHES_PAST

PUBLIC_IP = "93.184.216.34"
USER = "maya@mail.example"

SUITES: dict[str, tuple[Any, Any]] = {
    "mail": (mail.MailCase, mail.CHECKS),
    "calendar": (calendar.CalendarCase, calendar.CHECKS),
    "files": (files.FilesCase, files.CHECKS),
    "tasks": (tasks.TasksCase, tasks.CHECKS),
}


class TemplateNetwork(modctx.Network):
    """The manifest's hosts are the template's fake; any other is refused."""

    def __init__(self, service: api.Service, hosts: tuple[str, ...]) -> None:
        self.service = service
        self.hosts = hosts
        self.seen: list[httpx.Request] = []

    def resolve(self, host: str, port: int) -> list[str]:
        return [PUBLIC_IP]

    def own_ports(self) -> set[int]:
        return {54321}  # Steve's own, as a run file would name it

    def transport_for(self, host: str) -> httpx.BaseTransport | None:
        if host not in self.hosts:
            raise AssertionError(f"a request reached {host}, which the manifest does not name")
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        answer = self.service.answer(api.Request(
            method=request.method, path=request.url.path, query=dict(request.url.params),
            body=json.loads(request.content) if request.content else None,
            headers={k.lower(): v for k, v in request.headers.items()}))
        return httpx.Response(answer.status, content=answer.content,
                              headers={"content-type": "application/json"})


class Rig:
    def __init__(self, klass: str, tmp_path, *, token: str = api.TOKEN) -> None:
        where = templates.folder(klass)
        self.manifest = load(where / "manifest.toml")
        code = importlib.import_module(f"steve_sdk.templates.{klass}.provider")
        fake = importlib.import_module(f"steve_sdk.templates.{klass}.fake")
        self.service: api.Service = getattr(fake, f"{klass.title()}Service")(USER)
        self.network = TemplateNetwork(self.service, self.manifest.fixed_hosts())
        account = Account(key="acct0012", name="Example", module=self.manifest.id,
                          address=USER, fields={})
        credstore.write(credstore.target("m0dkey0012", "acct0012", "token"), token)
        self.ctx = modctx.ModuleContext(manifest=self.manifest, module_key="m0dkey0012",
                                        account=account, state_dir=tmp_path / "state",
                                        network=self.network, zone=lambda: dt.UTC)
        spec = self.manifest.specs[klass]
        self.provider = getattr(code, spec.settings["provider"])(self.ctx)

    def case(self, klass: str) -> Any:
        return SUITES[klass][0](self.provider, self.service, writes, USER)


@pytest.fixture
def make_rig(tmp_path):
    made: list[Rig] = []

    def make(klass: str, **kwargs: Any) -> Rig:
        made.append(Rig(klass, tmp_path, **kwargs))
        return made[-1]

    yield make
    for rig in made:
        rig.ctx.close()


@pytest.mark.parametrize(("klass", "check"), [
    (klass, check) for klass in templates.CLASSES for check in SUITES[klass][1]],
    ids=lambda v: v if isinstance(v, str) else v.__name__)
def test_each_template_passes_its_classes_contract_suite(make_rig, klass, check):
    rig = make_rig(klass)
    check(rig.case(klass))


def test_every_class_has_a_template_and_a_suite():
    """A class with no template is one step 6's builder starts from nothing."""
    assert set(templates.CLASSES) == set(classes.INTERFACES) == set(SUITES)
    for klass in templates.CLASSES:
        for name in ("manifest.toml", "provider.py", "fake.py"):
            assert (templates.folder(klass) / name).is_file(), (klass, name)


@pytest.mark.parametrize("klass", templates.CLASSES)
def test_each_template_is_a_module_of_its_class_reaching_only_its_own_host(make_rig, klass):
    rig = make_rig(klass)
    assert rig.manifest.classes == (klass,)
    assert isinstance(rig.provider, classes.INTERFACES[klass])
    assert rig.provider.capabilities <= classes.CAPABILITIES[klass]
    assert rig.provider.test()
    assert {r.url.host for r in rig.network.seen} == {"api.example.com"}


@pytest.mark.parametrize("klass", templates.CLASSES)
def test_the_token_is_filled_in_by_core_and_never_read_by_the_template(make_rig, klass):
    """A module that read its token could send it anywhere; core fills it
    into the one header, for the one host, that the manifest binds."""
    rig = make_rig(klass)
    rig.provider.test()
    assert {r.headers["authorization"] for r in rig.network.seen} == {f"Bearer {api.TOKEN}"}
    source = (templates.folder(klass) / "provider.py").read_text(encoding="utf-8")
    assert ".secret(" not in source

    wrong = make_rig(klass, token="not-the-token")
    with pytest.raises(WrongPassword):
        wrong.provider.test()


def test_a_template_imports_only_what_a_module_may():
    """A copy is a module, held to the module rule: the standard library's
    plain parts and `steve_sdk`, nothing that reaches past the context."""
    bad = []
    for path in sorted(templates.HERE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for node in ast.walk(tree):
            names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                     else [node.module or "."] if isinstance(node, ast.ImportFrom) else [])
            for name in names:
                top = name.split(".")[0]
                if top in REACHES_PAST or top not in (*sys.stdlib_module_names,
                                                      "steve_sdk", "__future__"):
                    bad.append(f"{path.name}:{node.lineno} imports {name}")
    assert not bad, bad
