"""Accounts are rows the database keeps consistent, not names in code.

Before modules, "one Gmail and one Outlook" was the shape of the code: a
second Gmail could not exist, and two accounts with the same address would
both have answered for it, so a reply could go from either. Now an account
is a row, and the rules a check could skip are keys in the schema: one
account per address in each class, one name per account, and a module's
accounts gone with the module (05-connectors.md, sections 1 and 3).
"""

from __future__ import annotations

import pytest

from core import db, modules, paths
from steve_sdk import ids
from steve_sdk.manifest import ManifestError, parse


def _manifest(module_id, classes=("mail", "calendar")):
    data = {"manifest_version": 1, "sdk": 1, "id": module_id, "label": module_id.title(),
            "for": "Tests", "classes": list(classes),
            "setup": {"steps": ["address", "fields", "test"]}}
    if "mail" in classes:
        data["mail"] = {"engine": "imap", "imap": "imap.mail.example:993",
                        "smtp": "smtp.mail.example:465"}
    if "calendar" in classes:
        data["calendar"] = {"engine": "ics"}
    return parse(data)


@pytest.fixture
def conn(tmp_path):
    conn = db.open_store(tmp_path / "accounts.db")
    manifests = {"mailish": _manifest("mailish"), "feedish": _manifest("feedish", ("calendar",))}
    modules.register(conn, manifests)
    yield conn, manifests
    conn.close()


def test_an_address_is_held_by_one_account_in_each_class(conn):
    """Two accounts answering for one inbox would each send as it."""
    conn, manifests = conn
    modules.add_account(conn, module_id="mailish", name="Home", classes=["mail"],
                        addresses=["Me@Mail.example"], manifests=manifests)
    with pytest.raises(modules.AccountError, match="move it here instead"):
        modules.add_account(conn, module_id="mailish", name="Again", classes=["mail"],
                            addresses=["me@mail.example "], manifests=manifests)
    calendar = modules.add_account(conn, module_id="feedish", name="Feed",
                                   classes=["calendar"], addresses=["me@mail.example"],
                                   manifests=manifests)
    assert modules.holder(conn, "calendar", "ME@mail.example").key == calendar


def test_addresses_an_account_finds_are_held_to_it_in_each_class_or_refused_together(conn):
    """Classic Outlook merges every account in its profile, so each address
    it finds is that one account's. One already held elsewhere would show
    the same mail twice; refused, none of them is linked."""
    conn, manifests = conn
    work = modules.add_account(conn, module_id="mailish", name="Work",
                               classes=["mail", "calendar"], addresses=["me@work.example"],
                               manifests=manifests)
    assert modules.add_addresses(conn, work, ["Me@Work.example", "me@side.example"]) == [
        "me@side.example"]
    assert modules.holder(conn, "calendar", "me@side.example").key == work
    assert modules.account(conn, work).addresses == ("me@work.example", "me@side.example")

    home = modules.add_account(conn, module_id="mailish", name="Home", classes=["mail"],
                               addresses=["me@home.example"], manifests=manifests)
    with pytest.raises(modules.AccountError, match='connected as "Home"'):
        modules.add_addresses(conn, work, ["me@new.example", "me@home.example"])
    assert modules.holder(conn, "mail", "me@new.example") is None
    assert modules.holder(conn, "mail", "me@home.example").key == home


def test_with_no_account_at_all_a_row_of_no_account_is_still_read(conn):
    """SQLite answers `NULL IN ()` false, so with no live account the rows
    of no account -- the calendar's, the user's own -- went too. A stand-in
    account kept the list from being empty until step 5's item 10."""
    conn, _ = conn
    gone = ids.make("mail", "0123456789ab", "m1")
    read = [r[0] for r in conn.execute(
        "SELECT key FROM (SELECT ? AS key UNION ALL SELECT ?) AS o WHERE "
        + modules.live(conn, "observations", "o"), ("horizon:soon", gone))]
    assert modules.live_keys(conn) == frozenset()
    assert read == ["horizon:soon"]


