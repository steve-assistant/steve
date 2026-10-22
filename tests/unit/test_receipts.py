"""An action's receipt says what reading it back found.

The verifier catches a reply claiming what no tool did; receipts catch a tool
that ran and was wrong -- on 25 September 2026 three tasks were "added to your
Tasks" and were not on their phone. These pin that each check reads back the
right thing from the right mailbox, that a contradiction turns into a failure
everyone downstream sees, and that a check which cannot run leaves the action
as the tool reported it rather than calling it failed.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import time

import pytest

from agent import actions, approvals, receipts, runtime, sqltime
from core import db
from steve_sdk.classes import Event, Message, Task
from steve_sdk.errors import SignInNeeded
from tests import persona
from tests.fakes import google as fake_google
from tests.gate import slotfake
from tests.gate.mailfake import Mailbox, connect
from tools import commitments, obligations


@pytest.fixture(autouse=True)
def _quick(monkeypatch):
    monkeypatch.setattr(receipts, "RETRY_AFTER_S", 0.0)


@pytest.fixture
def ledger(monkeypatch):
    conn = db.open_store()
    monkeypatch.setattr(runtime, "conn", conn)
    yield conn
    conn.close()


def _attach(tool, args, result):
    return asyncio.run(receipts.attach(tool, args, result))


EVENT = {"summary": "Game Theory", "start": "2026-10-06T10:05"}


def _event(**changes) -> Event:
    found = Event(native_id="ev1", title="Game Theory",
                  start=slotfake.at("2026-10-06T10:05:00-04:00"),
                  end=slotfake.at("2026-10-06T11:25:00-04:00"))
    return dataclasses.replace(found, **changes)


class _Reads(slotfake.Calendar):
    """A calendar whose read-back answers in turn as the test says, the last
    answer for every read after: an event, None for gone, or an error."""

    def __init__(self, *answers) -> None:
        super().__init__()
        self.answers, self.asked = list(answers), []

    def fetch(self, native_id: str) -> Event | None:
        self.asked.append(native_id)
        said = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(said, BaseException):
            raise said
        return said


def _reading(monkeypatch, *answers) -> tuple[_Reads, str]:
    """A calendar answering so, and the id of its event `ev1`."""
    held = _Reads(*answers)
    return held, slotfake.plug(monkeypatch, "calendar", google=held)["google"].id("ev1")


# --- the calendar -----------------------------------------------------------------


def test_an_event_that_is_there_as_asked_is_verified(monkeypatch):
    held, event_id = _reading(monkeypatch, _event())
    out = _attach("create_event", EVENT, {"created": True, "id": event_id})
    assert out["receipt"]["status"] == receipts.VERIFIED
    assert out["receipt"]["target"] == event_id and held.asked == ["ev1"]
    assert approvals.reported_failure(out) is None


def test_a_receipt_is_timed_in_utc_as_everything_steve_keeps(monkeypatch):
    """The receipt's time was the local time in SQLite's shape, so read as
    the UTC every other stamp is, it was hours off, and unmarked."""
    monkeypatch.setitem(receipts.CHECKS, "probe", lambda args, result: ("it", {}))

    receipt = receipts.check("probe", {}, {})

    off = dt.datetime.now(dt.UTC) - sqltime.moment(receipt.checked_at)
    assert abs(off.total_seconds()) < 60, receipt.checked_at


@pytest.mark.parametrize("found,said", [
    (None, "not there"),
    (_event(title="Game theory (copy)"), "titled"),
    # The classic: asked for 10:05 local, stored as 10:05 UTC.
    (_event(start=slotfake.at("2026-10-06T06:05:00-04:00")), "starts"),
])
def test_an_event_read_back_wrong_reports_a_failure(monkeypatch, ledger, found, said):
    _, event_id = _reading(monkeypatch, found)
    out = _attach("create_event", EVENT, {"created": True, "id": event_id})
    assert out["receipt"]["status"] == receipts.FAILED and said in out["receipt"]["why"]
    assert out["ok"] is False and "did not happen as asked" in out["error"]
    assert approvals.reported_failure(out)  # so a gated one is announced as failed
    issue = ledger.execute("SELECT kind FROM issues").fetchone()
    assert issue["kind"] == "action_unverified"


def test_an_event_google_keeps_as_cancelled_is_not_there(monkeypatch, ledger):
    """Google keeps a deleted event a while, marked cancelled: read back
    through Google's own provider, it is not on the calendar."""
    from modules.google_cloud import calendar as google_calendar
    from modules.google_cloud.provider import GoogleCalendar

    class Client:
        def events(self):
            return self

        def get(self, **_):
            return self

        def execute(self):
            return {"id": "ev1", "status": "cancelled", "summary": "Game Theory",
                    "start": {"dateTime": "2026-10-06T10:05:00-04:00"},
                    "end": {"dateTime": "2026-10-06T11:25:00-04:00"}}

    monkeypatch.setattr(google_calendar, "_calendar", lambda: Client())
    found = slotfake.plug(monkeypatch, "calendar",
                          google=GoogleCalendar(fake_google.Context()))["google"]
    out = _attach("create_event", EVENT, {"created": True, "id": found.id("ev1")})
    assert out["receipt"]["status"] == receipts.FAILED and "not there" in out["receipt"]["why"]


