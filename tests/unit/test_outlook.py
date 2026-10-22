"""Reading mail through the Outlook that is already signed in.

Every bug this file pins was found live, in the first hour, and they share a
shape worth naming: **MAPI fails by returning nothing.** Sort a folder by a
property it does not have and you get an empty collection, not an error. Ask
an Exchange recipient for its address and you get an X.500 string that is
perfectly valid and is not an email address. Read the To line and you get
display names that look like data.

None of those raise. All of them produce a night that quietly learns nothing
and a ledger that looks like an empty mailbox. So the tests here are mostly
about the difference between "no messages" and "asked wrongly".

Driven by a fake COM object rather than by Outlook: the real one is on this
machine, which is exactly why a test must not reach for it -- see
`tests/unit/conftest.py` for the two occasions that lesson was learned.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from core import modules
from modules.outlook_classic import outlook
from modules.outlook_classic.provider import OutlookCalendar, OutlookMail
from steve_sdk.errors import NotConnected
from tests.fakes import outlook as fake_outlook

NEW_YORK = ZoneInfo("America/New_York")


class _Recipient:
    def __init__(self, name: str, address: str, *, exchange: bool = False) -> None:
        self.Name = name
        # An Exchange recipient's `.Address` is a distinguished name. The SMTP
        # address lives behind GetExchangeUser, and nothing about the X.500
        # string announces that it is not an address.
        self.Address = (
            "/O=CONTOSO/OU=EXCHANGE/CN=RECIPIENTS/CN=" + name if exchange else address
        )
        self._smtp = address

    class _Entry:
        def __init__(self, smtp):
            self._smtp = smtp

        def GetExchangeUser(self):  # noqa: N802 - the COM name
            return type("U", (), {"PrimarySmtpAddress": self._smtp})()

    @property
    def AddressEntry(self):  # noqa: N802 - the COM name
        return self._Entry(self._smtp)


class _Item:
    def __init__(self, *, subject, when, to=None, sender=None, body="", cls=43,
                 unread=False):
        self.Subject = subject
        self.UnRead = unread
        self.SentOn = when
        self.ReceivedTime = when
        self.EntryID = f"entry-{subject}"
        self.ConversationID = f"conv-{subject}"
        self.Body = body
        self.Class = cls
        self.SenderName = (sender or ("", ""))[0]
        self.SenderEmailAddress = (sender or ("", ""))[1]
        self._to = to or []

    @property
    def Sender(self):  # noqa: N802 - the COM name
        if not self.SenderEmailAddress:
            return None
        return _Recipient(self.SenderName, self.SenderEmailAddress)

    @property
    def Recipients(self):  # noqa: N802 - the COM name
        items = self._to

        class _Collection:
            Count = len(items)

            def Item(self, i):  # noqa: N802 - the COM name
                return items[i - 1]

        return _Collection()


class _Items:
    """A folder's items, which only sort by the right property."""

    def __init__(self, items, *, sorts_by: str) -> None:
        self._all = items
        self._sorts_by = sorts_by
        self._view: list = []
        self._at = 0

    Count = property(lambda self: len(self._view))

    def Sort(self, key, descending):  # noqa: N802 - the COM name
        # The live failure, reproduced: Sent Items sorted by [ReceivedTime]
        # yields an empty collection rather than raising, and the first probe
        # reported "sent: 0" against forty-six real messages.
        self._view = (
            sorted(self._all, key=lambda i: i.SentOn, reverse=descending)
            if key == self._sorts_by
            else []
        )
        self._at = 0

    def GetFirst(self):  # noqa: N802 - the COM name
        self._at = 0
        return self.GetNext()

    def GetNext(self):  # noqa: N802 - the COM name
        if self._at >= len(self._view):
            return None
        self._at += 1
        return self._view[self._at - 1]


class _Folder:
    """A folder, which in a real mailbox almost always has children.

    Modelled here because the first version of the reader walked only the
    Inbox root, and the user files with server-side rules: 641 of their messages
    sat one level down and were reported as not existing.
    """

    def __init__(self, items, sorts_by, name="Inbox", children=()):
        self.Items = _Items(list(items), sorts_by=sorts_by)
        self.Name = name
        self.EntryID = f"folder-{name}"
        self._children = list(children)

    @property
    def Folders(self):  # noqa: N802 - the COM name
        kids = self._children

        class _Collection:
            Count = len(kids)

            def Item(self, i):  # noqa: N802 - the COM name
                return kids[i - 1]

        return _Collection()


