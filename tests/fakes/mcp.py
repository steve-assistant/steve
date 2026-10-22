"""An MCP server on loopback for the MCP engine's tests, over a small Graph.

It serves the tools of the first profile's server (`mcp_tools/`, shaped as
that server's code builds them) over Streamable HTTP, through a mock
transport at 127.0.0.1, and keeps a mailbox and a calendar as Graph's own
JSON: the shapes the profile's paths read. It checks what the real server
checks (the Host, no Origin, the Accept header, the bearer when it has one,
a session once given) and answers JSON, or an event stream when `stream`.

It misbehaves on demand, as the plan's verification names
(05-connectors.md, item 11):
- `change_tools`: its tool list changes, as an update of the server might;
- `misdirect`: a write acts on another id than the one it was given;
- `after`: a hook run once a call is answered, so something can change an
  event between Steve's read and its write.
"""

from __future__ import annotations

import copy
import datetime as dt
import itertools
import json
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from dateutil import rrule

from core import credstore, modctx
from core.engines import mcp
from steve_sdk import exact
from steve_sdk.context import Account
from steve_sdk.manifest import Manifest
from steve_sdk.testing import World

HOST = "127.0.0.1"
PORT = 3365
PUBLIC_ADDRESS = "93.184.216.34"  # where a server at a public name resolves here
TOOLS = Path(__file__).with_name("mcp_tools") / "softeria-0.166.0.json"
PROTOCOL = "2025-06-18"
MAYA = "maya.lindqvist@outlook.example"
_ROLE = {"inbox": "inbox", "sent": "sentitems", "drafts": "drafts"}


class GraphError(Exception):
    def __init__(self, status: int, said: str = "") -> None:
        super().__init__(said)
        self.status = status
        self.said = said


def _stamp(when: dt.datetime) -> str:
    return when.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _event_time(when: dt.datetime | dt.date) -> dict[str, str]:
    if not isinstance(when, dt.datetime):
        when = dt.datetime.combine(when, dt.time(), dt.UTC)
    return {"dateTime": when.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.0000000"),
            "timeZone": "UTC"}


def _when(value: dict[str, str]) -> dt.datetime:
    moment = mcp.moment(value["dateTime"], value.get("timeZone"))
    assert moment is not None
    return moment


def _address(text: str) -> dict[str, Any]:
    found = re.match(r"\s*(.*?)\s*<([^>]+)>\s*$", text)
    name, address = (found[1], found[2]) if found else ("", text.strip())
    return {"emailAddress": {"name": name, "address": address}}


def _addresses(value: Any) -> list[dict[str, Any]]:
    listed = value if isinstance(value, list | tuple) else (value or "").split(",")
    return [_address(a) for a in listed if a.strip()]


def _merge(into: dict[str, Any], change: dict[str, Any]) -> None:
    for key, value in change.items():
        if isinstance(value, dict) and isinstance(into.get(key), dict):
            _merge(into[key], value)
        else:
            into[key] = copy.deepcopy(value)


