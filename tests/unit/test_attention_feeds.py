"""The public feeds: weather, alerts and news, from real captured payloads.

The fixtures in tests/unit/fixtures/attention/ are responses captured from
the live endpoints on 24 September 2026 and trimmed (protocols §11): an
Open-Meteo forecast for the user's town, a real NWS Flood Watch (from Texas -- there
was none in Pennsylvania), and four World headlines from Google News. The
live feeds themselves are refused in unit tests (tests/unit/conftest.py),
so these fake `fetch.get_json`/`get_text`, above that seam.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from agent.attention import fetch, interests, settings
from agent.attention.sources import SourceError, weather
from agent.attention.sources import news as news_source
from agent.home import Place

FIXTURES = Path(__file__).parent / "fixtures" / "attention"
FORECAST = json.loads((FIXTURES / "open_meteo.json").read_text(encoding="utf-8"))
ALERTS = json.loads((FIXTURES / "nws_alerts.json").read_text(encoding="utf-8"))
WORLD = (FIXTURES / "google_news_world.xml").read_text(encoding="utf-8")

# The morning the fixtures were captured, at home.
CAPTURED = dt.datetime(2026, 9, 24, 11, 0)
HOME = Place("Pittsburgh, Pennsylvania", 40.44062, -79.99589, "America/New_York", "US")
SLUG = "pittsburgh-pennsylvania"
ZONE = ZoneInfo(HOME.timezone)


@pytest.fixture
def following(conn):
    """Three of Google News's sections, followed as a user would."""
    for topic in ("world news", "business", "technology"):
        assert interests.follow(conn, topic)["ok"]
    return conn


@pytest.fixture
def at_home(monkeypatch):
    """A home the forecast fixtures were captured for."""
    from agent import home

    monkeypatch.setattr(home, "home", lambda conn: HOME)
    return HOME


# --------------------------------------------------------------------------
# The forecast
# --------------------------------------------------------------------------


def test_the_forecast_is_read_as_three_days_of_hours():
    days = weather.parse_forecast(FORECAST)

    assert [d.day.isoformat() for d in days] == ["2026-09-24", "2026-09-25", "2026-09-26"]
    assert all(len(d.hours) == 24 for d in days)
    assert days[2].rain_chance == 69 and days[2].rain_mm > 5 * 25.4


def test_a_day_reads_as_one_line():
    saturday = weather.parse_forecast(FORECAST)[2]

    line = saturday.line("us")
    assert line.startswith("Sat: 54-55°F, heavy rain") and "rain likely" in line


def test_the_forecast_is_kept_metric_whatever_units_the_feed_was_asked_in(monkeypatch):
    """The fixture was captured in °F and inches, and says so; the feed is
    now asked for °C and millimetres. Either way the numbers kept are
    metric, so the thresholds are one set."""
    asked = []
    monkeypatch.setattr(fetch, "get_json", lambda url, params=None: asked.append(params) or {})
    weather.forecast(HOME)
    assert (asked[0]["temperature_unit"], asked[0]["precipitation_unit"]) == ("celsius", "mm")

    saturday = weather.parse_forecast(FORECAST)[2]

    daily = FORECAST["daily"]
    assert saturday.low_c == pytest.approx((daily["temperature_2m_min"][2] - 32) * 5 / 9)
    assert saturday.rain_mm == pytest.approx(daily["precipitation_sum"][2] * 25.4)
    assert saturday.line("metric").startswith("Sat: 12-13°C, heavy rain")
    metric = {**FORECAST, "daily_units": {}, "hourly_units": {}}
    assert weather.parse_forecast(metric)[2].high_c == FORECAST["daily"]["temperature_2m_max"][2]


@pytest.mark.parametrize(("chosen", "country", "said_in"), [
    ("auto", "US", "us"), ("auto", "PR", "us"), ("auto", "GB", "metric"),
    ("auto", "DE", "metric"), ("auto", "", "metric"),
    ("metric", "US", "metric"), ("us", "GB", "us"),
])
def test_the_weather_is_said_in_the_units_of_where_the_user_lives_unless_they_choose(
        monkeypatch, chosen, country, said_in):
    """It was °F and inches for everyone: a user in Leeds was told it would
    be 41°F."""
    monkeypatch.setattr(settings, "UNITS", chosen)

    assert weather.units(country) == said_in