def _namespace(sent=(), inbox=(), filed=None, stores=True):
    """`filed` is {folder name: items} one level under the Inbox."""
    subfolders = [
        _Folder(items, "[ReceivedTime]", name=name)
        for name, items in (filed or {}).items()
    ]

    def _default(fid):
        if fid == outlook.FOLDER_SENT:
            return _Folder(sent, "[SentOn]", name="Sent Items")
        return _Folder(inbox, "[ReceivedTime]", children=subfolders)

    class _Store:
        DisplayName = "maya@contoso.example"

        def GetDefaultFolder(self, fid):  # noqa: N802 - the COM name
            return _default(fid)

    class _Namespace:
        Accounts = type(
            "A", (), {
                "Count": 1,
                "Item": lambda self, i: type(
                    "Acc", (), {"SmtpAddress": "maya@contoso.example"}
                )(),
            },
        )()

        def GetDefaultFolder(self, fid):  # noqa: N802 - the COM name
            return _default(fid)

    ns = _Namespace()
    if stores:
        ns.Stores = type(
            "S", (), {"Count": 1, "Item": lambda self, i: _Store()},
        )()
    return ns


@pytest.fixture
def outlook_is():
    """Outlook answering with a test's namespace, reached as the account's,
    through its context (tests/fakes/outlook.py): a member the module
    reaches that its manifest does not list fails the test."""
    with contextlib.ExitStack() as stack:
        yield lambda namespace: stack.enter_context(fake_outlook.reached(namespace))


@pytest.fixture
def mailbox(outlook_is):
    """Install a fake MAPI namespace."""

    def install(sent=(), inbox=(), filed=None, stores=True):
        outlook_is(_namespace(sent, inbox, filed, stores))

    return install


NOW = datetime.now()


# --------------------------------------------------------------------------
# The silent-nothing failures
# --------------------------------------------------------------------------


def test_sent_items_are_sorted_by_the_property_they_actually_have(mailbox):
    """The first live bug. Sent Items has no ReceivedTime, sorting by it
    returns an empty collection rather than an error, and the probe reported
    zero messages against a folder holding forty-six."""
    mailbox(sent=[_Item(subject="Thank you!", when=NOW, to=[
        _Recipient("Mehta, Arjun", "arjun.mehta@contoso.example", exchange=True)
    ])])

    found = outlook.headers("sent", days=30)

    assert found["count"] == 1, "sorted by the wrong property and saw nothing"


def test_an_exchange_recipient_resolves_to_a_real_address(mailbox):
    """`.Address` on an Exchange recipient is an X.500 distinguished name.
    It is a valid string, it is not an email address, and storing it would
    make every Contoso colleague their own unsearchable row."""
    mailbox(sent=[_Item(subject="Hi", when=NOW, to=[
        _Recipient("Mehta, Arjun", "arjun.mehta@contoso.example", exchange=True)
    ])])

    to = outlook.headers("sent", days=30)["messages"][0]["to"]

    assert "arjun.mehta@contoso.example" in to
    assert "/O=" not in to, "an Exchange address went out in place of the SMTP one"


def test_the_display_name_travels_with_the_address(mailbox):
    """The second live run produced thirty-seven contacts and not one name,
    because only the address was carried. A ledger searchable only by email
    is most of the way to useless."""
    mailbox(sent=[_Item(subject="Hi", when=NOW, to=[
        _Recipient("Mehta, Arjun", "arjun.mehta@contoso.example", exchange=True)
    ])])

    from tools.people import parse_address

    email, name = parse_address(outlook.headers("sent", days=30)["messages"][0]["to"])

    assert email == "arjun.mehta@contoso.example"
    assert name == "Mehta, Arjun"


def test_an_external_address_without_a_directory_entry_still_works(mailbox):
    """Recruiters are not in Contoso's address book. `.Address` is already SMTP
    for them, and the resolution path must not insist on Exchange."""
    mailbox(sent=[_Item(subject="Hi", when=NOW, to=[
        _Recipient("june.harper@fabrikam.example", "june.harper@fabrikam.example")
    ])])

    assert "fabrikam.example" in outlook.headers("sent", days=30)["messages"][0]["to"]


# --------------------------------------------------------------------------
# What it reads, and what it refuses to
# --------------------------------------------------------------------------


