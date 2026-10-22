"""COM through the broker: a module reaches a desktop program only as its manifest lists.

A program's automation reaches further than it looks. Outlook's Application
opens any other program by its ProgID, and a mail item's `Session` is the
whole profile. A module in the sandbox reaches it through the broker's
`com.call`, which keeps the program's objects, hands the module plain values
and handles, and checks every step against the manifest, as a built-in's
`ctx.com` is checked (`core/modctx.ModuleCom`). A member that sends, saves,
deletes or runs is reached only in `perform`, on the user's yes; anywhere
else the module's way to it is a card.

These tests drive the module's own end (`steve_sdk.link.SandboxCom`)
against a broker in this process, on fake COM objects;
test_module_broker_sandbox.py does it from a real container. The program
is `tests/fakes/com.py`'s Outlook. Frozen.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import threading

import pytest

from core.modctx import ModuleCom, Network, Refused
from core.sandbox import broker
from core.sandbox.broker import Broker, Local, grants_for
from steve_sdk.errors import ConnectorError, Transient
from steve_sdk.link import BAD_PARAMS, SandboxCom, line
from steve_sdk.manifest import ManifestError, parse
from tests.fakes import com as fake
from tests.fakes.com import WHEN

MAIL = "GetNamespace.GetDefaultFolder.Items[]"


def _manifest(**com):
    return parse({
        "manifest_version": 1, "sdk": 1, "id": "mailer", "label": "Mailer", "for": "Tests",
        "com": ["Outlook.Application"],
        "com_members": ["GetNamespace", "GetDefaultFolder", "Name", "Items", "Count",
                        "Restrict", "Items[].*", "Restrict[].*"],
        "com_actions": ["Items[].Send"],
        **com})


def _broker(outlook: fake.Outlook, *, performing: bool = False) -> Broker:
    granted = grants_for(_manifest())
    return Broker(module="mailer", grants=granted if performing else granted.reading(),
                  secret=lambda name: None, network=Network(), redact=str,
                  dispatch=outlook.dispatch)


def _inbox(com: SandboxCom):
    return com.open("Outlook.Application").GetNamespace("MAPI").GetDefaultFolder(6)


def _raw(held: Broker, **params):
    return json.loads(held.handle(line({"jsonrpc": "2.0", "id": 1, "method": "com.call",
                                        "params": params})))


@pytest.fixture
def outlook():
    return fake.Outlook()


@pytest.fixture
def held(outlook):
    made = _broker(outlook)
    yield made
    made.close()


def test_a_sandboxed_module_reads_a_program_through_handles_and_plain_values(outlook, held):
    """The module is handed what it reads, never the program's objects: a
    folder is a handle, a subject is text, and a time is a time."""
    inbox = _inbox(SandboxCom(Local(held)))

    assert inbox.Name == "Inbox"
    assert inbox.Items.Count == 3
    mails = list(inbox.Items)
    assert [m.Subject for m in mails] == ["note 0", "note 1", "note 2"]
    assert mails[1].ReceivedTime == WHEN + dt.timedelta(minutes=1)
    assert [m.Subject for m in inbox.Items.Restrict("2")] == ["note 2"]
    assert outlook.opened == ["Outlook.Application"]


def test_every_call_of_a_session_runs_on_one_thread_of_the_brokers_own(outlook, held):
    """A program's objects belong to the thread that made them; one called
    from another fails, or worse, works by chance."""
    list(_inbox(SandboxCom(Local(held))).Items)

    assert len(outlook.threads) == 1
    assert outlook.threads != {threading.get_ident()}


def test_a_program_or_member_the_manifest_does_not_list_is_refused_from_the_sandbox(
        outlook, held):
    """Each way past the list a module told to leak would try: a program
    that runs others, one it did not name, the Application's own way to
    open any program, the profile behind a mail, and changing a mail."""
    com = SandboxCom(Local(held))
    with pytest.raises(PermissionError, match="never opened"):
        com.open("WScript.Shell")
    with pytest.raises(PermissionError, match="not among the programs"):
        com.open("Excel.Application")
    root = com.open("Outlook.Application")
    with pytest.raises(PermissionError, match="never reached"):
        root.CreateObject("WScript.Shell")
    with pytest.raises(PermissionError, match="not among the members"):
        root.Session  # noqa: B018 - reaching it is the test
    mail = next(iter(_inbox(com).Items))
    with pytest.raises(PermissionError, match="wildcard does not reach"):
        mail.Session  # noqa: B018 - reaching it is the test
    with pytest.raises(PermissionError, match="not among the members"):
        mail.Subject = "changed"

    assert outlook.opened == ["Outlook.Application"] * 2
    assert held.com.refused == ["CreateObject", "Session", f"{MAIL}.Session",
                                f"{MAIL}.Subject="]


def test_a_member_that_sends_is_reached_only_in_perform_so_elsewhere_it_takes_a_card(
        outlook, held):
    """Outside `perform`, which runs on the user's yes, a module may look
    but not change: a mail is sent only after a card."""
    mail = next(iter(_inbox(SandboxCom(Local(held))).Items))
    with pytest.raises(PermissionError, match="only in `perform`"):
        mail.Send()
    assert outlook.sent == []

    performing = _broker(outlook, performing=True)
    try:
        next(iter(_inbox(SandboxCom(Local(performing))).Items)).Send()
    finally:
        performing.close()
    assert outlook.sent == ["note 0"]


def test_a_member_that_sends_is_reached_through_an_action_alone_whatever_else_lists_it():
    """Admission refuses a send listed as a read, but `ctx.com` does not
    lean on that: a list it is handed some other way, or a wildcard,
    still reaches a send only through `com_actions`."""
    com = ModuleCom(("Outlook.Application",), ("Items[].Send", "Items[].*"), acts=False)
    with pytest.raises(Refused, match="com_actions"):
        com.reach("Items[].Send")
    assert com.listed("Items[].Send") is None
    assert ModuleCom(("Outlook.Application",), (), actions=("Items[].Send",)).reach(
        "GetDefaultFolder.Items[].Send") == "action"


def test_a_member_that_sends_is_an_action_in_whatever_case_it_is_written(outlook):
    """COM finds a member by its name in any case (IDispatch's
    GetIDsOfNames), so `send` is Outlook's Send. Until the review of 15
    October a module that listed `Items[].send` as a read reached it outside
    `perform`, and the mail went with no card. Admission refuses that list
    now, and `ctx.com` and the broker refuse the send whatever list they
    are handed."""
    com = ModuleCom(("Outlook.Application",), ("Items[].send",), acts=False)
    with pytest.raises(Refused, match="com_actions"):
        com.reach("GetNamespace.GetDefaultFolder.Items[].send")

    listed = dataclasses.replace(_manifest(),
                                 com_members=(*_manifest().com_members, "Items[].send"))
    held = Broker(module="mailer", grants=grants_for(listed).reading(),
                  secret=lambda name: None, network=Network(), redact=str,
                  dispatch=outlook.dispatch)
    try:
        mail = next(iter(_inbox(SandboxCom(Local(held))).Items))
        with pytest.raises(PermissionError, match="com_actions"):
            mail.send()
    finally:
        held.close()
    assert outlook.sent == []


def test_a_handle_is_good_only_in_its_own_session_and_only_until_it_is_let_go(outlook, held):
    """A handle is a number: one session must not reach another's objects
    by guessing it, nor one let go."""
    inbox = _inbox(SandboxCom(Local(held)))
    number = object.__getattribute__(inbox, "_handle")
    assert _raw(held, op="get", handle=number, name="Name")["result"] == "Inbox"

    other = _broker(fake.Outlook())
    try:
        assert _raw(other, op="get", handle=number, name="Name")["error"]["code"] == BAD_PARAMS
    finally:
        other.close()
    assert _raw(held, op="release", handles=[number])["result"] is None
    assert _raw(held, op="get", handle=number, name="Name")["error"]["code"] == BAD_PARAMS


@pytest.mark.parametrize("params", [
    {"op": "run"},
    {"op": "get", "handle": 1, "name": "Name", "extra": 1},
    {"op": "get", "handle": True, "name": "Name"},
    {"op": "get", "handle": 1, "name": "_oleobj_"},
    {"op": "get", "handle": 1, "name": "Name.Value"},
    {"op": "call", "handle": 1, "args": [{"object": 1}]},
    {"op": "call", "handle": 1, "args": [{"handle": 1, "date": "2026-10-01"}]},
    {"op": "call", "handle": 1, "args": list(range(17))},
    {"op": "items", "handle": 1, "limit": 201},
    {"op": "items", "cursor": 1},
    {"op": "release", "handles": list(range(201))},
])
def test_a_com_call_out_of_shape_is_refused_before_it_reaches_the_program(outlook, held,
                                                                           params):
    """What a module writes is outside input, parsed here and never guessed
    at: an unknown step, a stray parameter, a handle that is not a number, a
    private or dotted name, an argument of no known kind, too many."""
    _raw(held, op="open", progid="Outlook.Application")

    assert _raw(held, **params)["error"]["code"] == BAD_PARAMS


def test_a_module_walking_a_large_folder_lets_go_as_it_walks_so_it_never_meets_the_cap(
        monkeypatch):
    """A module holds one handle per mail it is looking at. Were none let
    go, a walk of a large folder would end at the cap partway; every proxy
    the module drops is let go in the broker a batch at a time."""
    monkeypatch.setattr(broker, "MAX_HANDLES", 250)
    big = _broker(fake.Outlook(count=1000))
    try:
        com = SandboxCom(Local(big))
        assert len([mail.Subject for mail in _inbox(com).Items]) == 1000
        # The cap is real: a module that keeps every mail meets it.
        with pytest.raises(PermissionError, match="let some go"):
            list(_inbox(com).Items)
    finally:
        big.close()


def test_a_program_that_does_not_answer_ends_the_sessions_com_and_says_why(monkeypatch):
    """Outlook may hold a call behind a question on the desktop. The module
    is told so in time, and its next calls do not queue behind the stuck one."""
    monkeypatch.setattr(broker, "COM_TIMEOUT_S", 0.2)
    answered = threading.Event()
    named: list[str] = []

    class Asking(fake.Outlook):
        def GetNamespace(self, name):  # noqa: N802 - the COM name
            answered.wait(10)
            return super().GetNamespace(name)

        @property
        def Name(self):  # noqa: N802 - the COM name
            named.append("Name")
            return "Outlook"

    stuck = _broker(Asking())
    try:
        root = SandboxCom(Local(stuck)).open("Outlook.Application")
        with pytest.raises(Transient, match="question on the desktop"):
            root.GetNamespace("MAPI")
        with pytest.raises(Transient, match="did not answer"):
            root.Name  # noqa: B018 - reaching it is the test
    finally:
        answered.set()
        stuck.close()
    # The COM thread ends once all that was put on it is done: a step queued
    # behind the stuck one would have reached Outlook by then.
    for thread in threading.enumerate():
        if thread.name == "com-mailer":
            thread.join(10)
    assert named == [], "a step waited behind the stuck call"


def test_an_error_the_program_raises_reaches_the_module_as_a_connector_error(held):
    """A filter Outlook cannot parse is the module's mistake to read, not
    the broker's failure: it comes back in the SDK's words."""
    with pytest.raises(ConnectorError, match="the program refused: RuntimeError: Cannot parse"):
        _inbox(SandboxCom(Local(held))).Items.Restrict("")


class _Ledger:
    """A program Steve does not know: a title to read, and a document whose
    one method runs what it is given, as MMC20's ExecuteShellCommand did
    (step 11's audit, B1)."""

    def __init__(self) -> None:
        self.ran: list[str] = []
        self.Title = "Ledger"
        self.Document = self

    def ExportReport(self, command):  # noqa: N802 - the COM name
        self.ran.append(command)


def test_a_sandboxed_modules_call_in_a_program_steve_does_not_know_takes_a_card():
    """Its name said nothing of what it does: listed as a member, it ran from
    the sandbox as a read. Now every call there is an action, reached only
    in `perform`, on the user's yes; a plain value is still read."""
    ledger = _Ledger()
    manifest = _manifest(com=["Contoso.Ledger"], com_members=["Title", "Document",
                                                              "Document.ExportReport"],
                         com_actions=[])
    reading = Broker(module="mailer", grants=grants_for(manifest).reading(),
                     secret=lambda name: None, network=Network(), redact=str,
                     dispatch=lambda progid: ledger)
    try:
        root = SandboxCom(Local(reading)).open("Contoso.Ledger")
        assert root.Title == "Ledger"
        with pytest.raises(PermissionError, match="com_actions"):
            root.Document.ExportReport("calc.exe")
    finally:
        reading.close()
    assert ledger.ran == []

    listed = ModuleCom(("Contoso.Ledger",), ("Document",), actions=("Document.ExportReport",),
                       acts=False, dispatch=lambda progid: ledger)
    with pytest.raises(Refused, match="only in `perform`"):
        listed.open("Contoso.Ledger").Document.ExportReport("calc.exe")
    acting = ModuleCom(("Contoso.Ledger",), ("Document",), actions=("Document.ExportReport",),
                       dispatch=lambda progid: ledger)
    acting.open("Contoso.Ledger").Document.ExportReport("a report")
    assert ledger.ran == ["a report"]


def test_in_a_program_steve_knows_only_the_calls_it_knows_read_are_free():
    """Outlook's GetNamespace reads; a method Steve has no word for, or a
    value set that is not a view, is an action wherever the manifest
    lists it."""

    class Outlook(fake.Outlook):
        def AdvancedSearch(self, scope):  # noqa: N802 - the COM name
            raise AssertionError("searched with no card")

    outlook = Outlook()
    com = ModuleCom(("Outlook.Application",),
                    ("GetNamespace", "GetDefaultFolder", "AdvancedSearch", "Items",
                     "Items.IncludeRecurrences=", "Items.Name="), dispatch=outlook.dispatch)
    root = com.open("Outlook.Application")
    inbox = root.GetNamespace("MAPI").GetDefaultFolder(6)
    inbox.Items.IncludeRecurrences = True

    with pytest.raises(Refused, match="com_actions"):
        root.AdvancedSearch("Inbox")
    with pytest.raises(Refused, match="com_actions"):
        inbox.Items.Name = "renamed"
    assert com.refused == ["AdvancedSearch", "GetNamespace.GetDefaultFolder.Items.Name="]


def test_a_member_that_runs_code_is_never_reached_whatever_lists_it():
    """A card for the module's own action does not show what such a member
    would run, so not even `perform` reaches it."""
    com = ModuleCom(("Excel.Application",), ("Workbooks",),
                    actions=("Application.Run", "Document.ActiveView.ExecuteShellCommand"))
    for path in ("Application.Run", "Document.ActiveView.ExecuteShellCommand",
                 "Workbooks.DDEExecute"):
        with pytest.raises(Refused, match="never reached"):
            com.reach(path)


@pytest.mark.parametrize("com, says", [
    ({"com": ["WScript.Shell"]}, "never opened"),
    ({"com": ["Scripting.FileSystemObject"]}, "never opened"),
    # Windows' speech programs listen on the microphone (step 10's doors).
    ({"com": ["SAPI.SpSharedRecognizer"]}, "opens the microphone"),
    ({"com": ["SAPI.SpMMAudioIn"]}, "opens the microphone"),
    ({"com": ["Outlook.*"]}, "not a ProgID"),
    ({"com_members": ["Items[].Send"]}, "list it under com_actions"),
    ({"com_members": ["CreateItem"]}, "list it under com_actions"),
    ({"com_members": ["Items[].Delete="]}, "list it under com_actions"),
    # COM finds a member in any case, and these write a file wherever the
    # program may, past the sandbox (the review of 15 October).
    ({"com_members": ["Items[].send"]}, "list it under com_actions"),
    ({"com_members": ["Attachments[].SaveAsFile"]}, "list it under com_actions"),
    ({"com_members": ["Items[].saveas2"]}, "list it under com_actions"),
    ({"com_members": ["SaveCopyAs"]}, "list it under com_actions"),
    # Programs made to run things, and members that run code, which no
    # card shows (step 11's audit, B1): never, even as an action.
    ({"com": ["MMC20.Application"]}, "never opened"),
    ({"com": ["Schedule.Service"]}, "never opened"),
    ({"com": ["WbemScripting.SWbemLocator"]}, "never opened"),
    ({"com": ["InternetExplorer.Application"]}, "never opened"),
    ({"com": ["Microsoft.XMLHTTP"]}, "never opened"),
    ({"com_members": ["FollowHyperlink"]}, "never reached"),
    ({"com_members": ["Document.ActiveView.ExecuteShellCommand"]}, "never reached"),
    ({"com_actions": ["Workbooks.Application.Run"]}, "never reached"),
    ({"com_actions": ["ExecuteExcel4Macro"]}, "never reached"),
    ({"com_actions": ["ddeinitiate"]}, "never reached"),
    ({"com_actions": ["VBProject.VBComponents.Add"]}, "never reached"),
    # A value set changes what is there, unless Steve knows it is a view.
    ({"com_members": ["Items[].Subject="]}, "changes a value"),
    ({"com_members": ["Items..Name"]}, "not a member path"),
    ({"com_actions": ["Items[].*"]}, "named in full"),
    ({"com": [], "com_actions": ["Items[].Send"]}, "lists none"),
])
def test_admission_refuses_a_manifest_that_lists_a_sending_member_as_a_read_or_a_denied_program(
        com, says):
    """The deny-list and the actions are read from the manifest before a
    card is shown: a module that hides a send among its reads is refused
    there, not discovered at its first call."""
    with pytest.raises(ManifestError, match=says):
        _manifest(**com)
