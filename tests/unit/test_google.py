"""Google integration: the parts that do not need a network.

Deliberately not mocking the API surface. Tests that assert "we called
messages().list with these arguments" only restate the implementation and pass
just as happily when the arguments are wrong. What is worth pinning is the
logic that transforms Google's shapes into ours: MIME walking, timezone
conversion, query escaping, and error classification -- each of which has a
wrong answer that looks plausible.
"""

from __future__ import annotations

import base64
import datetime as dt
import logging
from zoneinfo import ZoneInfo

import pytest
from googleapiclient.errors import HttpError

from modules.google_cloud import calendar as gcal
from modules.google_cloud import drive as gdrive
from modules.google_cloud import gmail
from modules.google_cloud.auth import scopes
from modules.google_cloud.common import clamp, explain, guard
from tests.fakes import google as fake_google


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


# --------------------------------------------------------------------------
# Gmail: getting the text out of a message
# --------------------------------------------------------------------------


def test_plain_text_body_is_extracted():
    payload = {"mimeType": "text/plain", "body": {"data": _b64("Hello Maya.")}}
    assert gmail._extract_body(payload) == "Hello Maya."


def test_plain_text_is_preferred_over_html():
    """Multipart mail carries both. The plain part is what the sender's client
    produced for exactly this purpose, so stripping tags from the HTML instead
    is strictly worse."""
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/plain", "body": {"data": _b64("the plain version")}},
            {"mimeType": "text/html", "body": {"data": _b64("<p>the html version</p>")}},
        ],
    }
    assert gmail._extract_body(payload) == "the plain version"


def test_html_is_used_when_there_is_no_plain_part():
    payload = {
        "mimeType": "text/html",
        "body": {"data": _b64("<div>Hi <b>there</b></div>")},
    }
    result = gmail._extract_body(payload)
    assert "Hi" in result and "there" in result
    assert "<" not in result


def test_style_and_script_content_is_not_read_as_prose():
    """A marketing email inlines its CSS; without this the 'body' is a
    stylesheet."""
    html = "<style>.x{color:red}</style><script>var a=1</script><p>Real text</p>"
    payload = {"mimeType": "text/html", "body": {"data": _b64(html)}}
    result = gmail._extract_body(payload)
    assert "color:red" not in result
    assert "var a" not in result
    assert "Real text" in result


def test_nested_multipart_is_walked():
    """Real mail nests: multipart/mixed wrapping multipart/alternative."""
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": _b64("buried text")}}
                ],
            },
            {"mimeType": "application/pdf", "body": {"attachmentId": "x"}},
        ],
    }
    assert "buried text" in gmail._extract_body(payload)


def test_html_entities_are_decoded():
    payload = {
        "mimeType": "text/html",
        "body": {"data": _b64("<p>Tom &amp; Jerry &lt;3 &quot;quotes&quot;</p>")},
    }
    result = gmail._extract_body(payload)
    assert "Tom & Jerry" in result
    assert "&amp;" not in result


def test_headers_are_found_case_insensitively():
    payload = {"headers": [{"name": "message-id", "value": "<abc@x>"}]}
    assert gmail._header(payload, "Message-ID") == "<abc@x>"


# --------------------------------------------------------------------------
# Calendar: times
# --------------------------------------------------------------------------

EASTERN = ZoneInfo("America/New_York")


def test_a_utc_event_is_shown_in_local_time():
    """The account this was built against has its calendar set to UTC while
    the user is in Eastern. Echoing Google's string would put every class four
    hours late."""
    event = {
        "id": "1",
        "summary": "Basics of Economics",
        "start": {"dateTime": "2026-09-21T14:05:00Z"},
        "end": {"dateTime": "2026-09-21T15:25:00Z"},
    }
    formatted = gcal._format_event(event, EASTERN)
    assert formatted["start"].startswith("2026-09-21T10:05")
    assert formatted["all_day"] is False


def test_all_day_events_are_not_shifted():
    """An all-day event is a date, not a moment. Converting it can move it to
    the previous day and turn a birthday into the wrong date."""
    event = {
        "id": "2",
        "summary": "Someone's Birthday",
        "start": {"date": "2026-09-22"},
        "end": {"date": "2026-09-23"},
    }
    formatted = gcal._format_event(event, EASTERN)
    assert formatted["start"] == "2026-09-22"
    assert formatted["all_day"] is True


def test_a_room_and_a_calendar_on_the_guest_list_are_marked_as_not_people():
    """The attention loop looks up who the user will meet. It used to know
    Google's room addresses itself; now the module marks them, and a room
    booked for a coffee chat is not researched as a person."""
    from modules.google_cloud import provider

    event = provider._event({"id": "3", "start": {"dateTime": "2026-09-21T14:05:00Z"},
                             "end": {"dateTime": "2026-09-21T15:00:00Z"}, "attendees": [
        {"email": "jane@northwind.example"},
        {"email": "c_1889@resource.calendar.google.com", "resource": True},
        {"email": "c_2201@resource.calendar.google.com"},  # no flag: the address says it
    ]})

    assert [g.room for g in event.guests] == [False, True, True]