def test_only_sent_mail_ever_comes_back_with_a_body(mailbox):
    """The invariant. Received mail is headers only, by pure code, and that
    restraint is why ingesting it at all is acceptable."""
    mailbox(
        sent=[_Item(subject="Hi", when=NOW, body="I'll send the deck Friday.")],
        inbox=[_Item(subject="Newsletter", when=NOW, body="secret things")],
    )

    assert "body" in outlook.sent_with_bodies(days=30)["messages"][0]
    assert "body" not in outlook.headers("received", days=30)["messages"][0]


def test_a_meeting_acceptance_is_not_offered_for_extraction(mailbox):
    """"Accepted: Case | Maya X Nadia" is a real sent item with no prose in
    it. Worth recording as an interaction, worth nothing to a model."""
    mailbox(sent=[
        _Item(subject="Accepted: Case", when=NOW, cls=53),
        _Item(subject="Thank you!", when=NOW, body="I'll follow up next week."),
    ])

    subjects = [m["subject"] for m in outlook.sent_with_bodies(days=30)["messages"]]

    assert subjects == ["Thank you!"]
    # Still a person the user dealt with, so the header sweep keeps it.
    assert len(outlook.headers("sent", days=30)["messages"]) == 2


def test_messages_older_than_the_window_are_left_alone(mailbox):
    mailbox(sent=[
        _Item(subject="recent", when=NOW),
        _Item(subject="ancient", when=NOW - timedelta(days=400)),
    ])

    assert [m["subject"] for m in outlook.headers("sent", days=30)["messages"]] == [
        "recent"
    ]


def test_the_limit_is_a_limit(mailbox):
    mailbox(sent=[_Item(subject=f"m{i}", when=NOW) for i in range(10)])

    assert outlook.headers("sent", days=30, limit=3)["count"] == 3


# --------------------------------------------------------------------------
# No Outlook is a normal machine
# --------------------------------------------------------------------------


@pytest.mark.parametrize("hresult", [-2147221005, -2147221164])
def test_no_classic_outlook_is_said_in_words_the_user_can_act_on(hresult):
    """A computer without classic Outlook, or with only the new one, which
    cannot be automated, answers "Invalid class string". Relayed as that,
    it told the user nothing they could do."""
    ctx = fake_outlook.context(None, dispatch=fake_outlook.failing(hresult))

    for call in (lambda: OutlookMail(ctx).test(), lambda: OutlookCalendar(ctx).test(),
                 lambda: OutlookMail(ctx).search("anything", limit=5)):
        with pytest.raises(NotConnected) as said:
            call()
        assert said.value.detail == outlook.NOT_INSTALLED
    assert "Outlook.com, Hotmail or Live" in outlook.NOT_INSTALLED


def test_outlooks_prompt_or_a_policy_refusing_steve_is_said_as_that(outlook_is):
    """Outlook asks the user before a program reads addresses, and a policy
    can deny it outright; either comes back as an exception inside
    "Exception occurred.", whose words name neither."""
    class _Guarded:
        @property
        def Accounts(self):  # noqa: N802 - the COM name
            raise fake_outlook.ComError(-2147352567, "Exception occurred.", (
                0, "Microsoft Outlook", "Operation aborted", None, 0, -2147467260), None)

    ctx = outlook_is(_Guarded())

    with pytest.raises(NotConnected) as said:
        OutlookMail(ctx).test()
    assert said.value.detail == outlook.REFUSED


def test_outlook_with_no_account_in_it_says_so(outlook_is):
    class _Empty:
        Accounts = type("A", (), {"Count": 0})()

    with pytest.raises(NotConnected, match="no account in it yet"):
        OutlookMail(outlook_is(_Empty())).test()


async def test_the_ingest_skips_a_machine_without_outlook(tmp_path, monkeypatch):
    """An account not on this computer is passed over in silence: a machine
    without classic Outlook is a normal machine, not a failed night."""
    from core import db
    from tools import ingest

    monkeypatch.setattr("core.modctx._dispatch", fake_outlook.failing(-2147221005))
    conn = db.open_store(tmp_path / "t.db")
    try:
        fake_outlook.add(conn)
        result = await ingest.mail(conn)
    finally:
        conn.close()

    assert result["errors"] == [] and result["accounts"] == {}


# --------------------------------------------------------------------------
# Reaching it from a conversation
# --------------------------------------------------------------------------


