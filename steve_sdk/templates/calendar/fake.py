"""Example Calendar's API in memory: the template's fake, and a `World` for
the calendar contract suite (`steve_sdk.testing.calendar`).

It answers the routes `provider.py` names, strictly (`steve_sdk.testing.api`):
a write needs the version it was read at, a range lists each occurrence of
a repeating event as its own item, and an occurrence is not changed alone.
It repeats only by day and by week, which is all the suite asks; a copy
rewrites it to answer as its own service does, habits included.
"""

from __future__ import annotations

import datetime as dt
import itertools
from typing import Any

from steve_sdk.testing.api import TOKEN, Answer, Refusal, Request, Service

DAYS = {"DAILY": 1, "WEEKLY": 7}


def _iso(when: dt.datetime) -> str:
    return when.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _moment(text: str) -> dt.datetime:
    when = dt.datetime.fromisoformat(text)
    return when if when.tzinfo else when.replace(tzinfo=dt.UTC)


def _times(spec: dict[str, Any]) -> tuple[str, str]:
    start, end = spec["start"], spec["end"]
    if isinstance(start, dt.datetime):
        return _iso(start), _iso(end)
    return start.isoformat(), end.isoformat()


class CalendarService(Service):
    """The service, holding one calendar of the account at `user`."""

    def __init__(self, user: str = "maya@mail.example", *, token: str = TOKEN) -> None:
        super().__init__(token=token)
        self.user = user
        self._uids = itertools.count(1)

    def _uid(self) -> str:
        return f"{next(self._uids)}-fake@calendar.example"

    def _now(self) -> str:
        return _iso(dt.datetime.now(dt.UTC))

    # --- the World the contract suite drives ----------------------------------------

    def seed(self, klass: str, items: list[dict[str, Any]]) -> list[str]:
        assert klass == "calendar", klass
        ids = []
        for spec in items:
            start, end = _times(spec)
            item = {
                "uid": spec.get("uid") or self._uid(), "title": str(spec.get("title") or ""),
                "start": start, "end": end, "all_day": bool(spec.get("all_day")),
                "location": str(spec.get("location") or ""), "description": "",
                "organizer": spec.get("organizer") or self.user,
                "attendees": [{"email": e, "name": "", "response": "needsAction"}
                              for e in spec.get("guests") or []],
                "recurrence": str(spec.get("rrule") or ""), "series": "", "busy": True,
                "updated": self._now(),
            }
            if item["recurrence"]:
                self._rule(item["recurrence"])      # one this fake cannot repeat is refused now
            ids.append(self.add("event", item)["id"])
        return ids

    def mutate(self, klass: str, native_id: str, **changes: Any) -> None:
        """As another device would: a new title, time or place, or `gone`."""
        assert klass == "calendar", klass
        item = self.one("event", native_id.split(".")[0])
        if changes.pop("gone", False):
            del self.items["event"][item["id"]]
            return
        for key in ("title", "location"):
            if key in changes:
                item[key] = changes[key]
        for key in ("start", "end"):
            if key in changes:
                item[key] = _iso(changes[key])
        item["updated"] = self._now()
        self.touch(item)

    def snapshot(self, klass: str) -> dict[str, Any]:
        """Every event by UID: its id and version."""
        return {item["uid"]: (item["id"], item["version"])
                for item in self.items.get("event", {}).values()}

    # --- repeats ----------------------------------------------------------------------

    def _rule(self, text: str) -> tuple[int, int, dt.datetime | None]:
        rule = dict(part.split("=", 1) for part in text.split(";") if "=" in part)
        if rule.get("FREQ") not in DAYS or not ({"COUNT", "UNTIL"} & set(rule)):
            raise ValueError(f"this fake repeats by day or week, with an end: {text!r}")
        until = rule.get("UNTIL")
        return (DAYS[rule["FREQ"]] * int(rule.get("INTERVAL", "1")),
                int(rule.get("COUNT", "1000")),
                dt.datetime.strptime(until, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.UTC)
                if until else None)

    def _occurrences(self, item: dict[str, Any]) -> list[dict[str, Any]]:
        if not item["recurrence"]:
            return [item]
        step, count, until = self._rule(item["recurrence"])
        start, end = _moment(item["start"]), _moment(item["end"])
        out = []
        for n in range(count):
            begins = start + dt.timedelta(days=step * n)
            if until and begins > until:
                break
            out.append({**item, "id": f"{item['id']}.{begins:%Y%m%dT%H%M%S}",
                        "start": _iso(begins), "end": _iso(begins + (end - start)),
                        "series": item["id"], "recurrence": ""})
        return out

    def _event(self, native: str) -> dict[str, Any]:
        """An event, or one occurrence of a repeating one, by its id."""
        series = self.one("event", native.split(".")[0])
        if "." not in native:
            return series
        for occurrence in self._occurrences(series):
            if occurrence["id"] == native:
                return occurrence
        raise Refusal(404, f"no event has the id {native}")

    # --- the API ----------------------------------------------------------------------

    def route(self, request: Request) -> Answer:
        parts = request.parts
        if parts[:1] != ["v1"]:
            raise Refusal(404, f"no such route: {request.path}")
        match (request.method, parts[1:]):
            case ("GET", ["events"]):
                return self._listing(request)
            case ("GET", ["events", native]):
                return Answer(200, dict(self._event(native)))
            case ("POST", ["events"]):
                return Answer(201, dict(self._create(request.body or {})))
            case ("PATCH", ["events", native]):
                return self._patch(request, native)
            case ("DELETE", ["events", native]):
                item = self._writable(request, native)
                del self.items["event"][item["id"]]
                return Answer(204)
        raise Refusal(404, f"no such route: {request.method} {request.path}")

    def _listing(self, request: Request) -> Answer:
        stored = list(self.items.get("event", {}).values())
        if not (request.query.get("start") and request.query.get("end")):
            return self.page(stored, request)
        start, end = _moment(request.query["start"]), _moment(request.query["end"])
        found = [o for item in stored for o in self._occurrences(item)
                 if _moment(o["start"]) < end and _moment(o["end"]) > start]
        found.sort(key=lambda o: _moment(o["start"]))
        return self.page(found, request)

    def _create(self, body: dict[str, Any]) -> dict[str, Any]:
        if body.get("attendees"):
            raise Refusal(400, "this fake sends no invitations")
        return self.add("event", {
            "uid": self._uid(), "title": str(body.get("title") or ""),
            "start": str(body["start"]), "end": str(body["end"]),
            "all_day": bool(body.get("all_day")), "location": str(body.get("location") or ""),
            "description": str(body.get("description") or ""), "organizer": self.user,
            "attendees": [], "recurrence": "", "series": "",
            "busy": bool(body.get("busy", True)), "updated": self._now()})

    def _writable(self, request: Request, native: str) -> dict[str, Any]:
        if "." in native:
            raise Refusal(409, "an occurrence is not changed alone here")
        item = self.one("event", native)
        self.condition(request, item)
        return item

    def _patch(self, request: Request, native: str) -> Answer:
        item = self._writable(request, native)
        body = request.body or {}
        for key in ("title", "start", "end", "all_day", "location", "description", "busy"):
            if key in body:
                item[key] = body[key]
        item["updated"] = self._now()
        self.touch(item)
        return Answer(200, dict(item))
