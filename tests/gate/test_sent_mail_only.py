"""Only their sent mail reaches a model unredacted.

In the gate rather than in unit tests because it is a promise made to the
user about their data, not a property of the code's behaviour, and because
the thing it guards against is a one-line change that would look entirely
reasonable in review: a query without `in:sent`, or a body read from a
folder other than Sent.

Since step 5 every account is reached through the slot (`agent/connectors.py`),
and a mail account's `sent_items` is the one read bound to its sent folder.
So the rule has two halves: learning and style ask an account for nothing
else, and each mail provider's `sent_items` reads nothing else.
"""

from __future__ import annotations

import ast
import datetime as dt
from pathlib import Path

import pytest

from steve_sdk.classes import Message

# What reads mail text for a model: the night's learning (commitments and the
# style guide) and a draft's examples (`tools/style.py`).
READERS = [*sorted(Path("agent/night").glob("*.py")), Path("tools/style.py")]

# The mail tools beneath the slot. A reader that imported one could read any
# folder it liked.
BENEATH = {"google_gmail", "outlook", "google_providers", "outlook_providers",
           "engines", "imap"}


def _asked_of_accounts(tree: ast.AST, path: Path) -> list[str]:
    """Every method a reader calls on an account's provider: the calls
    inside the lambdas it hands `connectors.blocking` and `connectors.call`."""
    asked = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"blocking", "call"}
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "connectors"):
            continue
        given = node.args[1] if len(node.args) > 1 else None
        assert isinstance(given, ast.Lambda), (
            f"{path.as_posix()}:{node.lineno} hands an account something other "
            "than a lambda; this test cannot see what it asks")
        provider = given.args.args[0].arg
        asked += [call.func.attr for call in ast.walk(given.body)
                  if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                  and isinstance(call.func.value, ast.Name) and call.func.value.id == provider]
    return asked


def test_learning_and_style_ask_an_account_for_its_sent_items_only():
    """The user's decision, and the reason received mail can be ingested by
    code at all. A `fetch` or a `search` here would quietly widen the one
    path where a model sees unredacted personal content.

    Every module of the night, not one file: `agent/night.py` became a
    package, and reading all of it means the next move inside the package
    needs no edit to a frozen test -- the edit that is the easiest place to
    weaken one.

    Until 14 October 2026 this read the night's Gmail query and required
    `in:sent` in it; the query is now the Gmail provider's, held below.
    """
    asked: list[str] = []
    for path in READERS:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = {alias.name.split(".")[-1] for node in ast.walk(tree)
                    if isinstance(node, ast.Import | ast.ImportFrom) for alias in node.names}
        imported |= {node.module.split(".")[-1] for node in ast.walk(tree)
                     if isinstance(node, ast.ImportFrom) and node.module}
        assert not imported & BENEATH, (
            f"{path.as_posix()} imports {sorted(imported & BENEATH)}: mail is read "
            "through the slot's sent items, which are bound to the sent folder")
        asked += _asked_of_accounts(tree, path)

    assert asked, "the sent-mail read has moved; this test cannot see it"
    assert set(asked) == {"sent_items"}, (
        f"learning and style ask an account for {sorted(set(asked))}; only sent "
        "mail may reach a model unredacted")


def _gmail():
    """Gmail's provider with its two reads answered here: a search that
    returns a sent and a received message, whatever it was asked."""
    from modules.google_cloud import provider as google_providers
    from tests.fakes import google as fake_google

    class Gmail(google_providers.GmailMail):
        def __init__(self) -> None:
            super().__init__(fake_google.Context())
            self.queries: list[str] = []

        def search(self, query, *, limit, since=None):
            self.queries.append(query)
            return [Message("sent1"), Message("inbox1")]

        def fetch(self, native_id, *, body_chars=20_000):
            labels = ("SENT",) if native_id.startswith("sent") else ("INBOX",)
            return Message(native_id, labels=labels, to="jane@northwind.example", body="words")

    return Gmail()


SINCE = dt.datetime(2026, 9, 1, tzinfo=dt.UTC)


def test_gmail_sent_items_asks_for_the_sent_folder_and_keeps_only_what_is_in_it():
    """The query asks for `in:sent`, and what comes back is checked on its
    labels as well: the folder is the rule, the query only a filter."""
    gmail = _gmail()

    found = gmail.sent_items(since=SINCE, limit=5, to="jane@northwind.example")

    assert [m.native_id for m in found] == ["sent1"]
    assert gmail.queries == ["in:sent to:jane@northwind.example"]


@pytest.mark.parametrize("to", ["jane@northwind.example OR in:inbox", "(x@y.example)",
                                "in:inbox", 'a@b.example" OR "c'])
def test_gmail_sent_items_takes_one_bare_address_and_nothing_that_widens_the_query(to):
    gmail = _gmail()

    with pytest.raises(ValueError):
        gmail.sent_items(since=SINCE, limit=5, to=to)
    assert gmail.queries == []


def test_outlook_sent_items_reads_only_the_sent_folders_reader(monkeypatch):
    """`sent_with_bodies` is the Outlook read bound to Sent Items (the test
    below holds it to FOLDER_SENT); every other read fails here."""
    from modules.outlook_classic import outlook
    from modules.outlook_classic import provider as outlook_providers
    from tests.fakes import outlook as fake_outlook

    asked: list[tuple[int, int]] = []

    def sent_with_bodies(days, limit=20):
        asked.append((days, limit))
        return {"messages": [{"id": "a", "to": "Jane <jane@northwind.example>", "body": "x"},
                             {"id": "b", "to": "rina@contoso.example", "body": "y"}]}

    def elsewhere(*a, **k):
        raise AssertionError("read something other than Sent Items")

    monkeypatch.setattr(outlook, "sent_with_bodies", sent_with_bodies)
    for name in ("search", "read", "headers", "thread"):
        if hasattr(outlook, name):
            monkeypatch.setattr(outlook, name, elsewhere)

    found = outlook_providers.OutlookMail(fake_outlook.context(None)).sent_items(
        since=SINCE, limit=2, to="jane@northwind.example")

    assert [m.native_id for m in found] == ["a"] and len(asked) == 1