def test_an_outlook_id_is_tagged_so_reads_can_be_routed():
    """Two mailboxes, one `read_email`. Guessing the mailbox from the shape
    of an id would work until the day it did not, and the failure would be
    reading the wrong account's message."""
    tagged = outlook.tag("00000000ABCD")

    assert tagged.startswith(outlook.ID_PREFIX)
    assert outlook.untag(tagged) == "00000000ABCD"


def test_a_gmail_id_is_not_mistaken_for_an_outlook_one():
    assert outlook.untag("1a0c0f502e20ab99") is None
    assert outlook.untag("") is None


def test_search_matches_the_subject_and_the_correspondents(mailbox):
    mailbox(
        sent=[_Item(subject="Late Add/Drop Form", when=NOW, to=[
            _Recipient("Lowe, Petra", "petra.lowe@contoso.example", exchange=True)
        ])],
        inbox=[_Item(subject="Lunch?", when=NOW, sender=("Dan", "dan@fabrikam.example"))],
    )

    assert outlook.search("lowe")["count"] == 1
    assert outlook.search("add/drop")["count"] == 1
    assert outlook.search("fabrikam")["count"] == 1
    assert outlook.search("nonsense")["count"] == 0


def test_every_term_has_to_match(mailbox):
    """Otherwise "petra fabrikam" returns both, which reads as the search
    being broken rather than as there being no such message."""
    mailbox(
        sent=[_Item(subject="Form", when=NOW, to=[
            _Recipient("Lowe, Petra", "petra.lowe@contoso.example", exchange=True)
        ])],
        inbox=[_Item(subject="Lunch?", when=NOW, sender=("Dan", "dan@fabrikam.example"))],
    )

    assert outlook.search("petra fabrikam")["count"] == 0
    assert outlook.search("petra form")["count"] == 1


def test_search_covers_both_directions(mailbox):
    """"What did I say to the registrar" and "what did they say to me" are
    the same question to them."""
    mailbox(
        sent=[_Item(subject="registrar reply", when=NOW)],
        inbox=[_Item(subject="registrar notice", when=NOW)],
    )

    found = outlook.search("registrar")

    assert found["count"] == 2
    assert {m["direction"] for m in found["messages"]} == {"sent", "received"}


def test_search_results_say_which_mailbox_they_came_from(mailbox):
    mailbox(sent=[_Item(subject="hello", when=NOW)])

    assert outlook.search("hello")["messages"][0]["mailbox"] == "outlook"


def test_a_search_failure_is_a_result_not_a_traceback():
    """One unreachable mailbox must not take down a search across two."""
    def closed(progid):
        raise RuntimeError("Outlook is closed")

    with outlook.as_account(fake_outlook.context(None, dispatch=closed)):
        found = outlook.search("anything")

    assert found["error"]
    assert found.get("messages", []) == []


# --------------------------------------------------------------------------
# The three ways it was lying about the mailbox
# --------------------------------------------------------------------------


def test_mail_filed_into_a_subfolder_is_still_mail(mailbox):
    """The one the user noticed. The user files with server-side rules, so the Inbox
    root held 457 of their 1,098 messages and `GetDefaultFolder(6).Items`
    stops there -- LinkedIn, Canvas, digests and a rule-made `Junk/Auto`
    were reported as not existing rather than as filed.
    """
    mailbox(
        inbox=[_Item(subject="In the root", when=NOW)],
        filed={
            "Digests": [_Item(subject="Filed one down", when=NOW)],
            "Canvas": [_Item(subject="Filed elsewhere", when=NOW)],
        },
    )

    subjects = [m["subject"] for m in outlook.headers("received", days=2)["messages"]]

    assert "In the root" in subjects
    assert "Filed one down" in subjects
    assert "Filed elsewhere" in subjects


def test_outlooks_own_bookkeeping_is_not_mail(mailbox):
    """`Sync Issues` holds replication logs that are permanently unread. A
    reader that counted them would make "you have unread mail" permanently
    and uselessly true, which is the same as making it mean nothing.
    """
    mailbox(
        inbox=[_Item(subject="Real", when=NOW)],
        filed={"Sync Issues": [_Item(subject="Replication log", when=NOW, unread=True)]},
    )

    subjects = [m["subject"] for m in outlook.headers("received", days=2)["messages"]]

    assert subjects == ["Real"]


