"""Where the user is going, from their calendar, so the weather can be about there.

The weather source was only ever for home (`agent.home`), so a weekend in
Boston or a wedding in New York came with home's forecast.
A trip is read from the calendar, not asked for:

- an event whose location names a town, or whose title says so ("Trip to
  Boston", "Lisbon holiday");
- geocoded with Open-Meteo's keyless geocoder, populated places only, so a
  room like "Larkspur Hall 2400" finds nothing rather than a hamlet somewhere
  called Larkspur;
- more than `AWAY_KM` from home.

Consecutive days at the same place are one trip. Geocodes are kept in
`places`, so a location is looked up once.

A place found is not yet a trip the user is taking (`verify`). On 8 October one
three-hour ropes course at "...Pittsburgh, PA 15222, United States" became a
trip to Ridgeview, Alabama, and its hurricane warning woke them at 23:03.
A trip is verified by a travel event -- the title says the user is going, a
flight, a hotel, several days -- or by an address whose own state agrees
with where it was geocoded. Only a verified trip gets weather
(`weather.trip_observations`), and only its alerts can reach them
(`vet.location`).
"""

from __future__ import annotations

import datetime as dt
import logging
import math
import re
import sqlite3
from dataclasses import dataclass, field

from agent.attention import fetch
from agent.home import Place
from core import db

log = logging.getLogger("attention")

GEOCODE = "https://geocoding-api.open-meteo.com/v1/search"
# Farther than this from home is away. Pittsburgh to Washington, PA is 40 km, to
# Morgantown 90.
AWAY_KM = 80
HORIZON_DAYS = 7
# A found place kept this long; a place that was not found, this long, in
# case the geocoder learns it. A found place older than this is still used
# while the geocoder cannot be reached.
KEEP_FOUND_DAYS = 180
KEEP_MISSING_DAYS = 14
# How many towns of a name are asked for: more when a region is named, so a
# small town of a common name ("Portland, UK") is among them.
TOWNS_ASKED = 5
TOWNS_ASKED_IN_REGION = 20

# The travel words in any case ("Trip to", "flight to"); the place must be
# capitalised, which is what keeps "trip to the store" from being a trip.
_TRAVEL = re.compile(
    r"\b(?i:trip|travel(?:ling|ing)?|flight|fly(?:ing)?|visit(?:ing)?|conference|retreat|"
    r"holiday|vacation|wedding|offsite)\b[^,;:()]*?\b(?i:to|in)\s+"
    r"(?P<place>[A-Z][A-Za-z.'-]+(?:\s+[A-Z][A-Za-z.'-]+){0,2})"
)
_NAMED_TRIP = re.compile(
    r"^(?P<place>[A-Z][A-Za-z.'-]+(?:\s+[A-Z][A-Za-z.'-]+){0,2})\s+"
    r"(?i:trip|retreat|weekend|holiday|vacation|getaway)\b"
)
_ROOM = re.compile(r"\b(?:hall|room|rm|classroom|floor|suite|building|bldg|auditorium|lab)\b|#",
                   re.IGNORECASE)
_STATE = re.compile(r"^[A-Z]{2}(?:\s+\d{5}(?:-\d{4})?)?$")
# Words in a location that mean nobody walks there; the weather's plans read
# them too, to warn only of rain on the way to a place.
VIRTUAL = ("zoom", "teams", "meet.google", "webex", "http", "virtual", "online", "phone")
# The last part of an address that is a country, not a town: "…, Pittsburgh,
# PA 15222, United States" made "United States" the town, which the geocoder
# placed in Ridgeview, Alabama -- and its hurricane warning woke them at
# 23:03 on 8 October 2026, for a ropes course in Pittsburgh.
_COUNTRIES = frozenset({
    "united states", "united states of america", "usa", "us", "u s", "u s a", "america",
    "canada", "united kingdom", "uk", "great britain", "england", "mexico",
})


