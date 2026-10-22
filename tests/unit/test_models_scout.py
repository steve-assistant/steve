"""The model scout reads OpenAI's list the way it is written.

What it finds goes to the frozen bounds (tests/gate/test_model_scout.py), so
what matters here is what it hands over and what it leaves out: OpenAI's own
names, with the maker in front, as the ledger keys them; only the last
year's models; no dated snapshot, fine-tune or tier; and nothing asked
without the key. Until 12 October 2026 it read OpenRouter's catalog too.
"""

from __future__ import annotations

import json
import time

import pytest

from agent import models_scout as scout
from agent.models import MODELS, Role
from core import db, endpoints, models_guard

NOW = 1_790_000_000.0
DAY = 86_400

LISTED = {"object": "list", "data": [
    {"id": "gpt-7-luna", "object": "model", "created": NOW - DAY, "owned_by": "system"},
    {"id": "gpt-7-sol", "created": NOW - 2 * DAY},
    {"id": "gpt-7-luna-2026-10-01", "created": NOW - DAY},  # a dated snapshot
    {"id": "ft:gpt-7-luna:acme::x1", "created": NOW - DAY},  # someone's fine-tune
    {"id": "gpt-6-luna", "created": NOW - 30 * DAY},
    {"id": "gpt-4o", "created": NOW - 900 * DAY},  # older than a year
    {"id": "", "created": NOW},
    {"id": "gpt-7-mini", "created": "soon"},  # no date it can read: too old to trust
]}


class Listing:
    """OpenAI's list, and how it was asked for."""

    def __init__(self, body=None):
        self.body, self.asked = body or LISTED, []

    def __call__(self, url, headers=None):
        self.asked.append((url, headers or {}))
        return self.body


@pytest.fixture(autouse=True)
def key(monkeypatch):
    keys = {"OPENAI_API_KEY": "sk-test"}
    monkeypatch.setattr(scout.secrets, "get", lambda name, default=None: keys.get(name, default))
    return keys


@pytest.fixture
def next_luna(monkeypatch):
    """GPT-7 Luna in the guard's frozen table, at half Luna's price."""
    monkeypatch.setitem(models_guard.PRICED, "openai/gpt-7-luna", models_guard.Priced(
        0.05, 0.005, 0.25, context=1_050_000, max_output=128_000,
        efforts=("none", "low", "medium", "high")))


def test_only_the_last_years_models_themselves_are_gathered_under_openais_name():
    found, listed = scout.gather(Listing(), now=NOW)

    assert listed == len(LISTED["data"])
    assert [(c.slug, released) for c, released in found] == [
        ("openai/gpt-7-luna", time.strftime("%Y-%m-%d", time.gmtime(NOW - DAY))),
        ("openai/gpt-7-sol", time.strftime("%Y-%m-%d", time.gmtime(NOW - 2 * DAY))),
        ("openai/gpt-6-luna", time.strftime("%Y-%m-%d", time.gmtime(NOW - 30 * DAY))),
    ]


def test_the_list_is_asked_for_with_their_key_and_not_at_all_without_it(key):
    listing = Listing()
    scout.gather(listing, now=NOW)
    assert listing.asked == [(f"{endpoints.OPENAI}/models", {"Authorization": "Bearer sk-test"})]

    key.clear()
    listing = Listing()
    with pytest.raises(scout.ScoutError, match="OPENAI_API_KEY"):
        scout.gather(listing, now=NOW)
    assert listing.asked == []


def test_an_empty_list_is_nothing_found_not_a_crash():
    assert scout.gather(Listing({"data": None}), now=NOW) == ([], 0)


def test_a_scan_records_what_it_saw_and_names_only_the_new(monkeypatch, next_luna):
    monkeypatch.setattr(scout.time, "time", lambda: NOW)
    conn = db.open_store()
    try:
        first = scout.scan(conn, Listing())
        assert sorted(first["new"]) == ["openai/gpt-6-luna", "openai/gpt-7-luna",
                                        "openai/gpt-7-sol"]
        assert scout.scan(conn, Listing())["new"] == []
        rows = {r["slug"]: r for r in conn.execute(
            "SELECT slug, roles, eligible, facts FROM model_candidates")}
        # Half Luna's price, with the efforts every rung asks for.
        assert rows["openai/gpt-7-luna"]["eligible"] == 5
        assert json.loads(rows["openai/gpt-7-luna"]["facts"]) == {"slug": "openai/gpt-7-luna"}
        # No price in the frozen table: judged unable to play anything.
        sol = json.loads(rows["openai/gpt-7-sol"]["roles"])
        assert rows["openai/gpt-7-sol"]["eligible"] == 0
        assert all("frozen table" in why for why in sol.values())
        # What every role uses now is not a candidate for it.
        assert rows["openai/gpt-6-luna"]["eligible"] == 0
    finally:
        conn.close()


def test_the_report_lists_each_role_what_may_replace_it_and_what_has_no_price(
        monkeypatch, next_luna):
    monkeypatch.setattr(scout.time, "time", lambda: NOW)
    text = scout.render(scout.scan(None, Listing()))

    assert f"converse -- now {MODELS[Role.CONVERSE].slug}" in text
    assert "openai/gpt-7-luna" in text and "0.05/0.25 per M, 0.50x, none" in text
    unpriced = text.split("Not judged, with no price")[1]
    assert "openai/gpt-7-sol" in unpriced and "openai/gpt-7-luna" not in unpriced