def test_a_message_says_whether_it_has_been_read(mailbox):
    """`_summarise` claims to return the same shape as the Gmail reader and
    did not: Gmail carries `unread`, Contoso silently did not. So a search
    across both could only ever answer "nothing unread" -- the Gmail half
    knew and the Contoso half had no opinion.
    """
    mailbox(inbox=[
        _Item(subject="Not yet read", when=NOW, unread=True),
        _Item(subject="Already read", when=NOW),
    ])

    by_subject = {
        m["subject"]: m for m in outlook.headers("received", days=2)["messages"]
    }

    assert by_subject["Not yet read"]["unread"] is True
    assert by_subject["Already read"]["unread"] is False


def test_is_unread_is_a_filter_and_not_a_word_to_look_for(mailbox):
    """`search_email` hands the same query string to both mailboxes, so an
    ordinary `is:unread` was matched as literal text against Contoso subject
    lines. That can never hit. The mailbox was not saying "none unread", it
    was saying "no subject contains the words is:unread", and from upstream
    those are indistinguishable.
    """
    mailbox(inbox=[
        _Item(subject="Waiting for you", when=NOW, unread=True),
        _Item(subject="Old news", when=NOW),
    ])

    found = outlook.search("is:unread")

    assert [m["subject"] for m in found["messages"]] == ["Waiting for you"]


def test_an_operator_it_cannot_honour_is_dropped_and_declared(mailbox):
    """Between a wrong answer and an empty one, take the wrong one -- as
    long as it says so. Keeping an unknown operator as a search term
    narrows to nothing and looks exactly like an empty mailbox.
    """
    mailbox(inbox=[_Item(subject="Quarterly review", when=NOW)])

    found = outlook.search("has:attachment quarterly")

    assert [m["subject"] for m in found["messages"]] == ["Quarterly review"]
    assert found["ignored_terms"] == ["has:attachment"]


def test_a_outlook_message_is_stamped_when_it_arrived_not_four_hours_early(mailbox, monkeypatch):
    """pywin32 labels Outlook's local wall-clock times UTC. Trusted, the
    label put LinkedIn's two new-device emails of 7 October 2026 at 09:07
    EDT -- four hours before the 13:07:51 sign-in by the agent that caused
    them -- and the alert about them reached the user as news.
    """
    monkeypatch.setattr(outlook, "_local_zone", lambda: NEW_YORK)
    signed_in = datetime(2026, 10, 7, 13, 7, 51, tzinfo=NEW_YORK)
    mailbox(inbox=[
        # What pywin32 hands back: 13:07 on Outlook's clock, labelled UTC.
        _Item(subject="New device", when=datetime(2026, 10, 7, 13, 7, 31, tzinfo=UTC)),
        _Item(subject="Verify it", when=datetime(2026, 10, 7, 13, 7, 50, tzinfo=UTC)),
    ])

    found = outlook.headers("received", days=3650)["messages"]
    stamps = sorted(m["internal_date"] for m in found)

    assert stamps == [1_791_392_851_000, 1_791_392_870_000]
    for stamp in stamps:
        assert 0 < signed_in.timestamp() * 1000 - stamp < 60_000, (
            "an email about the sign-in must come just after it, not hours before")


def test_a_naive_timestamp_is_still_read_as_local(mailbox, monkeypatch):
    """The other half. If Outlook hands back no offset at all, local is the
    honest reading too.
    """
    monkeypatch.setattr(outlook, "_local_zone", lambda: NEW_YORK)
    when = datetime(2026, 9, 21, 14, 5, 0)
    mailbox(inbox=[_Item(subject="No offset", when=when)])

    [message] = outlook.headers("received", days=3650)["messages"]

    assert message["internal_date"] == int(when.replace(tzinfo=NEW_YORK).timestamp() * 1000)


def test_every_store_is_read_not_just_the_default_one(mailbox):
    """`mapi.GetDefaultFolder` only ever answers for the default delivery
    store. One account today -- but the day a personal account joins this
    profile, the old call would go on quietly reading half the mail.
    """
    mailbox(inbox=[_Item(subject="Contoso", when=NOW)])

    assert outlook.headers("received", days=2)["count"] == 1


def test_a_profile_with_no_stores_collection_still_reads(mailbox):
    """Belt and braces: older MAPI profiles and some mocked namespaces have
    no `Stores`, and the fallback to `GetDefaultFolder` is what keeps a
    missing collection from emptying the mailbox.
    """
    mailbox(inbox=[_Item(subject="Still here", when=NOW)], stores=False)

    assert outlook.headers("received", days=2)["count"] == 1


# --------------------------------------------------------------------------
# Drafting a reply
# --------------------------------------------------------------------------


