"""The weather where the user is going, not just where the user lives.

The weather source only ever read Pittsburgh, so a weekend in Boston came with
Pittsburgh's forecast. Trips are read from the calendar; the geocoder's shape
is pinned by a real payload captured on 25 September 2026.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from agent.attention import fetch, trips
from agent.attention.sources import weather
from agent.home import Place
from core import db

FIXTURES = Path(__file__).parent / "fixtures" / "attention"
THU = dt.date(2026, 10, 1)
BOSTON = Place("Boston, Massachusetts", 42.35843, -71.05977, "America/New_York")
HOME = Place("Pittsburgh, Pennsylvania", 40.44062, -79.99589, "America/New_York", "US")
# Two towns of one name, the larger three time zones from the other.
PORTLANDS = {"results": [
    {"name": "Portland", "feature_code": "PPLA2", "country": "United States",
     "country_code": "US", "admin1": "Oregon", "latitude": 45.52345, "longitude": -122.67621,
     "timezone": "America/Los_Angeles", "population": 652503},
    {"name": "Portland", "feature_code": "PPLA2", "country": "United States",
     "country_code": "US", "admin1": "Maine", "latitude": 43.66147, "longitude": -70.25533,
     "timezone": "America/New_York", "population": 66881},
    {"name": "Portland", "feature_code": "PPL", "country": "United Kingdom",
     "country_code": "GB", "admin1": "England", "admin2": "Dorset", "latitude": 50.54577,
     "longitude": -2.44101, "timezone": "Europe/London", "population": 12844},
]}


@pytest.fixture
def geocoder(monkeypatch):
    """The captured Boston answer for Boston, nothing for anything else."""
    asked: list[str] = []
    boston = json.loads((FIXTURES / "open_meteo_geocode.json").read_text(encoding="utf-8"))

    def get_json(url, params=None):
        assert url == trips.GEOCODE
        asked.append(params["name"])
        return boston if params["name"].lower() == "boston" else {}

    monkeypatch.setattr(fetch, "get_json", get_json)
    return asked


# ---------------------------------------------------------------------------
# Reading a place out of an event
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("event", "found"), [
    ({"location": "55 Mill Street, 3rd Floor, Pittsburgh, PA 15213"}, ["Pittsburgh"]),
    ({"location": "Larkspur Hall 2400"}, []),
    ({"location": "https://contoso.zoom.us/j/123"}, []),
    ({"summary": "Trip to Boston"}, ["Boston"]),
    ({"summary": "Lisbon holiday"}, ["Lisbon"]),
    ({"summary": "Sam and Alex's wedding in Savannah"}, ["Savannah"]),
    ({"summary": "Vacation to Lake Tahoe"}, ["Lake Tahoe"]),
    ({"summary": "Flight to San Francisco", "location": "BDL"}, ["BDL", "San Francisco"]),
    ({"location": "It Summit Ropes Course, 40 Ferris Dr, Pittsburgh, PA 15213, "
                  "United States"}, ["Pittsburgh"]),
    ({"location": "Union Station, Toronto, Canada"}, ["Toronto"]),
])
def test_the_place_an_event_names(event, found):
    """A country is not the town: "…, PA 15213, United States" read as the
    town "United States" (8 October 2026)."""
    assert trips.candidates(event) == found


@pytest.mark.parametrize("where", ["Zoom", "Microsoft Teams Meeting", "Online", "Phone call"])
def test_a_meeting_held_online_is_no_place_for_a_trip_or_for_the_rain(where):
    """The trips' places and the weather's warnings each kept a copy of these
    words, so a word added to one would be missed by the other. They read one
    list now (`trips.VIRTUAL`)."""
    assert trips.candidates({"location": where}) == []
    assert not weather._in_person({"location": where})


# Open-Meteo's answer to "United States": it matches alternate names, and the
# largest populated place it gave was Ridgeview, Alabama.
RIDGEVIEW = {"results": [
    {"name": "Ridgeview", "latitude": 31.08518, "longitude": -88.01333, "feature_code": "PPL",
     "country_code": "US", "admin1": "Alabama", "population": 1574,
     "timezone": "America/Chicago"}]}


def test_a_town_whose_name_is_not_the_one_asked_for_is_not_found(conn, monkeypatch):
    """Alabama's hurricane warning woke them at 23:03 on 8 October 2026, for
    a ropes course in Pittsburgh: "United States" had been geocoded to
    Ridgeview, Alabama, a trip more than 80 km from home."""
    monkeypatch.setattr(fetch, "get_json", lambda url, params=None: RIDGEVIEW)

    assert trips.geocode(conn, "United States") is None
    assert trips.geocode(conn, "Ridgeview")[0].name == "Ridgeview, Alabama"


def test_a_wrong_town_already_in_the_cache_is_looked_up_again_and_dropped(conn, monkeypatch):
    """The wrong answer was cached on 30 September, to be kept 180 days."""
    with conn:
        conn.execute("INSERT INTO places (query, found, name, latitude, longitude, timezone,"
                     " country, fetched_at) VALUES ('united states', 1, 'Ridgeview, Alabama',"
                     " 31.08518, -88.01333, 'America/Chicago', 'US', datetime('now', '-9 days'))")
    asked = []

    def get_json(url, params=None):
        asked.append(params["name"])
        return RIDGEVIEW

    monkeypatch.setattr(fetch, "get_json", get_json)

    assert trips.geocode(conn, "United States") is None
    assert asked == ["United States"]
    assert conn.execute("SELECT found FROM places WHERE query = 'united states'").fetchone()[0] == 0


def test_a_town_is_geocoded_once_and_a_room_is_not_a_town(conn, geocoder):
    place, country = trips.geocode(conn, "Boston")
    assert place.name == "Boston, Massachusetts" and country == "US"
    assert trips.geocode(conn, "boston")[0] == place

    assert trips.geocode(conn, "Larkspur Hall") is None
    assert trips.geocode(conn, "Larkspur Hall") is None
    assert geocoder == ["Boston", "Larkspur Hall"], "looked the same name up twice"


def test_a_town_of_a_common_name_is_the_one_in_the_region_the_user_named(conn, monkeypatch):
    """A home was one city, written into the code. Now it is the user's own
    words, "Portland, ME", and the geocoder's largest Portland is Oregon's:
    a forecast for the wrong coast, in the wrong time zone, every morning."""
    monkeypatch.setattr(fetch, "get_json", lambda url, params=None: PORTLANDS)

    for within in ("ME", "Maine", "maine"):
        place, _ = trips.geocode(conn, "Portland", within=within)
        assert (place.name, place.timezone) == ("Portland, Maine", "America/New_York"), within
    assert trips.geocode(conn, "Portland")[0].name == "Portland, Oregon"


@pytest.mark.parametrize(("within", "found"), [
    ("ME, USA", "Portland, Maine"),
    ("Maine, United States", "Portland, Maine"),
    ("OR 97201", "Portland, Oregon"),
    ("USA", "Portland, Oregon"),
    ("UK", "Portland, United Kingdom"),
    ("Dorset, England", "Portland, United Kingdom"),
    ("GB", "Portland, United Kingdom"),
    ("Narnia", None),
    ("Maine, UK", "Portland, Maine"),
])
def test_every_part_of_where_the_user_said_a_town_is_counts(conn, monkeypatch, within, found):
    """"Portland, ME, USA" and "Portland, UK" were both Portland, Oregon:
    the whole of what followed the town had to equal one state's or
    country's name, and when it did not, the largest Portland won. Now
    each part is a region the town must be in, the town in the most of
    them wins, and a region no Portland is in finds none -- a home in
    Narnia is unknown, not Oregon. A postcode says nothing."""
    monkeypatch.setattr(fetch, "get_json", lambda url, params=None: PORTLANDS)

    place = trips.geocode(conn, "Portland", within=within)

    assert (place[0].name if place else None) == found


def test_more_towns_are_asked_for_when_the_user_names_a_region(conn, monkeypatch):
    """A small town of a common name is not among the five largest."""
    asked = []

    def get_json(url, params=None):
        asked.append(params["count"])
        return PORTLANDS

    monkeypatch.setattr(fetch, "get_json", get_json)

    trips.geocode(conn, "Portland")
    trips.geocode(conn, "Portland", within="UK")

    assert asked == [trips.TOWNS_ASKED, trips.TOWNS_ASKED_IN_REGION]
    assert trips.TOWNS_ASKED_IN_REGION > trips.TOWNS_ASKED


def _unreachable(url, params=None):
    raise fetch.FetchError(f"{url}: ConnectError: no route to host")


def test_a_town_placed_long_ago_stays_placed_while_the_geocoder_is_down(conn, monkeypatch):
    """A cached home past its keeping time was looked up again, the lookup
    failed, and home was gone: no weather, and the user asked for the
    nearest town to one the geocoder knew perfectly well."""
    with db.transaction(conn):
        conn.execute("INSERT INTO places (query, found, name, latitude, longitude, timezone,"
                     " country, fetched_at) VALUES ('portland, me', 1, 'Portland, Maine',"
                     " 43.66147, -70.25533, 'America/New_York', 'US',"
                     " datetime('now', ?))", (f"-{trips.KEEP_FOUND_DAYS + 20} days",))
    monkeypatch.setattr(fetch, "get_json", _unreachable)

    place, country = trips.geocode(conn, "Portland", within="ME")

    assert (place.name, country) == ("Portland, Maine", "US")


def test_an_unreachable_geocoder_is_not_an_unknown_town(conn, monkeypatch):
    """Unknown is something the user can fix; unreachable is not, and asking
    them for "the nearest town" during an outage sent them looking for a
    mistake they had not made."""
    from agent import facts, home
    from agent.toolset import weather as weather_tool

    facts.add(conn, facts.USER, "home_city", "Portland, ME", source="confirmed")
    monkeypatch.setattr(fetch, "get_json", _unreachable)

    with pytest.raises(fetch.FetchError):
        home.home(conn)
    assert home.placed(conn)["status"] == "unreachable"
    said = weather_tool.lookup(conn)["error"]
    assert "could not look up" in said and "nearest town" not in said
    assert conn.execute("SELECT COUNT(*) FROM places").fetchone()[0] == 0, "nothing cached"

    monkeypatch.setattr(fetch, "get_json", lambda url, params=None: PORTLANDS)
    assert home.placed(conn) == {"status": "placed", "place": "Portland, Maine",
                                 "says": "The weather is for Portland, Maine."}


def test_a_home_no_town_of_that_name_is_in_is_said_to_be_unknown(conn, monkeypatch):
    from agent import facts, home

    assert home.placed(conn)["status"] == "none"
    facts.add(conn, facts.USER, "home_city", "Portland, Narnia", source="confirmed")
    monkeypatch.setattr(fetch, "get_json", lambda url, params=None: PORTLANDS)

    assert home.home(conn) is None
    assert home.placed(conn)["status"] == "unknown"


class _Reply:
    """What a captive portal answers a JSON request with."""

    status_code = 200
    text = "<html><body>Sign in to the network</body></html>"

    def json(self):
        raise ValueError("Expecting value: line 1 column 1 (char 0)")


def test_a_reply_that_is_not_json_is_a_feed_that_could_not_be_read(monkeypatch):
    """It raised a decoding error that no reader caught, so a sign-in page on
    a café's network failed the whole weather source with a traceback."""
    monkeypatch.setattr(fetch, "_get", lambda *args, **kwargs: _Reply())

    with pytest.raises(fetch.FetchError, match="not JSON"):
        fetch.get_json(trips.GEOCODE, {"name": "Portland"})

    _Reply.json = lambda self: ["a list"]
    with pytest.raises(fetch.FetchError, match="not a JSON object"):
        fetch.get_json(trips.GEOCODE, {"name": "Portland"})