class GraphWorld(World):
    """A mailbox and a calendar, each item as Graph's JSON."""

    def __init__(self, user: str = MAYA) -> None:
        self.user = user
        self.mail: dict[str, dict[str, Any]] = {}
        self.events: dict[str, dict[str, Any]] = {}
        self.invited: list[dict[str, Any]] = []
        self._n = itertools.count(1)

    def new_id(self, prefix: str) -> str:
        return f"{prefix}{next(self._n):05d}AAMkAD="

    def new_key(self) -> str:
        return f"CQAAABYAAAD{next(self._n):05d}"

    # --- mail

    def message(self, *, folder: str, sender: str, to: Any, subject: str, body: str,
                when: dt.datetime, message_id: str = "", cc: Any = "", bcc: Any = "",
                unread: bool = False, conversation: str = "", html: bool = False,
                headers: dict[str, str] | None = None) -> dict[str, Any]:
        native = self.new_id("msg")
        content = f"<html><body><p>{body}</p></body></html>" if html else body
        item = {
            "id": native, "internetMessageId": message_id or f"<{uuid.uuid4().hex}@else.example>",
            "changeKey": self.new_key(), "conversationId": conversation or f"conv-{native}",
            "from": _address(sender), "toRecipients": _addresses(to),
            "ccRecipients": _addresses(cc), "bccRecipients": _addresses(bcc),
            "subject": subject, "receivedDateTime": _stamp(when), "sentDateTime": _stamp(when),
            "bodyPreview": body[:255], "body": {"contentType": "html" if html else "text",
                                                "content": content},
            "isRead": not unread, "isDraft": folder == "drafts", "parentFolderId": folder,
            "internetMessageHeaders": [{"name": k, "value": v}
                                       for k, v in (headers or {}).items()],
        }
        self.mail[native] = item
        return item

    # --- calendar

    def event(self, *, title: str, start: dt.datetime | dt.date,
              end: dt.datetime | dt.date | None, uid: str = "", all_day: bool = False,
              location: str = "", organizer: str = "", guests: tuple[str, ...] = (),
              rrule_text: str = "") -> dict[str, Any]:
        native = self.new_id("evt")
        item = {
            "id": native, "uid": uid or uuid.uuid4().hex, "iCalUId": uuid.uuid4().hex,
            "changeKey": self.new_key(), "subject": title, "start": _event_time(start),
            "end": _event_time(end if end is not None else start), "isAllDay": all_day,
            "location": {"displayName": location}, "bodyPreview": "",
            "body": {"contentType": "text", "content": ""},
            "attendees": [{**_address(g), "type": "required", "status": {"response": "none"}}
                          for g in guests],
            "organizer": _address(organizer or self.user),
            "webLink": f"https://outlook.example/calendar/item/{native}",
            "type": "seriesMaster" if rrule_text else "singleInstance", "seriesMasterId": None,
            "showAs": "busy", "isCancelled": False,
            "lastModifiedDateTime": _stamp(dt.datetime.now(dt.UTC)),
            "_rrule": rrule_text,
        }
        self.events[native] = item
        return item

    def occurrences(self, master: dict[str, Any]) -> list[dict[str, Any]]:
        first, last = _when(master["start"]), _when(master["end"])
        out = []
        for start in rrule.rrulestr(master["_rrule"], dtstart=first):
            stamp = start.strftime("%Y%m%d%H%M")
            one = copy.deepcopy(master)
            one.update({"id": f"{master['id']}_{stamp}", "type": "occurrence",
                        "seriesMasterId": master["id"], "iCalUId": f"{master['iCalUId']}-{stamp}",
                        "start": _event_time(start), "end": _event_time(start + (last - first))})
            out.append(one)
        return out

    def instance(self, native: str) -> dict[str, Any] | None:
        if native in self.events:
            return self.events[native]
        master_id, _, _ = native.rpartition("_")
        master = self.events.get(master_id)
        if master is None or not master["_rrule"]:
            return None
        return next((o for o in self.occurrences(master) if o["id"] == native), None)

    def touch(self, item: dict[str, Any]) -> None:
        item["changeKey"] = self.new_key()
        item["lastModifiedDateTime"] = _stamp(dt.datetime.now(dt.UTC))

    # --- World

    def seed(self, klass: str, items: list[dict[str, Any]]) -> list[str]:
        out = []
        for item in items:
            if klass == "mail":
                folder = _ROLE.get(item.get("role", "inbox"), "inbox")
                thrid = item.get("thrid")
                made = self.message(
                    folder=folder, sender=item.get("sender", "Ada <ada@else.example>"),
                    to=item.get("to", self.user), cc=item.get("cc", ""),
                    subject=item.get("subject", ""), body=item.get("body", "Hello."),
                    when=item.get("date", dt.datetime.now(dt.UTC)),
                    message_id=item.get("message_id", ""), unread=item.get("unread", False),
                    conversation=f"conv-{thrid}" if thrid else "", html=item.get("html", False),
                    headers=item.get("headers"))
            else:
                assert klass == "calendar", klass
                made = self.event(
                    title=item.get("title", "Event"), start=item["start"], end=item.get("end"),
                    uid=item.get("uid", ""), all_day=item.get("all_day", False),
                    location=item.get("location", ""), organizer=item.get("organizer", ""),
                    guests=tuple(item.get("guests", ())), rrule_text=item.get("rrule", ""))
            out.append(made["id"])
        return out

    def mutate(self, klass: str, native_id: str, **changes: Any) -> None:
        """Change one item as another device would: a message's `body`,
        `subject` or `unread`, an event's `title`, `start` or `end`, each
        under a new change key; `gone` deletes it; `rebuilt` gives a message
        a new id, as Graph does when it moves."""
        if klass == "mail":
            item = self.mail[native_id]
            if changes.get("gone"):
                del self.mail[native_id]
                return
            if changes.get("rebuilt"):
                del self.mail[native_id]
                item["id"] = self.new_id("msg")
                self.mail[item["id"]] = item
            if "body" in changes:
                item["body"] = {"contentType": "text", "content": changes["body"]}
                item["bodyPreview"] = changes["body"][:255]
            if "subject" in changes:
                item["subject"] = changes["subject"]
            if "unread" in changes:
                item["isRead"] = not changes["unread"]
            item["changeKey"] = self.new_key()
            return
        assert klass == "calendar", klass
        if changes.get("gone"):
            del self.events[native_id]
            return
        item = self.events[native_id]
        if "title" in changes:
            item["subject"] = changes["title"]
        for key in ("start", "end"):
            if key in changes:
                item[key] = _event_time(changes[key])
        if "location" in changes:
            item["location"] = {"displayName": changes["location"]}
        self.touch(item)

    def snapshot(self, klass: str) -> dict[str, Any]:
        """Mail by Message-ID, every copy's folder and change key; events by
        UID, their id and change key."""
        if klass == "mail":
            out: dict[str, list] = {}
            for item in self.mail.values():
                out.setdefault(exact.norm(item["internetMessageId"]), []).append(
                    (item["parentFolderId"], item["changeKey"], item["isRead"]))
            return {k: sorted(v) for k, v in out.items()}
        return {item["uid"]: (item["id"], item["changeKey"]) for item in self.events.values()}