def test_gmail_gives_its_categories_as_the_accounts_own_sorting_of_a_sender():
    """The attention loop drops mail a system or a list sent. It once read
    Gmail's category labels itself, which no other account's mail carries;
    the module now names the kind of sender and Gmail's word for it."""
    from modules.google_cloud import provider

    def sorted_as(*labels):
        extra = provider._mail({"id": "m", "labels": ["INBOX", *labels]}).extra
        return extra.get("sorted_as"), extra.get("filed_under")

    assert sorted_as("CATEGORY_PROMOTIONS") == ("automated", "Promotions")
    assert sorted_as("CATEGORY_FORUMS") == ("group", "Forums")
    assert sorted_as("CATEGORY_PERSONAL") == (None, None)


def test_a_naive_time_is_interpreted_as_local():
    """"Schedule lunch at 13:00" means 1pm where the user is, not in UTC."""
    result = gcal._to_rfc3339("2026-09-21T13:00", EASTERN)
    parsed = dt.datetime.fromisoformat(result)
    assert parsed.hour == 13
    assert parsed.utcoffset() == dt.timedelta(hours=-4)  # EDT in September


def test_an_explicit_offset_is_respected():
    result = gcal._to_rfc3339("2026-09-21T13:00:00+00:00", EASTERN)
    assert dt.datetime.fromisoformat(result).utcoffset() == dt.timedelta(0)


def test_local_conversion_leaves_unparseable_values_alone():
    assert gcal._local("not a date", EASTERN) == "not a date"
    assert gcal._local(None, EASTERN) is None


# --------------------------------------------------------------------------
# Drive: query building
# --------------------------------------------------------------------------


def test_apostrophes_in_a_search_are_escaped():
    """Drive's query language uses single quotes as delimiters, so an
    unescaped apostrophe is a syntax error rather than no results -- and
    filenames with apostrophes are extremely common."""
    captured: dict = {}

    class FakeFiles:
        def list(self, **kwargs):
            captured.update(kwargs)
            return self

        def execute(self):
            return {"files": []}

    class FakeDrive:
        def files(self):
            return FakeFiles()

    original = gdrive._drive
    gdrive._drive = lambda: FakeDrive()
    try:
        gdrive.search("Maya's notes")
    finally:
        gdrive._drive = original

    assert "\\'" in captured["q"], captured["q"]
    assert "trashed = false" in captured["q"]


# --------------------------------------------------------------------------
# Failure handling
# --------------------------------------------------------------------------


def _http_error(status: int) -> HttpError:
    class Response:
        def __init__(self, code):
            self.status = code
            self.reason = "because"

    return HttpError(Response(status), b"{}")


@pytest.mark.parametrize(
    "status,kind",
    [(401, "auth"), (403, "permission"), (404, "not_found"), (429, "rate_limit")],
)
def test_google_errors_are_classified(status, kind):
    """Each of these implies a different next step, so collapsing them into
    "an error occurred" loses the only useful part."""
    assert explain(_http_error(status))["kind"] == kind


def test_a_tool_returns_a_failure_rather_than_raising():
    """A 403 is information the model should relay, not an exception that
    aborts the turn."""

    @guard
    def boom():
        raise _http_error(403)

    result = boom()
    assert isinstance(result, dict)
    assert result["kind"] == "permission"


def test_unexpected_errors_are_still_contained():
    @guard
    def boom():
        raise ValueError("something odd")

    assert guard(boom)()["kind"] == "unexpected"


@pytest.mark.parametrize(
    "given,default,expected",
    [(None, 10, 10), (5, 10, 5), (0, 10, 1), (9999, 10, 100), (-3, 10, 1)],
)
def test_result_limits_are_clamped(given, default, expected):
    assert clamp(given, default) == expected


# --------------------------------------------------------------------------
# Scopes
# --------------------------------------------------------------------------


def test_full_scopes_can_read_mail_and_drive():
    asked = scopes(["mail", "files"])
    assert any("gmail.modify" in s for s in asked)
    assert any(s.endswith("/auth/drive") for s in asked)


def test_the_sign_ins_redirect_line_with_its_one_time_code_stays_out_of_the_log(caplog):
    """The library logs the browser's redirect at INFO, the code that buys
    a token in it, and the worker's log took everything at INFO (step 11's
    audit, E4)."""
    caplog.set_level(logging.INFO)
    logging.getLogger("google_auth_oauthlib.flow").info(
        '"GET /?state=s&code=4/0Ab-canary HTTP/1.1" 200 65')
    assert "canary" not in caplog.text