def test_home_is_the_city_the_user_gave_placed_where_they_said(conn, monkeypatch):
    from agent import facts, home

    monkeypatch.setattr(fetch, "get_json", lambda url, params=None: PORTLANDS)
    assert home.home(conn) is None, "no home until the user gives one"

    facts.add(conn, facts.USER, "home_city", "Portland, ME", source="confirmed")

    assert home.home(conn) == Place("Portland, Maine", 43.66147, -70.25533,
                                    "America/New_York", "US")


def test_a_park_or_a_building_is_not_a_town(conn, monkeypatch):
    """Populated places only: the geocoder also knows parks, stations and
    buildings, and a room name sometimes matches one of those."""
    monkeypatch.setattr(fetch, "get_json", lambda url, params=None: {"results": [
        {"name": "New York State Reservation", "latitude": 43.0, "longitude": -79.0,
         "feature_code": "PRK", "country_code": "US", "population": 0,
         "timezone": "America/New_York"}]})

    assert trips.geocode(conn, "State Reservation") is None


# ---------------------------------------------------------------------------
# Trips
# ---------------------------------------------------------------------------


def test_a_weekend_away_is_one_trip_and_a_class_at_home_is_none(conn, geocoder, monkeypatch):
    monkeypatch.setattr(trips, "geocode", lambda c, name: {
        "boston": (BOSTON, "US"),
        "pittsburgh": (Place("Pittsburgh, Pennsylvania", 40.4, -80.0, "America/New_York"), "US"),
    }.get(name.lower()))
    events = [
        # All-day, Thu to Sat: Google's end date is the Sunday, exclusive.
        {"summary": "Trip to Boston", "start": "2026-10-01", "end": "2026-10-04", "all_day": True},
        {"summary": "Dinner", "start": "2026-10-03T19:00:00-04:00",
         "end": "2026-10-03T21:00:00-04:00", "location": "Row 34, Boston, MA"},
        {"summary": "DES 203", "start": "2026-10-01T09:00:00-04:00",
         "end": "2026-10-01T10:30:00-04:00", "location": "Larkspur Hall, Pittsburgh, PA"},
        {"summary": "Trip to Boston", "start": "2026-11-20", "end": "2026-11-22", "all_day": True},
    ]

    [trip] = trips.upcoming(conn, events, THU, HOME)

    assert trip.place == BOSTON and trip.country == "US"
    assert (trip.start, trip.end, trip.span) == (THU, dt.date(2026, 10, 3), "Thu-Sat")
    assert trip.events == ["Trip to Boston", "Dinner"]