def test_a_read_back_that_cannot_run_leaves_the_action_as_reported(monkeypatch):
    """An expired sign-in while reading back is not evidence it failed."""
    _, event_id = _reading(monkeypatch, SignInNeeded("signed out"))
    out = _attach("create_event", EVENT, {"created": True, "id": event_id})
    assert out["receipt"]["status"] == receipts.UNCHECKED
    assert out.get("ok") is not False and approvals.reported_failure(out) is None


def test_an_event_id_steve_did_not_hand_out_is_left_unchecked_not_failed(monkeypatch):
    _reading(monkeypatch, _event())
    out = _attach("create_event", EVENT, {"created": True, "id": "ev1"})
    assert out["receipt"]["status"] == receipts.UNCHECKED and out.get("ok") is not False


def test_a_service_slow_to_show_what_it_made_gets_a_second_look(monkeypatch):
    held, event_id = _reading(monkeypatch, None, _event())
    out = _attach("create_event", EVENT, {"created": True, "id": event_id})
    assert out["receipt"]["status"] == receipts.VERIFIED and len(held.asked) == 2


def test_an_update_is_checked_field_by_field(monkeypatch, ledger):
    _, event_id = _reading(monkeypatch, _event(location="Room 2410"))
    ok = _attach("update_event", {"event_id": event_id, "location": "Room 2410"},
                 {"updated": True})
    assert ok["receipt"]["status"] == receipts.VERIFIED
    wrong = _attach("update_event", {"event_id": event_id, "location": "Room 2420",
                                     "start": "2026-10-06T10:05"}, {"updated": True})
    assert wrong["ok"] is False and "location" in wrong["receipt"]["why"]


def test_a_deleted_event_is_gone_from_the_calendar_it_was_on(monkeypatch, ledger):
    _, event_id = _reading(monkeypatch, _event())
    still = _attach("delete_event", {"event_id": event_id}, {"deleted": True})
    assert still["ok"] is False and "still on" in still["receipt"]["why"]
    _, event_id = _reading(monkeypatch, None)
    gone = _attach("delete_event", {"event_id": event_id}, {"deleted": True})
    assert gone["receipt"]["status"] == receipts.VERIFIED


# --- mail -------------------------------------------------------------------------


@pytest.fixture
def boxes(monkeypatch):
    """Two mail accounts on the slot, each a mailbox in a dict
    (tests/gate/mailfake.py), and the ids each would hand out."""
    found = {"gmail": Mailbox(persona.EMAIL), "outlook": Mailbox(persona.WORK_EMAIL)}
    return found, connect(monkeypatch, **found)


