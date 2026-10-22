"""The weather, as it bears on their plans.

Two free, keyless feeds: Open-Meteo for the forecast and the National
Weather Service for official alerts, both for the user's home
(`home.home`), the alerts only for a US one. Response
shapes are pinned by real payloads captured on 24 September 2026, in
tests/unit/fixtures/attention/ (protocols §11).

**The conditions are code; the judgement is not.** What makes weather worth
mentioning is a small set of facts that need no model to notice -- an
official alert, rain during something the user is walking to, a day much colder
than the one before, a forecast that changed its mind -- and each becomes
an observation with a key that names the condition and its date. Whether it
matters to *them* today is the judge's question, which it answers with their
calendar in front of it.

**Keyed so the same forecast is one mention.** `weather:day:<place>:<date>:rain`
is new once; the forecast saying it again every half hour adds nothing. A
forecast that stops saying it is a different key (`...:<date>:drier`),
because "the rain is off" is news if "rain tomorrow" was.

The forecast is also kept, per place and day, in `weather_days`, which is
what a briefing reads and what "changed" is measured against. Kept by day
alone, a user who moved was told the rain was off, because their old town's
had been on.

**Kept metric, said in the user's units** (`units`): those of the country
their home is in, unless they choose. It was °F and inches for everyone,
down to the stored columns.
"""

from __future__ import annotations

import datetime as dt
import logging
import sqlite3
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from agent.attention import fetch, settings
from agent.attention.sources import SourceError
from agent.attention.store import Observation
from agent.attention.trips import VIRTUAL
from agent.home import Place
from core import db

log = logging.getLogger("attention")

OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
NWS_ALERTS = "https://api.weather.gov/alerts/active"

# Chance of rain in any hour at or above which it is "likely".
RAIN_LIKELY = 50
# A day's high this far from the day before's is a different day to dress
# for: 8°C is about 15°F.
SWING_C = 8
FREEZING_C = 0
# 90°F.
HOT_C = 32
# A day's rain total at which it is a storm rather than a wet day: roads,
# trains and flights start to be affected around here, about an inch.
HEAVY_RAIN_MM = 25
# How far ahead rain during a plan is looked for.
PLANS_AHEAD = dt.timedelta(hours=36)

# WMO weather codes, as words. https://open-meteo.com/en/docs (the table at
# the end of the page).
_WMO = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "freezing fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    56: "freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "showers", 81: "showers", 82: "violent showers", 85: "snow showers", 86: "snow showers",
    95: "thunderstorms", 96: "thunderstorms with hail", 99: "thunderstorms with hail",
}
SNOW = {71, 73, 75, 77, 85, 86}
STORM = {95, 96, 99}

# Where a forecast is said in °F and inches: the United States, the
# territories that follow it, and the few countries that also do.
FAHRENHEIT = frozenset({"US", "PR", "GU", "VI", "AS", "MP", "UM", "BS", "BZ", "KY", "PW",
                        "LR", "MH", "FM"})


def units(country: str) -> str:
    """"us" (°F, inches) or "metric" (°C, millimetres): as the user chose
    (`settings.UNITS`), or, left on "auto", those of `country`, where their
    home is."""
    if settings.UNITS in ("us", "metric"):
        return settings.UNITS
    return "us" if country.upper() in FAHRENHEIT else "metric"


def _degrees(celsius: float, units: str) -> float:
    return celsius * 9 / 5 + 32 if units == "us" else celsius


def temp(celsius: float, units: str) -> str:
    """"75°F" or "24°C"."""
    return f"{_degrees(celsius, units):.0f}°{'F' if units == 'us' else 'C'}"


def temps(low: float, high: float, units: str) -> str:
    """A day's range: "58-71°F", or "-3 to 1°C" below zero."""
    lo = f"{_degrees(low, units):.0f}"
    return f"{lo}{' to ' if lo.startswith('-') else '-'}{temp(high, units)}"


def rain(mm: float, units: str) -> str:
    """"0.12 in" or "3.0 mm"."""
    if units == "us":
        inches = mm / 25.4
        return f"{inches:.1f} in" if inches >= 1 else f"{inches:.2f} in"
    return f"{mm:.0f} mm" if mm >= 10 else f"{mm:.1f} mm"


