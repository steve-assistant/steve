"""A connector from outside, end to end (06-modules.md, item 10).

The exit criterion: a mail module copied from the template runs in the
sandbox and passes the mail contract suite there, through core's write
path. So nothing here is faked but the service: the template is admitted
as a module from outside, approved, turned on and given an account as the
app does it; its provider is the slot's own (`connectors.accounts`), whose
every method starts the module in its container; and the suite's checks
run on that provider with `core.writes`, against the template's fake API
behind the broker. A runtime that is not built fails it.
"""

from __future__ import annotations

import asyncio
import shutil

import pytest

from agent import accounts, connectors, modadmit, modconnect, screening
from core import audit, db, lifecycle, modules, passphrase, paths, screen, writes
from core.sandbox import probe, testrun
from steve_sdk import templates
from steve_sdk.classes import Draft
from steve_sdk.errors import ConnectorError
from steve_sdk.templates.mail.fake import MailService
from steve_sdk.testing import api, mail
from tests.unit.test_templates import PUBLIC_IP, USER, TemplateNetwork

pytestmark = [pytest.mark.processes, pytest.mark.appcontainer]

ID = "example_mail"
WORDS = "correct horse battery"


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.setattr(probe, "_verdict", probe.Support(True, probe.LPAC, ""))
    conn = db.open_store(paths.db_path())
    modules.register(conn)
    yield conn
    conn.close()
    lifecycle.drop_container(ID)


def _installed(conn, tmp_path) -> None:
    """The template, admitted from outside as the user's yes installs it."""
    source = tmp_path / ID
    shutil.copytree(templates.folder("mail"), source,
                    ignore=shutil.ignore_patterns("__pycache__"))
    (source / "tests").mkdir()
    (source / "tests" / "test_mail.py").write_text(
        "def test_nothing():\n    pass\n", encoding="utf-8", newline="\n")

    async def reviewed(source, module_id, said):
        return audit.Verdict(True, "it reads and writes mail at Example", model="fake")

    admitted = asyncio.run(modadmit.admit(
        conn, source, origin="imported",
        screener=lambda conn, artifact: screen.Verdict(verdict="as_is"),
        reviewer=reviewed, tester=lambda module_id, folder: testrun.Ran(True, "1 passed")))
    # The template's own code reads the mail, so its card asks the passphrase.
    passphrase.choose(WORDS)
    lifecycle.approve(conn, admitted, given=WORDS)
    modules.set_enabled(conn, ID, True)
    accounts.add(conn, module_id=ID, address=USER, values={"token": api.TOKEN},
                 resolve=lambda host, port: [PUBLIC_IP])


def test_a_mail_module_copied_from_the_template_keeps_the_contract_in_its_sandbox(
        conn, tmp_path, monkeypatch):
    _installed(conn, tmp_path)
    broken: list[str] = []
    for check in mail.CHECKS:
        service = MailService(USER)
        monkeypatch.setattr(connectors, "NETWORK", TemplateNetwork(service, ("api.example.com",)))
        [account] = connectors.accounts(conn, "mail")
        assert type(account.provider).__name__ == "RemoteMailProvider"
        try:
            check(mail.MailCase(account.provider, service, writes, USER))
        except AssertionError as exc:
            broken.append(f"{check.__name__}: {exc}")
    assert broken == []
    assert modules.module(conn, ID).enabled, "nothing it did counted against it"


def test_a_read_cannot_change_anything_at_the_service_even_with_its_writes_declared(
        conn, tmp_path, monkeypatch):
    """The template declares POST, PATCH and DELETE for its writes; a
    search runs with GET alone, so one that tried to write is refused in
    the broker before it leaves."""
    _installed(conn, tmp_path)
    network = TemplateNetwork(MailService(USER), ("api.example.com",))
    monkeypatch.setattr(connectors, "NETWORK", network)
    [account] = connectors.accounts(conn, "mail")
    monkeypatch.setitem(modconnect.WRITES, "mail", frozenset())
    # What a module's call says when it fails is screened before anything
    # reads it, and the screen does not run here: this reads the refusal.
    monkeypatch.setattr(screening, "shown_here", lambda conn, source, source_id, origin, text:
                        screen.Shown(text, "text"))

    with pytest.raises(ConnectorError, match="POST to api.example.com: the manifest declares GET"):
        account.provider.make_draft(Draft(to="ada@else.example", subject="Plot",
                                               body="Water rota?"))
    assert [r.method for r in network.seen] == []