def test_below_zero_reads_as_a_range():
    assert weather.temps(-3.4, 1.2, "metric") == "-3 to 1°C"
    assert weather.temps(-10, -2, "us") == "14-28°F"
    assert (weather.rain(30, "metric"), weather.rain(2.5, "metric")) == ("30 mm", "2.5 mm")
    assert (weather.rain(30, "us"), weather.rain(2.54, "us")) == ("1.2 in", "0.10 in")


def _f(degrees: float) -> float:
    return (degrees - 32) * 5 / 9


def _day(date, *, high=70, low=55, rain=0, rain_in=0.0, code=3, wet=()):
    """A day given in °F and inches, as the captured feed was; kept metric."""
    hours = [
        weather.Hour(
            at=dt.datetime.combine(date, dt.time(h)), temp_c=_f(high - 5),
            rain_chance=80 if h in wet else 5, rain_mm=2.54 if h in wet else 0.0, code=code,
        )
        for h in range(24)
    ]
    return weather.Day(date, _f(high), _f(low), rain, rain_in * 25.4, code, hours)


def test_the_wet_hours_are_the_span_of_likely_rain():
    day = _day(dt.date(2026, 9, 25), rain=80, wet=(14, 15, 16, 17))

    assert day.wet_hours == "2pm-6pm"


def test_rain_into_midnight_says_midnight_and_a_wet_day_says_all_day():
    """A span ending at midnight was printed "12am", so a wet day read
    "rain likely 12am-12am" in a briefing."""
    evening = _day(dt.date(2026, 9, 25), rain=80, wet=(20, 21, 22, 23))
    wet_day = _day(dt.date(2026, 9, 25), rain=80, wet=range(24))

    assert evening.wet_hours == "8pm-midnight"
    assert wet_day.wet_hours == "all day"
    assert wet_day.line("us").endswith("rain likely all day (80%)")


# --------------------------------------------------------------------------
# What is notable about a day
# --------------------------------------------------------------------------

TODAY = dt.date(2026, 9, 24)


def test_the_captured_forecast_notices_saturdays_storm():
    days = weather.parse_forecast(FORECAST)

    keys = {o.key for o in weather.day_observations(days, {}, TODAY, place=HOME.name,
                                                    units="us")}

    assert f"weather:day:{SLUG}:2026-09-26:rain" in keys
    assert f"weather:day:{SLUG}:2026-09-26:downpour" in keys
    assert not any(k.startswith(f"weather:day:{SLUG}:2026-09-24") for k in keys)  # grey, dry


def test_a_much_colder_day_is_noticed():
    days = [_day(TODAY, high=72), _day(TODAY + dt.timedelta(days=1), high=52)]

    [obs] = weather.day_observations(days, {}, TODAY, place=HOME.name, units="us")

    assert obs.key.endswith(":swing-colder") and "52°F after 72°F" in obs.title


def test_the_same_forecast_again_is_the_same_keys():
    """Fetched every half hour; the judge must see "rain Saturday" once."""
    days = weather.parse_forecast(FORECAST)

    first = [o.key for o in weather.day_observations(days, {}, TODAY, place=HOME.name,
                                                     units="us")]
    again = [o.key for o in weather.day_observations(days, {}, TODAY, place=HOME.name,
                                                     units="metric")]

    assert first == again


def test_rain_that_is_called_off_is_news_because_the_rain_was():
    tomorrow = TODAY + dt.timedelta(days=1)
    before = {tomorrow.isoformat(): {"rain_chance": 75}}

    keys = [o.key for o in weather.day_observations([_day(tomorrow, rain=10)], before, TODAY,
                                                    place=HOME.name, units="us")]

    assert keys == [f"weather:day:{SLUG}:{tomorrow.isoformat()}:drier"]


def _dry(forecast: dict) -> dict:
    """The captured forecast with every chance of rain gone."""
    daily, hourly = dict(forecast["daily"]), dict(forecast["hourly"])
    daily["precipitation_probability_max"] = [0] * len(daily["time"])
    hourly["precipitation_probability"] = [0] * len(hourly["time"])
    return {**forecast, "daily": daily, "hourly": hourly}