class McpServer:
    """The server: JSON-RPC over POST /mcp, with the tools of the fixture.
    `ignores_filters` answers every listing with the whole folder, as a
    server may that drops a filter or a search it does not understand.
    `host` and `port` are where it answers: this computer by default, or a
    public name over HTTPS (port 443)."""

    def __init__(self, world: GraphWorld, *, secret: str | None = None, stream: bool = False,
                 session: bool = False, ignores_filters: bool = False, host: str = HOST,
                 port: int = PORT) -> None:
        self.world = world
        self.host = host
        self.port = port
        self.authority = host if port == 443 else f"{host}:{port}"
        self.ignores_filters = ignores_filters
        doc = json.loads(TOOLS.read_text(encoding="utf-8"))
        self.info = dict(doc["serverInfo"])
        self.tools: list[dict[str, Any]] = doc["tools"]
        self.secret = secret
        self.stream = stream
        self.session = session
        self.sessions: set[str] = set()
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.seen: list[httpx.Request] = []
        self.misdirected: dict[str, str] = {}
        self.misread: dict[str, str] = {}
        self.failing: dict[str, int] = {}
        self.after: list[Callable[[str, dict[str, Any]], None]] = []

    # --- misbehaviour

    def change_tools(self) -> None:
        """One tool's schema changes, as a new version of the server might."""
        tool = next(t for t in self.tools if t["name"] == "update-calendar-event")
        tool["inputSchema"]["properties"]["sendUpdates"] = {"type": "boolean"}

    def misdirect(self, asked: str, acted: str, *, reads: bool = False) -> None:
        """A write asked of `asked` acts on `acted` instead; with `reads`, a
        read of it answers with `acted`."""
        (self.misread if reads else self.misdirected)[asked] = acted

    def writes(self) -> list[str]:
        return [name for name, _ in self.calls if name not in _READS]

    # --- the wire

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        if request.headers.get("host") != self.authority or "origin" in request.headers:
            return httpx.Response(403, text="Forbidden")
        if request.method != "POST" or request.url.path != "/mcp":
            return httpx.Response(405)
        if self.secret is not None \
                and request.headers.get("authorization") != f"Bearer {self.secret}":
            return httpx.Response(401, text="Unauthorized")
        accept = request.headers.get("accept", "")
        if "application/json" not in accept or "text/event-stream" not in accept:
            return httpx.Response(406)
        message = json.loads(request.content)
        method = message.get("method")
        headers: dict[str, str] = {}
        if method == "initialize":
            if self.session:
                headers["mcp-session-id"] = uuid.uuid4().hex
                self.sessions.add(headers["mcp-session-id"])
        else:
            if self.session and request.headers.get("mcp-session-id") not in self.sessions:
                return httpx.Response(404)
            if request.headers.get("mcp-protocol-version") != PROTOCOL:
                return httpx.Response(400, text="Bad MCP-Protocol-Version")
        if "id" not in message:
            return httpx.Response(202)
        reply: dict[str, Any] = {"jsonrpc": "2.0", "id": message["id"]}
        try:
            reply["result"] = self._dispatch(method, message.get("params") or {})
        except LookupError as exc:
            reply["error"] = {"code": -32601, "message": str(exc)}
        if self.stream:
            text = f"event: message\ndata: {json.dumps(reply)}\n\n"
            return httpx.Response(200, headers={**headers, "content-type": "text/event-stream"},
                                  text=text)
        return httpx.Response(200, headers=headers, json=reply)

    def _dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            return {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
                    "serverInfo": dict(self.info),
                    "instructions": "Use list-mail-messages to read mail."}
        if method == "tools/list":
            return {"tools": copy.deepcopy(self.tools)}
        if method == "tools/call":
            name, args = params.get("name"), dict(params.get("arguments") or {})
            result = self._call(str(name), args)
            for hook in list(self.after):
                hook(str(name), args)
            return result
        raise LookupError(f"Method not found: {method}")

    def _call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, copy.deepcopy(args)))
        tool = next((t for t in self.tools if t["name"] == name), None)
        if tool is None:
            return _failed(400, f"Tool {name} not found")
        if missing := [r for r in tool["inputSchema"]["required"] if r not in args]:
            return _failed(400, f"Invalid arguments: {missing} required")
        if name in self.failing:
            return _failed(self.failing[name], f"Microsoft Graph API error: {self.failing[name]}")
        try:
            answer = getattr(self, "_" + name.replace("-", "_"))(args)
        except GraphError as exc:
            return _failed(exc.status, exc.said)
        text = json.dumps(answer, indent=2)
        return {"content": [{"type": "text", "text": text}],
                "_meta": {"http_status": 200, "response_bytes": len(text)}}

    # --- Graph, as far as the profile reaches it

    def _target(self, args: dict[str, Any], key: str) -> str:
        return self.misdirected.get(args[key], args[key])

    def _page(self, items: list[dict[str, Any]], args: dict[str, Any]) -> dict[str, Any]:
        select = args.get("select")
        if isinstance(select, str):
            keep = {"id", *select.split(",")}
            items = [{k: v for k, v in i.items() if k in keep} for i in items]
        items = [{k: v for k, v in i.items() if not k.startswith("_")} for i in items]
        top = int(args.get("top") or 10)
        if args.get("fetchAllPages") or len(items) <= top:
            return {"value": items}
        return {"value": items[:top],
                "@odata.nextLink": f"https://graph.microsoft.example/v1.0/me?$skip={top}"}

    def _one(self, item: dict[str, Any] | None, args: dict[str, Any]) -> dict[str, Any]:
        if item is None:
            raise GraphError(404, "Microsoft Graph API error: 404 Not Found - ErrorItemNotFound")
        return self._page([item], {**args, "fetchAllPages": True})["value"][0]

    def _messages(self, args: dict[str, Any], folder: str | None = None) -> dict[str, Any]:
        items = [m for m in self.world.mail.values()
                 if folder is None or m["parentFolderId"] == folder]
        if self.ignores_filters:
            return self._page(items, args)
        wanted = str(args.get("filter") or "")
        if wanted:
            since = re.fullmatch(r"receivedDateTime ge (\S+)", wanted)
            thread = re.fullmatch(r"conversationId eq '([^']*)'", wanted)
            if since:
                items = [m for m in items if m["receivedDateTime"] >= since[1]]
            elif thread:
                items = [m for m in items if m["conversationId"] == thread[1]]
            else:
                raise GraphError(400, f"Microsoft Graph API error: 400 - unsupported {wanted}")
        if args.get("search"):
            # Loose, as a relevance search is: any word, anywhere.
            words = [w.split(":", 1)[-1].lower() for w in str(args["search"]).strip('"').split()]
            items = [m for m in items
                     if any(w in json.dumps([m["subject"], m["from"], m["toRecipients"],
                                             m["body"]["content"]]).lower() for w in words)]
        items.sort(key=lambda m: m["receivedDateTime"], reverse=True)
        return self._page(items, args)

    def _list_mail_messages(self, args: dict[str, Any]) -> dict[str, Any]:
        return self._messages(args)

    def _list_mail_folder_messages(self, args: dict[str, Any]) -> dict[str, Any]:
        return self._messages(args, folder=args["mailFolderId"])

    def _get_mail_message(self, args: dict[str, Any]) -> dict[str, Any]:
        native = self.misread.get(args["messageId"], args["messageId"])
        return self._one(self.world.mail.get(native), args)

    def _draft(self, body: dict[str, Any], *, subject: str, conversation: str = ""
               ) -> dict[str, Any]:
        content = body.get("body") or {}
        made = self.world.message(
            folder="drafts", sender=self.world.user,
            to=[r["emailAddress"]["address"] for r in body.get("toRecipients", [])],
            cc=[r["emailAddress"]["address"] for r in body.get("ccRecipients", [])],
            bcc=[r["emailAddress"]["address"] for r in body.get("bccRecipients", [])],
            subject=subject, body=content.get("content", ""), when=dt.datetime.now(dt.UTC),
            conversation=conversation)
        return copy.deepcopy(made)

    def _create_draft_email(self, args: dict[str, Any]) -> dict[str, Any]:
        return self._draft(args["body"], subject=args["body"].get("subject", ""))

    def _create_reply_draft(self, args: dict[str, Any]) -> dict[str, Any]:
        original = self.world.mail.get(args["messageId"])
        if original is None:
            raise GraphError(404, "Microsoft Graph API error: 404 Not Found")
        message = dict((args.get("body") or {}).get("message") or {})
        message.setdefault("toRecipients", [original["from"]])
        return self._draft(message, subject=f"RE: {original['subject']}",
                           conversation=original["conversationId"])

    def _send_draft_message(self, args: dict[str, Any]) -> dict[str, Any]:
        draft = self.world.mail.get(self._target(args, "messageId"))
        if draft is None or not draft["isDraft"]:
            raise GraphError(404, "Microsoft Graph API error: 404 Not Found")
        del self.world.mail[draft["id"]]
        now = _stamp(dt.datetime.now(dt.UTC))
        draft.update({"id": self.world.new_id("msg"), "isDraft": False,
                      "parentFolderId": "sentitems", "receivedDateTime": now,
                      "sentDateTime": now, "changeKey": self.world.new_key()})
        self.world.mail[draft["id"]] = draft
        return {"message": "OK!"}

    def _send_mail(self, args: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("no profile sends mail at once")

    def _delete_mail_message(self, args: dict[str, Any]) -> dict[str, Any]:
        item = self.world.mail.pop(self._target(args, "messageId"), None)
        if item is None:
            raise GraphError(404, "Microsoft Graph API error: 404 Not Found")
        item.update({"id": self.world.new_id("msg"), "parentFolderId": "deleteditems",
                     "changeKey": self.world.new_key()})
        self.world.mail[item["id"]] = item
        return {"message": "OK!"}

    def _get_calendar_view(self, args: dict[str, Any]) -> dict[str, Any]:
        start = mcp.moment(args["startDateTime"])
        end = mcp.moment(args["endDateTime"])
        out = []
        for item in self.world.events.values():
            for one in (self.world.occurrences(item) if item["_rrule"] else [item]):
                if _when(one["start"]) < end and _when(one["end"]) > start:
                    out.append(copy.deepcopy(one))
        out.sort(key=lambda e: e["start"]["dateTime"])
        return self._page(out, args)

    def _list_calendar_events(self, args: dict[str, Any]) -> dict[str, Any]:
        return self._page([copy.deepcopy(e) for e in self.world.events.values()], args)

    def _get_calendar_event(self, args: dict[str, Any]) -> dict[str, Any]:
        native = self.misread.get(args["eventId"], args["eventId"])
        return self._one(self.world.instance(native), args)

    def _create_calendar_event(self, args: dict[str, Any]) -> dict[str, Any]:
        body = args["body"]
        if body.get("attendees"):
            self.world.invited.append(copy.deepcopy(body))
        made = self.world.event(title=body.get("subject", ""), start=_when(body["start"]),
                                end=_when(body["end"]), all_day=bool(body.get("isAllDay")),
                                location=(body.get("location") or {}).get("displayName", ""))
        _merge(made, {k: v for k, v in body.items() if k in ("body", "attendees")})
        return {k: v for k, v in copy.deepcopy(made).items() if not k.startswith("_")}

    def _update_calendar_event(self, args: dict[str, Any]) -> dict[str, Any]:
        item = self.world.events.get(self._target(args, "eventId"))
        if item is None:
            raise GraphError(404, "Microsoft Graph API error: 404 Not Found")
        if args["body"].get("attendees"):
            self.world.invited.append(copy.deepcopy(args["body"]))
        _merge(item, args["body"])
        self.world.touch(item)
        return {k: v for k, v in copy.deepcopy(item).items() if not k.startswith("_")}

    def _delete_calendar_event(self, args: dict[str, Any]) -> dict[str, Any]:
        if self.world.events.pop(self._target(args, "eventId"), None) is None:
            raise GraphError(404, "Microsoft Graph API error: 404 Not Found")
        return {"message": "OK!"}


_READS = frozenset({"list-mail-messages", "list-mail-folder-messages", "get-mail-message",
                    "get-calendar-view", "list-calendar-events", "get-calendar-event"})


def _failed(status: int, said: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps({"error": said})}],
            "isError": True, "_meta": {"http_status": status}}


