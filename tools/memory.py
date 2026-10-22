"""Durable memory: markdown in a git repo, indexed for semantic recall.

Content lives in `data/memories/`, which is its **own git repository** and is
gitignored from the agent's source tree. Two reasons, and both matter:

  1. The coding model edits the agent's source inside a git worktree. A
     separate repo means personal content is never in that worktree, so it is
     never sent to the coding path.
  2. `git log` over memories is an audit trail of what the agent chose to
     remember, independent of code history and independently revertable.

The repository is written through dulwich, a git in Python, not the `git`
program: a stranger's Windows has no git, so the first "remember this" on an
installed Steve would have failed (step 8). The format is git's own, so a
repository the `git` program made reads and grows unchanged, and `git log`
still reads what dulwich wrote.

SQLite holds only the index and the vectors. If the index is ever lost it can
be rebuilt from the markdown; if the markdown is lost, nothing can rebuild it.
That asymmetry is why the files are the source of truth.

A memory about a date says so (`until`). On 5 October 2026 "The user is heading
out camping on 2026-10-02; do not assume tomorrow is light" came back for "my
run tomorrow", three days after the trip, and every memory was shown with no
date at all -- nothing said it was old. Now one with an `until` past is left
out of what the user is told about themselves and of recall, unless the past is asked
for, and the night forgets it a week later (`expire`; git keeps it). The
field is optional: a file without it reads as before.

And a memory is a file *and* an index row, or it is invisible. Ten files were
on disk and not in the index on 8 October 2026, each swept into another
memory's commit by `git add -A` while half-written. `remember` now embeds
before it writes, commits only its own file, and `reconcile` indexes what is
on disk and not in the index.

The vectors are OpenAI's (core/embed.py), billed under the caps. They were
a local model's, which needed the voice add-on's onnxruntime: on a plain
install no note could be kept and no recall made (audit C2). When the
database's vectors are emptied for a new model (`db.open_store`), the
worker embeds the index again (`reembed`).
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from dulwich.file import FileLocked
from dulwich.objects import Tree
from dulwich.repo import Repo

from core import db, identity, paths
from core import embed as embedding
from core.embed import EmbedderUnavailable as EmbedderUnavailable  # caught as memory's

log = logging.getLogger("memory")

VALID_KINDS = {"user", "feedback", "project", "reference"}

# How much of a memory feeds the embedding. Beyond a few hundred words the
# vector stops discriminating -- it drifts toward the document's average topic
# rather than what makes this memory distinct.
EMBED_CHARS = 1200


@dataclass
class Memory:
    path: str  # relative to paths.memories_dir()
    kind: str
    summary: str
    body: str
    created_at: str | None = None
    score: float | None = None  # similarity, when returned from recall()
    until: str | None = None  # local YYYY-MM-DD: the last day it matters

    def as_dict(self) -> dict:
        d = {
            "path": self.path,
            "kind": self.kind,
            "summary": self.summary,
            "body": self.body,
            "created_at": self.created_at,
        }
        if self.until:
            d["until"] = self.until
        if self.score is not None:
            d["similarity"] = round(self.score, 4)
        return d


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------


def embed(texts: list[str]) -> list[list[float]]:
    """Each text as a vector, or `EmbedderUnavailable`, which says why: a
    setup fact the model can tell the user, not a fault. Blocking."""
    return embedding.vectors(texts)


def _vec_literal(vector: list[float]) -> str:
    """sqlite-vec accepts a JSON array literal for a float[] column."""
    return "[" + ",".join(f"{x:.6f}" for x in vector) + "]"


# ---------------------------------------------------------------------------
# The memories git repo
# ---------------------------------------------------------------------------


def ensure_repo() -> Path:
    """The memories folder, as a git repository of its own, made on first use.

    Nothing made it but a test fixture. On a fresh install the first
    memory's `git add` ran in a plain folder, so git looked upward for a
    repository -- in a source tree, the checkout itself, where data/ is
    ignored -- and the turn that asked to remember something broke.
    """
    root = paths.memories_dir()
    if not (root / ".git").exists():
        root.mkdir(parents=True, exist_ok=True)
        Repo.init(str(root)).close()
    return root


class _Locked(RuntimeError):
    """Another process holds one of the repository's lock files."""