def test_moving_home_is_not_the_forecast_changing_its_mind(conn, monkeypatch):
    """The kept forecast was by day alone, so a dry Saturday in the new town
    was measured against the old town's wet one: "Rain no longer expected
    Saturday", about a place the user had left. And the new town's own
    rain had the old town's key, so it was never news."""
    from agent import home

    leeds = Place("Leeds, United Kingdom", 53.8, -1.55, "Europe/London", "GB")
    now = {"home": HOME, "forecast": FORECAST}
    monkeypatch.setattr(home, "home", lambda conn: now["home"])
    monkeypatch.setattr(fetch, "get_json", lambda url, params=None:
                        now["forecast"] if "open-meteo" in url else {"features": []})

    weather.weather(conn, now=CAPTURED)
    now["forecast"] = _dry(FORECAST)
    found = weather.weather(conn, now=CAPTURED)
    assert any(o.key.endswith(":drier") for o in found), "at the same home it is news"

    now["forecast"] = FORECAST
    weather.weather(conn, now=CAPTURED)
    now["home"], now["forecast"] = leeds, _dry(FORECAST)
    found = weather.weather(conn, now=CAPTURED)

    assert not any(o.key.endswith(":drier") for o in found)
    assert {r["place"] for r in conn.execute("SELECT place FROM weather_days")} == {leeds.name}

    now["forecast"] = FORECAST
    found = weather.weather(conn, now=CAPTURED)
    assert "weather:day:leeds-united-kingdom:2026-09-26:rain" in {o.key for o in found}
    assert any("°C" in o.detail for o in found if o.key.startswith("weather:day:"))


def test_the_kept_forecast_is_read_back_for_a_briefing_and_goes_stale(conn):
    days = weather.parse_forecast(FORECAST)
    today = dt.date.today()
    for offset, day in enumerate(days):
        day.day = today + dt.timedelta(days=offset)
    weather.keep(conn, HOME, days)

    lines = weather.forecast_lines(conn)
    assert len(lines) == 2 and lines[0].startswith("Today: ") and "°F" in lines[0]

    with conn:
        conn.execute("UPDATE weather_days SET fetched_at = datetime('now', '-7 hours')")
    assert weather.forecast_lines(conn) == []


# --------------------------------------------------------------------------
# Weather during their plans
# --------------------------------------------------------------------------


def _event(start: dt.datetime, *, location="Larkspur Hall 2400", **kw):
    return {
        "id": kw.pop("id", "ev1"), "summary": kw.pop("summary", "DES 203 Workshop"),
        "start": start.isoformat(), "end": (start + dt.timedelta(hours=1)).isoformat(),
        "location": location, "all_day": False, **kw,
    }


NOW = dt.datetime(2026, 9, 25, 9, 0)
WET_AFTERNOON = [_day(NOW.date(), rain=80, wet=(15, 16, 17))]


def test_rain_during_something_the_user_walks_to_is_noticed():
    [obs] = weather.plan_observations([_event(NOW.replace(hour=16))], WET_AFTERNOON, NOW, ZONE,
                                      "us")

    assert obs.key == "weather:plans:ev1:2026-09-25:rain"
    assert "DES 203 Workshop" in obs.title and "4pm" in obs.title


@pytest.mark.parametrize("location", ["Zoom", "https://contoso.zoom.us/j/1", "Microsoft Teams", ""])
def test_rain_during_a_call_is_not_their_problem(location):
    events = [_event(NOW.replace(hour=16), location=location)]

    assert weather.plan_observations(events, WET_AFTERNOON, NOW, ZONE, "us") == []


def test_only_plans_in_the_next_day_and_a_half_are_checked():
    later = NOW + dt.timedelta(hours=40)
    days = [_day(later.date(), rain=80, wet=tuple(range(24)))]

    assert weather.plan_observations([_event(later)], days, NOW, ZONE, "us") == []


def test_a_freezing_walk_is_noticed():
    cold = [_day(NOW.date(), high=30, low=20)]

    [obs] = weather.plan_observations([_event(NOW.replace(hour=16))], cold, NOW, ZONE, "us")

    assert obs.key.endswith(":temp") and "25°F" in obs.title


def test_an_event_time_with_an_offset_is_compared_in_local_time():
    """The calendar gives times with an offset; the forecast's hours are
    naive local. Compared naively, a 4pm event looked at 8pm's weather."""
    aware = NOW.replace(hour=16).astimezone(dt.timezone(dt.timedelta(hours=-4)))
    event = _event(aware.replace(tzinfo=None))
    event["start"] = "2026-09-25T16:00:00-04:00"
    event["end"] = "2026-09-25T17:00:00-04:00"

    assert [o.key for o in weather.plan_observations([event], WET_AFTERNOON, NOW, ZONE,
                                                     "us")] == [
        "weather:plans:ev1:2026-09-25:rain"
    ]


# --------------------------------------------------------------------------
# Official alerts
# --------------------------------------------------------------------------