class _Reply:
    """What `MailItem.Reply()` hands back: addressed, threaded, quoting."""

    def __init__(self):
        self.HTMLBody = (
            "<html><head></head><body lang=EN-US><p>-----Original-----</p></body></html>"
        )
        self.EntryID = "draft-entry"
        self.To = "Rina Sato"
        self.Subject = "RE: DES 203"
        self.saved = False
        self.sent = False

    def Save(self):  # noqa: N802 - the COM name
        self.saved = True

    def Send(self):  # noqa: N802 - the COM name
        self.sent = True


class _Original:
    def __init__(self):
        self.reply = _Reply()
        self.Sent = True
        self.deleted = False

    def Reply(self):  # noqa: N802 - the COM name
        return self.reply

    def Delete(self):  # noqa: N802 - the COM name
        self.deleted = True


@pytest.fixture
def one_message(outlook_is):
    original = _Original()

    class _Namespace:
        def GetItemFromID(self, entry_id):  # noqa: N802 - the COM name
            if entry_id != "abc":
                raise RuntimeError("The operation failed.")
            return original

    outlook_is(_Namespace())
    return original


def test_a_outlook_reply_is_saved_as_a_draft_and_never_sent(one_message):
    """The user said yes to drafts and no to sending. Outlook's own Reply is
    what addresses and threads it, exactly as pressing Reply would."""
    result = outlook.draft_reply(outlook.tag("abc"), "Yes, I'm enrolled.\n\nBest,\nMaya")

    reply = one_message.reply
    assert reply.saved and not reply.sent
    assert result["drafted"] and result["draft_id"] == outlook.tag("draft-entry")
    assert result["where"] == "Outlook Drafts" and result["subject"] == "RE: DES 203"


def test_user_words_go_above_the_quote_inside_the_body(one_message):
    outlook.draft_reply(outlook.tag("abc"), "Yes <definitely>.\nSee you Friday.")

    body = one_message.reply.HTMLBody
    ours = body.index("Yes &lt;definitely&gt;.<br>See you Friday.")
    assert body.index("<body lang=EN-US>") < ours
    assert body.index("See you Friday") < body.index("-----Original-----")


class _NewMail(_Reply):
    """What `Application.CreateItem(0)` hands back: an empty email."""

    def __init__(self):
        super().__init__()
        self.HTMLBody = ""
        self.To = ""
        self.Subject = ""
        self.EntryID = "new-entry"
        self.SendUsingAccount = None


def test_a_new_outlook_email_is_saved_as_a_draft_and_never_sent(outlook_is):
    """The first half of a Contoso send: the whole email waits in their Drafts,
    where the user can read it before the user says yes to the card."""
    mail = _NewMail()
    on_outlook = type("Acc", (), {"SmtpAddress": "maya@contoso.example"})()

    class _Namespace:
        Application = type("App", (), {"CreateItem": lambda self, kind: mail})()
        Accounts = type("A", (), {"Count": 1, "Item": lambda self, i: on_outlook})()

    outlook_is(_Namespace())

    result = outlook.draft_new("rina.sato@contoso.example", "DES 203", "I'm in <Thursday>.")

    assert mail.saved and not mail.sent
    assert (mail.To, mail.Subject, mail.SendUsingAccount) == ("rina.sato@contoso.example",
                                                              "DES 203", on_outlook)
    assert "&lt;Thursday&gt;." in mail.HTMLBody
    assert result["draft_id"] == outlook.tag("new-entry") and result["where"] == "Outlook Drafts"


def test_sending_a_outlook_draft_sends_it_once_and_never_a_sent_message(outlook_is):
    draft = _Reply()
    draft.Sent = False
    items = {"draft": draft, "sent": _Original()}

    class _Namespace:
        def GetItemFromID(self, entry_id):  # noqa: N802 - the COM name
            return items[entry_id]

    outlook_is(_Namespace())

    assert outlook.send_draft(outlook.tag("draft"), unchanged=lambda now: True)["sent"]
    assert draft.sent
    assert "already sent" in outlook.send_draft(outlook.tag("sent"),
                                                unchanged=lambda now: True)["error"]


def test_a_shortened_id_says_what_to_do_about_it(one_message):
    result = outlook.draft_reply(outlook.tag("ab"), "text")

    assert "exactly as given" in result["error"]


def test_a_sent_message_is_never_deleted_as_if_it_were_a_draft(one_message):
    """`delete_draft` is the undo of a draft. Pointed at a sent item it
    would be destroying their record, not undoing ours."""
    result = outlook.delete_draft(outlook.tag("abc"))

    assert "error" in result and not one_message.deleted


