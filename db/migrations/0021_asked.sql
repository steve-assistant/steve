-- Migration 21: the month's ask, $1 before its cap, and notices kept per
-- cap amount.
--
-- With $1 left of the month, Steve asks once whether to raise the cap
-- (core/spend.py `_warn`, agent/caps.py `by_message`), so 'asked' is a
-- notice's kind too. And a notice was one per cap and period: raised from
-- $15 to $25 in the month, the cap was never asked about or stopped at
-- again, since the month's ask and stop were already written. The cap's
-- amount joins the key, so each amount the cap holds has its own. SQLite
-- cannot change a CHECK or a UNIQUE in place, so the table is made again
-- and its rows copied.

CREATE TABLE spend_notices_new (
    id         INTEGER PRIMARY KEY,
    kind       TEXT NOT NULL CHECK (kind IN ('warned', 'asked', 'stopped')),
    cap        TEXT NOT NULL,            -- its knob: MONTH_USD, CHAT_DAY_USD, ...
    period     TEXT NOT NULL,            -- '2026-10', or a day's or a night's date
    limit_usd  REAL NOT NULL,            -- the cap's amount when it was written
    spent_usd  REAL NOT NULL,
    told_at    TEXT,                     -- NULL until it has been passed on
    at         TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (kind, cap, period, limit_usd)
);

INSERT INTO spend_notices_new (id, kind, cap, period, limit_usd, spent_usd, told_at, at)
    SELECT id, kind, cap, period, limit_usd, spent_usd, told_at, at FROM spend_notices;

DROP TABLE spend_notices;

ALTER TABLE spend_notices_new RENAME TO spend_notices;