def _head_tree(repo: Repo) -> bytes:
    try:
        return repo[repo.head()].tree
    except KeyError:  # no commit yet
        return Tree().id


def _commit_once(message: str, rels: tuple[str, ...]) -> str | None:
    """Stage these paths -- written or deleted -- and commit them, as Steve's
    memory. The SHA, or None if none of them changed.

    As Steve's memory, by name: through the `git` program a user's git with
    no identity configured failed every commit with "Please tell me who you
    are". A path neither on disk nor in the repository is refused, as `git
    add` refused it: a commit naming nothing is a caller's mistake."""
    root = ensure_repo()
    name, email = identity.git_identity("memory")
    who = f"{name} <{email}>".encode()
    with Repo(str(root)) as repo:
        tracked = repo.open_index()
        for rel in rels:
            if not (root / rel).exists() and rel.encode() not in tracked:
                raise RuntimeError(f"git add: {rel} matches no file in the memories")
        try:
            repo.get_worktree().stage([rel.encode() for rel in rels])
            if repo.open_index().commit(repo.object_store) == _head_tree(repo):
                return None
            return repo.get_worktree().commit(message=message.encode(), committer=who,
                                              author=who).decode()
        except FileLocked as exc:
            # Another process's commit -- a retiring worker's, during a swap.
            raise _Locked(f"git: {exc}") from exc


# One writer at a time. Memories are written from the tool, from reflection
# and from the night, each on a thread of its own, and two `git add` at once
# collide on the index lock: on 5 October 2026 a turn broke with "Unable to
# create '.../memories/.git/index.lock': File exists", after the file was
# written and before it was indexed -- on disk, and invisible to recall.
# dulwich takes the same lock file, so the same holds.
_writing = threading.RLock()

# And another process's commit -- a retiring worker's, during a swap -- is
# waited out rather than failed on.
LOCKED_TRIES = 10
LOCKED_WAIT_S = 0.2


def _waiting_out_the_lock(message: str, rels: tuple[str, ...]) -> str | None:
    """A commit, retried while another process holds the index lock."""
    for attempt in range(LOCKED_TRIES):
        try:
            return _commit_once(message, rels)
        except _Locked:
            if attempt == LOCKED_TRIES - 1:
                raise
            time.sleep(LOCKED_WAIT_S)
    return None  # unreachable: the last attempt raises


def _commit(message: str, *paths: str) -> str | None:
    """Commit these paths -- written or deleted -- and nothing else. Returns
    the SHA, or None if none of them changed.

    Only its own paths. It was `git add -A`, which swept in whatever else
    was in the folder at that moment: ten memories half-written by another
    writer were committed inside other memories' commits and never indexed
    (found 8 October 2026)."""
    if not paths:
        raise ValueError("a commit names what it commits")
    return _waiting_out_the_lock(message, paths)


def _slugify(text: str, max_len: int = 48) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (slug[:max_len].rstrip("-")) or "memory"


# ---------------------------------------------------------------------------
# Markdown format
# ---------------------------------------------------------------------------

_FRONTMATTER = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)


def _render(kind: str, summary: str, body: str, until: str | None = None) -> str:
    created = datetime.now(UTC).strftime("%Y-%m-%d")
    return (
        "---\n"
        f"kind: {kind}\n"
        f"summary: {summary}\n"
        f"created: {created}\n"
        + (f"until: {until}\n" if until else "")
        + "---\n\n"
        f"{body.strip()}\n"
    )


def _today() -> date:
    """The day it is where the user is. A function, so a test can pin it."""
    from agent import zones

    return datetime.now(zones.local_zone()).date()


def _until(value) -> str | None:
    """A memory's last day as YYYY-MM-DD, or None if it has none or it is
    not a date -- an old file, or a frontmatter line written by hand."""
    text = str(value or "").strip()[:10]
    try:
        return date.fromisoformat(text).isoformat() if text else None
    except ValueError:
        return None