def test_a_sent_email_is_in_sent_or_the_send_failed(boxes, ledger):
    found, accounts = boxes
    found["gmail"].items["m1"] = Message(native_id="m1", labels=("SENT",), direction="out")
    sent = {"sent": True, "id": accounts["gmail"].id("m1")}
    assert _attach("send_email", {"to": "x@y.z"}, sent)["receipt"]["status"] == receipts.VERIFIED
    found["gmail"].items["m1"] = Message(native_id="m1", labels=("DRAFT",))
    assert _attach("send_email", {"to": "x@y.z"}, sent)["ok"] is False


def test_mail_is_read_back_from_the_account_its_id_names_and_no_other(boxes, ledger):
    """Asked in Gmail, a sent Contoso draft would read as missing -- a false
    failure for the mail the user most needs to trust. Until step 5 Contoso's
    and the own-domain mailbox's mail was not read back at all; now each account's is
    read back from that account. Gmail's reads are recorded, not raised: a
    check's own error handling would swallow an exception."""
    found, accounts = boxes
    found["outlook"].items["d1"] = Message(native_id="d1", draft=True)
    asked = []
    found["gmail"].fetch = lambda *a, **k: asked.append(a)
    draft = accounts["outlook"].id("d1")

    made = _attach("draft_email", {"to": "x"}, {"drafted": True, "draft_id": draft})
    still = _attach("delete_draft", {"draft_id": draft}, {"deleted": True})

    assert made["receipt"]["status"] == receipts.VERIFIED
    assert still["ok"] is False and "still there" in still["receipt"]["why"]
    assert asked == []


def test_a_draft_is_there_and_a_deleted_one_is_gone(boxes, ledger):
    found, accounts = boxes
    found["gmail"].items["r1"] = Message(native_id="r1", draft=True)
    draft = accounts["gmail"].id("r1")
    assert _attach("draft_email", {"to": "x"}, {"draft_id": draft})[
        "receipt"]["status"] == receipts.VERIFIED
    still = _attach("delete_draft", {"draft_id": draft}, {"deleted": True})
    assert still["ok"] is False and "still there" in still["receipt"]["why"]
    del found["gmail"].items["r1"]
    assert _attach("delete_draft", {"draft_id": draft}, {"deleted": True})[
        "receipt"]["status"] == receipts.VERIFIED


def test_a_message_archived_and_read_has_the_labels_to_show_it(boxes, ledger):
    found, accounts = boxes
    found["gmail"].items["m1"] = Message(native_id="m1", labels=("INBOX",), unread=True)
    out = _attach("modify_email", {"message_id": accounts["gmail"].id("m1"), "archive": True,
                                   "mark_read": True}, {"changed": True})
    assert out["ok"] is False and "still in the inbox" in out["receipt"]["why"]
    assert "still unread" in out["receipt"]["why"]


def test_a_mail_id_steve_did_not_hand_out_is_left_unchecked_not_failed(boxes, ledger):
    """An id from before the slot, in a card answered after it, names no
    account: the action is reported as the tool said, not as a failure."""
    out = _attach("draft_email", {"to": "x"}, {"drafted": True, "draft_id": "r-old"})
    assert out["receipt"]["status"] == receipts.UNCHECKED and out.get("ok") is not False


# --- the tasks list -----------------------------------------------------------------


@pytest.fixture
def board(monkeypatch):
    """Their tasks list, holding the commitment's task `t1`, open."""
    held = slotfake.TaskBoard(Task(native_id="t1", title="Submit the reflection journal"))
    held.account = slotfake.plug(monkeypatch, "tasks", google=held)["google"]
    return held


def _commitment(conn, task_id):
    cid = commitments.add(conn, text="Submit the reflection journal", quote="x",
                          source="conversation", source_id=f"said:{time.time()}")
    obligations.set_task(conn, cid, task_id)
    return cid