@dataclass
class Hour:
    at: dt.datetime  # naive, local to the place
    temp_c: float
    rain_chance: int
    rain_mm: float
    code: int


@dataclass
class Day:
    day: dt.date
    high_c: float
    low_c: float
    rain_chance: int
    rain_mm: float
    code: int
    hours: list[Hour] = field(default_factory=list)

    @property
    def sky(self) -> str:
        return _WMO.get(self.code, "mixed")

    @property
    def wet_hours(self) -> str:
        """The span of hours with rain likely, as "2pm-6pm", "all day", or ""."""
        wet = [h.at for h in self.hours if h.rain_chance >= RAIN_LIKELY]
        if not wet:
            return ""
        start, end = wet[0], wet[-1] + dt.timedelta(hours=1)
        # A span that ends at midnight read "12am", so a wet day was
        # "rain likely 12am-12am" (the persona eval's evening briefing).
        if start.hour == 0 and end.hour == 0:
            return "all day"
        return f"{_clock(start)}-{'midnight' if end.hour == 0 else _clock(end)}"

    def line(self, units: str) -> str:
        """One line for a briefing: "Fri: 58-71°F, rain likely 2pm-6pm (80%)"."""
        text = f"{self.day:%a}: {temps(self.low_c, self.high_c, units)}, {self.sky}"
        if self.wet_hours:
            text += f", rain likely {self.wet_hours} ({self.rain_chance}%)"
        return text


def _clock(moment: dt.datetime) -> str:
    return moment.strftime("%I%p").lstrip("0").lower()


# ---------------------------------------------------------------------------
# Reading the feeds
# ---------------------------------------------------------------------------


def forecast(place: Place, days: int = 3) -> list[Day]:
    """The next `days` days (three at home), hour by hour."""
    data = fetch.get_json(OPEN_METEO, {
        "latitude": place.latitude, "longitude": place.longitude,
        "timezone": place.timezone, "forecast_days": max(1, min(days, 16)),
        "temperature_unit": "celsius", "precipitation_unit": "mm",
        "hourly": "temperature_2m,precipitation_probability,precipitation,weather_code",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,"
                 "precipitation_sum,weather_code",
    })
    return parse_forecast(data)


def _from_fahrenheit(degrees: float) -> float:
    return (degrees - 32) * 5 / 9


def _from_inches(inches: float) -> float:
    return inches * 25.4


def _metric(declared: dict, temp_key: str, rain_key: str):
    """To °C and to millimetres, from the units a payload says it is in."""
    to_c = _from_fahrenheit if "F" in str(declared.get(temp_key) or "") else float
    to_mm = _from_inches if "inch" in str(declared.get(rain_key) or "") else float
    return to_c, to_mm


def parse_forecast(data: dict) -> list[Day]:
    """In °C and millimetres, whatever units the payload says it is in: the
    captured fixtures are in °F and inches."""
    hourly, daily = data.get("hourly") or {}, data.get("daily") or {}
    hour_c, hour_mm = _metric(data.get("hourly_units") or {}, "temperature_2m", "precipitation")
    day_c, day_mm = _metric(data.get("daily_units") or {}, "temperature_2m_max",
                            "precipitation_sum")
    hours = [
        Hour(
            at=dt.datetime.fromisoformat(t),
            temp_c=hour_c(float(hourly["temperature_2m"][i] or 0)),
            rain_chance=int(hourly["precipitation_probability"][i] or 0),
            rain_mm=hour_mm(float(hourly["precipitation"][i] or 0)),
            code=int(hourly["weather_code"][i] or 0),
        )
        for i, t in enumerate(hourly.get("time") or [])
    ]
    days = []
    for i, d in enumerate(daily.get("time") or []):
        day = dt.date.fromisoformat(d)
        days.append(Day(
            day=day,
            high_c=day_c(float(daily["temperature_2m_max"][i] or 0)),
            low_c=day_c(float(daily["temperature_2m_min"][i] or 0)),
            rain_chance=int(daily["precipitation_probability_max"][i] or 0),
            rain_mm=day_mm(float(daily["precipitation_sum"][i] or 0)),
            code=int(daily["weather_code"][i] or 0),
            hours=[h for h in hours if h.at.date() == day],
        ))
    return days