def past(until: str | None, today: date | None = None) -> bool:
    """Whether a memory's last day has gone by."""
    return bool(until) and until < (today or _today()).isoformat()


def _parse(text: str) -> tuple[dict, str]:
    match = _FRONTMATTER.match(text)
    if not match:
        return {}, text.strip()
    header, body = match.groups()
    meta: dict = {}
    for line in header.splitlines():
        key, _, value = line.partition(":")
        if value:
            meta[key.strip()] = value.strip()
    return meta, body.strip()


# ---------------------------------------------------------------------------
# Public operations
# ---------------------------------------------------------------------------

def remember(
    conn: sqlite3.Connection,
    *,
    kind: str,
    summary: str,
    body: str,
    name: str | None = None,
    until: str | None = None,
) -> Memory:
    """Write a memory, commit it, and index it for recall.

    Re-remembering the same `name` overwrites in place rather than
    accumulating near-duplicates, which otherwise poison recall: five slightly
    different versions of one fact crowd out every other result.

    `until` is the last day a memory about a date matters (YYYY-MM-DD): a
    trip, a deadline, "busy this week".

    Embedded before anything is written: an embedding that fails then
    leaves nothing behind, where it used to leave a committed file the
    index never heard of.
    """
    if kind not in VALID_KINDS:
        raise ValueError(f"kind must be one of {sorted(VALID_KINDS)}, got {kind!r}")
    if until is not None and str(until).strip() and _until(until) is None:
        raise ValueError(f"until must be a date, YYYY-MM-DD, got {until!r}")
    until = _until(until)

    slug = _slugify(name or summary)
    rel = f"{kind}/{slug}.md"
    path = paths.memories_dir() / rel
    vector = embed([f"{summary}\n\n{body}"[:EMBED_CHARS]])[0]
    with _writing:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_render(kind, summary, body, until), encoding="utf-8", newline="\n")
        sha = _commit(f"remember({kind}): {summary[:60]}", rel)
        _index(conn, rel, kind, summary, vector, sha=sha, until=until)

    return Memory(path=rel, kind=kind, summary=summary, body=body.strip(), until=until)


def _index(conn: sqlite3.Connection, rel: str, kind: str, summary: str, vector: list[float],
           *, sha: str | None = None, until: str | None = None,
           created: str | None = None) -> int:
    """One memory into the index, replacing what was there for its path."""
    with db.transaction(conn):
        conn.execute(
            """
            INSERT INTO memories (path, kind, summary, commit_sha, until, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, COALESCE(?, datetime('now')), datetime('now'))
            ON CONFLICT(path) DO UPDATE SET
                kind = excluded.kind,
                summary = excluded.summary,
                commit_sha = COALESCE(excluded.commit_sha, memories.commit_sha),
                until = excluded.until,
                updated_at = datetime('now')
            """,
            (rel, kind, summary, sha, until, created),
        )
        memory_id = conn.execute("SELECT id FROM memories WHERE path = ?", (rel,)).fetchone()["id"]
        # vec0 has no upsert; delete-then-insert is the documented pattern.
        conn.execute("DELETE FROM memory_vectors WHERE memory_id = ?", (memory_id,))
        conn.execute(
            "INSERT INTO memory_vectors (memory_id, embedding) VALUES (?, ?)",
            (memory_id, _vec_literal(vector)),
        )
    return int(memory_id)


def _judge_lesson() -> re.Pattern:
    """A lesson the reflection judge wrote (agent/reflect._keep_lesson): its
    summary opens with the rubric dimension it scored low -- "grounded: ..."."""
    from core.rubric import KEYS

    return re.compile(rf"^(?:{'|'.join(KEYS)}): ")


# The one `reference` memory that is not a research note: their writing style
# (tools/style.py), which `writing_style` reads before every draft.
STYLE_GUIDE = "reference/writing-style.md"