@dataclass
class Trip:
    place: Place
    start: dt.date
    end: dt.date
    events: list[str] = field(default_factory=list)
    country: str = ""
    # Whether an event in it shows the user is really going (`verify`), and why --
    # or, unverified, what was missing.
    verified: bool = False
    why: str = ""

    @property
    def span(self) -> str:
        if self.start == self.end:
            return f"{self.start:%a}"
        return f"{self.start:%a}-{self.end:%a}"

    def covers(self, day: dt.date) -> bool:
        return self.start <= day <= self.end


def km_between(a: Place, b: Place) -> float:
    """Great-circle distance, in kilometres."""
    lat1, lon1, lat2, lon2 = map(math.radians, (a.latitude, a.longitude,
                                                b.latitude, b.longitude))
    a = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 6371 * 2 * math.asin(math.sqrt(a))


def candidates(event: dict) -> list[str]:
    """Place names an event gives, most specific source first."""
    found = []
    where = str(event.get("location") or "").strip()
    if where and not any(v in where.lower() for v in VIRTUAL):
        # "55 Lock Street, 3rd Floor, Pittsburgh, PA 15222": the town is the
        # last part that is not a state, a postcode or a room.
        for part in reversed([p.strip() for p in where.split(",")]):
            if not part or _STATE.match(part) or re.fullmatch(r"[\d\s-]+", part):
                continue
            if _plain(part) in _COUNTRIES:
                continue
            if _ROOM.search(part) or re.match(r"^\d", part):
                continue
            found.append(part)
            break
    title = str(event.get("summary") or "")
    for pattern in (_TRAVEL, _NAMED_TRIP):
        if match := pattern.search(title):
            found.append(match.group("place").strip())
    return found


def _plain(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())


def _names(found: str, query: str) -> bool:
    """Whether the geocoder's town is the one asked for: its name and the
    query's town, one inside the other ("New York" for "New York City").
    Open-Meteo also matches alternate names, and on 30 September 2026 it
    answered "United States" with Ridgeview, Alabama."""
    asked = _plain(query.split(",")[0])
    name = _plain(found.split(",")[0])
    return bool(asked and name) and (f" {asked} " in f" {name} " or f" {name} " in f" {asked} ")


def _regions(within: str) -> list[str]:
    """The parts of what a user wrote after a town that say where it is:
    "Maine, USA" is two, and "OR 97201" is Oregon. A postcode says nothing
    the geocoder can check."""
    parts = (re.sub(r"[\s-]*\d[\d\s-]*$", "", part.strip()) for part in within.split(","))
    return [part for part in parts if _plain(part)]


def _called(part: str) -> set[str]:
    """What the geocoder may call a region a user wrote: "ME" is Maine,
    "UK" the United Kingdom, "ON" Ontario."""
    plain = _plain(part)
    code = part.strip().upper()
    names = {plain, _COUNTRY_CALLED.get(plain, plain)}
    names |= {_plain(name) for name in (US_STATES.get(code), _PROVINCES.get(code)) if name}
    return names - {""}


def _in(result: dict, part: str) -> bool:
    """Whether a geocoded town is in one region a user named: its state,
    county or country, or a word-for-word part of one ("Yorkshire" is in
    West Yorkshire)."""
    known = {_plain(str(result.get(k) or "")) for k in
             ("admin1", "admin2", "admin3", "admin4", "country", "country_code")} - {""}
    return any(name == k or f" {name} " in f" {k} " for name in _called(part) for k in known)


def _parse(data: dict, query: str, within: str = "") -> tuple[Place, str] | None:
    towns = [r for r in data.get("results") or []
             if str(r.get("feature_code") or "").startswith("PPL")
             and _names(str(r.get("name") or ""), query)]
    # The towns in the most of the regions the user named, largest first:
    # "Portland, ME, USA" is Maine's, and "Portland, UK" is not Oregon's.
    # A region that no town of the name is in means none was found. It used
    # to mean the largest: the whole of "ME, USA" was compared with each
    # state's name, matched none, and every such home was Portland, Oregon.
    if regions := _regions(within):
        scores = [sum(_in(r, part) for part in regions) for r in towns]
        best = max(scores, default=0)
        if best == 0:
            return None
        towns = [r for r, score in zip(towns, scores, strict=True) if score == best]
    for result in towns:
        name = str(result.get("name") or "")
        country = str(result.get("country_code") or "")
        region = result.get("admin1") if country == "US" else result.get("country")
        return Place(f"{name}, {region}" if region else name,
                     float(result["latitude"]), float(result["longitude"]),
                     str(result.get("timezone") or "UTC"), country), country
    return None