def test_the_schema_holds_one_address_per_class_even_past_the_function(conn):
    conn, manifests = conn
    key = modules.add_account(conn, module_id="mailish", name="Home", classes=["mail"],
                              addresses=["me@mail.example"], manifests=manifests)
    with pytest.raises(Exception, match="UNIQUE"):
        conn.execute("INSERT INTO account_classes (account, class, address) "
                     "VALUES (?, 'mail', 'ME@MAIL.EXAMPLE')", (key,))


def test_an_account_name_is_taken_once_whatever_its_case(conn):
    conn, manifests = conn
    modules.add_account(conn, module_id="mailish", name="Work", classes=["mail"],
                        addresses=["a@mail.example"], manifests=manifests)
    with pytest.raises(modules.AccountError, match="already named"):
        modules.add_account(conn, module_id="mailish", name="work", classes=["mail"],
                            addresses=["b@mail.example"], manifests=manifests)


def test_an_account_takes_only_its_modules_classes_and_an_installed_module(conn):
    conn, manifests = conn
    with pytest.raises(modules.AccountError, match="does not do"):
        modules.add_account(conn, module_id="feedish", name="X", classes=["mail"],
                            addresses=["x@mail.example"], manifests=manifests)
    with pytest.raises(modules.AccountError, match="no module"):
        modules.add_account(conn, module_id="nothere", name="X", classes=["mail"],
                            addresses=[], manifests=manifests)


def test_switching_a_module_off_takes_its_accounts_out_of_service_and_keeps_them(conn):
    conn, manifests = conn
    key = modules.add_account(conn, module_id="mailish", name="Home", classes=["mail"],
                              addresses=["me@mail.example"], manifests=manifests)
    modules.set_enabled(conn, "mailish", False)
    assert modules.accounts(conn, klass="mail", manifests=manifests) == []
    assert modules.account(conn, key) is not None
    modules.set_enabled(conn, "mailish", True)
    assert [a.key for a in modules.accounts(conn, klass="mail", manifests=manifests)] == [key]


def test_a_module_whose_built_in_is_gone_serves_no_account(conn):
    conn, manifests = conn
    modules.add_account(conn, module_id="feedish", name="Feed", classes=["calendar"],
                        addresses=[], manifests=manifests)
    assert modules.accounts(conn, manifests={"mailish": manifests["mailish"]}) == []


def test_removing_a_module_removes_its_accounts_in_the_same_transaction(conn):
    conn, manifests = conn
    key = modules.add_account(conn, module_id="mailish", name="Home", classes=["mail"],
                              addresses=["me@mail.example"], manifests=manifests)
    assert modules.remove_module(conn, "mailish") == [key]
    assert modules.account(conn, key) is None
    assert conn.execute("SELECT COUNT(*) FROM account_classes").fetchone()[0] == 0


def test_registering_twice_keeps_each_modules_key(conn):
    """Secrets are stored under the key, so a new key on restart would lose
    every password."""
    conn, manifests = conn
    before = {m.id: m.key for m in modules.modules(conn, manifests)}
    modules.register(conn, manifests)
    assert {m.id: m.key for m in modules.modules(conn, manifests)} == before


def test_a_built_in_whose_id_is_not_its_folders_name_is_refused(tmp_path):
    folder = tmp_path / "gmailish"
    folder.mkdir()
    (folder / "manifest.toml").write_text(
        'manifest_version = 1\nsdk = 1\nid = "other"\nlabel = "X"\nfor = "Tests"\n'
        'classes = ["calendar"]\n[calendar]\nengine = "ics"\n', encoding="utf-8", newline="\n")
    with pytest.raises(ManifestError, match="folder"):
        modules.read_builtins(tmp_path)


def test_every_built_in_module_parses():
    for module_id, manifest in modules.read_builtins().items():
        assert manifest.id == module_id
        assert paths.module_dir(module_id).name == module_id


def test_removal_says_what_becomes_of_each_table_that_holds_an_accounts_rows():
    """Queued work is deleted, what Steve learned is detached (or forgotten),
    and the cards' history is kept. A table added to `TAGGED` with no word
    here would keep a removed account's rows, live to nothing."""
    said = [*modules.QUEUED, *modules.DETACHED, *modules.KEPT]
    assert sorted(said) == sorted(modules.TAGGED), "each tagged table, said once"
