"""Where the user lives: the town the weather is for.

Home was a constant, one town fixed in code, because the agent this was
copied from served one person. Every reader assumed it: the weather source,
the trips that measure "away" from it, the checks on an alert's place, the
briefing's lines. A user somewhere else got that town's forecast, and a user
who never said where they live got it too.

So home is what the user said, the `home_city` fact (agent/facts.py), and
nothing else. The town is geocoded by the weather's own provider, which
needs no key, and kept in `places` (`trips.geocode`), so it is looked up
once. A user who has not said has no home, and each reader has a branch for
that: no weather source, no trips, no town checks.

Three outcomes, kept apart: placed; not a town the geocoder knows where
the user said it is; and the geocoder unreachable, which raises. They were
one None, so an outage told the user their town was unknown, and asked
them for the nearest one.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

log = logging.getLogger("attention")


@dataclass(frozen=True)
class Place:
    name: str
    latitude: float
    longitude: float
    timezone: str
    # ISO 3166 code, as the geocoder gives it: the National Weather Service's
    # alerts cover the US only.
    country: str = ""


def home_city(conn: sqlite3.Connection) -> str | None:
    """The town the user said they live in, as they said it, or None. Reads
    SQLite only, so it is what decides whether there is weather at all."""
    from agent import facts

    marks = ", ".join("?" * len(facts.PROFILE_SOURCES))
    row = conn.execute(
        "SELECT value FROM facts WHERE subject = ? AND attribute = 'home_city'"
        f" AND status = 'active' AND source IN ({marks}) ORDER BY id DESC LIMIT 1",
        (facts.USER, *facts.PROFILE_SOURCES)).fetchone()
    return " ".join(str(row["value"]).split()) if row else None


def home(conn: sqlite3.Connection) -> Place | None:
    """The user's home, geocoded, or None: they have not said, or the
    geocoder knows no town of that name where they said it is. Raises
    `fetch.FetchError` when the geocoder cannot be reached and the town was
    never placed; one placed before is used however old. Blocking the first
    time a town is asked for; from `places` after that."""
    city = home_city(conn)
    if not city:
        return None
    found = place(conn, city)
    if found is None:
        log.info("home (%s) is not a town the geocoder knows", city)
    return found


def place(conn: sqlite3.Connection, text: str) -> Place | None:
    """A town as a user writes it, or None if the geocoder knows no such
    town. "Portland, Maine, USA" is the town and where it is: the town is
    looked up, and the rest picks among the towns of that name
    (`trips.geocode`). Raises `fetch.FetchError` as `home` does."""
    from agent.attention import trips

    town, _, within = text.partition(",")
    found = trips.geocode(conn, town.strip(), within=within.strip())
    return found[0] if found else None


def placed(conn: sqlite3.Connection) -> dict:
    """Where the weather is for, as the profile page shows it once home is
    saved: `status` "placed" with the town the geocoder chose ("Portland,
    Maine"), "unknown", "unreachable" or "none", and `says`, in words."""
    from agent.attention import fetch

    try:
        found = home(conn)
    except fetch.FetchError as exc:
        log.warning("could not place home: %s", exc)
        return {"status": "unreachable", "place": None,
                "says": "The town could not be looked up just now. It will be tried again."}
    if found is not None:
        return {"status": "placed", "place": found.name,
                "says": f"The weather is for {found.name}."}
    if home_city(conn) is None:
        return {"status": "none", "place": None, "says": "No home is set, so there is no "
                                                         "weather."}
    return {"status": "unknown", "place": None,
            "says": "No town of that name is known there. Try the nearest town, or add its "
                    "region or country."}