def geocode(conn: sqlite3.Connection, query: str,
            within: str = "") -> tuple[Place, str] | None:
    """A town by name, and its country, from the cache or the geocoder.
    None if it is not a town. `within` is the region or country the town is
    in, as a user wrote it after a comma ("Maine"), to choose among towns of
    one name."""
    key = " ".join(f"{query}, {within}".lower().split()) if within else " ".join(
        query.lower().split())
    row = conn.execute("SELECT * FROM places WHERE query = ?", (key,)).fetchone()
    kept = None
    # A town kept before its name was checked is looked up again: the cache
    # held "united states" as Ridgeview, Alabama for nine days.
    if row is not None and (not row["found"] or _names(row["name"], query)):
        age = dt.datetime.now(dt.UTC) - dt.datetime.fromisoformat(row["fetched_at"]).replace(
            tzinfo=dt.UTC)
        if row["found"]:
            kept = (Place(row["name"], row["latitude"], row["longitude"], row["timezone"],
                          row["country"] or ""), row["country"] or "")
        if age < dt.timedelta(days=KEEP_FOUND_DAYS if row["found"] else KEEP_MISSING_DAYS):
            return kept
    asked = TOWNS_ASKED_IN_REGION if _regions(within) else TOWNS_ASKED
    try:
        data = fetch.get_json(GEOCODE, {"name": query, "count": asked, "language": "en",
                                        "format": "json"})
    except fetch.FetchError as exc:
        # A town placed before is still where it was. Dropping it in an
        # outage took the user's home, and with it their weather.
        if kept is None:
            raise
        log.warning("geocoder unreachable, so %r stays where it was placed: %s", key, exc)
        return kept
    # Largest first: "Boston" is Boston, Massachusetts, not Boston, Georgia.
    data = {"results": sorted(data.get("results") or [],
                              key=lambda r: -(r.get("population") or 0))}
    found = _parse(data, query, within)
    place, country = found if found else (None, None)
    with db.transaction(conn):
        conn.execute(
            "INSERT OR REPLACE INTO places (query, found, name, latitude, longitude, timezone,"
            " country, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))",
            (key, int(place is not None), place.name if place else None,
             place.latitude if place else None, place.longitude if place else None,
             place.timezone if place else None, country),
        )
    return found


def _days(event: dict) -> tuple[dt.date, dt.date] | None:
    try:
        start = dt.date.fromisoformat(str(event.get("start"))[:10])
        end = dt.date.fromisoformat(str(event.get("end") or event.get("start"))[:10])
    except ValueError:
        return None
    if event.get("all_day") and end > start:
        end -= dt.timedelta(days=1)  # an all-day end date is exclusive
    return start, max(start, end)


def upcoming(conn: sqlite3.Connection, events: list[dict], today: dt.date,
             home: Place) -> list[Trip]:
    """Trips in the calendar from today to `HORIZON_DAYS` out, soonest first.
    Away is measured from `home`: with no home, nothing is away."""
    horizon = today + dt.timedelta(days=HORIZON_DAYS)
    trips: list[Trip] = []
    for event in events:
        span = _days(event)
        if span is None or span[1] < today or span[0] > horizon:
            continue
        place, country = None, ""
        for name in candidates(event):
            try:
                found = geocode(conn, name)
            except fetch.FetchError as exc:
                log.info("could not geocode %r: %s", name, exc)
                continue
            if found is not None and km_between(home, found[0]) > AWAY_KM:
                place, country = found
                break
            if found is not None:
                break  # a town near home: not a trip
        if place is None:
            continue
        start, end = max(span[0], today), span[1]
        title = str(event.get("summary") or "")
        verified, why = verify(event, place, country)
        day = dt.timedelta(days=1)
        for trip in trips:
            # Same town, touching or overlapping days: one trip.
            if (trip.place.name == place.name
                    and start <= trip.end + day and end >= trip.start - day):
                trip.start, trip.end = min(trip.start, start), max(trip.end, end)
                trip.events.append(title)
                if verified and not trip.verified:
                    trip.verified, trip.why = True, why
                break
        else:
            trips.append(Trip(place, start, end, [title], country, verified, why))
    return sorted(trips, key=lambda t: t.start)