def test_an_official_alert_is_one_observation_by_its_id():
    [obs] = weather.alert_observations(weather_alerts := [
        f["properties"] for f in ALERTS["features"]
    ], HOME)

    assert obs.key == f"weather:alert:{weather_alerts[0]['id']}"
    assert obs.title.startswith("Flood Watch") and "Severe" in obs.detail


def test_a_cancelled_alert_is_not_news():
    cancelled = [{**ALERTS["features"][0]["properties"], "messageType": "Cancel"}]

    assert weather.alert_observations(cancelled, HOME) == []


# --------------------------------------------------------------------------
# The weather source as a whole
# --------------------------------------------------------------------------


def test_with_every_feed_down_the_source_says_so(conn, at_home, monkeypatch):
    def down(*_a, **_k):
        raise fetch.FetchError("api.open-meteo.com: HTTP 503")

    monkeypatch.setattr(fetch, "get_json", down)

    with pytest.raises(SourceError, match="503"):
        weather.weather(conn, now=NOW)


def test_a_geocoder_that_cannot_be_reached_is_a_failed_run_not_no_home(conn, monkeypatch):
    """No home is nothing to do; an unplaced home is a run that failed, and
    says why, so the source's health shows it."""
    from agent import home

    def unreachable(conn):
        raise fetch.FetchError("geocoding-api.open-meteo.com: ConnectError")

    monkeypatch.setattr(home, "home", unreachable)

    with pytest.raises(SourceError, match="home: geocoding"):
        weather.weather(conn, now=CAPTURED)


def test_with_no_calendar_the_forecast_still_counts(conn, at_home, monkeypatch):
    """conftest refuses Google; the forecast and alerts still go through."""
    monkeypatch.setattr(
        fetch, "get_json", lambda url, params=None: FORECAST if "open-meteo" in url else ALERTS
    )

    found = weather.weather(conn, now=CAPTURED)

    assert any(o.key.startswith("weather:alert:") for o in found)
    assert any(o.key == f"weather:day:{SLUG}:2026-09-26:rain" for o in found)
    assert conn.execute("SELECT COUNT(*) FROM weather_days").fetchone()[0] == 3


def test_with_no_home_there_is_no_forecast_and_nothing_fetched(conn, monkeypatch):
    """The forecast was always one city's. A user who has not said where they
    live gets no weather source at all -- not another person's town, and
    not a look that fails every ninety minutes."""
    from agent.attention import sources

    monkeypatch.setattr(fetch, "get_json", lambda *a, **kw: pytest.fail("nothing is fetched"))

    assert not weather.wanted(conn)
    assert weather.weather(conn, now=CAPTURED) == []
    assert "weather" not in [s.name for s in sources.watching(conn)]


def test_a_home_outside_the_us_has_a_forecast_and_no_nws_alerts(conn, monkeypatch):
    """The National Weather Service covers the US only; asked about Leeds it
    answers with nothing useful, or an error that sinks the forecast."""
    from agent import home

    leeds = home.Place("Leeds, United Kingdom", 53.8, -1.55, "Europe/London", "GB")
    monkeypatch.setattr(home, "home", lambda conn: leeds)
    asked: list[str] = []
    monkeypatch.setattr(fetch, "get_json", lambda url, params=None: asked.append(url) or FORECAST)

    weather.weather(conn, now=CAPTURED)

    assert asked and all("open-meteo" in url for url in asked)


# --------------------------------------------------------------------------
# News
# --------------------------------------------------------------------------

CAPTURED_UTC = dt.datetime(2026, 9, 24, 16, 0, tzinfo=dt.UTC)


def test_a_headline_loses_its_outlet_suffix():
    stories = news_source.parse(WORLD)

    assert len(stories) == 4
    assert stories[1].title == "Police in Poland Arrest Ukrainian Citizen Over Fatal Stabbing"
    assert stories[1].outlet == "The New York Times"
    assert stories[1].published.tzinfo is not None


def test_one_story_from_two_outlets_is_one_key():
    same = news_source.key_for("Fed holds rates steady, signals two cuts")

    assert news_source.key_for("Fed Holds Rates Steady -- Signals Two Cuts!") == same


def test_a_section_topic_reads_the_section_and_anything_else_is_a_search():
    url, params = news_source.feed("section:WORLD")
    assert url.endswith("/section/topic/WORLD") and "q" not in params

    url, params = news_source.feed('"Anthropic"')
    assert url.endswith("/rss/search") and params["q"] == '"Anthropic" when:1d'


