"""Example Tasks: the template for a tasks provider with a web API of its own.

To make a module of it, copy this folder to `modules/<id>/`, fill each
line of `manifest.toml` marked FILL, and rewrite each call below for the
service's API. Keep what each method returns and refuses: core's write
path (`core/writes.py`) relies on it, and the tasks contract suite
(`steve_sdk.testing.tasks`) checks it, against `fake.py` once that answers
as the service does (`steve_sdk/templates/__init__.py` says how).

Example's API, which this speaks (`API`):

- `GET /lists`: the account's task lists; `own` marks the one a task goes
  to when no list is named.
- `GET /lists/{list}/tasks?state=open|all&limit=&cursor=`: a list's tasks.
- `GET /tasks/{id}`: one task, whatever its list; 404 once it is gone.
- `POST /lists/{list}/tasks`: a task made. `PATCH /tasks/{id}`: one
  changed, or ticked done.
- Every task has a `version`. A write sends it as `If-Match`, and a 412
  says the task changed since.

What every copy keeps, beyond the mail template's rules (only `ctx.http`,
exactly the item named, a condition where the service has one, the
server's answer checked here, errors core can say):

- **A task ticked is done, everywhere.** Core reads ticks back to close
  the commitment a task mirrors (`agent/taskmirror.py`), so `done` is the
  service's word, and a ticked task is left off the open list.
- **A tick or a change on the phone is not undone here.** A write goes on
  the version read; Steve's older copy never overwrites the user's newer
  one.
"""

from __future__ import annotations

import re
from typing import Any

from steve_sdk import classes
from steve_sdk.classes import Task, TaskList, TasksProvider
from steve_sdk.context import Context
from steve_sdk.errors import (
    ConnectorError,
    NotConnected,
    TargetChanged,
    Transient,
    WrongPassword,
)

API = "https://api.example.com/v1"   # FILL: the service's API, on a host the manifest names
TIMEOUT_S = 30.0
PAGES = 10                           # a listing reads at most this many pages
ID = re.compile(r"[A-Za-z0-9._~-]{1,200}")


# --- the service --------------------------------------------------------------------

def call(ctx: Context, method: str, path: str, *, params: dict[str, str] | None = None,
         body: Any = None, version: str | None = None) -> Any:
    """One request: the JSON answered ({} for none), or None for a 404."""
    headers = {"Authorization": "{{secret:token}}", "Accept": "application/json"}
    if version is not None:
        headers["If-Match"] = version
    try:
        answer = ctx.http.request(method, f"{API}{path}", headers=headers, params=params,
                                  json=body, timeout=TIMEOUT_S)
    except PermissionError as exc:
        # The host rules refused it: the manifest or the account is wrong,
        # which trying again will not mend.
        raise NotConnected(str(exc)) from exc
    if answer.ok:
        return answer.json() if answer.content else {}
    if answer.status == 404:
        return None
    if answer.status in (401, 403):
        raise WrongPassword("the service did not take the token")
    if answer.status == 412:
        raise TargetChanged("the task changed on the service since it was read")
    if answer.status == 429 or answer.status >= 500:
        raise Transient(f"the service answered {answer.status}")
    raise ConnectorError(f"the service answered {answer.status}")


def listing(ctx: Context, path: str, params: dict[str, str], *, limit: int
            ) -> tuple[list[dict[str, Any]], bool]:
    """Up to `limit` items of a listing, page by page; and whether there
    were more than were read."""
    found: list[dict[str, Any]] = []
    cursor = ""
    for _ in range(PAGES):
        asked = {**params, "limit": str(min(max(limit - len(found), 1), 100))}
        if cursor:
            asked["cursor"] = cursor
        page = call(ctx, "GET", path, params=asked) or {}
        found += page.get("items") or []
        cursor = page.get("next") or ""
        if not cursor or len(found) >= limit:
            break
    return found[:limit], bool(cursor) or len(found) > limit


def path_id(native_id: str) -> str:
    """An id as it goes into a path: never one that could name another path."""
    if not ID.fullmatch(native_id or ""):
        raise TargetChanged(f"{native_id!r} is not an id this service gives")
    return native_id


def _task(raw: dict[str, Any]) -> Task:
    return Task(native_id=str(raw["id"]), list_id=str(raw.get("list") or ""),
                title=str(raw.get("title") or ""), notes=str(raw.get("notes") or ""),
                due=str(raw["due"])[:10] if raw.get("due") else None,
                done=bool(raw.get("done")), condition=raw.get("version"),
                link=str(raw.get("link") or ""))


# --- the provider -------------------------------------------------------------------

class ExampleTasks(TasksProvider):
    # FILL: CONDITIONAL only where the service refuses a write on an old version.
    capabilities = frozenset({classes.CONDITIONAL})

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx

    def test(self) -> str:
        """Setup's test and the health check: one read that needs the token."""
        found = self.lists()
        return f"Example's tasks answer for {self.ctx.account.address}, " \
               f"with {len(found)} list{'s' if len(found) != 1 else ''}"

    def _own(self) -> str:
        """The list a task goes to when none is named."""
        raw, _ = listing(self.ctx, "/lists", {}, limit=100)
        for found in raw:
            if found.get("own"):
                return path_id(str(found["id"]))
        raise ConnectorError("the service names no list of the account's own")

    def lists(self) -> list[TaskList]:
        raw, _ = listing(self.ctx, "/lists", {}, limit=100)
        return [TaskList(native_id=str(t["id"]), title=str(t.get("title") or "")) for t in raw]

    def tasks(self, list_id: str | None = None, *, open_only: bool = True, limit: int = 50
              ) -> list[Task]:
        where = path_id(list_id) if list_id else self._own()
        raw, _ = listing(self.ctx, f"/lists/{where}/tasks",
                         {"state": "open" if open_only else "all"}, limit=limit)
        found = [_task(t) for t in raw]
        return [t for t in found if t.list_id == where and not (open_only and t.done)]

    def fetch(self, native_id: str, list_id: str | None = None) -> Task | None:
        raw = call(self.ctx, "GET", f"/tasks/{path_id(native_id)}")
        return None if raw is None else _task(raw)

    def create(self, task: Task) -> Task:
        where = path_id(task.list_id) if task.list_id else self._own()
        made = call(self.ctx, "POST", f"/lists/{where}/tasks", body={
            "title": task.title, "notes": task.notes, "due": task.due})
        if not made or not made.get("id"):
            raise ConnectorError("the service kept no task")
        return _task(made)

    def _write(self, task: Task, body: dict[str, Any]) -> Task:
        changed = call(self.ctx, "PATCH", f"/tasks/{path_id(task.native_id)}", body=body,
                       version=task.condition)
        if changed is None:
            raise TargetChanged("the task was gone when it was to be changed")
        return _task(changed)

    def update(self, task: Task) -> Task:
        """The task's title, notes and due date, written on the version read."""
        return self._write(task, {"title": task.title, "notes": task.notes, "due": task.due})

    def complete(self, task: Task) -> Task:
        """Ticked done, on the version read: one changed on the phone since
        is not ticked over."""
        return self._write(task, {"done": True})