def research_note(path: str, kind: str) -> bool:
    """A note the night (or a turn) kept from what it read on the web.

    Kept, and out of the turn path: on 10 October 2026 they were 112 of their
    169 memories and 87% of their text, none of them ever expired, and they
    crowded every recall -- "why do you think I'm in Alabama" got ten
    weather notes. Every `reference` memory but the style guide is one: the
    night files all it reads as `reference` (agent/toolset/memory.py)."""
    return kind == "reference" and path != STYLE_GUIDE


# The least a note or a fact must resemble what was asked to be returned at
# all: a cosine similarity, on the embedder's unit vectors. Below it,
# nothing -- an empty answer says nothing is kept, which the model can say;
# a near guess was stretched to fit (7 of 7 recalls, 6 to 9 October 2026).
#
# Each is the embedder's, measured (12 October 2026) on labelled pairs about
# the fictional persona: a note that answers a recall query, or the nearest
# one that does not; a fact a message calls for, or the nearest one it does
# not (tests/unit/fixtures/floors/pairs.json, `python -m devtools.floors`).
# A fact scores lower than a note -- one line beside a whole message -- so
# each has its own floor. Each drops every miss and keeps 6 of 10 hits, and
# no floor did better on any model measured: the local one these vectors
# replaced, whose 0.72 kept 5 of the 20, nor OpenAI's larger model. It loses
# real hits ("dentist appointment details", 0.52); a fact may still be
# linked by the names it carries (agent/facts.py `linked`) before any
# embedding is consulted, and the profile says who the user is in every turn.
RELEVANCE_FLOOR = 0.57
FACT_FLOOR = 0.38


def similarity(distance: float) -> float:
    """The cosine similarity behind an L2 distance from the vector table.
    The embedder's vectors are unit length, where |a - b|^2 = 2 - 2 cos."""
    return 1.0 - (float(distance) ** 2) / 2.0


# What `recall` searches, by what it is about. On 6 October 2026, of 125
# memories 7 were facts about them; 81 were the night's research notes and 37
# the judge's lessons, and "my class schedule" came back as lessons about
# creating events. Facts about them are `user` and `project`; ways of working
# are `feedback` -- their corrections and the judge's lessons -- and `reference`.
ABOUT = {
    "user": frozenset({"user", "project"}),
    "how": frozenset({"feedback", "reference"}),
    "any": frozenset(VALID_KINDS),
}


def recall(conn: sqlite3.Connection, query: str, limit: int = 5, *,
           kinds: frozenset[str] | None = None, lessons: bool = True,
           past_too: bool = False, research: bool = True,
           floor: float | None = RELEVANCE_FLOOR) -> list[Memory]:
    """Semantic search over stored memories, nearest first: of `kinds` only,
    when given, without the judge's lessons unless `lessons`, without the
    research notes unless `research`, without memories whose day has gone
    by (`until`) unless `past_too`, and none less similar than `floor`
    (`RELEVANCE_FLOOR`) -- however few that leaves."""
    vector = embed([query])[0]
    # The vector table cannot filter on a kind, so it is asked for more and
    # the rest is done here; there are a few hundred memories, not millions.
    # Asked for more whenever anything is filtered: an expired memory can
    # be anywhere in the list.
    rows = conn.execute(
        """
        SELECT m.path, m.kind, m.summary, m.created_at, m.until, v.distance
        FROM memory_vectors v
        JOIN memories m ON m.id = v.memory_id
        WHERE v.embedding MATCH ? AND k = ?
        ORDER BY v.distance
        """,
        (_vec_literal(vector), min(200, limit * 10)),
    ).fetchall()
    judged = _judge_lesson()
    today = _today()
    rows = [r for r in rows
            if (kinds is None or r["kind"] in kinds)
            and (lessons or not judged.match(r["summary"] or ""))
            and (research or not research_note(r["path"], r["kind"]))
            and (past_too or not past(r["until"], today))
            and (floor is None or similarity(r["distance"]) >= floor)][:limit]

    out: list[Memory] = []
    for row in rows:
        # The file is the source of truth; the index only finds it.
        body = ""
        file = paths.memories_dir() / row["path"]
        if file.exists():
            _, body = _parse(file.read_text(encoding="utf-8"))
        out.append(
            Memory(
                path=row["path"],
                kind=row["kind"],
                summary=row["summary"],
                body=body,
                created_at=row["created_at"],
                # vec0 returns L2 distance; smaller is nearer. Report a
                # similarity so callers do not have to invert it themselves.
                score=1.0 / (1.0 + row["distance"]),
                until=row["until"],
            )
        )
    return out