def test_a_trip_already_under_way_starts_today(conn, monkeypatch):
    monkeypatch.setattr(trips, "geocode", lambda c, name: (BOSTON, "US"))
    events = [{"summary": "Trip to Boston", "start": "2026-09-29", "end": "2026-10-03",
               "all_day": True}]

    [trip] = trips.upcoming(conn, events, THU, HOME)

    assert trip.start == THU and trip.end == dt.date(2026, 10, 2)


# ---------------------------------------------------------------------------
# The weather there
# ---------------------------------------------------------------------------


def _day(date, *, rain=10, low=48.0, high=60.0, code=3):
    """In °F, as these were written; kept metric."""
    return weather.Day(day=date, high_c=(high - 32) * 5 / 9, low_c=(low - 32) * 5 / 9,
                       rain_chance=rain, rain_mm=0.0, code=code, hours=[])


def test_rain_on_a_day_away_is_said_and_the_forecast_kept(conn, monkeypatch):
    trip = trips.Trip(BOSTON, THU, dt.date(2026, 10, 3), ["Trip to Boston"], "US",
                      verified=True, why="the event is a trip")
    ahead = [_day(THU), _day(dt.date(2026, 10, 2), rain=80), _day(dt.date(2026, 10, 3), low=30.0),
             _day(dt.date(2026, 10, 4), rain=90)]
    monkeypatch.setattr(weather, "forecast", lambda place, days=3: ahead)
    monkeypatch.setattr(weather, "alerts", lambda place: [])

    found = weather.trip_observations(conn, [trip], THU, "us")

    keys = sorted(o.key for o in found)
    assert keys == ["weather:trip:boston-massachusetts:2026-10-02:rain",
                    "weather:trip:boston-massachusetts:2026-10-03:freezing"], "or said Sunday"
    rain = next(o for o in found if o.key.endswith(":rain"))
    assert rain.title == "Rain likely in Boston on Fri Oct 2, while the user is there (Thu-Sat)"

    [line] = weather.trip_lines(conn, THU)
    assert line.startswith("Boston, Massachusetts: Thu: 48-60°F")
    assert line.count(";") == 2, "three days, not four"


def test_a_warning_where_the_user_is_going_is_read_two_days_out(conn, monkeypatch):
    alert = json.loads((FIXTURES / "nws_alerts.json").read_text(encoding="utf-8"))
    asked: list[Place] = []
    monkeypatch.setattr(weather, "forecast", lambda place, days=3: [])
    monkeypatch.setattr(weather, "alerts", lambda place: asked.append(place) or [
        f["properties"] for f in alert["features"]])
    soon = trips.Trip(BOSTON, THU, THU, ["Interview"], "US", verified=True)
    later = trips.Trip(BOSTON, THU + dt.timedelta(days=5), THU + dt.timedelta(days=5), ["x"], "US",
                       verified=True)
    abroad = trips.Trip(Place("London, United Kingdom", 51.5, -0.1, "Europe/London"),
                        THU, THU, ["x"], "GB", verified=True)

    found = weather.trip_observations(conn, [soon, later, abroad], THU, "us")

    assert asked == [BOSTON], "US alerts only, and only for a trip two days off"
    assert found and all("in Boston, where the user is going (Thu)" in o.title for o in found)
