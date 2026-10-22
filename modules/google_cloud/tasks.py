"""Google Tasks' calls, beneath `modules/google_cloud/provider.GoogleTasks`.

The mirror of commitments to a tasks list, which lived here until step 5,
is `agent/taskmirror.py`, and speaks to whichever tasks account there is.
What stays is Google's side of it: the agent's own list, found or made, and
a task added to it or ticked.

Needs no new consent: `modules/google_cloud/auth.py` has asked for the `tasks` scope since
it was written.
"""

from __future__ import annotations

import logging
from datetime import datetime

from .common import guard, service

log = logging.getLogger("tasks")

# The list Steve writes to. Its own, so nothing here can disturb a list the
# user keeps by hand -- and so "delete Steve's tasks" is one action.
LIST_TITLE = "Steve"


def _tasks():
    return service("tasks", "v1")


@guard
def lists() -> dict:
    data = _tasks().tasklists().list(maxResults=50).execute()
    return {
        "lists": [
            {"id": item["id"], "title": item.get("title", "")}
            for item in data.get("items", [])
        ]
    }


def _agent_list_id() -> str | None:
    """The agent's own list, created on first use."""
    found = lists()
    if found.get("error"):
        return None
    for item in found["lists"]:
        if item["title"] == LIST_TITLE:
            return item["id"]
    created = _tasks().tasklists().insert(body={"title": LIST_TITLE}).execute()
    return created.get("id")


def _due(date: str | None) -> str | None:
    """Google wants RFC 3339 and ignores the time part of a task due date."""
    if not date:
        return None
    try:
        return datetime.fromisoformat(date).strftime("%Y-%m-%dT00:00:00.000Z")
    except ValueError:
        return None


@guard
def create(title: str, *, notes: str = "", due: str | None = None) -> dict:
    list_id = _agent_list_id()
    if not list_id:
        return {"error": "could not reach Google Tasks", "kind": "api"}
    body: dict = {"title": title[:1024]}
    if notes:
        body["notes"] = notes[:8192]
    if (when := _due(due)):
        body["due"] = when
    task = _tasks().tasks().insert(tasklist=list_id, body=body).execute()
    return {"id": task.get("id"), "list_id": list_id, "title": task.get("title")}


@guard
def complete(task_id: str, list_id: str | None = None) -> dict:
    list_id = list_id or _agent_list_id()
    if not list_id:
        return {"error": "could not reach Google Tasks", "kind": "api"}
    task = (
        _tasks().tasks()
        .patch(tasklist=list_id, task=task_id, body={"status": "completed"})
        .execute()
    )
    return {"id": task.get("id"), "status": task.get("status")}
