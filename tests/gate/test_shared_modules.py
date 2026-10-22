"""A module shared from one data folder to another (06-modules.md, section 6).

The exit criterion: a module exported from one data folder and imported
into another runs there with the second folder's keys, after a fresh
approval, and the zip holds none of the first folder's state or secrets.
Two data folders as one Windows user share the Credential Manager, as two
installs do, so the second must reach its own keys and never the first's.
Nothing is faked but the service and admission's models: the template is
installed in the first folder as the user's yes installs it, exported,
imported in the second through the strict extractor and admission, and
run there in its own container. The extractor's refusals are in
tests/unit/test_sharing.py.
"""

from __future__ import annotations

import asyncio
import io
import shutil
import zipfile

import pytest

from agent import accounts, connectors, modadmit, modshare
from core import addons, audit, db, lifecycle, modules, passphrase, paths, screen
from core.sandbox import probe, testrun
from steve_sdk import templates
from steve_sdk.templates.mail.fake import MailService
from tests.unit.test_templates import PUBLIC_IP, USER, TemplateNetwork

pytestmark = [pytest.mark.processes, pytest.mark.appcontainer]

ID = "example_mail"
WORDS = "correct horse battery"
FIRST_TOKEN = "token-of-the-first-folder"
SECOND_TOKEN = "token-of-the-second-folder"
FIRST_ADDRESS = "dana@first.example"
STATE = "state-of-the-first-folder"


async def _reviewed(source, module_id, said):
    return audit.Verdict(True, "it reads and writes mail at Example", model="fake")


ADMISSION = {"screener": lambda conn, artifact: screen.Verdict(verdict="as_is"),
             "reviewer": _reviewed,
             "tester": lambda module_id, folder: testrun.Ran(True, "1 passed")}


def _resolve(host, port):
    return [PUBLIC_IP]


def _store():
    paths.data_dir().mkdir(parents=True, exist_ok=True)
    conn = db.open_store(paths.db_path())
    modules.register(conn)
    return conn


def _first_folder(tmp_path) -> tuple[bytes, dict[str, str]]:
    """The template installed, given an account and some state, and
    exported: the zip, and what of the first folder it must not hold."""
    source = tmp_path / ID
    shutil.copytree(templates.folder("mail"), source,
                    ignore=shutil.ignore_patterns("__pycache__"))
    (source / "tests").mkdir()
    (source / "tests" / "test_mail.py").write_text(
        "def test_nothing():\n    pass\n", encoding="utf-8", newline="\n")
    conn = _store()
    try:
        admitted = asyncio.run(modadmit.admit(conn, source, origin="imported", **ADMISSION))
        # The template's own code reads the mail, so its card asks the passphrase.
        passphrase.choose(WORDS)
        lifecycle.approve(conn, admitted, given=WORDS)
        modules.set_enabled(conn, ID, True)
        account = accounts.add(conn, module_id=ID, address=FIRST_ADDRESS,
                               values={"token": FIRST_TOKEN}, resolve=_resolve)
        (paths.module_state_dir(ID) / "cursor.json").write_text(
            f'{{"cursor": "{STATE}"}}', encoding="utf-8", newline="\n")
        held = modules.module(conn, ID)
        name, data = modshare.export_zip(conn, ID)
        assert name == f"{ID}-{held.version}.zip"
        kept = {"the account's token": FIRST_TOKEN, "the module's state": STATE,
                "the account's address": FIRST_ADDRESS, "the account's key": account,
                "the module's key": held.key,
                "the approved hash": addons.tree_hash(paths.module_code_dir(ID, held.version))}
        return data, kept
    finally:
        conn.close()
        lifecycle.drop_container(ID)


def test_a_module_exported_from_one_folder_runs_in_another_on_that_folders_keys_after_its_yes(
        tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "_verdict", probe.Support(True, probe.LPAC, ""))
    data, first = _first_folder(tmp_path)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        unpacked = b"".join(archive.read(n) for n in archive.namelist())
    held_back = {what: value for what, value in first.items()
                 if what != "the approved hash"
                 and (value.encode() in data or value.encode() in unpacked)}
    assert held_back == {}, "the zip holds the first folder's own"

    monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path / "second"))
    conn = _store()
    try:
        admission = asyncio.run(modshare.import_zip(conn, data, **ADMISSION))
        # Nothing runs before the second user's own yes.
        assert modules.module(conn, ID) is None
        [card] = lifecycle.waiting(conn)
        assert (card["origin"], card["kind"]) == ("imported", "new")
        assert card["details"]["hash"] == first["the approved hash"]
        # The second folder's own passphrase: nothing of the first's comes with the zip.
        passphrase.choose(WORDS)
        lifecycle.approve(conn, admission, given=WORDS)
        modules.set_enabled(conn, ID, True)
        held = modules.module(conn, ID)
        assert held.key != first["the module's key"]
        assert addons.tree_hash(paths.module_code_dir(ID, held.version)) \
            == first["the approved hash"]
        accounts.add(conn, module_id=ID, address=USER, values={"token": SECOND_TOKEN},
                     resolve=_resolve)
        network = TemplateNetwork(MailService(USER, token=SECOND_TOKEN), ("api.example.com",))
        monkeypatch.setattr(connectors, "NETWORK", network)
        [account] = connectors.accounts(conn, "mail")
        assert type(account.provider).__name__ == "RemoteMailProvider"

        assert account.provider.test().startswith(f"Example answers for {USER}")
        sent = [request.headers.get("authorization", "") for request in network.seen]
        assert sent and all(SECOND_TOKEN in header for header in sent)
        assert not (paths.module_state_dir(ID) / "cursor.json").exists()
    finally:
        conn.close()
        lifecycle.drop_container(ID)