async def test_a_reply_goes_to_the_mailbox_the_message_came_to(monkeypatch):
    """Every draft used to go to Gmail, which cannot find a Contoso message --
    so every draft reply to Contoso mail failed. Now the id names its account."""
    from agent.toolset import mail
    from steve_sdk.classes import Message
    from tests import persona
    from tests.gate.mailfake import Mailbox, connect

    boxes = {"gmail": Mailbox(persona.EMAIL, Message(native_id="18c2f")),
             "outlook": Mailbox(persona.WORK_EMAIL, Message(native_id="abc"))}
    accounts = connect(monkeypatch, **boxes)

    for reply_to in (accounts["outlook"].id("abc"), accounts["gmail"].id("18c2f"), None):
        await mail._do_draft_email(to="x@y.example", subject="s", body="b",
                                   reply_to_message_id=reply_to)

    assert [d.reply_to for d in boxes["outlook"].drafted] == ["abc"]
    assert [d.reply_to for d in boxes["gmail"].drafted] == ["18c2f", ""]


class _Addressee:
    def __init__(self, address):
        self.Address = address


class _ToLine:
    def __init__(self, addresses):
        self.items = [_Addressee(a) for a in addresses]

    @property
    def Count(self):  # noqa: N802 - the COM name
        return len(self.items)

    def Item(self, i):  # noqa: N802 - the COM name
        return self.items[i - 1]

    def Remove(self, i):  # noqa: N802 - the COM name
        del self.items[i - 1]


def test_a_follow_up_on_their_own_message_goes_to_the_people_the_user_wrote_to(outlook_is):
    """"Just checking in on the below" replies to their own unanswered email,
    and Outlook's Reply on that is addressed to them."""
    reply_all = _Reply()
    reply_all.Recipients = _ToLine(["jane@northwind.example", "Maya@contoso.example"])

    class _Sent(_Original):
        SenderEmailAddress = "maya@contoso.example"
        SenderName = "Lindqvist, Maya"
        Sender = None

        def Reply(self):  # noqa: N802 - the COM name
            raise AssertionError("replied to herself")

        def ReplyAll(self):  # noqa: N802 - the COM name
            return reply_all

    sent = _Sent()

    class _Namespace:
        Accounts = type("A", (), {
            "Count": 1,
            "Item": lambda self, i: type("Acc", (), {"SmtpAddress": "maya@contoso.example"})(),
        })()

        def GetItemFromID(self, entry_id):  # noqa: N802 - the COM name
            return sent

    outlook_is(_Namespace())

    result = outlook.draft_reply(outlook.tag("abc"), "Just checking in on the below.")

    assert result["drafted"] and reply_all.saved
    assert [r.Address for r in reply_all.Recipients.items] == ["jane@northwind.example"]


# ---------------------------------------------------------------------------
# Envelopes kept between searches (27 September 2026: six searches in one
# question, 18 s each, and the user waited nearly two minutes)
# ---------------------------------------------------------------------------


class _Counted(_Folder):
    """A folder that can say how many of its messages are unread, which is
    what lets a walk be reused."""

    @property
    def UnReadItemCount(self):  # noqa: N802 - the COM name
        return sum(1 for i in self.Items._all if i.UnRead)


@pytest.fixture
def kept(outlook_is, monkeypatch):
    """One inbox that stays the same object between searches, and a count of
    every envelope read out of it."""
    inbox = _Counted(
        [_Item(subject=f"note {n}", when=NOW - timedelta(hours=n),
               sender=("Dan", "dan@fabrikam.example")) for n in range(1, 6)],
        "[ReceivedTime]",
    )
    sent = _Counted([_Item(subject="re: note", when=NOW, body="See you then.",
                           to=[_Recipient("Dan", "dan@fabrikam.example")])],
                    "[SentOn]", name="Sent Items")

    class _Namespace:
        def GetDefaultFolder(self, fid):  # noqa: N802 - the COM name
            return sent if fid == outlook.FOLDER_SENT else inbox

    reads = {"n": 0}
    real = outlook._summarise

    def counted(*args, **kwargs):
        reads["n"] += 1
        return real(*args, **kwargs)

    outlook_is(_Namespace())
    monkeypatch.setattr(outlook, "_summarise", counted)
    outlook._envelopes.clear()
    yield {"inbox": inbox, "reads": reads}
    outlook._envelopes.clear()


