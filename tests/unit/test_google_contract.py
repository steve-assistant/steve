"""Google Cloud's providers through their classes' contract suites.

Every other provider runs `steve_sdk/testing`'s suites: a search whose
server ignores its filter, a delete aimed at one of two drafts alike, a
draft changed after its card, an event changed on the phone between the
read and the write. Google's four ran only their own tests, against a
fake that answers each test's calls but holds nothing a suite can seed
and look inside, so the cases that went wrong before were never put to
them. A Gmail draft, for one, came back with no Message-ID: the write
path had nothing but its id to check before sending it or throwing it
away.

Here the golden world's stateful Gmail, Calendar, Drive and Tasks
(`golden_world.py`) are each a suite's `World`, and every check runs
through `core.writes`, the one write path, on the clock the golden world
pins.

Gmail's suite runs twice: on a Gmail that applies every filter, and on
one that ignores them. On the first alone, a provider that kept whatever
the server's search returned passed the very check that names a server
ignoring its filter (the review of steps 5 and 6). Run on the second,
Gmail's provider is stopped by exactly that in four checks: its search
and its new mail keep what the server answered, unchecked. (Its thread is
asked for by id, no filter, and holds.) Each is listed
in `TRUSTS_THE_SERVER` and must still fail there, so a gap closed fails
here until it leaves the list. Closing them checks Gmail's own syntax on
our side, which may drop what Gmail rightly found, and reads each listed
message's labels before a poll asks what it has seen, up to fifty reads
more every few minutes: that waits for a decision. Its sent mail costs
nothing more to check, since each message is read whole anyway, and is
checked as the IMAP engine checks its own: the folder, the date and the
recipient.
"""

from __future__ import annotations

import copy
import itertools

import pytest

from core import writes
from modules.google_cloud import common as google_common
from modules.google_cloud import tasks as google_tasks
from modules.google_cloud.provider import GmailMail, GoogleCalendar, GoogleDrive, GoogleTasks
from steve_sdk import exact
from steve_sdk.classes import Draft
from steve_sdk.errors import TargetChanged
from steve_sdk.testing import World, calendar, files, mail, tasks
from tests.fakes import google as fake_google
from tests.unit import golden_world as golden

DOC = "application/vnd.google-apps.document"


