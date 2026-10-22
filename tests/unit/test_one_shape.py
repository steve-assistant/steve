"""One shape for every connector: a manifest, and code only where no engine fits.

Before step 5 each provider was its own code: Gmail's client, Outlook's, a
password mailbox's, each with its own checks or none, and a new mail
provider meant a new client. Now a provider that speaks a protocol core
knows is a manifest on core's engine, and only a provider with a web API of
its own brings code. These hold that: which built-ins are only a manifest,
that a mail provider Steve does not ship joins as a manifest alone and
keeps the whole mail contract with no code, and that everything shipped
that describes a module parses with the reader core uses.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from agent import accounts
from core import modules, writes
from core.engines import imap as engine
from core.engines import mcp
from steve_sdk import manifest as sdk_manifest
from steve_sdk.testing import mail
from tests.fakes import mailrig
from tests.fakes.imap import STANDARD

MAYA = "maya@mail.example"
SHIPPED = Path(importlib.import_module("modules").__path__[0])
TEMPLATES = Path(importlib.import_module("steve_sdk.templates").__path__[0])

# A mail provider that does not ship, written as an outsider would write
# one for a server that speaks IMAP and SMTP: its servers, its domain, the
# one field it asks for. No code.
UNSHIPPED = """\
manifest_version = 1
sdk = 1
id = "mailbox_example"
version = "1"
label = "Mailbox Example"
for = "Mail from a mail.example address, with its password"
effort = "A minute"
tier = "backend"
domains = ["mail.example"]
classes = ["mail"]

[mail]
engine = "imap"
imap = "imap.mail.example:993"
smtp = "smtp.mail.example:465"
sent_copy = "check"

[setup]
steps = ["address", "fields", "classes", "test"]
revoke = "https://mail.example/security"
fields = [{ name = "password", type = "secret", label = "Password" }]
"""


def _code(folder: Path) -> list[str]:
    return sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*.py")
                  if "__pycache__" not in p.parts)


@pytest.fixture
def unshipped(tmp_path) -> sdk_manifest.Manifest:
    folder = tmp_path / "mailbox_example"
    folder.mkdir()
    (folder / "manifest.toml").write_text(UNSHIPPED, encoding="utf-8", newline="\n")
    return sdk_manifest.load(folder / "manifest.toml")


@pytest.fixture
def standard():
    rig = mailrig.rig(STANDARD)
    yield rig
    rig.close()


def test_four_of_the_six_connectors_are_a_manifest_with_no_code_and_the_rest_run_in_the_sandbox():
    """Only the two connectors that reach a provider's own API (Google's,
    and Outlook on this computer over COM) bring code; the rest run on
    core's engines. A connector that grew a file of code would be a
    provider core's checks no longer wrap. A built-in that is no connector
    runs in the sandbox, where its code holds nothing (`countdown`, a tool),
    or has no code to run (`skills`, a switch)."""
    built_ins = modules.read_builtins()
    connectors = {module_id for module_id, manifest in built_ins.items() if manifest.classes}
    with_code = {module_id for module_id in connectors if _code(SHIPPED / module_id)}

    assert len(connectors) == 6
    assert with_code == {"google_cloud", "outlook_classic"}
    assert {module_id: manifest.tier for module_id, manifest in built_ins.items()
            if module_id not in connectors} == {"countdown": "sandbox", "skills": "backend"}
    assert not _code(SHIPPED / "skills")


def test_a_mail_provider_that_does_not_ship_is_a_manifest_alone_picked_by_its_domain(
        unshipped):
    assert unshipped.specs["mail"].engine == "imap"
    found = accounts.pick(MAYA, {**modules.builtins(), unshipped.id: unshipped})
    assert found == {"module": "mailbox_example", "preset": None}


@pytest.mark.parametrize("check", mail.CHECKS, ids=lambda c: c.__name__)
def test_a_mail_provider_that_does_not_ship_keeps_the_mail_contract_with_no_code(
        unshipped, standard, tmp_path, monkeypatch, check):
    """The same contract every built-in keeps, through core's write path,
    on a provider Steve has never seen: what its manifest says is all core
    needs."""
    monkeypatch.setattr(engine, "SENT_WAIT_S", 0.3)
    provider = standard.provider(unshipped, address=MAYA, state_dir=tmp_path)
    check(mail.MailCase(provider, standard.world, writes, MAYA))


def test_every_built_in_template_and_profile_parses_with_the_reader_core_uses():
    """A file that describes a module and does not parse would be found by
    the user, at setup. Manifests are read by `steve_sdk/manifest.py`; an
    MCP server's profile by the MCP engine, which is what reads it."""
    manifests = sorted([*SHIPPED.glob("*/manifest.toml"), *TEMPLATES.glob("*/manifest.toml")])
    profiles = sorted(SHIPPED.glob("*/profiles/*.toml"))

    parsed = {p.parent.name: sdk_manifest.load(p).id for p in manifests}
    assert len(parsed) == 12, parsed  # eight built-ins, four templates
    assert profiles and all(mcp.load(p).classes for p in profiles)