def alerts(place: Place) -> list[dict]:
    """Official watches, warnings and advisories in effect at the place."""
    data = fetch.get_json(NWS_ALERTS, {"point": f"{place.latitude:.4f},{place.longitude:.4f}"})
    return [f.get("properties") or {} for f in data.get("features") or []]


# ---------------------------------------------------------------------------
# Conditions
# ---------------------------------------------------------------------------


# How many of an alert's county and zone codes are kept: a hurricane warning
# can list a hundred, and home's are checked against these (`vet.py`).
MAX_AREA_CODES = 60


def alert_observations(found: list[dict], place: Place, trip=None) -> list[Observation]:
    """`place` is where the alerts were fetched for: home, or the place of
    `trip`, the trip it was fetched for (`trips.Trip`).

    The alert's own area, severity, end and whom it is for go into `ref`,
    for the checks in code (`vet.location`) and what the user is told: the hurricane
    warning that woke them on 8 October said none of it anywhere code read."""
    if trip is None:
        where = f"at home ({place.name})"
    else:
        where = f"in {place.name.split(',')[0]}, where the user is going ({trip.span})"
    out = []
    for alert in found:
        if alert.get("status", "Actual") != "Actual" or alert.get("messageType") == "Cancel":
            continue
        event = alert.get("event") or "Weather alert"
        out.append(Observation(
            source="weather",
            key=f"weather:alert:{alert.get('id')}",
            title=f"{event} in effect {where}",
            detail="; ".join(part for part in (
                f"severity {alert.get('severity')}, urgency {alert.get('urgency')}",
                f"from {alert.get('onset')} until {alert.get('ends') or alert.get('expires')}",
                (alert.get("headline") or "")[:200],
                (alert.get("description") or "")[:300],
                (alert.get("instruction") or "")[:200],
            ) if part),
            when=alert.get("onset"),
            ref=_alert_ref(alert, place, trip),
        ))
    return out


def _alert_ref(alert: dict, place: Place, trip) -> dict:
    geocode = alert.get("geocode") or {}
    ref = {
        "official": "National Weather Service",
        "severity": alert.get("severity"),
        "urgency": alert.get("urgency"),
        "same": [str(c) for c in (geocode.get("SAME") or [])][:MAX_AREA_CODES],
        "ugc": [str(c) for c in (geocode.get("UGC") or [])][:MAX_AREA_CODES],
        "area": str(alert.get("areaDesc") or "")[:300],
        "ends": alert.get("ends") or alert.get("expires"),
        "for": "trip" if trip is not None else "home",
    }
    if trip is None:
        ref["home"] = place.name
    else:
        ref |= {"trip": trip.place.name, "trip_verified": trip.verified, "trip_why": trip.why}
    return ref


def _in_person(event: dict) -> bool:
    where = str(event.get("location") or "").lower()
    return bool(where) and not any(word in where for word in VIRTUAL)


def _local(moment: str, zone: ZoneInfo) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(moment)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed
    return parsed.astimezone(zone).replace(tzinfo=None)


def plan_observations(
    events: list[dict], days: list[Day], now: dt.datetime, zone: dt.tzinfo, units: str
) -> list[Observation]:
    """Rain, or a temperature worth dressing for, during an in-person plan.
    `zone` is the forecast's: its hours are naive, local to the place."""
    hours = [h for d in days for h in d.hours]
    out = []
    for event in events:
        if event.get("all_day") or not _in_person(event):
            continue
        start = _local(event.get("start"), zone)
        end = _local(event.get("end"), zone) or (start and start + dt.timedelta(hours=1))
        if start is None or not (now <= start <= now + PLANS_AHEAD):
            continue
        during = [h for h in hours if start - dt.timedelta(minutes=30) <= h.at < end]
        if not during:
            continue
        wettest = max(during, key=lambda h: h.rain_chance)
        coldest = min(h.temp_c for h in during)
        hottest = max(h.temp_c for h in during)
        what = str(event.get("summary") or "a plan")
        common = dict(source="weather", when=event.get("start"),
                      ref={"event_id": event.get("id"), "location": event.get("location")})
        if wettest.rain_chance >= RAIN_LIKELY:
            out.append(Observation(
                key=f"weather:plans:{event.get('id')}:{start:%Y-%m-%d}:rain",
                title=f"Rain likely ({wettest.rain_chance}%) around {what} at {_clock(start)}",
                detail=f"at {event.get('location')}; {_WMO.get(wettest.code, 'rain')},"
                       f" about {rain(wettest.rain_mm, units)} in that hour",
                **common,
            ))
        if coldest <= FREEZING_C or hottest >= HOT_C:
            extreme = temp(coldest if coldest <= FREEZING_C else hottest, units)
            out.append(Observation(
                key=f"weather:plans:{event.get('id')}:{start:%Y-%m-%d}:temp",
                title=f"{extreme} around {what} at {_clock(start)}",
                detail=f"at {event.get('location')}",
                **common,
            ))
    return out


