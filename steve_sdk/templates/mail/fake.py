"""Example Mail's API in memory: the template's fake, and a `World` for the
mail contract suite (`steve_sdk.testing.mail`).

It answers the routes `provider.py` names, strictly (`steve_sdk.testing.api`):
a write needs the version it was read at, and a listing filters by folder,
words and date as the service would. A copy rewrites it to answer as its
own service does, and adds that service's habits -- a search that ignores
its filter, ids that change when a folder is rebuilt -- since those are
what a provider must survive.
"""

from __future__ import annotations

import datetime as dt
import itertools
from typing import Any

from steve_sdk.exact import norm
from steve_sdk.testing.api import TOKEN, Answer, Refusal, Request, Service

ROLES = {"inbox": "inbox", "sent": "sent", "drafts": "drafts"}
STRANGER = "Someone <someone@else.example>"


def _iso(when: dt.datetime) -> str:
    return when.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _person(text: str) -> dict[str, str]:
    name, _, rest = text.rpartition("<")
    email = rest.rstrip(">").strip() if rest.endswith(">") else text.strip()
    return {"name": name.strip(), "email": email}


def _people(value: Any) -> list[dict[str, str]]:
    if isinstance(value, list):
        return [dict(p) for p in value]
    return [_person(part) for part in str(value or "").split(",") if "@" in part]


class MailService(Service):
    """The service, holding one account's mail at `user`."""

    def __init__(self, user: str = "maya@mail.example", *, token: str = TOKEN) -> None:
        super().__init__(token=token)
        self.user = user
        self._numbers = itertools.count(1)

    def _message_id(self) -> str:
        return f"<{next(self._numbers)}.fake@mail.example>"

    def _store(self, item: dict[str, Any]) -> dict[str, Any]:
        item.setdefault("thread", f"t{next(self._numbers)}")
        return self.add("message", item)

    # --- the World the contract suite drives ----------------------------------------

    def seed(self, klass: str, items: list[dict[str, Any]]) -> list[str]:
        assert klass == "mail", klass
        ids = []
        for spec in items:
            folder = ROLES[spec.get("role", "inbox")]
            subject = str(spec.get("subject") or "")
            mine = folder != "inbox"
            item = {
                "folder": folder,
                "message_id": spec.get("message_id") or self._message_id(),
                "from": _person(spec.get("sender") or (self.user if mine else STRANGER)),
                "to": _people(spec.get("to") or self.user),
                "cc": _people(spec.get("cc") or ""), "bcc": [],
                "subject": subject,
                "body": spec.get("body") or f"About {subject}.",
                "date": _iso(spec.get("date") or dt.datetime.now(dt.UTC)),
                "unread": bool(spec.get("unread")),
                "headers": dict(spec.get("headers") or {}),
            }
            if spec.get("thrid"):
                item["thread"] = str(spec["thrid"])
            ids.append(self._store(item)["id"])
        return ids

    def mutate(self, klass: str, native_id: str, **changes: Any) -> None:
        """As another device would: a new body, subject or read state; the
        message `gone`; or the store `rebuilt`, every message under a new id."""
        assert klass == "mail", klass
        held = self.items.setdefault("message", {})
        if changes.pop("rebuilt", False):
            old = list(held.values())
            held.clear()
            for item in old:
                item["id"] = self.new_id("message")
                held[item["id"]] = item
            return
        item = self.one("message", native_id)
        if changes.pop("gone", False):
            del held[native_id]
            return
        for key in ("body", "subject", "unread"):
            if key in changes:
                item[key] = changes[key]
        self.touch(item)

    def snapshot(self, klass: str) -> dict[str, Any]:
        """Every message by Message-ID: each copy's folder, version and read state."""
        out: dict[str, list] = {}
        for item in self.items.get("message", {}).values():
            out.setdefault(norm(item["message_id"]), []).append(
                (item["folder"], item["version"], item["unread"]))
        return {key: sorted(copies) for key, copies in out.items()}

    # --- the API ----------------------------------------------------------------------

    def route(self, request: Request) -> Answer:
        parts = request.parts
        if parts[:1] != ["v1"]:
            raise Refusal(404, f"no such route: {request.path}")
        match (request.method, parts[1:]):
            case ("GET", ["messages"]):
                return self._listing(request)
            case ("GET", ["messages", native]):
                return Answer(200, self._whole(self.one("message", native)))
            case ("PATCH", ["messages", native]):
                return self._patch(request, native)
            case ("POST", ["drafts"]):
                return Answer(201, self._whole(self._draft(request.body or {})))
            case ("POST", ["messages", native, "send"]):
                return self._send(request, native)
            case ("DELETE", ["messages", native]):
                return self._trash(request, native)
        raise Refusal(404, f"no such route: {request.method} {request.path}")

    def _whole(self, item: dict[str, Any]) -> dict[str, Any]:
        return {**item, "snippet": item["body"][:100]}

    def _envelope(self, item: dict[str, Any], *, body: bool) -> dict[str, Any]:
        shown = {k: v for k, v in self._whole(item).items() if k != "headers"}
        if not body:
            shown.pop("body")
        return shown

    def _listing(self, request: Request) -> Answer:
        query = request.query
        folder = query.get("folder")
        words = [w for w in (query.get("q") or "").lower().split() if w]
        found = []
        for item in self.items.get("message", {}).values():
            if item["folder"] == "trash" and folder != "trash":
                continue
            if folder and item["folder"] != folder:
                continue
            if query.get("since") and item["date"] < query["since"]:
                continue
            if query.get("thread") and item["thread"] != query["thread"]:
                continue
            text = " ".join([item["subject"], item["body"], item["from"]["email"],
                             item["from"]["name"], *(p["email"] for p in item["to"])]).lower()
            # Loose, as a service's relevance search is: any word, anywhere.
            if words and not any(w in text for w in words):
                continue
            found.append(item)
        found.sort(key=lambda i: i["date"], reverse=True)
        body = query.get("body") == "1"
        return self.page(found, request, lambda i: self._envelope(i, body=body))

    def _patch(self, request: Request, native: str) -> Answer:
        item = self.one("message", native)
        self.condition(request, item)
        if "unread" in (request.body or {}):
            item["unread"] = bool(request.body["unread"])
        self.touch(item)
        return Answer(200, self._whole(item))

    def _draft(self, body: dict[str, Any]) -> dict[str, Any]:
        item = {"folder": "drafts", "message_id": self._message_id(),
                "from": _person(self.user), "to": _people(body.get("to")),
                "cc": _people(body.get("cc")), "bcc": _people(body.get("bcc")),
                "subject": str(body.get("subject") or ""), "body": str(body.get("body") or ""),
                "date": _iso(dt.datetime.now(dt.UTC)), "unread": False, "headers": {}}
        if body.get("in_reply_to"):
            item["thread"] = self.one("message", str(body["in_reply_to"]))["thread"]
        return self._store(item)

    def _send(self, request: Request, native: str) -> Answer:
        item = self.one("message", native)
        if item["folder"] != "drafts":
            raise Refusal(409, "only a draft is sent")
        self.condition(request, item)
        del self.items["message"][native]
        sent = self._store({**item, "folder": "sent", "date": _iso(dt.datetime.now(dt.UTC))})
        return Answer(200, self._whole(sent))

    def _trash(self, request: Request, native: str) -> Answer:
        item = self.one("message", native)
        self.condition(request, item)
        del self.items["message"][native]
        self._store({**item, "folder": "trash"})
        return Answer(204)
