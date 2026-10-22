"""Where the attention loop looks: what each source turns into observations.

Every source is tested against a small fake of the integration it reads --
no Google, no Outlook, nothing live (tests/unit/conftest.py shuts those
doors). What matters is the key each observation gets, because the key is
what decides whether a thing is mentioned once, once per change, or never
again; and what each source leaves out, because most of what a mailbox and
a calendar contain is not news.
"""

from __future__ import annotations

import datetime as dt

import pytest

from agent.attention import store
from agent.attention.sources import SourceError, obligations
from agent.attention.sources import calendar as cal_source
from agent.attention.sources import drive as drive_source
from agent.attention.sources import mail as mail_source
from tests.fakes import google as fake_google

# --------------------------------------------------------------------------
# Mail
# --------------------------------------------------------------------------


class _Gmail:
    """Gmail's own functions, beneath its provider: ids listed for free,
    each message's headers a call."""

    def __init__(self, ids, fail_list=False):
        self.ids = ids
        self.fail_list = fail_list
        self.fetched: list[str] = []

    def message_ids(self, query, limit):
        if self.fail_list:
            return {"error": "invalid_grant"}
        return {"ids": list(self.ids)}

    def summary(self, message_id):
        self.fetched.append(message_id)
        return {
            "id": message_id, "thread_id": f"t-{message_id}", "subject": f"Subject {message_id}",
            "from": "Rina <rina@contoso.example>", "snippet": "Are you enrolled in DES 203?",
            "unread": message_id != "read-one", "internal_date": 1_758_700_000_000,
        }


def _account(provider, name: str, key: str, module: str):
    from agent.connectors import Connected

    return Connected(key=key, name=name, klass="mail", module=module, provider=provider,
                     address="maya@example.com")


@pytest.fixture
def gmail(monkeypatch):
    from modules.google_cloud import gmail as google_gmail
    from modules.google_cloud.provider import GmailMail

    fake = _Gmail(["m1", "m2", "read-one"])
    for name in ("message_ids", "summary"):
        monkeypatch.setattr(google_gmail, name, getattr(fake, name))
    fake.account = _account(GmailMail(fake_google.Context()), "gmail", fake_google.KEY,
                            "google_cloud")
    return fake


def test_gmail_fetches_only_what_it_has_not_seen(conn, gmail):
    """Every three minutes is affordable only because a quiet poll costs
    one call. The watcher this replaces fetched headers for every message
    on every sweep."""
    account = gmail.account
    store.record_new(conn, [store.Observation(f"mail:{account.key}",
                                              f"mail:{account.id('m1')}", "old")])

    found = mail_source.watch(conn, account)

    assert gmail.fetched == ["m2", "read-one"]
    assert [o.key for o in found] == [f"mail:{account.id('m2')}",
                                      f"mail:{account.id('read-one')}"]


def test_a_mail_observation_says_whether_the_user_has_read_it(conn, gmail):
    """Telling them about something the user has plainly already seen is the noise
    that gets a notifier muted; the judge is told which is which."""
    account = gmail.account
    by_key = {o.key: o for o in mail_source.watch(conn, account)}

    assert "unread" in by_key[f"mail:{account.id('m1')}"].detail
    assert "already read" in by_key[f"mail:{account.id('read-one')}"].detail


def test_the_id_reaches_the_doer_as_the_slot_id_that_names_its_account(conn, gmail):
    """`read_email` takes the slot id back to the account; Gmail's bare id
    would be refused ("search again")."""
    [first, *_] = mail_source.watch(conn, gmail.account)

    assert first.ref["message_id"] == gmail.account.id("m1")
    assert first.ref["account"] == "gmail"
    assert first.source == f"mail:{gmail.account.key}"