class GoogleWorld(World):
    """The golden world's Google half, seeded and looked at as a suite asks.

    It is the fakes' own `world` too: they record what they wrote and what
    they could not answer on it."""

    def __init__(self) -> None:
        self.unhandled: list[str] = []
        self.wrote: list[dict] = []
        self.gmail, self.calendar = golden.Gmail(self), golden.Calendar(self)
        self.drive, self.tasks = golden.Drive(self), golden.Tasks(self)
        self.google = golden.Google(self)
        self._ids = itertools.count(1)

    def seed(self, klass, items):
        return [getattr(self, f"_seed_{klass}")(item) for item in items]

    def _seed_mail(self, item: dict) -> str:
        role = item.get("role", "inbox")
        labels = {"SENT"} if role == "sent" else {"INBOX"} | ({"UNREAD"} if item.get("unread")
                                                              else set())
        thread = f"t{item['thrid']}" if "thrid" in item else None
        message_id = item.get("message_id") or f"<seed-{next(self._ids)}@else.example>"
        made = self.gmail.put(sender=item.get("sender", "Cy <cy@else.example>"),
                              to=item.get("to", fake_google.ADDRESS),
                              subject=item.get("subject", ""),
                              body=item.get("body", "A few lines."),
                              when=item.get("date") or golden.MOMENT, labels=labels,
                              thread=thread, message_id=message_id)
        made.in_reply_to = (item.get("headers") or {}).get("In-Reply-To", "")
        return made.id

    def _seed_calendar(self, item: dict) -> str:
        minutes = int((item["end"] - item["start"]).total_seconds() // 60)
        extra: dict = {}
        if item.get("rrule"):
            extra["recurrence"] = [f"RRULE:{item['rrule']}"]
        if item.get("organizer"):
            extra["organizer"] = {"email": item["organizer"],
                                  "self": item["organizer"] == fake_google.ADDRESS}
        guests = tuple(g for g in item.get("guests", ()) if g != fake_google.ADDRESS)
        return self.calendar.put(item["title"], item["start"], minutes, guests=guests,
                                 **extra)["id"]

    def _seed_files(self, item: dict) -> str:
        return self.drive.put(item["name"], DOC, fake_google.ADDRESS,
                              item.get("modified") or golden.MOMENT, item["text"])["id"]

    def _list(self, title: str) -> str:
        for found in self.tasks.lists:
            if found["title"] == title:
                return found["id"]
        return self.tasks.put_list(title)

    def _seed_tasks(self, item: dict) -> str:
        where = self._list(item.get("list", google_tasks.LIST_TITLE))
        return self.tasks.put(where, item["title"], due=item.get("due"),
                              done=item.get("done", False))["id"]

    def mutate(self, klass, native_id, **changes):
        if klass == "mail":
            # A suite changes a draft, named by the draft's id: saved again,
            # as Gmail saves one, it holds a new message.
            if changes.get("body") is not None:
                self.gmail.save_draft_again(native_id, body=changes["body"])
            if changes.get("rebuilt"):
                # Saved again from another device: the same message under a
                # new draft id, and the old one gone.
                self.gmail.drafts[f"r{next(self._ids):019d}"] = self.gmail.drafts.pop(native_id)
        elif klass == "calendar":
            event = self.calendar.get("primary", native_id)
            event["summary"] = changes["title"]
            self.calendar._touched(event)
        elif klass == "tasks":
            for items in self.tasks.items.values():
                for task in items:
                    if task["id"] == native_id:
                        task["title"] = changes["title"]
        else:
            raise AssertionError(f"no suite mutates {klass}")

    def snapshot(self, klass):
        if klass == "mail":
            held: dict[str, list] = {}
            for m in self.gmail.messages.values():
                folder = ",".join(sorted(m.labels - {"UNREAD"}))
                held.setdefault(exact.norm(m.message_id), []).append(
                    (folder, "UNREAD" in m.labels, m.subject, m.body, m.to, m.cc, m.bcc))
            return {key: sorted(entries) for key, entries in held.items()}
        if klass == "calendar":
            return {e["iCalUID"]: copy.deepcopy(e) for e in self.calendar.events}
        if klass == "files":
            return {f["id"]: (f["name"], f["mimeType"], f["modifiedTime"], f["_text"],
                              sorted(f.get("_cells", {}).items()))
                    for f in self.drive.files}
        if klass == "tasks":
            return {t["id"]: dict(t) for items in self.tasks.items.values() for t in items}
        raise AssertionError(f"no suite looks at {klass}")


@pytest.fixture
def world(monkeypatch):
    golden.pin_the_clock(monkeypatch)
    made = GoogleWorld()
    google_common._service.cache_clear()
    monkeypatch.setattr(google_common, "build", made.google.build)
    yield made
    google_common._service.cache_clear()
    assert made.unhandled == [], f"the fake was asked what it does not know: {made.unhandled}"


def _named(checks):
    return pytest.mark.parametrize("check", checks, ids=lambda c: c.__name__)


# The checks a Gmail that ignores its filters stops, each by the suite's own
# assertion, because the provider keeps what the server's search answered
# (the module's docstring says why they wait).
TRUSTS_THE_SERVER = frozenset({
    mail.reads_what_matches_newest_first,
    mail.a_server_that_ignores_its_filter_is_checked_here,
    mail.dates_are_checked_whatever_the_server_returned,
    mail.new_mail_is_the_inbox_less_what_was_seen,
})


@pytest.mark.parametrize("ignoring", [False, True], ids=["honest", "ignores_filters"])
@_named(mail.CHECKS)
def test_gmail_keeps_the_mail_contract(world, check, ignoring):
    """Gmail is the mail most users bring: each case the suite holds, from a
    filter the server ignored to a draft changed after its card, holds here
    on a Gmail that applies every filter. On one that ignores them, every
    check holds but those `TRUSTS_THE_SERVER` lists, and those still fail:
    a provider that trusted the server's filter passed on the honest fake
    alone (rule 4's own incident)."""
    world.gmail.ignores_filters = ignoring
    case = mail.MailCase(GmailMail(fake_google.Context()), world, writes, fake_google.ADDRESS)
    if not (ignoring and check in TRUSTS_THE_SERVER):
        check(case)
        return
    with pytest.raises(AssertionError):
        check(case)


def test_gmail_sent_mail_since_a_date_is_only_what_went_since_whatever_gmail_answered(world):
    """"What did I send this week" is asked of Gmail as `after:`, and a
    Gmail that ignored it answered every sent message there is. Kept as
    answered, last year's mail read as this week's. The suite's sent mail
    is all recent, so this one is old."""
    world.gmail.ignores_filters = True
    world.seed("mail", [
        {"role": "sent", "sender": fake_google.ADDRESS, "to": "ada@else.example",
         "subject": "Last spring", "body": "Old.", "date": mail._ago(400)},
        {"role": "sent", "sender": fake_google.ADDRESS, "to": "ada@else.example",
         "subject": "This week", "body": "New.", "date": mail._ago(2)}])

    found = GmailMail(fake_google.Context()).sent_items(since=mail._ago(7), limit=10)

    assert [m.subject for m in found] == ["This week"]


def test_a_gmail_draft_saved_again_after_its_check_is_not_sent(world):
    """Gmail's own send takes a draft's id and sends whatever the draft
    holds by then. An autosave from the user's browser between the write
    path's check and the send added a Cc, and it went, though the card
    showed none (the review of steps 5 and 6). What goes now is the message
    read for the check; saved again since, that is gone, and nothing goes."""

    class SavedAgainFirst(GmailMail):
        def send_draft(self, message):
            world.gmail.save_draft_again(message.native_id, cc="dana@example.com")
            return super().send_draft(message)

    provider = SavedAgainFirst(fake_google.Context())
    made = provider.make_draft(Draft(to="ada@else.example", subject="Saturday", body="Ten."))

    with pytest.raises(TargetChanged):
        writes.send_draft(provider, made.native_id, card=made.digest())

    assert [w for w in world.wrote if w.get("gmail") in ("drafts.send", "messages.send")] == []
    assert provider.fetch(made.native_id).cc == "dana@example.com", "the user's draft stays"


@_named(calendar.CHECKS)
def test_google_calendar_keeps_the_calendar_contract(world, check):
    """Google's calendar writes on a condition: a change made on the phone
    since the read is refused, not overwritten, and a write reaches only the
    event it names."""
    check(calendar.CalendarCase(GoogleCalendar(fake_google.Context()), world, writes,
                                fake_google.ADDRESS))


@_named(files.CHECKS)
def test_google_drive_keeps_the_files_contract(world, check):
    """Drive finds by name and text, reads a file cut where asked and says
    so, and of two spreadsheets alike changes only the one named."""
    check(files.FilesCase(GoogleDrive(fake_google.Context()), world, writes,
                          fake_google.ADDRESS))


@_named(tasks.CHECKS)
def test_google_tasks_keeps_the_tasks_contract(world, check):
    """Tasks reads open ones and leaves ticked ones off, and of two tasks
    alike ticks or changes only the one named."""
    check(tasks.TasksCase(GoogleTasks(fake_google.Context()), world, writes,
                          fake_google.ADDRESS))