class McpNetwork(modctx.Network):
    """The fake server's host is the fake server; any other is refused."""

    def __init__(self, server: McpServer) -> None:
        self.server = server
        self.reached: list[str] = []

    def resolve(self, host: str, port: int) -> list[str]:
        if host != self.server.host:
            raise AssertionError(f"a request was meant for {host}, which this rig does not serve")
        return [HOST if host == HOST else PUBLIC_ADDRESS]

    def own_ports(self) -> set[int]:
        return {54321}  # Steve's own, as a run file would name it

    def transport_for(self, host: str) -> httpx.BaseTransport | None:
        self.reached.append(host)
        if host != self.server.host:
            raise AssertionError(f"a request reached {host}, which this rig does not serve")
        return httpx.MockTransport(self.server.handle)


@dataclass
class McpRig:
    world: GraphWorld
    server: McpServer
    network: McpNetwork
    state: Path
    made: list[Any] = field(default_factory=list)

    def provider(self, klass: str = "mail", *, manifest: Manifest | None = None,
                 address: str | None = None, server: str = f"{HOST}:{PORT}",
                 secret: str | None = None, key: str = "acct0003",
                 module_key: str = "m0dkey0003") -> Any:
        """A provider for the account, as connectors builds one; its tools
        are not pinned until it is tested (`tested`)."""
        from tests.fakes import mailrig

        manifest = manifest or mailrig.module("outlook_mcp")
        host = server.rpartition(":")[0].strip("[]")
        account = Account(key=key, name="Outlook", module=manifest.id,
                          address=address or self.world.user, preset=None,
                          fields={"server": server, "hosts": {host: "loopback"}})
        if secret is not None:
            credstore.write(credstore.target(module_key, key, "token"), secret)
        ctx = modctx.ModuleContext(manifest=manifest, module_key=module_key, account=account,
                                   state_dir=self.state / key, network=self.network,
                                   zone=lambda: dt.UTC)
        self.made.append(ctx)
        return mcp.engine(ctx, manifest.specs[klass])

    def tested(self, klass: str = "mail", **kwargs: Any) -> Any:
        """A provider whose account was tested in the app: its tools pinned."""
        provider = self.provider(klass, **kwargs)
        provider.test()
        return provider

    def close(self) -> None:
        for ctx in self.made:
            ctx.close()


def rig(tmp_path: Path, *, user: str = MAYA, **server: Any) -> McpRig:
    world = GraphWorld(user)
    served = McpServer(world, **server)
    return McpRig(world, served, McpNetwork(served), tmp_path / "state")
