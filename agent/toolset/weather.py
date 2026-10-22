"""The forecast, asked for directly.

One turn in fourteen of theirs was about the weather in the month to 6 October
2026 -- "will it rain tmrw", "do I want a jacket", "check the week's
weather". Each went to a web search: about 20 seconds, three weather sites'
pages read (accuweather, timeanddate and weather-us among the sites that
refuse a fetch), ~6,000 tokens of them, and an answer only as good as
whichever page loaded. The attention loop has read a proper forecast all
along -- Open-Meteo, free and keyless, and the National Weather Service's
alerts (agent/attention/sources/weather.py) -- so this asks the same feeds:
about a second, a few hundred tokens, and the numbers themselves.
"""

from __future__ import annotations

import sqlite3

from pydantic_ai import RunContext

from agent.turn import Deps

# How many hours ahead the hour-by-hour lines reach.
NEXT_HOURS = 12


def lookup(conn: sqlite3.Connection, place: str = "", days: int = 3) -> dict:
    """The forecast for `place` (the user's home if empty), `days` days of
    it. Blocking: the feeds are fetched here."""
    from agent import home
    from agent.attention import fetch
    from agent.attention.sources import weather

    days = max(1, min(int(days or 3), 16))
    if not place.strip():
        try:
            where = home.home(conn)
        except fetch.FetchError as exc:
            return {"error": f"could not look up where the user lives just now: {exc}"[:200]}
        if where is None:
            if city := home.home_city(conn):
                return {"error": f"{city!r}, where the user lives, is not a town the forecast "
                                 "service knows; ask for the nearest town"}
            return {"error": "the user has not said where they live; ask where, or name "
                             "the place"}
    else:
        try:
            where = home.place(conn, place)
        except fetch.FetchError as exc:
            return {"error": f"could not look the place up: {exc}"[:200]}
        if where is None:
            return {"error": f"{place!r} is not a town the forecast service knows; "
                             "try the nearest town, or search the web"}
    country = where.country
    # In the user's units, which are those of their home, wherever the
    # forecast is for; the place's own when home is not placed.
    try:
        theirs = home.home(conn) if place.strip() else where
    except fetch.FetchError:
        theirs = None
    said_in = weather.units((theirs or where).country)
    try:
        forecast = weather.forecast(where, days)
    except fetch.FetchError as exc:
        return {"error": f"the forecast could not be read: {exc}"[:200]}
    if not forecast:
        return {"error": "the forecast came back empty"}

    import datetime as dt
    from zoneinfo import ZoneInfo

    now = dt.datetime.now(ZoneInfo(where.timezone)).replace(tzinfo=None, minute=0, second=0,
                                                            microsecond=0)
    hours = [h for d in forecast for h in d.hours if now <= h.at < now + dt.timedelta(
        hours=NEXT_HOURS)]
    answer: dict = {
        "place": where.name,
        "days": [{
            "date": d.day.isoformat(),
            "summary": d.line(said_in),
            "high": weather.temp(d.high_c, said_in), "low": weather.temp(d.low_c, said_in),
            "rain_chance": d.rain_chance, "rain": weather.rain(d.rain_mm, said_in),
        } for d in forecast],
        "next_hours": [f"{h.at:%a %I%p}".replace(" 0", " ").lower()
                       + f": {weather.temp(h.temp_c, said_in)}, "
                       + weather._WMO.get(h.code, "mixed")
                       + (f", {h.rain_chance}% rain" if h.rain_chance else "")
                       for h in hours],
        "source": "Open-Meteo forecast",
    }
    if country == "US":
        try:
            answer["alerts"] = [{
                "event": a.get("event"), "headline": a.get("headline"),
                "until": a.get("ends") or a.get("expires"),
            } for a in weather.alerts(where)]
            answer["source"] += "; National Weather Service alerts"
        except fetch.FetchError:
            answer["alerts_unavailable"] = True
    return answer


def register(tool) -> None:
    """Attach the weather tool. `tool` is `register_tools`'s filter."""

    @tool
    async def weather(ctx: RunContext[Deps], place: str = "", days: int = 3) -> dict:
        """The forecast for a place: a line a day, the next hours one by
        one, and any official weather alerts there.

        For "will it rain tomorrow?", "do I need a jacket?", "the weather in
        Boston this weekend". Use it rather than a web search: it is the
        forecast itself, in about a second. `place` is a town ("Boston",
        "Lake Placid, NY"); leave it empty for the user's home. `days`
        is 1 to 16. For what a place is usually like ("October in the
        Catskills"), search the web instead.
        """
        from agent import offloop

        return await offloop.with_own_conn(ctx.deps.conn, lookup, place, days)