def read(path: str) -> Memory | None:
    file = paths.memories_dir() / path
    if not file.exists():
        return None
    meta, body = _parse(file.read_text(encoding="utf-8"))
    return Memory(
        path=path,
        kind=meta.get("kind", "project"),
        summary=meta.get("summary", ""),
        body=body,
        created_at=meta.get("created"),
        until=_until(meta.get("until")),
    )


def forget(conn: sqlite3.Connection, path: str) -> bool:
    """Delete a memory. The git history retains it, deliberately."""
    file = paths.memories_dir() / path
    with _writing:
        existed = file.exists()
        if existed:
            file.unlink()
            _commit(f"forget: {path}", path)

    with db.transaction(conn):
        row = conn.execute("SELECT id FROM memories WHERE path = ?", (path,)).fetchone()
        if row:
            conn.execute("DELETE FROM memory_vectors WHERE memory_id = ?", (row["id"],))
            conn.execute("DELETE FROM memories WHERE id = ?", (row["id"],))
    return existed


def forget_number(conn: sqlite3.Connection, number: int) -> str | None:
    """Forget the memory the index numbers `number` -- the number the
    morning digest gives a fact the night kept -- and return its summary,
    or None if there is none."""
    row = conn.execute("SELECT path, summary FROM memories WHERE id = ?", (number,)).fetchone()
    if row is None:
        return None
    forget(conn, row["path"])
    return row["summary"]


def _files():
    """Every memory file, in a stable order."""
    for file in sorted(paths.memories_dir().rglob("*.md")):
        if ".git" in file.parts or file.name == "README.md":
            continue
        yield file


def export_all(conn: sqlite3.Connection) -> list[dict]:
    """Every memory, flattened. Read from the files, which are the source
    of truth -- the index can be rebuilt but the markdown cannot."""
    out: list[dict] = []
    for file in _files():
        meta, body = _parse(file.read_text(encoding="utf-8"))
        out.append({
            "path": file.relative_to(paths.memories_dir()).as_posix(),
            "kind": meta.get("kind", "project"),
            "summary": meta.get("summary", file.stem),
            "body": body,
        })
    return out


def _from_file(file: Path) -> tuple[str, str, str, str, str | None, str | None]:
    """(path, kind, summary, embedding text, created, until) of one file."""
    meta, body = _parse(file.read_text(encoding="utf-8"))
    rel = file.relative_to(paths.memories_dir()).as_posix()
    summary = meta.get("summary", rel)
    return (rel, meta.get("kind", "project"), summary, f"{summary}\n\n{body}"[:EMBED_CHARS],
            _until(meta.get("created")), _until(meta.get("until")))


def reindex(conn: sqlite3.Connection) -> int:
    """Rebuild the index from the markdown files.

    The files are the source of truth; this exists so a corrupted or deleted
    database is an inconvenience rather than data loss. Each keeps the day
    its file says it was written, not the day it was reindexed.

    Every file is embedded before the index is touched, a few hundred to a
    request. Emptied first and filled a call at a time, the index was part
    there while it ran, and left empty by an embedder that failed partway.
    """
    entries = [_from_file(file) for file in _files()]
    found = embed([text for _, _, _, text, _, _ in entries])
    with db.transaction(conn):
        conn.execute("DELETE FROM memory_vectors")
        conn.execute("DELETE FROM memories")
        for (rel, kind, summary, _, created, until), vector in zip(entries, found, strict=True):
            _index(conn, rel, kind, summary, vector, until=until, created=created)
    return len(entries)


