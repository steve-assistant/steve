"""The forecast, asked for directly rather than searched for.

One turn in fourteen of their was about the weather (the month to 6 October
2026), and each went to a web search: ~20 s, three weather sites' pages,
several of which refuse a fetch. The `weather` tool reads the forecast the
attention loop already uses. The feeds are faked here, by URL; the hours are
built from the real clock, so the answer is the same at any time of day.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from agent.attention import fetch
from agent.home import Place
from agent.toolset import weather as tool


def _forecast(zone: str, wet_tomorrow=(14, 17)) -> dict:
    """Two days of hourly forecast from the start of today, in `zone`, with
    rain likely tomorrow afternoon."""
    today = dt.datetime.now(ZoneInfo(zone)).date()
    start = dt.datetime.combine(today, dt.time())
    hours = [start + dt.timedelta(hours=i) for i in range(48)]
    wet = [h.date() != today and wet_tomorrow[0] <= h.hour <= wet_tomorrow[1] for h in hours]
    return {
        "hourly": {
            "time": [h.isoformat(timespec="minutes") for h in hours],
            "temperature_2m": [55.0 + (h.hour % 12) for h in hours],
            "precipitation_probability": [80 if w else 5 for w in wet],
            "precipitation": [0.1 if w else 0.0 for w in wet],
            "weather_code": [61 if w else 1 for w in wet],
        },
        "daily": {
            "time": [today.isoformat(), (today + dt.timedelta(days=1)).isoformat()],
            "temperature_2m_max": [66.0, 61.0], "temperature_2m_min": [50.0, 48.0],
            "precipitation_probability_max": [5, 80], "precipitation_sum": [0.0, 0.4],
            "weather_code": [1, 61],
        },
    }


@pytest.fixture
def feeds(monkeypatch):
    asked: list[str] = []
    answers: dict = {}

    def get_json(url, params=None, **kwargs):
        asked.append(url)
        answer = answers.get(url)
        if isinstance(answer, Exception):
            raise answer
        return answer if answer is not None else {}

    monkeypatch.setattr(fetch, "get_json", get_json)
    return asked, answers


HOME = Place("Pittsburgh, Pennsylvania", 40.44062, -79.99589, "America/New_York", "US")


@pytest.fixture
def at_home(monkeypatch):
    from agent import home

    monkeypatch.setattr(home, "home", lambda conn: HOME)


def test_rain_tomorrow_at_home_is_answered_from_the_forecast(feeds, conn, at_home):
    from agent.attention.sources import weather

    asked, answers = feeds
    answers[weather.OPEN_METEO] = _forecast(HOME.timezone)
    answers[weather.NWS_ALERTS] = {"features": [{"properties": {
        "event": "Wind Advisory", "headline": "Wind Advisory until 8 PM",
        "ends": "2026-10-07T20:00"}}]}

    said = tool.lookup(conn, "", 2)

    assert said["place"] == HOME.name and len(said["days"]) == 2
    assert "rain likely 2pm-6pm (80%)" in said["days"][1]["summary"]
    assert said["days"][1]["rain_chance"] == 80
    assert len(said["next_hours"]) == tool.NEXT_HOURS
    assert said["alerts"] == [{"event": "Wind Advisory", "headline": "Wind Advisory until 8 PM",
                               "until": "2026-10-07T20:00"}]
    assert "National Weather Service" in said["source"]


def test_a_town_abroad_is_looked_up_and_has_no_us_alerts(feeds, conn):
    from agent.attention import trips
    from agent.attention.sources import weather

    asked, answers = feeds
    answers[trips.GEOCODE] = {"results": [{
        "name": "Paris", "country": "France", "country_code": "FR", "feature_code": "PPLC",
        "latitude": 48.85, "longitude": 2.35, "timezone": "Europe/Paris", "population": 2_000_000}]}
    answers[weather.OPEN_METEO] = _forecast("Europe/Paris")

    said = tool.lookup(conn, "Paris", 2)

    assert said["place"] == "Paris, France" and "alerts" not in said
    assert weather.NWS_ALERTS not in asked, "the NWS covers the US only"


def test_a_place_the_geocoder_does_not_know_is_said_plainly(feeds, conn):
    from agent.attention import trips

    _, answers = feeds
    answers[trips.GEOCODE] = {"results": []}

    said = tool.lookup(conn, "Nowhereville Flats", 3)

    assert "not a town" in said["error"] and "search the web" in said["error"]


def test_with_no_home_the_user_is_asked_where_and_nothing_is_fetched(feeds, conn):
    """The forecast "at home" was one city's. For a user who has not said
    where they live, it is a question back -- never someone else's town."""
    asked, _ = feeds

    said = tool.lookup(conn, "", 3)

    assert "has not said where they live" in said["error"] and asked == []


def test_a_home_the_geocoder_cannot_place_is_said_as_such(feeds, conn):
    from agent import facts
    from agent.attention import trips

    asked, answers = feeds
    answers[trips.GEOCODE] = {"results": []}
    facts.add(conn, facts.USER, "home_city", "Nowhereville Flats", source="confirmed")

    said = tool.lookup(conn, "", 3)

    assert "'Nowhereville Flats', where the user lives" in said["error"]
    assert asked == [trips.GEOCODE]


def test_a_feed_that_fails_is_an_answer_not_a_broken_turn(feeds, conn, at_home):
    from agent.attention.sources import weather

    _, answers = feeds
    answers[weather.OPEN_METEO] = fetch.FetchError("open-meteo: HTTP 503")

    said = tool.lookup(conn, "", 3)

    assert said["error"].startswith("the forecast could not be read")


def test_the_weather_is_a_lookup_that_runs_alongside_others_and_is_reused():
    from agent.toolset import pace

    assert pace.group_of("weather") is None and "weather" in pace.READ