def day_observations(
    days: list[Day], before: dict[str, dict], today: dt.date, *, place: str, units: str
) -> list[Observation]:
    """What is notable about today and the next days at `place`, home, and
    what changed.

    `before` is `weather_days` for `place` as it was before this fetch, by
    date.
    """
    where = _slug(place)
    out = []
    previous: Day | None = None
    for day in days:
        if day.day < today:
            previous = day
            continue
        label = "today" if day.day == today else f"{day.day:%A}"
        stamp = day.day.isoformat()

        def obs(kind: str, title: str, day: Day = day, stamp: str = stamp) -> Observation:
            return Observation(
                source="weather", key=f"weather:day:{where}:{stamp}:{kind}", title=title,
                detail=day.line(units), when=stamp,
            )

        if day.rain_chance >= 60 and day.wet_hours:
            out.append(obs("rain", f"Rain likely {label} ({day.rain_chance}%), {day.wet_hours}"))
        if day.rain_mm >= HEAVY_RAIN_MM:
            out.append(obs("downpour", f"Heavy rain {label}: about {rain(day.rain_mm, units)}"))
        if day.code in SNOW:
            out.append(obs("snow", f"Snow {label}: {day.sky}"))
        if day.code in STORM:
            out.append(obs("storm", f"Thunderstorms {label}"))
        if day.low_c <= FREEZING_C:
            out.append(obs("freeze", f"Freezing {label}: low {temp(day.low_c, units)}"))
        if day.high_c >= HOT_C:
            out.append(obs("heat", f"Hot {label}: high {temp(day.high_c, units)}"))
        if previous and abs(day.high_c - previous.high_c) >= SWING_C:
            way = "colder" if day.high_c < previous.high_c else "warmer"
            out.append(obs(
                f"swing-{way}",
                f"Much {way} {label}: high {temp(day.high_c, units)}"
                f" after {temp(previous.high_c, units)}",
            ))

        # Turning wet needs no key of its own: `rain` above is new the first
        # time the forecast says it. Turning dry does -- "the rain is off"
        # is news only because "rain tomorrow" was.
        was = before.get(stamp)
        if was and (was.get("rain_chance") or 0) >= 60 and day.rain_chance < 30:
            out.append(obs(
                "drier",
                f"Rain no longer expected {label}: now {day.rain_chance}%,"
                f" was {was['rain_chance']}%",
            ))
        previous = day
    return out


# ---------------------------------------------------------------------------
# The kept forecast
# ---------------------------------------------------------------------------


def _stored(conn: sqlite3.Connection, place: str) -> dict[str, dict]:
    return {r["day"]: dict(r) for r in conn.execute(
        "SELECT * FROM weather_days WHERE place = ?", (place,))}


def keep(conn: sqlite3.Connection, place: Place, days: list[Day]) -> None:
    """Home's forecast, in place of what was kept: another place's, kept
    before the user moved, is no forecast of theirs."""
    with db.transaction(conn):
        conn.execute("DELETE FROM weather_days WHERE place != ?", (place.name,))
        conn.executemany(
            "INSERT INTO weather_days (place, day, country, high_c, low_c, rain_chance,"
            " rain_mm, code, wet_hours, fetched_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))"
            " ON CONFLICT(place, day) DO UPDATE SET country = excluded.country,"
            " high_c = excluded.high_c, low_c = excluded.low_c,"
            " rain_chance = excluded.rain_chance, rain_mm = excluded.rain_mm,"
            " code = excluded.code, wet_hours = excluded.wet_hours,"
            " fetched_at = excluded.fetched_at",
            [
                (place.name, d.day.isoformat(), place.country, d.high_c, d.low_c,
                 d.rain_chance, d.rain_mm, d.code, d.wet_hours)
                for d in days
            ],
        )


