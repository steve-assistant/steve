-- Migration 5: each account's health, said once.
--
-- A sign-in that lapsed was found by the user the next morning, from a tool
-- that failed (26 September 2026), and each provider said its trouble in its
-- own words, or not at all. Now every account is checked the same way, and
-- is in one of five states (core/health.py), each with one sentence made in
-- core and one action for the user (05-connectors.md, section 2).
--
-- A row per account checked, today's stand-ins included, so the state
-- outlives a restart: "can't reach since 09:12" stays true across one, and
-- what was said on the user's channel is said once (`told`), not after
-- every start. Removing an account removes its row (core/modules.py).

CREATE TABLE account_health (
    account     TEXT PRIMARY KEY,       -- the account's key
    state       TEXT NOT NULL CHECK (state IN ('working', 'needs_you', 'cant_reach',
                                               'cant_work_here', 'off')),
    kind        TEXT,                   -- the error behind it (steve_sdk.errors)
    detail      TEXT,                   -- the provider's words, cut short
    since       TEXT NOT NULL,          -- UTC: when this state began
    checked_at  TEXT NOT NULL,          -- UTC: the last check
    expires_at  TEXT,                   -- UTC: a sign-in's known end
    told        TEXT                    -- what was said, once: down:<since> or ends:<expires_at>
);