# Travel itself, in an event's title or location: a flight, a hotel, a train.
_JOURNEY = re.compile(
    r"\b(?:flight|fly(?:ing)?|airport|hotel|airbnb|motel|hostel|amtrak|acela|"
    r"train to|bus to|boarding|departure)\b", re.IGNORECASE)
# A US address's state, as its last parts write it: "..., PA 15222".
_ADDRESS_STATE = re.compile(r"(?:^|,)\s*(?P<state>[A-Z]{2})(?:\s+\d{5}(?:-\d{4})?)?\s*(?:,|$)")
US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan",
    "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey",
    "NM": "New Mexico", "NY": "New York", "NC": "North Carolina", "ND": "North Dakota",
    "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island",
    "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas",
    "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}
# Canada's and Australia's, which addresses abbreviate the same way.
_PROVINCES = {
    "AB": "Alberta", "BC": "British Columbia", "MB": "Manitoba", "NB": "New Brunswick",
    "NL": "Newfoundland and Labrador", "NS": "Nova Scotia", "NT": "Northwest Territories",
    "NU": "Nunavut", "ON": "Ontario", "PE": "Prince Edward Island", "QC": "Quebec",
    "SK": "Saskatchewan", "YT": "Yukon",
    "ACT": "Australian Capital Territory", "NSW": "New South Wales", "QLD": "Queensland",
    "SA": "South Australia", "TAS": "Tasmania", "VIC": "Victoria", "WA": "Western Australia",
}
# What people write for a country, as the geocoder names it. Its two-letter
# code ("GB", "DE") is checked as it is.
_COUNTRY_CALLED = {
    "usa": "united states", "u s a": "united states", "u s": "united states",
    "america": "united states", "united states of america": "united states",
    "uk": "united kingdom", "u k": "united kingdom", "britain": "united kingdom",
    "great britain": "united kingdom",
}


def verify(event: dict, place: Place, country: str) -> tuple[bool, str]:
    """Whether this event shows the user is going to `place`, and why in words.

    Yes for a travel event: its title names the trip ("Trip to Boston",
    "Lisbon holiday"), it is a flight, a hotel or a train, or it spans several
    days. Yes for an address whose own state is the state the place was
    geocoded to ("Row 34, Boston, MA" and Boston, Massachusetts). Anything
    else is one event somewhere a geocoder put it -- the ropes course whose
    address said "PA 15222" while its "place" was in Alabama."""
    title = str(event.get("summary") or "")
    where = str(event.get("location") or "")
    if any(p.search(title) for p in (_TRAVEL, _NAMED_TRIP)):
        return True, f'the event is a trip ("{title[:60]}")'
    if journey := _JOURNEY.search(f"{title} {where}"):
        return True, f'a travel event ("{journey.group(0)}" in "{(title or where)[:60]}")'
    span = _days(event)
    if span is not None and span[1] > span[0]:
        return True, f"{(span[1] - span[0]).days + 1} days there"
    region = place.name.rsplit(",", 1)[-1].strip() if "," in place.name else ""
    if (state := _ADDRESS_STATE.search(where)) and state.group("state") in US_STATES:
        named = US_STATES[state.group("state")]
        if country == "US" and named == region:
            return True, f"the address's own state ({state.group('state')}) is {region}"
        return False, (f"the address says {state.group('state')} ({named}), but the place"
                       f" found was {place.name}")
    return False, "one event, not travel, with no address that agrees with the place"