def day_line(conn: sqlite3.Connection, day: dt.date) -> str | None:
    """One day's kept forecast as "46-68°F, light rain, rain likely 2pm-6pm
    (70%)", or None if there is none fetched in the last six hours."""
    row = conn.execute(
        "SELECT * FROM weather_days WHERE day = ? AND fetched_at > datetime('now', '-6 hours')",
        (day.isoformat(),),
    ).fetchone()
    if row is None:
        return None
    text = (f"{temps(row['low_c'], row['high_c'], units(row['country']))},"
            f" {_WMO.get(row['code'], 'mixed')}")
    if row["wet_hours"]:
        text += f", rain likely {row['wet_hours']} ({row['rain_chance']}%)"
    return text


def forecast_lines(conn: sqlite3.Connection, days: int = 2) -> list[str]:
    """Today's and tomorrow's forecast as briefing lines, from what was kept.

    Nothing if the kept forecast is more than six hours old: a stale
    forecast read out as today's is worse than none.
    """
    today = dt.date.today()
    rows = conn.execute(
        "SELECT * FROM weather_days WHERE day >= ? AND fetched_at > datetime('now', '-6 hours')"
        " ORDER BY day LIMIT ?",
        (today.isoformat(), days),
    ).fetchall()
    lines = []
    for r in rows:
        day = dt.date.fromisoformat(r["day"])
        text = (f"{'Today' if day == today else f'{day:%a}'}:"
                f" {temps(r['low_c'], r['high_c'], units(r['country']))},"
                f" {_WMO.get(r['code'], 'mixed')}")
        if r["wet_hours"]:
            text += f", rain likely {r['wet_hours']} ({r['rain_chance']}%)"
        lines.append(text)
    return lines


# ---------------------------------------------------------------------------
# Where the user is going
# ---------------------------------------------------------------------------

# How soon before a trip its official alerts are worth reading.
TRIP_ALERTS_WITHIN = dt.timedelta(days=2)


def _slug(name: str) -> str:
    return "-".join("".join(c if c.isalnum() else " " for c in name.lower()).split())


def _notable(day: Day, units: str) -> list[tuple[str, str]]:
    """What about a day away is worth saying: (key part, words)."""
    out = []
    if day.code in STORM:
        out.append(("storm", "Thunderstorms"))
    elif day.code in SNOW:
        out.append(("snow", "Snow"))
    elif day.rain_chance >= RAIN_LIKELY:
        out.append(("rain", "Rain likely"))
    if day.low_c <= FREEZING_C:
        out.append(("freezing", f"Below freezing ({temp(day.low_c, units)})"))
    if day.high_c >= HOT_C:
        out.append(("hot", f"Hot ({temp(day.high_c, units)})"))
    return out


def trip_observations(conn: sqlite3.Connection, trips: list, today: dt.date,
                      units: str) -> list[Observation]:
    """The weather at each place the user is going, for the days they are there.

    The forecast lines are kept (`trip_weather`) for the briefing; a day
    with something worth saying is an observation, like one at home. A US
    trip's official alerts are read once it is two days off.
    """
    out: list[Observation] = []
    for trip in trips:
        if not trip.verified:
            # No forecast kept and nothing said: the kept forecast is what
            # the briefing reads as "where the user is", and a guessed trip put
            # Ridgeview, Alabama's hurricane in it on 8 October.
            log.info("not a verified trip, so no weather for it: %s (%s)",
                     trip.place.name, trip.why)
            continue
        days = forecast(trip.place, days=(trip.end - today).days + 1)
        mine = [d for d in days if trip.covers(d.day)]
        with db.transaction(conn):
            for d in mine:
                conn.execute(
                    "INSERT OR REPLACE INTO trip_weather (place, day, line, fetched_at)"
                    " VALUES (?, ?, ?, datetime('now'))",
                    (trip.place.name, d.day.isoformat(), d.line(units)),
                )
        town = trip.place.name.split(",")[0]
        for d in mine:
            for part, words in _notable(d, units):
                out.append(Observation(
                    source="weather",
                    key=f"weather:trip:{_slug(trip.place.name)}:{d.day.isoformat()}:{part}",
                    title=f"{words} in {town} on {d.day:%a %b} {d.day.day}, "
                          f"while the user is there ({trip.span})",
                    detail=f"{d.line(units)}; for {', '.join(trip.events[:2])}",
                    when=d.day.isoformat(),
                    ref={"trip": trip.place.name, "trip_verified": trip.verified,
                         "trip_why": trip.why},
                ))
        if trip.country == "US" and trip.start - today <= TRIP_ALERTS_WITHIN:
            out += alert_observations(alerts(trip.place), trip.place, trip)
    return out