def reembed(conn: sqlite3.Connection) -> int:
    """Embed each memory in the index that has no vector; return how many.

    `db.open_store` empties the vectors when they are not the code's
    model's, and every memory stays in the index with nothing to find it
    by. The text is the file's; a row whose file is gone is `reconcile`'s
    to drop."""
    have = {row[0] for row in conn.execute("SELECT memory_id FROM memory_vectors")}
    todo: list[tuple[int, str]] = []
    for row in conn.execute("SELECT id, path FROM memories ORDER BY id"):
        if row["id"] in have:
            continue
        file = paths.memories_dir() / row["path"]
        if file.exists():
            todo.append((row["id"], _from_file(file)[3]))
    if not todo:
        return 0
    found = embed([text for _, text in todo])
    with db.transaction(conn):
        for (memory_id, _), vector in zip(todo, found, strict=True):
            conn.execute("DELETE FROM memory_vectors WHERE memory_id = ?", (memory_id,))
            conn.execute("INSERT INTO memory_vectors (memory_id, embedding) VALUES (?, ?)",
                         (memory_id, _vec_literal(vector)))
    return len(todo)


def reconcile(conn: sqlite3.Connection) -> list[str]:
    """Index the memory files the index does not know, commit any git does
    not, and drop index rows whose file is gone. Returns what was indexed.

    The repair for a write that stopped between its file and its row -- ten
    such files on 8 October 2026, on disk and invisible to recall, which
    searches the index.
    """
    known = {row["path"] for row in conn.execute("SELECT path FROM memories")}
    on_disk: set[str] = set()
    added: list[str] = []
    for file in _files():
        rel, kind, summary, text, created, until = _from_file(file)
        on_disk.add(rel)
        if rel in known:
            continue
        with _writing:
            _commit(f"reconcile: {rel}", rel)
            _index(conn, rel, kind, summary, embed([text])[0], until=until, created=created)
        added.append(rel)
    gone = known - on_disk
    # No files at all is a folder that is not there, not a store emptied
    # file by file: the index is left alone.
    if gone and on_disk:
        with db.transaction(conn):
            for rel in gone:
                row = conn.execute("SELECT id FROM memories WHERE path = ?", (rel,)).fetchone()
                conn.execute("DELETE FROM memory_vectors WHERE memory_id = ?", (row["id"],))
                conn.execute("DELETE FROM memories WHERE id = ?", (row["id"],))
    return added


# A memory past its last day is kept this long before the night forgets it:
# long enough to ask "how was the trip?" with it still in reach.
EXPIRED_KEPT_DAYS = 7


def expire(conn: sqlite3.Connection, *, today: date | None = None) -> list[str]:
    """Forget memories more than `EXPIRED_KEPT_DAYS` past their `until`.
    Returns their paths. Git keeps them; a memory with no `until` -- every
    one written before there was one -- is never touched."""
    cutoff = ((today or _today()) - timedelta(days=EXPIRED_KEPT_DAYS)).isoformat()
    gone: list[str] = []
    for file in list(_files()):
        rel, *_, until = _from_file(file)
        if until and until < cutoff and forget(conn, rel):
            gone.append(rel)
    return gone


def _main() -> None:
    """Maintenance, from the command line.

    `reindex` exists so a corrupted or deleted database is an inconvenience
    rather than data loss -- the markdown files are the source of truth. It
    had no caller and no entry point, which made it a recovery plan nobody
    could actually run on the day they needed it.
    """
    import argparse

    from core import db

    parser = argparse.ArgumentParser(description="Memory maintenance")
    parser.add_argument(
        "--reindex", action="store_true",
        help="rebuild the search index from the markdown files",
    )
    parser.add_argument(
        "--reconcile", action="store_true",
        help="index memory files the index does not know (the night does this too)",
    )
    parser.add_argument("--list", action="store_true", help="what is remembered")
    args = parser.parse_args()

    conn = db.open_store()
    if args.reindex:
        print(f"reindexed {reindex(conn)} memories")
    if args.reconcile:
        for rel in reconcile(conn):
            print(f"  indexed {rel}")
    if args.list or not (args.reindex or args.reconcile):
        for row in export_all(conn):
            print(f"  [{row['kind']:9}] {row['summary'][:88]}")


if __name__ == "__main__":
    _main()
