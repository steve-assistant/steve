"""What Steve knows: the facts and memories it holds, for the user to read,
correct and forget (docs/design/back-pages.md, About you).

What Steve kept about the user was readable only by asking it, and
forgettable only by a number the morning digest gave ("FORGET MEMORY
1042"), so a wrong fact stayed until it surfaced in a reply. A stranger
can't audit what they can't see. So one page lists every fact held today
and every memory, and each can be put right or forgotten there.

A correction is the user's own word on it (`facts.correct`, source
"confirmed"), so a later guess from mail can't overwrite it. Forgetting a
fact keeps its row, marked forgotten (`facts.forget`); forgetting a memory
deletes its file, and the memories' git history keeps it (`memory.forget`),
as "FORGET MEMORY" always did.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from agent import facts, offloop, runtime
from agent.cut import cut
from agent.worker import bodies

# A memory's summary is what the list shows; its body is in its file.
SHORT = 300


def _about(fact: facts.Fact) -> str:
    """Whom a fact is about, as the page says it."""
    if fact.subject == facts.USER:
        return "You"
    if fact.subject == "world":
        return "The world"
    return fact.name or fact.subject.split(":", 1)[-1]


def known(conn) -> dict:
    """Every fact held today, the user's first, then by whom; and every
    memory, the newest first."""
    held = sorted(facts.held(conn), key=lambda f: (f.subject != facts.USER, _about(f), f.id))
    found = [{"id": f.id, "about": _about(f), "attribute": f.attribute.replace("_", " "),
              "value": f.value, "source": f.source,
              "until": f.valid_until.isoformat() if f.valid_until else None}
             for f in held if not f.attribute.startswith(facts.STYLE_PREFIX)]
    memories = [{"path": r["path"], "kind": r["kind"], "summary": cut(r["summary"], SHORT),
                 "at": r["created_at"], "until": r["until"]}
                for r in conn.execute(
                    "SELECT path, kind, summary, created_at, until FROM memories"
                    " ORDER BY created_at DESC, id DESC")]
    return {"ok": True, "facts": found, "memories": memories}


def _correct(conn, fact_id: int, value: str) -> dict:
    try:
        fact = facts.correct(conn, fact_id, value, source="confirmed")
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "id": fact.id, "value": fact.value}


def _forget(conn, fact_id: int | None, path: str | None) -> dict:
    from tools import memory

    if fact_id is not None:
        if not facts.forget(conn, fact_id):
            return {"ok": False, "error": f"no fact {fact_id} is held"}
        return {"ok": True}
    # Only a memory the index lists: a path from the request is never
    # joined onto the memories folder unchecked.
    if conn.execute("SELECT 1 FROM memories WHERE path = ?", (path,)).fetchone() is None:
        return {"ok": False, "error": f"no memory {path!r} is held"}
    memory.forget(conn, path)
    return {"ok": True}


async def get_known(_: Request) -> JSONResponse:
    return JSONResponse(await offloop.with_own_conn(runtime.conn, known))


async def post_correct(request: Request) -> JSONResponse:
    """`{"id": 1042, "value": "Leeds"}`: the fact replaced by the user's word."""
    body = await bodies.body(request)
    fact_id, value = body.get("id"), body.get("value")
    if not isinstance(fact_id, int) or not isinstance(value, str) or not value.strip():
        return bodies.bad('send {"id": <a fact>, "value": "<what is true>"}')
    said = await offloop.with_own_conn(runtime.conn, _correct, fact_id, value)
    return JSONResponse(said, status_code=200 if said["ok"] else 400)


async def post_forget(request: Request) -> JSONResponse:
    """`{"id": 1042}` for a fact, or `{"path": "user/theo.md"}` for a memory."""
    body = await bodies.body(request)
    fact_id, path = body.get("id"), body.get("path")
    if not ((isinstance(fact_id, int) and path is None)
            or (isinstance(path, str) and path and fact_id is None)):
        return bodies.bad('send {"id": <a fact>} or {"path": "<a memory>"}')
    said = await offloop.with_own_conn(runtime.conn, _forget, fact_id, path)
    return JSONResponse(said, status_code=200 if said["ok"] else 400)


ROUTES = [
    Route("/control/knows", get_known),
    Route("/control/knows/correct", post_correct, methods=["POST"]),
    Route("/control/knows/forget", post_forget, methods=["POST"]),
]