def test_a_sources_screen_reads_only_what_is_new_and_within_the_cap(conn, monkeypatch):
    """A feed hands over sixty headlines a look, most of them seen before,
    and each one screened is a call: the screen is given only what the
    judge will be, and the judge is given what the screen returned."""
    from dataclasses import replace

    from agent.attention import settings, sources
    from agent.attention.store import Observation

    monkeypatch.setattr(settings, "MAX_PER_SOURCE", 2)
    found = [Observation("feed", f"feed:{n}", f"Story {n}") for n in range(5)]
    store.record_new(conn, found[:1])
    screened: list[str] = []

    def screen(c, observation):
        screened.append(observation.key)
        return replace(observation, title="A story")

    got = sources.collect(conn, sources.Source("feed", lambda c: found, screen))

    assert screened == ["feed:1", "feed:2"]
    assert [(o.key, o.title) for o in got] == [("feed:1", "A story"), ("feed:2", "A story")]


def test_an_unreadable_gmail_is_a_failed_source_not_an_empty_one(conn, monkeypatch):
    from modules.google_cloud import gmail as google_gmail
    from modules.google_cloud.provider import GmailMail
    from steve_sdk.errors import ConnectorError

    fake = _Gmail([], fail_list=True)
    monkeypatch.setattr(google_gmail, "message_ids", fake.message_ids)

    with pytest.raises(ConnectorError, match="invalid_grant"):
        mail_source.watch(conn, _account(GmailMail(fake_google.Context()), "gmail",
                                         fake_google.KEY, "google_cloud"))


def test_outlook_mail_leaves_out_what_the_user_sent(conn, monkeypatch):
    """`outlook.search` walks Sent as well as the inbox; the old sweep
    handed the user's own outgoing mail to the judge as new mail."""
    from modules.outlook_classic import outlook as outlook_com
    from modules.outlook_classic.provider import OutlookMail
    from tests.fakes import outlook as fake_outlook

    monkeypatch.setattr(outlook_com, "search", lambda q, limit, days: {"messages": [
        {"id": "outlook:in", "subject": "Reading for Thursday", "from": "prof@contoso.example",
         "direction": "received", "unread": True},
        {"id": "outlook:out", "subject": "Re: Reading", "direction": "sent"},
    ]})

    [only] = mail_source.watch(
        conn, _account(OutlookMail(fake_outlook.context(None)), "outlook", fake_outlook.KEY,
                       "outlook_classic"))

    assert only.ref["subject"] == "Reading for Thursday" and only.ref["account"] == "outlook"


def test_no_outlook_is_a_normal_machine(conn):
    """An account that cannot be reached from here -- classic Outlook not
    installed -- is nothing, not a failure: its health says why."""
    from modules.outlook_classic.provider import OutlookMail
    from tests.fakes import outlook as fake_outlook

    # CO_E_CLASSSTRING: Windows knows no `Outlook.Application`.
    missing = fake_outlook.context(None, dispatch=fake_outlook.failing(-2147221005))
    account = _account(OutlookMail(missing), "outlook", fake_outlook.KEY, "outlook_classic")

    assert mail_source.watch(conn, account) == []


# --------------------------------------------------------------------------
# The calendar
# --------------------------------------------------------------------------


def _event(event_id, start, **kw):
    return {"id": event_id, "summary": kw.pop("summary", event_id), "start": start, **kw}


def test_each_change_to_an_event_is_its_own_observation(conn, monkeypatch):
    """Keyed by the event and its `updated` stamp: a move is news, and the
    same unchanged event seen again is not."""
    from agent import calendars

    events = [_event("e1", "2026-09-26T10:00:00-04:00", updated="2026-09-24T10:00:00Z")]
    monkeypatch.setattr(
        calendars, "changed", lambda conn, **k: {"events": [dict(e) for e in events]}
    )

    first = cal_source.changes(conn)
    events[0]["updated"] = "2026-09-24T12:00:00Z"
    second = cal_source.changes(conn)

    assert first[0].key != second[0].key
    assert first[0].key.startswith("calendar:e1:")