def test_searching_six_times_reads_the_mailbox_once(kept):
    for query in ("fabrikam", "note", "dan", "seattle", "coffee", "note 3"):
        outlook.search(query)

    assert kept["reads"]["n"] == 6  # five received, one sent


def test_what_a_search_is_given_is_its_own(kept):
    """`search` tags every id it returns. Handed the kept summaries rather
    than copies, each search would tag them once more."""
    for _ in range(3):
        again = outlook.search("note")

    assert all(m["id"].count(outlook.ID_PREFIX) == 1 for m in again["messages"])


def test_new_mail_is_read_on_its_own(kept):
    outlook.search("note")
    kept["inbox"].Items._all.append(
        _Item(subject="Fabrikam Seattle coffee chat", when=NOW, sender=("Lena",
                                                                        "lena@fabrikam.example")))
    before = kept["reads"]["n"]

    found = outlook.search("seattle")

    assert found["count"] == 1
    assert kept["reads"]["n"] - before == 1


def test_new_mail_does_not_hide_a_message_read_elsewhere(kept):
    """A new message arrives while an old one is read on their phone: the
    count grew by one, but the unread count did not grow by what arrived."""
    kept["inbox"].Items._all[2].UnRead = True
    outlook.search("note")
    kept["inbox"].Items._all[2].UnRead = False
    kept["inbox"].Items._all.append(
        _Item(subject="note 0", when=NOW, sender=("Dan", "dan@fabrikam.example"), unread=True))

    unread = outlook.search("is:unread")

    assert [m["subject"] for m in unread["messages"]] == ["note 0"]


def test_a_deletion_and_two_arrivals_are_not_one_arrival(kept):
    """The count grew by one, but the message under the newest is not the
    one that was newest before: more changed than new mail at the top."""
    outlook.search("note")
    inbox = kept["inbox"].Items._all
    inbox.pop(3)
    inbox += [_Item(subject="note fresh a", when=NOW - timedelta(minutes=2)),
              _Item(subject="note fresh b", when=NOW - timedelta(minutes=1))]

    subjects = {m["subject"] for m in outlook.search("note")["messages"]}

    assert {"note fresh a", "note fresh b"} <= subjects and "note 4" not in subjects


def test_a_message_read_elsewhere_is_seen(kept):
    """Same count, same newest message, one fewer unread: something below
    the top changed, and only walking again can say what."""
    kept["inbox"].Items._all[2].UnRead = True
    outlook.search("note")
    kept["inbox"].Items._all[2].UnRead = False

    assert outlook.search("is:unread")["count"] == 0


def test_nothing_is_kept_past_its_time(kept, monkeypatch):
    outlook.search("note")
    monkeypatch.setattr(outlook, "ENVELOPE_TTL_S", 0)

    outlook.search("note")

    assert kept["reads"]["n"] == 12


def test_bodies_are_never_kept(kept):
    """Only envelopes are held in memory; a read that needs bodies goes to
    Outlook every time."""
    for _ in range(2):
        assert outlook.sent_with_bodies(days=30)["messages"][0]["body"] == "See you then."

    assert kept["reads"]["n"] == 2
    assert outlook._envelopes == {}


# ---------------------------------------------------------------------------
# Only once the user adds it
# ---------------------------------------------------------------------------


def test_outlook_is_never_started_until_the_user_adds_the_account(monkeypatch, tmp_path):
    """Asking COM whether Outlook holds an account starts it, and a fresh
    install once asked at its first turn. A switch stood in front of that
    until step 5's item 10; now the only way in is an added account's
    context, and with no account, nothing opens COM."""
    from agent import connectors
    from core import db

    started = []
    monkeypatch.setattr("core.modctx._dispatch", lambda progid: started.append(progid)
                        or fake_outlook.Application(_namespace()))
    conn = db.open_store(tmp_path / "t.db")
    try:
        modules.register(conn)
        assert connectors.accounts(conn, "mail") == []
        assert connectors.addresses(conn) == frozenset()
        with pytest.raises(NotConnected):
            outlook.accounts()
        assert started == []

        fake_outlook.add(conn)
        assert started == [], "adding the account is not yet asking Outlook"
        said = fake_outlook.provider(conn, "mail").test()
    finally:
        conn.close()

    assert said == f"Outlook answers for {fake_outlook.ADDRESS}"
    assert started == ["Outlook.Application"]
