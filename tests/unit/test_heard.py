"""Their own messages, read back for the judge, the checks before a ping and
the night (agent/heard.py).

Each of those readers had its own copy of this query and of the channels
it ran on. What the copies agreed on is pinned here once: a 'user' row in
a session the agent runs on its own is not theirs, and the latest of their
messages are the ones kept.
"""

from __future__ import annotations

from agent import heard


def _said(conn, channel, content, created_at="2026-10-10 12:00:00", role="user"):
    with conn:
        session = conn.execute("INSERT INTO sessions (channel) VALUES (?)", (channel,)).lastrowid
        conn.execute("INSERT INTO messages (session_id, role, content, created_at)"
                     " VALUES (?, ?, ?, ?)", (session, role, content, created_at))


def _contents(rows):
    return [row["content"] for row in rows]


def test_only_what_the_user_wrote_in_their_own_conversations_is_theirs(conn):
    """An unprompted session holds 'user' rows the agent wrote itself -- a
    task's brief, the night's prompt -- and on 10 October such a brief was
    filed as their correction."""
    _said(conn, "signal", "from signal")
    _said(conn, "tray", "from the tray")
    for channel in ("initiative", "night", "job", "reminder"):
        _said(conn, channel, f"the agent, on {channel}")
    _said(conn, "signal", "the agent's answer", role="assistant")

    rows = heard.user_messages(conn, since="2026-10-01 00:00:00")

    assert _contents(rows) == ["from signal", "from the tray"]
    assert [row["channel"] for row in rows] == ["signal", "tray"]


def test_the_latest_are_kept_and_handed_back_oldest_first(conn):
    """Every reader quotes their words in the order the user said them, and a cap
    must drop the oldest, not what the user said a minute ago."""
    for n in range(5):
        _said(conn, "voice", f"message {n}", created_at=f"2026-10-10 12:0{n}:00")

    rows = heard.user_messages(conn, since="2026-10-10 00:00:00", latest=2)

    assert _contents(rows) == ["message 3", "message 4"]


def test_a_window_holds_both_of_its_ends(conn):
    for minute in ("00", "10", "20", "30"):
        _said(conn, "tray", minute, created_at=f"2026-10-10 12:{minute}:00")

    rows = heard.user_messages(conn, since="2026-10-10 12:10:00", until="2026-10-10 12:20:00")

    assert _contents(rows) == ["10", "20"]


def test_the_last_hours_are_counted_on_sqlites_clock(conn):
    """The database's times are UTC; a window worked out from Python's
    local clock would be four hours off (docs/protocols.md section 7)."""
    _said(conn, "signal", "an hour ago", created_at=conn.execute(
        "SELECT datetime('now', '-1 hours')").fetchone()[0])
    _said(conn, "signal", "five hours ago", created_at=conn.execute(
        "SELECT datetime('now', '-5 hours')").fetchone()[0])

    assert _contents(heard.user_messages(conn, hours=3)) == ["an hour ago"]