# --------------------------------------------------------------------------
# Not adding the same thing twice
# --------------------------------------------------------------------------


def test_punctuation_does_not_make_a_new_event():
    """The live failure. The unprompted sweep is told to search the calendar
    before creating anything; on its first real run it added "DES 203
    Workshop #3" beside the "DES 203 Workshop 3" that was already there.

    "Check before you act" is the instruction a model drops on the third
    item of a list, and a doubled calendar is what would make the user turn
    the whole feature off -- so the check is code.
    """
    from agent.calendars import _slug

    assert _slug("DES 203 Workshop #3") == _slug("DES 203 Workshop 3")
    assert _slug("CDO Fall Fridays 3: Designing Your Career") == _slug(
        "CDO Fall Fridays 3 - Designing your career"
    )


def test_two_different_things_at_the_same_time_are_still_two_things():
    """The check has to be narrow enough that it cannot refuse a real event.
    Two genuinely different things at two o'clock have two different names.
    """
    from agent.calendars import _slug

    assert _slug("Dentist") != _slug("Fabrikam coffee chat")


# --------------------------------------------------------------------------
# What each listing asks Google for
# --------------------------------------------------------------------------


def test_every_calendar_listing_asks_for_occurrences_not_series(monkeypatch):
    """A weekly lecture listed as its series is one event, dated the first
    week of term: "what's on today" would not find it, and the duplicate
    check would not see it. Every listing the slot makes goes through
    `_events`, which asks for occurrences; until 11 October 2026 five calls
    each said so, and no test checked any of them."""
    from modules.google_cloud.provider import GoogleCalendar

    asked: list[dict] = []

    class Listing:
        def __init__(self, query):
            asked.append(query)

        def execute(self):
            return {"items": []}

    class Events:
        def list(self, **query):
            return Listing(query)

    class Calendar:
        def events(self):
            return Events()

    monkeypatch.setattr(gcal, "_calendar", lambda: Calendar())
    monkeypatch.setattr(gcal, "_tz", lambda: ZoneInfo("America/New_York"))

    now = dt.datetime.now(ZoneInfo("America/New_York"))
    week = now + dt.timedelta(days=7)
    calendar = GoogleCalendar(fake_google.Context())
    calendar.events(now, week, limit=10)
    calendar.search(["lecture"], now, week, limit=10)
    calendar.changed(now - dt.timedelta(days=1), now, week, limit=10)

    assert len(asked) == 3 and all(query["singleEvents"] is True for query in asked)


def test_a_summary_asks_gmail_for_the_headers_that_tell_a_person_from_a_mailing(monkeypatch):
    """The attention loop's sender check reads List-Id, Precedence and the
    rest (agent/attention/vet.py), and Gmail's metadata call sends only the
    headers asked for by name. The address alone let Trey Research's "Personal
    Invitation from the Trey Research CEO" pass as a person on 9 October 2026."""
    asked: dict = {}

    class Messages:
        def get(self, **query):
            asked.update(query)
            return self

        def execute(self):
            return {"id": "m1", "payload": {"headers": [
                {"name": "From", "value": "Trey Research CEO <ceo@treyresearch.example>"},
                {"name": "List-Id", "value": "<news.treyresearch.example>"}]}}

    class Users:
        def messages(self):
            return Messages()

    class Gmail:
        def users(self):
            return Users()

    monkeypatch.setattr(gmail, "_gmail", lambda: Gmail())

    found = gmail.summary("m1")

    assert set(gmail.MAILING_HEADERS) <= set(asked["metadataHeaders"])
    assert found["list_id"] == "<news.treyresearch.example>"
    assert found["from"].startswith("Trey Research")


def test_a_document_is_built_in_memory_and_never_in_the_shared_temp_folder(monkeypatch):
    """Every program of this Windows user shares %TEMP%, and removing Steve's
    data would miss what a failed upload left there (CLAUDE.md). Until step
    5's item 4f, a new document went through a folder there."""
    import tempfile

    def refused(*args, **kwargs):
        raise AssertionError("a document went through %TEMP%")

    monkeypatch.setattr(tempfile, "TemporaryDirectory", refused)
    monkeypatch.setattr(tempfile, "mkdtemp", refused)
    sent: dict = {}

    class FakeFiles:
        def create(self, **kwargs):
            sent.update(kwargs)
            return self

        def execute(self):
            return {"id": "d1", "name": "Notes", "mimeType": gdrive.GOOGLE_DOC}

    class FakeDrive:
        def files(self):
            return FakeFiles()

    monkeypatch.setattr(gdrive, "_drive", lambda: FakeDrive())

    made = gdrive.create_doc("Notes", "# Plan\n\nSaturday at ten.")

    assert made["created"] is True, made
    media = sent["media_body"]
    assert media.mimetype() == gdrive.DOCX
    assert media.getbytes(0, 2) == b"PK", "a .docx is a zip"