def trip_lines(conn: sqlite3.Connection, about: dt.date, days: int = 3) -> list[str]:
    """The kept forecast for trips from `about` over the next `days`, a line
    per place: "Boston, Massachusetts: Thu: 45-58°F, light rain; ..."."""
    rows = conn.execute(
        "SELECT place, day, line FROM trip_weather WHERE day >= ? AND day < ?"
        " AND fetched_at > datetime('now', '-6 hours') ORDER BY place, day",
        (about.isoformat(), (about + dt.timedelta(days=days)).isoformat()),
    ).fetchall()
    by_place: dict[str, list[str]] = {}
    for r in rows:
        by_place.setdefault(r["place"], []).append(r["line"])
    return [f"{place}: " + "; ".join(lines) for place, lines in by_place.items()]


# ---------------------------------------------------------------------------
# The source
# ---------------------------------------------------------------------------


def wanted(conn: sqlite3.Connection) -> bool:
    """Whether there is weather to watch: only once the user has said where
    they live. Reads SQLite only."""
    from agent.home import home_city

    return home_city(conn) is not None


def weather(conn: sqlite3.Connection, *, now: dt.datetime | None = None) -> list[Observation]:
    """`now` is naive local time at home; pinned by tests. Nothing without a
    home the geocoder can place (`home.home`); a failed run if the geocoder
    cannot be reached and home was never placed."""
    from agent.home import home

    try:
        place = home(conn)
    except fetch.FetchError as exc:
        raise SourceError(f"home: {exc}") from None
    if place is None:
        return []
    zone = ZoneInfo(place.timezone)
    now = now or dt.datetime.now(zone).replace(tzinfo=None)
    said_in = units(place.country)
    problems = []
    out: list[Observation] = []

    try:
        days = forecast(place)
    except fetch.FetchError as exc:
        problems.append(f"forecast: {exc}")
        days = []
    if days:
        out += day_observations(days, _stored(conn, place.name), now.date(), place=place.name,
                                units=said_in)
        keep(conn, place, days)
        try:
            from agent import calendars

            agenda = calendars.agenda(conn, 2, limit=25)
            out += plan_observations(agenda.get("events") or [], days, now, zone, said_in)
        except Exception as exc:  # noqa: BLE001 - the forecast is still worth having
            problems.append(f"calendar: {exc}")

    if place.country == "US":
        try:
            out += alert_observations(alerts(place), place)
        except fetch.FetchError as exc:
            problems.append(f"alerts: {exc}")

    # Where the user is going, from the week ahead on their calendar.
    try:
        from agent import calendars
        from agent.attention import trips

        week = calendars.agenda(conn, trips.HORIZON_DAYS, limit=100)
        ahead = trips.upcoming(conn, week.get("events") or [], now.date(), place)
        out += trip_observations(conn, ahead, now.date(), said_in)
    except fetch.FetchError as exc:
        problems.append(f"trips: {exc}")
    except Exception as exc:  # noqa: BLE001 - home's weather is still worth having
        problems.append(f"trips: {type(exc).__name__}: {exc}")

    if problems and not out and not days:
        raise SourceError("; ".join(problems))
    for problem in problems:
        # Partial: the source ran and found what it could, so it is recorded
        # as a run, and the part that failed is said here.
        log.warning("weather source, partly: %s", problem)
    return out