def test_a_cancellation_says_so(conn, monkeypatch):
    from agent import calendars

    monkeypatch.setattr(calendars, "changed", lambda conn, **k: {"events": [
        _event("e1", "2026-09-26T10:00:00-04:00", status="cancelled", summary="Fabrikam kick-off"),
    ]})

    [obs] = cal_source.changes(conn)

    assert obs.title.startswith("CANCELLED: Fabrikam kick-off")


def test_an_event_gets_a_look_when_it_appears_and_another_when_it_is_close():
    """Without the pass in the key, "check nearer the time" never comes."""
    now = dt.datetime(2026, 9, 24, 12, 0).astimezone()

    far = (now + dt.timedelta(days=4)).isoformat()
    near = (now + dt.timedelta(hours=20)).isoformat()

    assert cal_source._pass(far, now) == "ahead"
    assert cal_source._pass(near, now) == "soon"
    assert cal_source._pass(None, now) == "soon"


def test_a_lecture_only_gets_the_close_look(conn, monkeypatch):
    """A recurring event first appeared months ago; twenty-odd lectures a
    week, each judged twice, would be most of the judge's day."""
    from agent import calendars

    now = dt.datetime.now().astimezone()
    monkeypatch.setattr(calendars, "agenda", lambda conn, days, limit: {"events": [
        _event("lecture-far", (now + dt.timedelta(days=4)).isoformat(), recurring=True),
        _event("lecture-near", (now + dt.timedelta(hours=5)).isoformat(), recurring=True),
        _event("trip", (now + dt.timedelta(days=4)).isoformat(), location="New York"),
    ]})

    keys = [o.key for o in cal_source.horizon(conn)]

    assert keys == ["horizon:lecture-near:soon", "horizon:trip:ahead"]


def test_the_people_and_place_reach_the_judge(conn, monkeypatch):
    from agent import calendars

    now = dt.datetime.now().astimezone()
    monkeypatch.setattr(calendars, "agenda", lambda conn, days, limit: {"events": [
        _event("chat", (now + dt.timedelta(hours=5)).isoformat(), summary="Northwind coffee chat",
               location="Zoom", attendees=[{"email": "recruiter@northwind.example"}]),
    ]})

    [obs] = cal_source.horizon(conn)

    assert "recruiter@northwind.example" in obs.detail and "Zoom" in obs.detail
    assert obs.ref["event_id"] == "chat"


# --------------------------------------------------------------------------
# Commitments and Drive
# --------------------------------------------------------------------------


def test_a_commitment_coming_due_is_keyed_by_its_date(conn):
    """Moving the date is a new mention; seeing it again unchanged is not."""
    from tools import commitments

    soon = (dt.date.today() + dt.timedelta(days=1)).isoformat()
    commitments.add(conn, text="send Mira the draft", quote="I'll send it", due_date=soon,
                    source="gmail", source_id="x")

    [obs] = obligations.commitments(conn)

    assert obs.key.endswith(f":{soon}") and "Mira" in obs.title


def test_their_own_files_are_not_news(conn, monkeypatch):
    """Owned by any of the user's addresses, whichever account it is in."""
    from steve_sdk.classes import FileItem
    from tests.gate import mailfake, slotfake

    mailfake.connect(monkeypatch, home=mailfake.Mailbox("maya@example.com"))
    accounts = slotfake.plug(monkeypatch, "files", google=slotfake.Drive(
        FileItem("mine", name="My notes", owners=("Maya@example.com",), modified="t1"),
        FileItem("theirs", name="Group deck", owners=("eli@contoso.example",), modified="t2")))

    [obs] = drive_source.shared(conn)

    assert obs.key == f"drive:{accounts['google'].id('theirs')}:t2"


def test_without_their_address_drive_waits_rather_than_guessing(conn, monkeypatch):
    from agent import connectors

    monkeypatch.setattr(connectors, "addresses", lambda conn, **kw: frozenset())

    with pytest.raises(SourceError):
        drive_source.shared(conn)
