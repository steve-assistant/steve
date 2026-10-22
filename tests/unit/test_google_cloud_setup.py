"""Google as an account like any other: added from its manifest, signed in
for its own classes, kept as a refresh token, and its sign-in's end and
its errors said as the account's health (step 5, item 9).

Until item 9 there was one Google connection, standing in as an account
with a fixed key: signed in from a command line for every scope at once,
its whole token kept, its seven-day end guessed from a file, and its errors
said as "run python -m ... login". These hold what replaced it.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json

import pytest
from google.auth.exceptions import RefreshError
from google_auth_oauthlib.flow import WSGITimeoutError
from googleapiclient.errors import HttpError

from agent import accounts, connectors
from core import credstore, db, health, modules
from modules.google_cloud import auth
from modules.google_cloud import common as google_common
from modules.google_cloud import tasks as google_tasks
from steve_sdk.errors import ConnectorError
from steve_sdk.manifest import SIGN_IN
from tests.fakes import google as fake

SUNDAY = dt.datetime(2026, 11, 1, 12, 0, tzinfo=dt.UTC)


@pytest.fixture()
def conn(tmp_path):
    found = db.open_store(tmp_path / "steve.db")
    modules.register(found)
    google_common._service.cache_clear()
    yield found
    found.close()


def _kept(conn, key: str = fake.KEY, name: str = SIGN_IN) -> str | None:
    held = modules.module(conn, "google_cloud")
    return credstore.read(credstore.target(held.key, key, name))


def _sign_in(conn, monkeypatch, flow: fake.Flow, key: str = fake.KEY) -> dict:
    monkeypatch.setattr(auth, "InstalledAppFlow", flow)
    return asyncio.run(accounts.sign_in(conn, key))


class _Api:
    """Google's client as `common.build` makes it, answering one call: a
    profile, a list, or an error. Records the credentials it was built with."""

    def __init__(self, *, fail: Exception | None = None) -> None:
        self.fail = fail
        self.built: list[tuple[str, object]] = []

    def build(self, api, version, *, credentials=None, **_kw):
        self.built.append((api, credentials))
        return self

    def __getattr__(self, name):
        return lambda *a, **kw: self

    def execute(self, *a, **kw):
        if self.fail is not None:
            raise self.fail
        return {"emailAddress": fake.ADDRESS, "items": [], "files": []}


def _http_error(status: int, body: dict) -> HttpError:
    class _Resp(dict):
        def __init__(self) -> None:
            super().__init__(status=str(status))
            self.status, self.reason = status, "Forbidden"

    return HttpError(_Resp(), json.dumps(body).encode())


def test_a_sign_in_asks_only_for_the_accounts_own_classes_with_its_address_as_the_hint(
        conn, monkeypatch):
    """Every scope at once asked a user who wanted only their calendar to
    let Steve read their mail too; and without the hint, a browser signed in
    to another account offered that one first."""
    fake.add(conn, classes=["calendar"])
    flow = fake.Flow()
    _sign_in(conn, monkeypatch, flow)
    assert flow.scopes == [*auth.IDENTITY, "https://www.googleapis.com/auth/calendar"]
    assert flow.asked["login_hint"] == fake.ADDRESS
    assert flow.asked["access_type"] == "offline" and flow.asked["prompt"] == "consent"
    assert flow.config["installed"]["client_id"] == fake.CLIENT_ID


def test_a_sign_in_as_another_address_is_refused_and_nothing_is_kept(conn, monkeypatch):
    """The account is an address: a sign-in as another would put someone
    else's mailbox, or the user's other one, under this account's name."""
    fake.add(conn)
    with pytest.raises(modules.AccountError, match="sign in again as"):
        _sign_in(conn, monkeypatch, fake.Flow(address="someone@example.org"))
    assert _kept(conn) is None


def test_a_sign_in_as_the_accounts_gmail_written_with_its_dots_is_the_accounts(
        conn, monkeypatch):
    """Google says the address as it was made, dots and all; the user may
    have typed it without, and Gmail delivers both to one mailbox. Refused,
    the sign-in could never be kept, whichever way they signed in."""
    fake.add(conn, address="mayalindqvist@gmail.com")
    _sign_in(conn, monkeypatch, fake.Flow(address="maya.lindqvist@gmail.com"))
    assert _kept(conn) is not None