def test_a_password_account_reads_its_sent_folder_alone_where_the_server_ignores_the_search(
        tmp_path):
    """Every password account is core's one IMAP engine. Its Sent folders
    are the rule and the search only a filter: on a server that answers any
    search with the whole folder, nothing from the inbox comes back, and an
    address is checked on each message."""
    from core import writes
    from steve_sdk.testing import mail
    from tests.fakes import mailrig
    from tests.fakes.imap import IGNORES_FILTERS

    rig = mailrig.rig(IGNORES_FILTERS)
    try:
        provider = rig.provider(
            mailrig.module("imap"), preset="other", state_dir=tmp_path,
            fields={"imap": "imap.mail.example:993", "smtp": "smtp.mail.example:465",
                    "hosts": {"imap.mail.example": "public", "smtp.mail.example": "public"}})
        mail.sent_items_come_from_sent_alone_with_bodies(
            mail.MailCase(provider, rig.world, writes, "maya@mail.example"))
    finally:
        rig.close()


def test_an_mcp_account_reads_its_sent_folder_alone_where_the_server_ignores_the_filter(
        tmp_path):
    """An MCP account's Sent is the folder its tool is called on, and the
    time filter only a filter: on a server that answers with the whole
    folder whatever it is asked, nothing from the inbox comes back, and an
    address is checked on each message."""
    from core import writes
    from steve_sdk.testing import mail
    from tests.fakes import mcp as fake_mcp

    rig = fake_mcp.rig(tmp_path, ignores_filters=True)
    try:
        mail.sent_items_come_from_sent_alone_with_bodies(
            mail.MailCase(rig.tested("mail"), rig.world, writes, rig.world.user))
    finally:
        rig.close()


def test_every_mail_provider_is_held_here():
    """A new mail provider is a new `sent_items` to bind to the sent folder:
    add its test above, then its name here. Found by what subclasses the
    SDK's mail class, so one in a new file is found too."""
    import importlib

    from steve_sdk.classes import MailProvider

    places = ("tools", "modules", "core/engines")
    for path in sorted(p for place in places for p in Path(place).rglob("*.py")):
        if "MailProvider" in path.read_text(encoding="utf-8"):
            importlib.import_module(".".join(path.with_suffix("").parts))

    def below(cls):
        for sub in cls.__subclasses__():
            yield sub
            yield from below(sub)

    assert {c.__name__ for c in below(MailProvider)
            if c.__module__.startswith(("tools.", "modules.", "core.engines."))} == {
        "GmailMail", "OutlookMail", "ImapMail", "McpMail"}


def test_outlook_bodies_are_only_ever_read_from_sent_items():
    """The same invariant as the Gmail query, in the shape Outlook has.

    There is no query string here to inspect -- a mailbox read by MAPI is a
    folder id and a flag. So this reads the flag: every call that asks for
    message bodies must name the Sent folder. Received mail is headers only,
    by pure code, and that restraint is the entire reason ingesting it is
    acceptable at all.

    Worth checking mechanically rather than trusting, because widening it is
    one keyword argument and the result would be the user's incoming Contoso mail
    going to a model unredacted, with nothing anywhere reporting it.
    """
    path = Path("modules/outlook_classic/outlook.py")
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    asked_for_bodies = [
        call
        for call in ast.walk(tree)
        if isinstance(call, ast.Call)
        and any(
            kw.arg == "bodies" and getattr(kw.value, "value", False) is True
            for kw in call.keywords
        )
    ]
    assert asked_for_bodies, "the bodies flag has moved; this test cannot see it"

    for call in asked_for_bodies:
        named = [
            node.id for node in ast.walk(call) if isinstance(node, ast.Name)
        ] + [
            kw.value.id
            for kw in call.keywords
            if isinstance(kw.value, ast.Name)
        ]
        assert "FOLDER_SENT" in named, (
            "a body read that does not name FOLDER_SENT. Only sent mail may "
            "reach a model unredacted."
        )
        assert "FOLDER_INBOX" not in named


def test_reading_outlook_mail_needs_no_stored_credential():
    """The point of the MAPI route rather than Graph. If this module ever
    grows a credential or an HTTP client, the security argument that
    justified it has quietly stopped being true.

    Read by AST rather than by substring: the first version grepped the text
    and failed on the word "token" in the module's own docstring, which is
    the sentence explaining that it holds none. Prose about a hazard is not
    the hazard.

    Every `modules/outlook_classic/*.py`, not just the one: the calendar reader was
    split out of it (`modules/outlook_classic/calendar.py`), and a rule that read only
    the old file would have stopped covering half of what it once did.
    """
    # Nothing here should be able to reach the network or a secret store.
    forbidden = {"httpx", "httpx2", "requests", "urllib", "msal", "http"}

    paths = sorted(p for p in Path("modules/outlook_classic").glob("*.py")
                   if p.name != "__init__.py")
    assert Path("modules/outlook_classic/outlook.py") in paths, "the Outlook reader has moved"

    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])

        assert imported, f"no imports found; this test cannot see {path}"
        assert not (imported & forbidden), (
            f"{path.as_posix()} imports {sorted(imported & forbidden)}. It "
            "reads through the Outlook already signed in precisely so that "
            "this agent holds no credential and opens no connection of its own."
        )
        assert "core.secrets" not in {
            node.module for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }, path.as_posix()