def test_a_task_said_to_be_on_user_phone_is_looked_for_there(board, ledger):
    task_id = board.account.id("t1")
    cid = _commitment(ledger, task_id)
    out = _attach("add_task", {"task": "x"}, {"ok": True, "commitment_id": cid, "on_phone": True})
    assert out["receipt"]["status"] == receipts.VERIFIED and out["receipt"]["target"] == task_id
    board.remove("t1")
    assert _attach("add_task", {"task": "x"},
                   {"ok": True, "commitment_id": cid, "on_phone": True})["ok"] is False


def test_a_task_not_yet_on_user_phone_is_reported_as_the_tool_said(monkeypatch):
    class Never(slotfake.TaskBoard):
        def fetch(self, native_id, list_id=None):
            raise AssertionError("looked for a task the result says is not there")

    slotfake.plug(monkeypatch, "tasks", google=Never())
    out = _attach("add_task", {"task": "x"}, {"ok": True, "commitment_id": 1, "on_phone": False})
    assert out["receipt"]["status"] == receipts.UNCHECKED and out["ok"] is True


def test_a_completed_task_is_completed_on_user_phone(board, ledger):
    cid = _commitment(ledger, board.account.id("t1"))
    out = _attach("complete_task", {"commitment_id": cid}, {"ok": True})
    assert out["ok"] is False and "not completed" in out["receipt"]["why"]
    board.tick("t1")
    assert _attach("complete_task", {"commitment_id": cid}, {"ok": True})["ok"] is True


# --- what is left alone ---------------------------------------------------------------


def test_a_failure_is_not_checked_and_an_unchecked_tool_has_no_receipt(monkeypatch):
    _reading(monkeypatch, AssertionError("read back a failed action"))
    failed = {"ok": False, "error": "calendar not shared"}
    assert _attach("create_event", EVENT, failed) == failed
    assert _attach("run_code", {"code": "1"}, {"ok": True}) == {"ok": True}


def test_a_read_back_that_hangs_is_given_up_on(monkeypatch):
    """The read-back hangs until the test lets it go, not for half a second:
    a fake that finishes on its own can finish before a loaded machine has
    looked at the 0.05 s timeout, and a result that arrives first is taken
    as the answer."""
    import threading

    hung = threading.Event()

    class Hangs(slotfake.Calendar):
        def fetch(self, native_id):
            hung.wait(30)
            return _event()  # what it would have read, had anyone still been waiting

    event_id = slotfake.plug(monkeypatch, "calendar", google=Hangs())["google"].id("ev1")

    async def attach_then_let_go():
        try:
            return await receipts.attach("create_event", EVENT,
                                         {"created": True, "id": event_id})
        finally:
            # The abandoned thread is let go inside the loop: asyncio.run's
            # shutdown waits for every thread it started, so let go after
            # it, the test waited out the whole 30 s hang.
            hung.set()

    monkeypatch.setattr(receipts, "CHECK_TIMEOUT_S", 0.05)
    out = asyncio.run(attach_then_let_go())
    assert out["receipt"]["status"] == receipts.UNCHECKED and out.get("ok") is not False


def test_every_action_through_perform_gets_its_receipt(monkeypatch, ledger):
    """The one door: free actions and approved ones alike."""
    # Registered first: `perform` imports the toolset, which would put the
    # real executor back over the fake, and the fake's undoing would then
    # take `create_event` out of EXECUTORS for every later test.
    import agent.toolset  # noqa: F401

    _, event_id = _reading(monkeypatch, _event(title="Other"))

    async def fake_create(**args):
        return {"created": True, "id": event_id}

    monkeypatch.setitem(actions.EXECUTORS, "create_event", fake_create)
    out = asyncio.run(actions.perform("create_event", EVENT))
    assert out["ok"] is False and out["receipt"]["status"] == receipts.FAILED


def test_every_check_is_for_a_tool_that_exists():
    import agent.toolset  # noqa: F401 - fills EXECUTORS

    assert set(receipts.CHECKS) <= set(actions.EXECUTORS)