def test_an_unverified_address_is_not_taken_as_the_accounts(conn, monkeypatch):
    """Google marks an address it has not verified; that is no proof of who
    signed in."""
    fake.add(conn)
    flow = fake.Flow()
    flow.answer["id_token"] = fake.id_token(fake.ADDRESS, verified=False)
    with pytest.raises(modules.AccountError):
        _sign_in(conn, monkeypatch, flow)
    assert _kept(conn) is None


def test_only_the_refresh_token_and_its_end_are_kept(conn, monkeypatch):
    """The whole token, as google-auth writes it, holds the access token and
    the client's secret again, and can pass the Credential Manager's 2,560
    bytes; the access token is an hour's, and stays in memory."""
    fake.add(conn)
    _sign_in(conn, monkeypatch, fake.Flow())
    kept = json.loads(_kept(conn) or "")
    assert set(kept) == {"refresh_token", "ends"}
    assert kept["refresh_token"] == fake.REFRESH
    assert "ya29" not in (_kept(conn) or "")


def test_the_client_file_is_kept_as_its_id_secret_and_project_and_nothing_else(conn):
    """A client file holds its secret: it goes to the Credential Manager,
    as its three parts, and no file or path is kept."""
    fake.add(conn)
    assert json.loads(_kept(conn, name=auth.CLIENT) or "") == {
        "client_id": fake.CLIENT_ID, "client_secret": fake.CLIENT_SECRET,
        "project_id": fake.PROJECT}
    row = modules.account(conn, fake.KEY)
    assert fake.CLIENT_SECRET not in json.dumps(row.fields)


def test_a_web_client_is_refused_when_its_file_is_given(conn):
    """A Web application client's sign-in cannot come back to this
    computer: refused at the file, not after a browser trip."""
    with pytest.raises(modules.AccountError, match="Desktop app"):
        accounts.add(conn, module_id="google_cloud", address=fake.ADDRESS,
                     values={"client": fake.client_file("web")})
    assert modules.account(conn, fake.KEY) is None


def test_a_client_in_testing_shows_its_end_as_health_and_is_said_a_day_before(
        conn, monkeypatch):
    """Google ends a Testing client's sign-in after seven days, and says
    so; the user hears it the day before, once, not as a broken morning."""
    fake.add(conn, classes=["tasks"])
    monkeypatch.setattr(google_common, "build", _Api().build)
    _sign_in(conn, monkeypatch, fake.Flow(ends_in_s=7 * 86_400))
    found = health.read(conn, fake.KEY)
    assert found is not None and found.state == health.WORKING
    assert found.expires_at is not None
    day_before = found.expires_at - dt.timedelta(hours=20)
    said = health.due(found, "gmail", day_before)
    assert said is not None and "sign-in ends" in said[1]
    assert health.due(found, "gmail", found.expires_at - dt.timedelta(days=3)) is None


def test_a_published_client_has_no_end(conn, monkeypatch):
    fake.add(conn, classes=["tasks"])
    monkeypatch.setattr(google_common, "build", _Api().build)
    _sign_in(conn, monkeypatch, fake.Flow(ends_in_s=None))
    assert set(json.loads(_kept(conn) or "")) == {"refresh_token"}
    assert health.read(conn, fake.KEY).expires_at is None


def test_a_lapsed_sign_in_puts_the_account_in_needs_you_sign_in_again(conn, monkeypatch):
    """Refused by Google's token endpoint: the one action is to sign in
    again, said where the user does it."""
    fake.signed_in(conn, classes=["tasks"])
    monkeypatch.setattr(google_common, "build",
                        _Api(fail=RefreshError("invalid_grant: Token has been expired")).build)
    tested = asyncio.run(accounts.test(conn, fake.KEY))
    assert not tested["ok"] and tested["state"] == health.NEEDS_YOU
    assert "sign in again" in tested["health"]