def test_a_story_under_two_topics_is_one_observation_naming_both(following, monkeypatch):
    conn = following
    monkeypatch.setattr(fetch, "get_text", lambda url, params=None: WORLD)

    found = news_source.news(conn, now=CAPTURED_UTC)

    assert len(found) == 4
    assert all(o.source == "news" for o in found)
    topics = found[0].detail
    assert "world news" in topics and "technology" in topics


def test_yesterdays_news_is_not_news(following, monkeypatch):
    conn = following
    monkeypatch.setattr(fetch, "get_text", lambda url, params=None: WORLD)

    found = news_source.news(conn, now=CAPTURED_UTC + dt.timedelta(days=2))

    assert found == []


def test_one_broken_feed_leaves_the_others(following, monkeypatch):
    conn = following
    def flaky(url, params=None):
        if "BUSINESS" in url:
            raise fetch.FetchError("news.google.com: HTTP 500")
        return WORLD

    monkeypatch.setattr(fetch, "get_text", flaky)

    assert len(news_source.news(conn, now=CAPTURED_UTC)) == 4


def test_with_every_feed_broken_the_source_says_so(following, monkeypatch):
    conn = following
    def down(url, params=None):
        raise fetch.FetchError("news.google.com: HTTP 500")

    monkeypatch.setattr(fetch, "get_text", down)

    with pytest.raises(SourceError):
        news_source.news(conn, now=CAPTURED_UTC)


# --------------------------------------------------------------------------
# What the user follows
# --------------------------------------------------------------------------


def test_nothing_is_followed_until_the_user_asks_and_then_no_news_is_watched(conn):
    """The topics this started with were one person's -- their employers,
    their job hunt. A new user follows nothing, and the news source stays
    off until they follow something."""
    from agent.attention import sources

    assert interests.active(conn) == [] and not news_source.wanted(conn)
    assert "news" not in [s.name for s in sources.watching(conn)]

    interests.follow(conn, "Anthropic")

    assert news_source.wanted(conn)
    assert "news" in [s.name for s in sources.watching(conn)]


def test_a_section_named_reads_the_section_and_anything_else_is_a_search(conn):
    """With no seeds, only following a section by name reaches Google
    News's edited section feeds, which are what "world news" means."""
    interests.follow(conn, "World News")
    interests.follow(conn, "Anthropic")

    assert [i.query for i in interests.active(conn)] == ["section:WORLD", '"Anthropic"']


def test_following_something_again_brings_it_back(conn):
    interests.follow(conn, "Anthropic")
    interests.unfollow(conn, "anthropic")

    assert interests.follow(conn, "Anthropic")["ok"]
    assert "Anthropic" in [i.topic for i in interests.active(conn)]


def test_the_list_has_a_limit(conn, monkeypatch):
    monkeypatch.setattr(interests, "MAX_TOPICS", 7)
    for n in range(6):
        assert interests.follow(conn, f"topic {n}")["ok"]
    assert interests.follow(conn, "one more")["ok"]

    refused = interests.follow(conn, "and another")

    assert not refused["ok"] and "drop one" in refused["error"]


def test_unfollowing_something_not_followed_says_what_is(following):
    result = interests.unfollow(following, "the weather in Paris")

    assert not result["ok"] and "world news" in result["following"]


def test_every_topic_gets_its_share_before_the_cap(conn, monkeypatch):
    """In feed order, the three section feeds filled the source's cap on
    every look, and the consulting and recruiting topics the user asked for
    were never judged. Interleaved, the first stories of every topic come
    first."""
    from xml.sax.saxutils import escape

    for topic in ("world news", "business", "technology", "AI", "Northwind", "design"):
        interests.follow(conn, topic)

    def feed_of(topic: str) -> str:
        items = "".join(
            f"<item><title>{escape(topic)} story {n}</title><source>X</source>"
            "<pubDate>Thu, 24 Sep 2026 15:00:00 GMT</pubDate></item>"
            for n in range(12)
        )
        return f"<rss><channel>{items}</channel></rss>"

    def get_text(url, params=None):
        return feed_of((params or {}).get("q") or url.rsplit("/", 1)[-1])

    monkeypatch.setattr(fetch, "get_text", get_text)
    found = news_source.news(conn, now=CAPTURED_UTC)
    first_six = [o.title for o in found[:6]]

    assert len({title.split(" story ")[0] for title in first_six}) == 6
