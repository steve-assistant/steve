"""Today's Google and Outlook behind the slot's interfaces
(`modules/google_cloud/provider.py`, `modules/outlook_classic/provider.py`).

From step 5's item 4c the tools reach these connections only through these
adapters. Each must answer in the slot's items with today's ids, untagged
(the golden outputs read every slot id back to exactly that), write where
today's functions wrote, and say a failure as the slot's error, which is
how core knows a lapsed sign-in from a server that is merely slow. Driven
against the golden world, whose fakes sit at the services' own seams.
"""

from __future__ import annotations

import datetime as dt

import httplib2
import pytest
from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError

from modules.google_cloud import gmail as google_gmail
from modules.google_cloud.provider import GmailMail, GoogleCalendar, GoogleDrive, GoogleTasks
from modules.outlook_classic.provider import OutlookCalendar, OutlookMail
from steve_sdk.classes import Draft, Task
from steve_sdk.errors import SignInNeeded, Transient, Unsupported
from tests.fakes import google as fake_google
from tests.fakes import outlook as fake_outlook
from tests.unit import golden_world
from tests.unit.golden_world import MOMENT, World


@pytest.fixture
def world(monkeypatch):
    world = World()
    golden_world.wire(monkeypatch, world)
    golden_world.pin_the_clock(monkeypatch)
    yield world
    world.close()


def _written(world, service):
    return [w for w in world.wrote if service in w]


def test_gmail_answers_with_its_own_ids_and_a_draft_goes_by_the_drafts_id(world):
    """A message's id is Gmail's; a draft's is the draft's own, which is
    what sending and deleting one take, and a draft read back says it is one."""
    gmail = GmailMail(fake_google.Context())
    [found] = gmail.search("quarterly plan", limit=10)
    assert (found.native_id, found.direction, found.unread) == ("19a0000000000001", "in", True)
    assert found.sender == golden_world.IRIS and found.body is None

    read = gmail.fetch("19a0000000000001")
    assert "section 2" in read.body and read.ident.startswith("<")
    assert gmail.fetch("19a00000000000ff") is None

    made = gmail.make_draft(Draft(to="iris@fabrikam.example", subject="Re: Quarterly plan draft",
                                  body="Thanks, I will look at it today.",
                                  reply_to="19a0000000000001"))
    assert made.native_id.startswith("r") and made.draft
    assert made.thread == "19a0000000000001"
    again = gmail.fetch(made.native_id)
    assert again.draft and again.body.startswith("Thanks") and again.native_id == made.native_id

    sent = gmail.send_draft(again)
    assert sent.labels == ("SENT",) and not sent.native_id.startswith("r")
    assert sent.ident.startswith("<") and sent.ident != again.ident
    # Sent as the message read, in its thread, and the draft thrown away.
    went, dropped = _written(world, "gmail")[-2:]
    assert (went["gmail"], went["thread"], went["to"]) == (
        "messages.send", "19a0000000000001", "iris@fabrikam.example")
    assert went["body"].startswith("Thanks") and went["in_reply_to"]
    assert dropped == {"gmail": "drafts.delete", "draft": made.native_id}

    spare = gmail.make_draft(Draft(to="dana@example.com", subject="Saturday", body="Free?"))
    gmail.delete(spare)
    assert _written(world, "gmail")[-1] == {"gmail": "drafts.delete", "draft": spare.native_id}
    with pytest.raises(Unsupported):
        gmail.delete(read)


def test_outlook_answers_with_entry_ids_untagged_and_drafts_where_outlook_puts_them(world):
    """The `outlook:` tag told Outlook's ids from Gmail's; the slot's id
    names the account instead, so the native id is the EntryID alone."""
    mail = OutlookMail(fake_outlook.context(world.outlook))
    [found] = mail.search("badge renewal", limit=10)
    assert not found.native_id.startswith("outlook:") and len(found.native_id) > 16
    assert found.direction == "in" and found.thread == "CONV0001"
    assert "renew their building badge" in mail.fetch(found.native_id).body
    assert mail.fetch("00000000DEADBEEF") is None

    reply = mail.make_draft(Draft(to="", subject="", body="Thanks.", reply_to=found.native_id))
    assert reply.draft and reply.subject == "RE: Badge renewal by Friday"
    assert reply.extra == {"where": "Outlook Drafts"}
    sent = mail.send_draft(reply)
    assert sent.subject == "RE: Badge renewal by Friday"

    spare = mail.make_draft(Draft(to="dana@example.com", subject="Saturday", body="Free?"))
    mail.delete(spare)
    copied = mail.make_draft(Draft(to="dana@example.com", subject="x", body="y",
                                   cc="iris@x.example"))
    assert copied.cc == "iris@x.example"


def test_the_calendars_answer_in_events_with_the_users_own_answer_apart_from_the_guests(world):
    """"Who is in the room" must not count the user; their own answer to an
    invitation is theirs, kept beside the guests."""
    start, end = MOMENT, MOMENT + dt.timedelta(days=2)
    calendar = GoogleCalendar(fake_google.Context())
    google = {e.title: e for e in calendar.events(start, end, limit=25)}
    review = google["Design review with Iris"]
    assert [g.email for g in review.guests] == ["iris@fabrikam.example"]
    assert review.extra["my_response"] == "accepted"
    assert review.start.tzinfo is not None and review.ident
    assert calendar.fetch(review.native_id).title == "Design review with Iris"
    assert calendar.fetch("ev9999golden") is None

    on_outlook = OutlookCalendar(fake_outlook.context(world.outlook))
    outlook_events = on_outlook.events(start, end, limit=25)
    vendor = next(e for e in outlook_events if e.title == "Vendor call")
    again = on_outlook.fetch(vendor.native_id)
    assert (again.title, again.start) == ("Vendor call", vendor.start)


def test_drive_and_tasks_answer_in_files_and_tasks_and_write_where_todays_did(world):
    """Tasks go on the agent's own list, as today; a completed one reads back done."""
    drive = GoogleDrive(fake_google.Context())
    [plan] = drive.search("quarterly plan", limit=10)
    assert plan.native_id == "1golden0001Drive" and plan.owners == ("iris@fabrikam.example",)
    assert drive.read(plan.native_id, max_chars=100).text.startswith("Quarterly plan")

    tasks = GoogleTasks(fake_google.Context())
    assert {t.title for t in tasks.lists()} >= {"My Tasks", "Steve"}
    added = tasks.create(Task(native_id="", title="Renew the parking permit", due="2026-10-20"))
    assert added.native_id and added.list_id
    listed = {t.title: t for t in tasks.tasks()}
    assert listed["Renew the parking permit"].due == "2026-10-20"
    done = tasks.complete(added)
    assert done.done
    assert tasks.fetch(added.native_id, added.list_id).done


def test_googles_failures_become_the_slots_errors(world, monkeypatch):
    """A refused refresh token is a sign-in to redo, and a 503 a server to
    ask again later: core says the two differently."""
    def revoked():
        raise RefreshError("invalid_grant: Token has been expired or revoked.")

    monkeypatch.setattr(google_gmail, "_gmail", revoked)
    with pytest.raises(SignInNeeded):
        GmailMail(fake_google.Context()).search("anything", limit=5)

    def busy():
        raise HttpError(httplib2.Response({"status": 503}), b"backend error")

    monkeypatch.setattr(google_gmail, "_gmail", busy)
    with pytest.raises(Transient):
        GmailMail(fake_google.Context()).fetch("19a0000000000001")