def test_an_api_not_turned_on_opens_its_page_in_the_users_own_project(conn, monkeypatch):
    """The commonest first failure: the client is made, the API is not on.
    The fix is one page in the user's own project, so that is what it opens."""
    fake.signed_in(conn, classes=["mail"])
    body = {"error": {"code": 403, "message": "Gmail API has not been used in project "
                      "584031 before or it is disabled.", "status": "PERMISSION_DENIED",
                      "errors": [{"reason": "accessNotConfigured"}],
                      "details": [{"reason": "SERVICE_DISABLED",
                                   "metadata": {"service": "gmail.googleapis.com"}}]}}
    monkeypatch.setattr(google_common, "build", _Api(fail=_http_error(403, body)).build)
    tested = asyncio.run(accounts.test(conn, fake.KEY))
    assert not tested["ok"]
    assert tested["fix"] == ("https://console.cloud.google.com/apis/library/"
                             f"gmail.googleapis.com?project={fake.PROJECT}")
    assert "not turned on" in tested["detail"]


def test_a_sign_in_that_does_not_finish_opens_the_test_users_page(conn, monkeypatch):
    """A user who is not a test user of a client in Testing is stopped on
    Google's own page, which never comes back: the wait runs out, and the
    fix is the project's test users."""
    fake.add(conn)
    with pytest.raises(ConnectorError) as said:
        _sign_in(conn, monkeypatch, fake.Flow(fail=WSGITimeoutError("timed out")))
    assert said.value.fix == f"https://console.cloud.google.com/auth/audience?project={fake.PROJECT}"
    assert fake.ADDRESS in said.value.detail
    assert _kept(conn) is None


def test_two_google_accounts_each_call_with_their_own_sign_in(conn, monkeypatch):
    """One fixed token served every call before: a second account would
    have read the first one's mail."""
    fake.add(conn, classes=["tasks"])
    fake.keep_sign_in(conn, refresh="1//first")
    other = fake.add(conn, address="maya.work@example.org", classes=["tasks"],
                     key="0a1b2c3d4e5f")
    fake.keep_sign_in(conn, other, refresh="1//second")
    api = _Api()
    monkeypatch.setattr(google_common, "build", api.build)
    for found in connectors.accounts(conn, "tasks"):
        found.provider.lists()
    assert sorted(c.refresh_token for _, c in api.built) == ["1//first", "1//second"]


def test_signing_in_again_is_used_at_the_next_call(conn, monkeypatch):
    """On 26 September 2026 a new sign-in changed nothing until a restart:
    every cached client kept refreshing the dead token."""
    fake.signed_in(conn, classes=["tasks"])
    api = _Api()
    monkeypatch.setattr(google_common, "build", api.build)
    fake.provider(conn, "tasks").lists()
    fake.keep_sign_in(conn, refresh="1//again")
    fake.provider(conn, "tasks").lists()
    assert [c.refresh_token for _, c in api.built] == [fake.REFRESH, "1//again"]


def test_the_tasks_list_steve_writes_to_is_named_steve(conn, monkeypatch):
    """Its own list, named for the product, so nothing disturbs a list the
    user keeps by hand."""
    assert google_tasks.LIST_TITLE == "Steve"


def test_adding_an_account_that_needs_its_sign_in_does_not_test_it_yet(conn):
    """Tested before its sign-in, a new account would start in "needs you"."""
    fake.add(conn)
    assert health.read(conn, fake.KEY) is None


def test_a_modules_code_runs_here_only_from_the_sources_own_modules_folder(tmp_path):
    """Only a built-in's code runs in the backend; a module the user installs
    runs its code in its sandbox (step 6). Until item 10 a built-in also had
    to be listed as taking its context, which Outlook's did not; the list
    went when the last one did, and the folder is the whole test."""
    import dataclasses

    from core.engines import code

    built_in = modules.builtins()["outlook_classic"]
    elsewhere = dataclasses.replace(
        built_in, path=tmp_path / "outlook_classic" / "manifest.toml")
    assert code.ready(built_in) and code.ready(modules.builtins()["google_cloud"])
    assert not code.ready(elsewhere)
