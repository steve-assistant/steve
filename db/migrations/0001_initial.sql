-- Migration 1: the store as it stood when Steve was copied from the
-- agent it began as (db/schema.sql there, with its added columns). Applied
-- once, in a transaction, by core/db.migrate. Until v0.1 ships, no user
-- database exists, so this file may still be edited; from v0.1 on a
-- change is a new numbered file.
--
-- The local store.
--
-- One SQLite file holds conversation history, the background job queue, cost
-- accounting, reflection scores, and the self-upgrade ledger. A single file is
-- a deliberate choice: it backs up with a copy, it has no daemon to supervise,
-- and on a single-user workload it is never the bottleneck.
--
-- Memory *content* is NOT here on the laptop. It lives as markdown in
-- data/memories/, which is a separate git repo so that the coding model never
-- sees personal content while editing the agent's source; the laptop's
-- `memories` table only carries the index.

-- One row per migration applied, with the SHA-256 of the file as it ran:
-- core/db.migrate refuses a database whose migration has since changed.
CREATE TABLE schema_version (
    version     INTEGER PRIMARY KEY,
    sha256      TEXT NOT NULL,
    applied_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------------------
-- Conversation
-- ---------------------------------------------------------------------------

CREATE TABLE sessions (
    id           INTEGER PRIMARY KEY,
    channel      TEXT NOT NULL,              -- 'tray' | 'voice' | 'signal'
    title        TEXT,
    started_at   TEXT NOT NULL DEFAULT (datetime('now')),
    ended_at     TEXT,
    reflected_at TEXT                        -- NULL until the reflection pass runs
);

CREATE INDEX idx_sessions_unreflected
    ON sessions(ended_at) WHERE reflected_at IS NULL;

CREATE TABLE messages (
    id          INTEGER PRIMARY KEY,
    session_id  INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role        TEXT NOT NULL,               -- 'user' | 'assistant' | 'tool' | 'system'
    content     TEXT NOT NULL,
    tool_name   TEXT,
    model       TEXT,                        -- which model produced this
    effort      TEXT,                        -- reasoning effort actually used
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    tool_args   TEXT                         -- a tool row: what it was called with, JSON
);

CREATE INDEX idx_messages_session ON messages(session_id, id);
-- "The latest message on this channel" (answering.recent_session) on every
-- reply and ping, and the canary's counts: without it a quiet channel read
-- the whole table back to front, overflow pages and all -- 47 ms and
-- growing with every message (measured on a scratch copy, 5 October 2026).
CREATE INDEX idx_messages_created ON messages(created_at);

-- ---------------------------------------------------------------------------
-- Cost accounting
--
-- Recorded per model call rather than per session, because the whole point of
-- the routing design is to know which tier the money went to.
-- ---------------------------------------------------------------------------

CREATE TABLE usage_events (
    id               INTEGER PRIMARY KEY,
    session_id       INTEGER REFERENCES sessions(id) ON DELETE SET NULL,
    message_id       INTEGER REFERENCES messages(id) ON DELETE SET NULL,
    job_id           INTEGER,
    model            TEXT NOT NULL,
    effort           TEXT,
    provider         TEXT,                   -- upstream OpenRouter actually used
    input_tokens     INTEGER NOT NULL DEFAULT 0,
    cached_tokens    INTEGER NOT NULL DEFAULT 0,
    output_tokens    INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd         REAL NOT NULL DEFAULT 0,
    latency_ms       INTEGER,
    -- What the call was for (core/spend.py): the category whose cap it
    -- counts against, and the part inside it, some with a budget of their own.
    category         TEXT NOT NULL DEFAULT 'background'
                     CHECK (category IN ('chat', 'background', 'night', 'extensions')),
    part             TEXT,
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_usage_model_day ON usage_events(model, created_at);
-- A category's spend in its window, asked before every model call.
CREATE INDEX idx_usage_category ON usage_events(category, created_at);
-- Today's spend and a session's: asked on every attention tick, every
-- status poll and every routine and goal check, and the index above leads
-- with the model, so each was a scan of every call ever made.
CREATE INDEX idx_usage_created ON usage_events(created_at);
CREATE INDEX idx_usage_session ON usage_events(session_id);

-- What the run that answered a turn did, beside what it cost: how many
-- requests it made to the model and how many tools it called. On 5-7 October
-- 2026 the slow turns were the ones with ten or more tools, each result resent
-- on every later request; the token totals showed they were heavy, not why.
-- By the answer, since step 3 bills each request on its own row.
CREATE TABLE turn_stats (
    message_id INTEGER PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
    requests   INTEGER NOT NULL DEFAULT 0,
    tool_calls INTEGER NOT NULL DEFAULT 0
);

-- What the user was told about a cap (core/spend.py): that one was nearly
-- reached ('warned', at 80%) or reached ('stopped'). Once per cap and period,
-- here rather than in memory, where a restart forgot it and told them again.
CREATE TABLE spend_notices (
    id         INTEGER PRIMARY KEY,
    kind       TEXT NOT NULL CHECK (kind IN ('warned', 'stopped')),
    cap        TEXT NOT NULL,            -- its knob: MONTH_USD, CHAT_DAY_USD, ...
    period     TEXT NOT NULL,            -- '2026-10', or a day's or a night's date
    limit_usd  REAL NOT NULL,
    spent_usd  REAL NOT NULL,
    told_at    TEXT,                     -- NULL until it has been passed on
    at         TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (kind, cap, period)
);

-- ---------------------------------------------------------------------------
-- Background work
--
-- This is what lets the fast model hand a hard task off and keep talking.
-- ---------------------------------------------------------------------------

CREATE TABLE jobs (
    id            INTEGER PRIMARY KEY,
    session_id    INTEGER REFERENCES sessions(id) ON DELETE SET NULL,
    kind          TEXT NOT NULL,             -- 'research' | 'reflect' | 'upgrade' | ...
    prompt        TEXT NOT NULL,
    why           TEXT,                      -- why the fast model deferred it
    status        TEXT NOT NULL DEFAULT 'queued',
                  -- queued | running | done | failed | cancelled
    priority      INTEGER NOT NULL DEFAULT 0,
    model         TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    max_attempts  INTEGER NOT NULL DEFAULT 3,
    result        TEXT,
    error         TEXT,
    notified      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    started_at    TEXT,
    finished_at   TEXT,
    claimed_by    TEXT,                      -- "pid:start-time" of the worker running it
    CHECK (status IN ('queued', 'running', 'done', 'failed', 'cancelled'))
);

-- The queue's hot path: highest priority, then oldest first.
CREATE INDEX idx_jobs_claimable
    ON jobs(priority DESC, id) WHERE status = 'queued';
CREATE INDEX idx_jobs_unnotified
    ON jobs(finished_at) WHERE notified = 0 AND status IN ('done', 'failed');

-- ---------------------------------------------------------------------------
-- Memory index (content lives in the separate data/memories git repo)
-- ---------------------------------------------------------------------------

CREATE TABLE memories (
    id          INTEGER PRIMARY KEY,
    path        TEXT NOT NULL UNIQUE,        -- relative to data/memories/
    kind        TEXT NOT NULL,               -- user | feedback | project | reference
    summary     TEXT NOT NULL,
    commit_sha  TEXT,                        -- memories-repo commit that wrote it
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    until       TEXT                         -- local YYYY-MM-DD: the last day it matters
);

-- What the agent knows, one typed fact a row (agent/facts.py). A memory was
-- a paragraph with a kind, and `kind = 'user'` put every one in front of
-- every turn: "Owen Tate's Northwind email format" became a standing fact about
-- the user (8 October 2026). A fact says whom it is about, where it came from,
-- and how long it holds; a new value supersedes the old one, and nothing is
-- deleted -- a forgotten or superseded row stays, with what retired it.
-- Ids start at 1000 (`facts.FIRST_ID`), so "FORGET MEMORY <n>" never means a
-- fact and a memory at once.
CREATE TABLE facts (
    id            INTEGER PRIMARY KEY,
    subject       TEXT NOT NULL,             -- user | person:<people id or name> | org:<domain> | world
    name          TEXT,                      -- how the subject reads: "Owen Tate", "Northwind"
    attribute     TEXT NOT NULL,             -- short: home_city, email_pattern, plan
    value         TEXT NOT NULL,
    source        TEXT NOT NULL,             -- user_words | confirmed | inferred:<what> | mail | web | judge
    source_ref    TEXT,                      -- e.g. message:2508
    valid_from    TEXT,                      -- local YYYY-MM-DD
    valid_until   TEXT,                      -- local YYYY-MM-DD: the last day it holds
    status        TEXT NOT NULL DEFAULT 'active',  -- active | superseded | forgotten
    superseded_by INTEGER,
    ended_by      TEXT,                      -- what retired it: corrected, forgotten, message:<n>
    embedding     BLOB,                      -- float32, for `facts.relevant`
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_facts_subject ON facts(subject, attribute, status);

-- ---------------------------------------------------------------------------
-- Reflection
-- ---------------------------------------------------------------------------

CREATE TABLE reflections (
    id            INTEGER PRIMARY KEY,
    session_id    INTEGER REFERENCES sessions(id) ON DELETE CASCADE,
    rubric        TEXT NOT NULL,
    scores_json   TEXT NOT NULL,
    notes         TEXT,
    redacted      INTEGER NOT NULL DEFAULT 1, -- 0 here is a bug, never a choice
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------------------
-- Self-upgrade ledger
--
-- Every attempt is recorded whether or not it merged, so the reflection pass
-- can ask "are my changes actually getting better?" rather than guessing.
-- ---------------------------------------------------------------------------

CREATE TABLE upgrades (
    id             INTEGER PRIMARY KEY,
    proposal       TEXT NOT NULL,
    origin         TEXT NOT NULL DEFAULT 'agent',  -- 'agent' | 'user'
    branch         TEXT,
    base_sha       TEXT,
    result_sha     TEXT,
    verdict        TEXT,                    -- minor | major | frozen_violation
    status         TEXT NOT NULL DEFAULT 'proposed',
                   -- proposed | running | awaiting_approval | merged
                   -- | rejected | failed | rolled_back
    files_changed  TEXT,                    -- JSON array, derived from git
    diff           TEXT,
    gate_output    TEXT,
    cost_usd       REAL NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    decided_at     TEXT,
    decided_by     TEXT,                    -- 'auto' | 'human'
    shadow         INTEGER NOT NULL DEFAULT 0,  -- ran in the shadow week: could not merge
    user_agrees   INTEGER,                 -- their word on the judge's call: 1, 0, or unrated
    user_note     TEXT,
    rated_at       TEXT,
    CHECK (status IN ('proposed', 'running', 'awaiting_approval', 'merged',
                      'rejected', 'failed', 'rolled_back'))
);

CREATE INDEX idx_upgrades_pending
    ON upgrades(created_at) WHERE status = 'awaiting_approval';

-- ---------------------------------------------------------------------------
-- Attention: everything the agent noticed on its own, and what came of it
--
-- One row per thing, keyed by the thing itself -- a message id, an event id
-- and how near it is, a forecast condition and its date -- so a source that
-- sees the same thing on every poll adds nothing, and the judge is never
-- shown it twice. The row then carries the thing's whole life: the verdict,
-- what the doer did about it, and when and how the user heard. See
-- `agent/attention/`.
--
-- Written before any model looks at it, never after: a crash between the
-- model acting and the row being written would otherwise show the same mail
-- to the next tick, which would act on it again.
-- ---------------------------------------------------------------------------

CREATE TABLE observations (
    id              INTEGER PRIMARY KEY,
    source          TEXT NOT NULL,           -- gmail | outlook | calendar | horizon | ...
    key             TEXT NOT NULL UNIQUE,
    title           TEXT NOT NULL,
    detail          TEXT NOT NULL DEFAULT '',
    happens_at      TEXT,                    -- local ISO: when the thing is or was
    ref             TEXT NOT NULL DEFAULT '{}',  -- JSON: the ids a tool needs
    first_seen      TEXT NOT NULL DEFAULT (datetime('now')),
    -- the judge
    say             TEXT,                    -- now | before | briefing | never
    critical        INTEGER NOT NULL DEFAULT 0,
    headline        TEXT,
    task            TEXT,                    -- what the doer was asked to do
    triaged_at      TEXT,
    triage_attempts INTEGER NOT NULL DEFAULT 0,
    -- the doer. `prepared_at` is stamped when it *starts*, for the reason above.
    prepared_at     TEXT,
    outcome         TEXT,                    -- what it did, in one line
    -- delivery, all UTC
    deliver_at      TEXT,                    -- for `before`: when to say it
    delivered_at    TEXT,
    delivered_in    TEXT,                    -- ping | before | morning | evening | dropped | legacy
    CHECK (say IS NULL OR say IN ('now', 'before', 'briefing', 'never'))
);

CREATE INDEX idx_observations_untriaged
    ON observations(id) WHERE triaged_at IS NULL;
CREATE INDEX idx_observations_undelivered
    ON observations(say) WHERE delivered_at IS NULL;

-- The forecast for each day at home, as last fetched, so a change can be
-- noticed: "rain likely tomorrow" is news the first time the forecast says
-- it, and again only if it stops saying it. By place too, so moving is not
-- a change of forecast; only home's rows are kept. Metric, and said in the
-- user's units (`sources/weather.units`). `rain_chance` is the day's
-- highest hourly precipitation probability.
CREATE TABLE weather_days (
    place        TEXT NOT NULL,            -- home as geocoded, 'Leeds, United Kingdom'
    day          TEXT NOT NULL,            -- local date
    country      TEXT NOT NULL DEFAULT '', -- home's ISO code, for its units
    high_c       REAL,
    low_c        REAL,
    rain_chance  INTEGER,
    rain_mm      REAL,
    code         INTEGER,                  -- the day's WMO weather code
    wet_hours    TEXT,                     -- e.g. '2pm-6pm', or ''
    fetched_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (place, day)
);

-- Towns the user's calendar names, geocoded once (`agent/attention/trips.py`).
-- `found` 0 is a name that is not a town -- a room, a building -- kept so it
-- is not looked up on every tick.
CREATE TABLE places (
    query        TEXT PRIMARY KEY,         -- the name as asked, lowercased
    found        INTEGER NOT NULL,
    name         TEXT,
    latitude     REAL,
    longitude    REAL,
    timezone     TEXT,
    country      TEXT,                     -- ISO code; official alerts are US-only
    fetched_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Newsletters a briefing has carried (`agent/attention/newsdesk.py`), so the
-- next briefing does not repeat them. By briefing, so the same briefing
-- written again keeps its own.
CREATE TABLE newsletters_said (
    key          TEXT NOT NULL,            -- 'letter:<gmail id>'
    briefing     TEXT NOT NULL,            -- e.g. 'briefing:morning:2026-09-26'
    said_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (key, briefing)
);

-- The forecast where the user is going, one line a day, for the briefing.
CREATE TABLE trip_weather (
    place        TEXT NOT NULL,
    day          TEXT NOT NULL,            -- local date there
    line         TEXT NOT NULL,            -- e.g. 'Fri: 46-58°F, light rain'
    fetched_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (place, day)
);

-- Why each observation was judged as it was, and what the checks in code
-- made of it (`agent/attention/vet.py`). The judge's sentence used to be
-- thrown away, so "why did you tell me that?" had no answer; and a model's
-- `critical` alone woke them for a hurricane in Alabama. A table of its own
-- rather than columns on `observations`: a column added to a live table is
-- a frozen change (core/db.py `ADDED_COLUMNS`), a new table is not.
CREATE TABLE verdicts (
    obs_id          INTEGER PRIMARY KEY REFERENCES observations(id) ON DELETE CASCADE,
    why             TEXT NOT NULL DEFAULT '',  -- the judge's one sentence
    judge_say       TEXT,                      -- what the judge said, before the checks
    judge_critical  INTEGER NOT NULL DEFAULT 0,
    judge_reason    TEXT,                      -- its code; null when it gave none
    reason          TEXT,                      -- the code after the checks
    sender          TEXT,                      -- mail: person | group | automated | unknown
    sender_why      TEXT,
    place           TEXT,                      -- where it is about, for the weather
    vetted          TEXT,                      -- kept | fyi | dropped | held
    vet_why         TEXT,                      -- what each check that changed it said
    vetted_at       TEXT,
    CHECK (reason IS NULL OR reason IN
           ('changes-plans', 'reply-owed', 'deadline', 'money', 'safety', 'fyi'))
);

-- Every message to the user, written before anything sends it
-- (`agent/outbox.py`). The app's inbox shows every row; the paired remote
-- channel is sent every row, retried until it goes or `until` passes.
CREATE TABLE outbox (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,
    text        TEXT NOT NULL,             -- what the user reads
    noted       TEXT,                      -- what the conversation records, if not `text`
    action_id   INTEGER REFERENCES actions(id) ON DELETE SET NULL,  -- a card's
    remote      TEXT,                      -- the channel it goes to; null: the app alone
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    until       TEXT NOT NULL,             -- UTC: tried until then, and not after
    seen_at     TEXT,                      -- UTC: shown in the app
    sent_at     TEXT,                      -- UTC: the remote channel took it
    attempts    INTEGER NOT NULL DEFAULT 0,
    next_try_at TEXT,                      -- UTC; null once nothing is left to try
    stopped     TEXT,                      -- why it stopped trying, unsent
    CHECK (kind IN ('reminder', 'briefing', 'card', 'notice', 'job', 'reply'))
);

CREATE INDEX idx_outbox_due ON outbox(next_try_at) WHERE next_try_at IS NOT NULL;

-- Each stretch the backend was awake, as its heartbeat found it
-- (`agent/awake.py`), and the gap before it: the computer asleep, or the
-- backend not running.
CREATE TABLE awake (
    id          INTEGER PRIMARY KEY,
    since       TEXT NOT NULL,             -- UTC: the first beat of the stretch
    last_beat   TEXT NOT NULL,             -- UTC: its latest, moved on each minute
    gap_from    TEXT,                      -- UTC: the last beat before it; NULL for the first
    gap         TEXT CHECK (gap IN ('asleep', 'stopped'))
);

-- What the user was told, line by line, and what each line came from: the
-- observation, its source, the place and the judge's reason
-- (`agent/attention/told.py`). On 9 October the user asked "why do you think I'm
-- in Ridgeview?" about a ping the model could not look up.
CREATE TABLE told (
    id          INTEGER PRIMARY KEY,
    message     TEXT NOT NULL,             -- ping:<utc stamp> | briefing:<kind>:<date> | before:<obs id>
    how         TEXT NOT NULL,             -- ping | before | morning | evening
    said_at     TEXT,                      -- UTC; null until the user has it
    outbox_id   INTEGER,                   -- the outbox row that carries it
    obs_id      INTEGER REFERENCES observations(id) ON DELETE SET NULL,
    line        TEXT NOT NULL,             -- the line as the user read it
    source      TEXT,
    place       TEXT,
    reason      TEXT,
    why         TEXT,
    written_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_told_message ON told(message);
CREATE INDEX idx_told_obs ON told(obs_id);
CREATE INDEX idx_told_outbox ON told(outbox_id) WHERE outbox_id IS NOT NULL;

-- What news the user follows. Seeded from what they asked for; "follow X" and
-- "stop telling me about X" in conversation add and drop rows. A dropped
-- row is kept (`dropped_at`), so following it again is the same row.
-- Every topic is one the user asked for; nothing is seeded.
CREATE TABLE interests (
    id          INTEGER PRIMARY KEY,
    topic       TEXT NOT NULL UNIQUE COLLATE NOCASE,   -- what the user calls it
    query       TEXT NOT NULL,                         -- what the feed is asked
    added_at    TEXT NOT NULL DEFAULT (datetime('now')),
    dropped_at  TEXT
);

-- Every unprompted message that buzzed their phone, as a message rather than
-- per item: three things said in one message are one interruption, and the
-- caps count interruptions.
CREATE TABLE pings (
    id        INTEGER PRIMARY KEY,
    sent_at   TEXT NOT NULL DEFAULT (datetime('now')),
    items     INTEGER NOT NULL,
    critical  INTEGER NOT NULL DEFAULT 0,
    at_night  INTEGER NOT NULL DEFAULT 0
);

-- ---------------------------------------------------------------------------
-- Reminders and recurring checks
--
-- Two kinds in one table because they differ only in what firing means:
--   'remind' — say the text back at the appointed time.
--   'check'  — answer the question, and say so if the answer changed.
--
-- Unlike the watcher, these fire whatever the hour. The watcher is
-- unsolicited and has quiet hours; a reminder was asked for, and an alarm
-- that declines to go off at 06:00 is not an alarm.
-- ---------------------------------------------------------------------------

CREATE TABLE reminders (
    id            INTEGER PRIMARY KEY,
    kind          TEXT NOT NULL,            -- 'remind' | 'check'
    text          TEXT NOT NULL,
    session_id    INTEGER REFERENCES sessions(id) ON DELETE SET NULL,
    next_fire_at  TEXT NOT NULL,            -- ISO, local time
    every_seconds INTEGER NOT NULL DEFAULT 0,  -- 0 = one-shot
    until_at      TEXT,                     -- ISO; NULL for one-shot
    fires         INTEGER NOT NULL DEFAULT 0,
    max_fires     INTEGER NOT NULL DEFAULT 1,
    last_result   TEXT,                     -- for 'check': the previous answer
    status        TEXT NOT NULL DEFAULT 'active',
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at   TEXT,
    CHECK (kind IN ('remind', 'check')),
    CHECK (status IN ('active', 'done', 'cancelled', 'failed'))
);

CREATE INDEX idx_reminders_due
    ON reminders(next_fire_at) WHERE status = 'active';

-- ---------------------------------------------------------------------------
-- Actions the agent took, or wants to take
--
-- Every effectful call lands here, including the free ones -- those go
-- straight to 'done'. An audit log that only records what you were already
-- watching is the one thing an audit log must not be.
--
-- `executing` is a distinct state for one reason: a tray click and a Signal
-- "yes" arriving together must not send two emails. It is claimed with the
-- same UPDATE...WHERE...RETURNING idiom as jobs.claim, and that is the whole
-- exactly-once story.
--
-- `args_json` is exactly what gets replayed on approval. `preview` is the
-- human rendering: what the user reads on the card.
-- ---------------------------------------------------------------------------

CREATE TABLE actions (
    id             INTEGER PRIMARY KEY,
    tool           TEXT NOT NULL,
    args_json      TEXT NOT NULL,
    args_hash      TEXT NOT NULL,          -- same request twice is one card
    preview        TEXT NOT NULL,
    reach          TEXT NOT NULL,          -- private | outward
    reversal       TEXT NOT NULL,          -- reversible | irreversible
    reasons        TEXT,                   -- JSON array: why it was gated
    spend_usd      REAL NOT NULL DEFAULT 0,
    session_id     INTEGER REFERENCES sessions(id) ON DELETE SET NULL,
    channel        TEXT,
    status         TEXT NOT NULL DEFAULT 'pending',
    token          TEXT,                   -- the spoken word used over Signal
    ask_message_ts INTEGER,                -- so a quote-reply can be matched
    asked_at       TEXT,
    expires_at     TEXT,
    decided_at     TEXT,
    decided_by     TEXT,                   -- signal | tray | voice | auto
    executed_at    TEXT,
    result_json    TEXT,
    undo_json      TEXT,
    undone_at      TEXT,
    error          TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (status IN ('pending','approved','executing','done','failed',
                      'rejected','expired','undone'))
);

CREATE INDEX idx_actions_pending
    ON actions(expires_at) WHERE status = 'pending';

-- Partial, so a short wordlist is not exhausted for all time: a token only
-- has to be unique among the things currently waiting for an answer.
CREATE UNIQUE INDEX idx_actions_token
    ON actions(token) WHERE status = 'pending';

-- ---------------------------------------------------------------------------
-- Knowing who the user deals with, and what they said they would do
--
-- The seam against `data/memories/`: if Gmail could tell you again tomorrow
-- it is a row here; if losing it means losing something no API can return it
-- is a memory.
--
-- But the argument that actually decides it is privacy, not query shape.
-- Memories were once copied to a second machine, one deliberately denied a
-- Google token, and a people dossier among them would have shipped the
-- user's contact graph with them. These tables are local SQLite and appear
-- in no export, and that is an invariant with a test rather than an accident.
--
-- Query shape agrees. "What is due this week" is a sorted scan over a date
-- column; `recall()` returns nearest-five by cosine and can only answer what
-- is *similar to* being due.
-- ---------------------------------------------------------------------------

CREATE TABLE people (
    id            INTEGER PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE,
    name          TEXT,
    -- A first pass over a real inbox yields ~800 "people", ~780 of them
    -- Substack, LinkedIn and DoorDash. Flagged rather than dropped: a false
    -- negative is then fixable without re-ingesting a year of mail.
    is_person     INTEGER NOT NULL DEFAULT 1,
    org           TEXT,
    note          TEXT,                    -- prose, written by the night
    first_seen    TEXT,
    last_seen     TEXT,
    sent_count    INTEGER NOT NULL DEFAULT 0,   -- mail the user sent them
    recv_count    INTEGER NOT NULL DEFAULT 0,   -- mail they sent the user
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_people_real ON people(last_seen) WHERE is_person = 1;

-- One person, several addresses. Kept separate from `people` so merging two
-- rows later is a row move rather than a rewrite.
CREATE TABLE person_aliases (
    id         INTEGER PRIMARY KEY,
    person_id  INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    email      TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Every exchange, ingested by pure code: sender, time, subject, direction.
-- No content leaves the machine for these, which is what makes ingesting
-- received mail acceptable at all.
CREATE TABLE interactions (
    id          INTEGER PRIMARY KEY,
    person_id   INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,             -- email | event
    direction   TEXT NOT NULL,             -- sent | received | both
    subject     TEXT,
    source_id   TEXT NOT NULL,             -- gmail message id, or event id
    thread_id   TEXT,
    occurred_at TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (person_id, source_id)
);

CREATE INDEX idx_interactions_person
    ON interactions(person_id, occurred_at DESC);

-- Everything open that has no other home, and how each closed
-- (`tools/obligations.py`). Commitments, things the user said they would do,
-- are rows here, each with the verbatim sentence it came from: a quote the
-- source does not support is discarded by code before it gets here, because
-- an invented obligation is acted on once and the feature never trusted
-- again. Before this ledger nothing closed anything: the user said "Solve is done"
-- at 21:00 and 22:49 on 8 October and the 07:40 briefing still said it was
-- due today, and a thank-you was drafted twice, the second time hours after
-- the first, because nothing recorded that the first had run.
--
-- Reminders and cards keep their own tables, which already know when each
-- went off or was answered; a copy here would be a fourth store to disagree
-- with the other three, which is how a weekly report was "still owed" on
-- 24 September after the user had sent it. What is open now is read across
-- all three (`agent/closing.py`). Laptop-only, like `people`.
CREATE TABLE obligations (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,              -- commitment | task | ask | question | follow_up
    text        TEXT NOT NULL,              -- "send Sarah the deck"
    quote       TEXT,                       -- the words it came from, verbatim
    source      TEXT NOT NULL,              -- sent_mail | conversation | agent
    source_id   TEXT,                       -- a gmail id; dedupe
    source_ref  TEXT UNIQUE,                -- commitment:<id>
    person_id   INTEGER REFERENCES people(id) ON DELETE SET NULL,
    stakes      TEXT NOT NULL DEFAULT 'low',  -- low | outward (would send, call, post or spend)
    due_date    TEXT,                       -- YYYY-MM-DD, local
    status      TEXT NOT NULL DEFAULT 'open',
    outcome     TEXT,                       -- what came of it, in a line
    closed_at   TEXT,                       -- UTC
    closed_by   TEXT,                       -- their words | tasks tick | ran | card | their no
    task_id     TEXT,                       -- the Google Tasks mirror
    detail      TEXT,                       -- JSON: what doing it later needs
    action_id   INTEGER,                    -- the card asking them about it
    asked_at    TEXT,                       -- UTC: when the user was last asked
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    -- Wider than today's code uses: a CHECK cannot be changed in place, and
    -- the next kinds are planned (data/audits/2026-10-10/REDESIGN.md).
    CHECK (kind IN ('commitment', 'task', 'ask', 'question', 'follow_up')),
    CHECK (status IN ('open', 'asked', 'waiting', 'done', 'dropped', 'expired'))
);

CREATE INDEX idx_obligations_open
    ON obligations(due_date) WHERE status IN ('open', 'asked', 'waiting');

-- How far each ingest has read. One row per source, so a nightly run picks up
-- where it left off instead of re-reading a year of mail every time.
CREATE TABLE sync_cursors (
    source     TEXT PRIMARY KEY,           -- gmail_sent | gmail_recv | calendar
    cursor     TEXT,                       -- an epoch second, or a historyId
    ran_at     TEXT,
    note       TEXT
);

-- ---------------------------------------------------------------------------
-- Independent review of tools the agent gave itself
--
-- A tool that touches nothing is admitted on a mechanical proof: its imports
-- cannot reach anything. A tool that wants the network is a judgement, and
-- the answer to "who judges" is a second model that did not write it.
--
-- Keyed on a hash of the source, and that is the load-bearing part. An audit
-- approves *these exact bytes*. Edit one character and the row no longer
-- matches, so the tool falls back to refused until it is reviewed again --
-- which is what stops "get approved once, then change what it does".
--
-- Written only by the frozen review path. The agent has no tool that can
-- insert here, which is the whole reason the verdict means anything.
-- ---------------------------------------------------------------------------

CREATE TABLE tool_audits (
    id           INTEGER PRIMARY KEY,
    tool         TEXT NOT NULL,
    module       TEXT NOT NULL,
    source_sha   TEXT NOT NULL,          -- sha256 of the module as reviewed
    capabilities TEXT NOT NULL,          -- JSON array, exactly what was asked
    verdict      TEXT NOT NULL,          -- approved | refused
    reasoning    TEXT,
    model        TEXT,                   -- who reviewed it
    revoked_at   TEXT,                   -- the user changed their mind
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (tool, source_sha),
    CHECK (verdict IN ('approved', 'refused'))
);

-- ---------------------------------------------------------------------------
-- The failure ledger (agent/issues.py): what went wrong, and what the user
-- thought of a reply. One row per kind of thing, keyed by a fingerprint so a
-- repeat counts up; one event per occurrence, so "lately" is a count.
-- ---------------------------------------------------------------------------

CREATE TABLE issues (
    id           INTEGER PRIMARY KEY,
    kind         TEXT NOT NULL,          -- agent/issues.KINDS
    fingerprint  TEXT NOT NULL UNIQUE,
    summary      TEXT NOT NULL,          -- one line: what went wrong
    excerpt      TEXT,                   -- the latest example
    session_id   INTEGER,
    message_id   INTEGER,
    count        INTEGER NOT NULL DEFAULT 1,
    status       TEXT NOT NULL DEFAULT 'open',  -- open | fixed | ignored
    first_seen   TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen    TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (status IN ('open', 'fixed', 'ignored'))
);

CREATE TABLE issue_events (
    id        INTEGER PRIMARY KEY,
    issue_id  INTEGER NOT NULL REFERENCES issues(id),
    at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX issue_events_at ON issue_events (at);

-- Each time the night tried to fix an issue (agent/night/tinkering.py), so
-- the same one waits a few nights before it is tried again, stops being
-- offered after a few tries, and is marked fixed once its change merges.
CREATE TABLE issue_attempts (
    id          INTEGER PRIMARY KEY,
    issue_id    INTEGER NOT NULL REFERENCES issues(id),
    upgrade_id  INTEGER,                 -- upgrades.id; 0 when none was made
    status      TEXT NOT NULL,           -- the pipeline's outcome
    at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One per background job (agent/workers.py): whether Jev thought the task
-- would split, whether the lead was offered workers, and what it did with
-- them -- so Jev's bar can be checked against what actually happened.
CREATE TABLE crews (
    id          INTEGER PRIMARY KEY,
    job_id      INTEGER,
    jev_p       REAL,                    -- P(the task splits); NULL if Jev was not asked
    offered     INTEGER NOT NULL,        -- 1 if the lead had the worker tools
    jev_error   TEXT,
    workers     INTEGER NOT NULL DEFAULT 0,
    finished    INTEGER NOT NULL DEFAULT 0,  -- workers that came back with a result
    cost_usd    REAL NOT NULL DEFAULT 0, -- the workers' and Jev's, not the lead's
    seconds     REAL,
    at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- The injection screen's verdicts (core/screen.py): one per item of outside
-- text, by content, so an email checked once is never checked again.
CREATE TABLE screened (
    id           INTEGER PRIMARY KEY,
    source       TEXT NOT NULL,          -- a kind in core/screen.py KINDS, or screen_extract
                                         -- (an extract's own second look)
    source_id    TEXT,
    origin       TEXT,                   -- the sender, or the page's host
    content_sha  TEXT NOT NULL UNIQUE,
    verdict      TEXT,                   -- as_is | sanitize | ignore; NULL if unscreened
    p_injection  REAL,
    choices      TEXT,                   -- the classifier's probability for each verdict, JSON
    hits         TEXT,                   -- the pre-screen's patterns, JSON
    chars        INTEGER,
    chunks       INTEGER,                -- how many parts it was read in
    cost_usd     REAL NOT NULL DEFAULT 0,
    latency_ms   INTEGER,
    error        TEXT,
    at           TEXT NOT NULL DEFAULT (datetime('now')),
    classifier   TEXT                    -- jev (core/decide.py); piguard, tev1 or
                                         -- piguard+tev1 when Jev could not answer
                                         -- (core/screen.py _locally); NULL if none did
);

-- What a model reads of an item the screen flagged (core/screen.py
-- `extract`): the extractor's fields, made once per item and laid out by
-- frozen code each time the item is read. No row means no extract yet; a
-- failed one is not kept, so the next read tries again.
CREATE TABLE screen_extracts (
    content_sha  TEXT PRIMARY KEY,       -- screened.content_sha
    model        TEXT NOT NULL,
    extraction   TEXT NOT NULL,          -- JSON, core.screen.Extraction's fields
    cost_usd     REAL NOT NULL DEFAULT 0,
    latency_ms   INTEGER,
    at           TEXT NOT NULL DEFAULT (datetime('now'))
);

-- The second factor each high-risk yes carried (core/monitor.py): their
-- authenticator's code over a channel, or their passphrase in the app. Each
-- is bound to the one action it approved, and to that action's arguments as
-- they were then. A code's step is accepted once, ever.
CREATE TABLE approval_proofs (
    action_id  INTEGER NOT NULL REFERENCES actions(id),
    factor     TEXT NOT NULL CHECK (factor IN ('code', 'passphrase')),
    step       INTEGER UNIQUE,         -- a code's 30-second TOTP step (core/totp.py)
    args_hash  TEXT NOT NULL,          -- actions.args_hash when it was given
    at         TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (action_id, factor),
    CHECK ((factor = 'code') = (step IS NOT NULL))
);

-- Every step the sandboxed browser took (agent/toolset/browser.py), so what
-- a session did can be reconstructed: where, what, how it went. Where its
-- pages went on the network is the gate's log (data/logs/browser/).
CREATE TABLE browser_steps (
    id          INTEGER PRIMARY KEY,
    session_id  INTEGER,                   -- the conversation
    browser     TEXT NOT NULL,             -- the sandbox session
    step        INTEGER NOT NULL,
    action      TEXT NOT NULL,             -- open | read | click | search | submit | ...
    detail      TEXT,                      -- the url, ref, or search typed
    url         TEXT,
    title       TEXT,
    ok          INTEGER NOT NULL,
    error       TEXT,
    screenshot  TEXT,                      -- under data/artifacts/browser/
    action_id   INTEGER REFERENCES actions(id),  -- a submit's approval card
    at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Jev's judgements of clicks in a logged-in browser (core/browser/clicks.py):
-- what was asked about, what it said, so the bar can be checked against what
-- the clicks did. A NULL p is "could not be asked" -- carded.
CREATE TABLE browser_judgements (
    id     INTEGER PRIMARY KEY,
    site   TEXT NOT NULL,                  -- the saved login's name
    label  TEXT NOT NULL,
    url    TEXT,
    p      REAL,                           -- P(the click does something)
    error  TEXT,
    at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- The model scout (agent/models_scout.py): every model a scan has seen on
-- OpenAI's list, and which automatic roles the frozen bounds
-- (core/models_guard.py) would let it play. A row is not a verdict on
-- quality -- only the evaluation on their own turns can say that.
CREATE TABLE model_candidates (
    slug        TEXT PRIMARY KEY,          -- "openai/" and OpenAI's id
    released    TEXT,                      -- the list's creation date
    facts       TEXT NOT NULL,             -- models_guard.Candidate, as JSON
    roles       TEXT NOT NULL,             -- JSON: role -> null if it may, or why not
    eligible    INTEGER NOT NULL DEFAULT 0, -- how many roles it may play
    first_seen  TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- The model scout's evaluations (agent/models_eval.py): a candidate tried on
-- their own turns against the model a role uses now. A row is written before
-- the first call, so a run that dies still counts against the month's
-- budget. `decision` is what core/models_guard.weigh made of `measured`;
-- writing it switches nothing.
CREATE TABLE model_evals (
    id           INTEGER PRIMARY KEY,
    slug         TEXT NOT NULL,
    role         TEXT NOT NULL,
    incumbent    TEXT NOT NULL,
    measured     TEXT,                     -- models_guard.Measured, as JSON
    decision     TEXT,                     -- models_guard.Decision, as JSON
    spent_usd    REAL NOT NULL DEFAULT 0,
    report       TEXT,                     -- under data/model_evals/
    error        TEXT,
    started_at   TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at  TEXT
);

-- The model scout's switches and reverts (agent/models_switch.py). The
-- switch in force is data/model_switches.json; this is the history, and the
-- day's watch after each switch: what the two days before looked like
-- (`baseline`, core.canary.Counts), until when it is watched, and how it
-- ended -- kept, or put back and why.
CREATE TABLE model_switches (
    id           INTEGER PRIMARY KEY,
    role         TEXT NOT NULL,
    from_slug    TEXT NOT NULL,
    to_slug      TEXT NOT NULL,
    kind         TEXT NOT NULL,            -- switch | revert
    by           TEXT NOT NULL,            -- user | night | watch
    eval_id      INTEGER REFERENCES model_evals(id),
    why          TEXT,
    baseline     TEXT,                     -- a switch: canary.Counts before, as JSON
    watch_until  TEXT,                     -- a switch: when its watch ends
    outcome      TEXT,                     -- a switch: kept | reverted: why
    at           TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (kind IN ('switch', 'revert'))
);

-- Routines the user sets by text (agent/routines.py): "every Sunday at 7pm, recap
-- my week", "check every few hours until the tickets drop". Times are local
-- and naive, as reminders' are. `envelope` is what a run may do outward
-- without asking; it counts only while an `approve_routine` card the user approved
-- carries its hash (core/monitor.approved_envelope), so a routine that has
-- one waits in `awaiting_approval` until then.
CREATE TABLE routines (
    id           INTEGER PRIMARY KEY,
    title        TEXT NOT NULL,
    instruction  TEXT NOT NULL,             -- what to do each run, from their words
    schedule     TEXT NOT NULL,             -- JSON: agent/routines.Schedule
    envelope     TEXT,                      -- JSON {tool: {"targets": [...]}}, or NULL
    urgent       INTEGER NOT NULL DEFAULT 0, -- a watch that may reach them in quiet hours
    status       TEXT NOT NULL DEFAULT 'active',
    session_id   INTEGER,                   -- the conversation it was set up in
    next_run_at  TEXT,
    last_run_at  TEXT,
    last_report  TEXT,                      -- what the user was last told, to report only change
    held         TEXT,                      -- a report kept back through quiet hours
    runs         INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (status IN ('awaiting_approval', 'active', 'paused', 'ended', 'cancelled'))
);

-- Goals the user sets (agent/goals.py): worked on over days, in unattended
-- sessions, until done. Only the user writes `ask`, `done_when`, `envelope`,
-- `budget_usd` and `deadline`; a session writes its plan, findings,
-- questions and next check through `goals.apply_update`, and nothing else.
CREATE TABLE goals (
    id              INTEGER PRIMARY KEY,
    title           TEXT NOT NULL,
    ask             TEXT NOT NULL,             -- their words, made to stand alone
    done_when       TEXT NOT NULL,
    steps           TEXT NOT NULL DEFAULT '[]', -- JSON [{n, text, status, note}]
    findings        TEXT NOT NULL DEFAULT '[]', -- JSON [{at, text}]
    questions       TEXT NOT NULL DEFAULT '[]', -- JSON [{n, text, asked_at, told, answer}]
    notes           TEXT NOT NULL DEFAULT '[]', -- JSON [{at, text}]: what the user told it since
    waiting_on      TEXT NOT NULL DEFAULT '[]', -- JSON [address]: mail from them wakes it
    actions         TEXT NOT NULL DEFAULT '[]', -- JSON [action id]: what its sessions did or asked
    envelope        TEXT,                      -- JSON {tool: {"targets": [...]}}, or NULL
    status          TEXT NOT NULL DEFAULT 'active',
    every_hours     REAL NOT NULL DEFAULT 12,
    next_check_at   TEXT,                      -- local ISO; NULL while it waits on them
    running_since   TEXT,                      -- a session is under way
    deadline        TEXT,                      -- local ISO
    budget_usd      REAL NOT NULL DEFAULT 2.0,
    spent_usd       REAL NOT NULL DEFAULT 0,
    sessions        INTEGER NOT NULL DEFAULT 0,
    last_session_at TEXT,
    last_report     TEXT,                      -- what the user was last told
    outcome         TEXT,                      -- how it ended
    session_id      INTEGER,                   -- the conversation it was set in
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    unsent          TEXT,                      -- JSON {"report": ...}: a message owed to them
    CHECK (status IN ('awaiting_approval', 'active', 'paused', 'done', 'dropped', 'expired'))
);

-- Mail to the agent's own mailbox (agent/mailbox.py), each judged once by
-- core/mailbox.py: their instruction, a reply in a thread the user brought it into,
-- or ignored -- and what was started from it.
CREATE TABLE mailbox (
    id          INTEGER PRIMARY KEY,
    message_id  TEXT NOT NULL UNIQUE,    -- Gmail's id
    thread_id   TEXT,
    sender      TEXT,
    subject     TEXT,
    kind        TEXT NOT NULL,           -- instruction | thread | ignored
    why         TEXT,                    -- the authentication verdict, in words
    note        TEXT,                    -- their note, for an instruction
    job_id      INTEGER,
    goal_id     INTEGER,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (kind IN ('instruction', 'thread', 'ignored'))
);

-- The action critic's reviews (core/critic.py): every action an unattended
-- session proposed, what the critic made of it against their instruction, and
-- what enforcing would have done. In shadow, the record is all it does.
CREATE TABLE critic_reviews (
    id          INTEGER PRIMARY KEY,
    session_id  INTEGER,
    channel     TEXT,
    tool        TEXT NOT NULL,
    action_key  TEXT,                   -- the ledger's args hash, to find the action
    gate        TEXT,                   -- free | card: what the monitor had decided
    verdict     TEXT,                   -- serves | unclear | against; NULL if unreviewed
    p_serves    REAL,
    choices     TEXT,                   -- the critic's probability for each, JSON
    hits        TEXT,                   -- the pre-screen's patterns in the action's text
    decision    TEXT,                   -- allow | card: what enforcing does
    mode        TEXT,                   -- shadow | enforce, when it was made
    mandate     TEXT,                   -- their instruction, as the critic was shown it
    action      TEXT,                   -- the action, as the critic was shown it
    cost_usd    REAL NOT NULL DEFAULT 0,
    latency_ms  INTEGER,
    error       TEXT,
    at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Short stand-ins for long ids (agent/handles.py): what a model is shown in
-- place of a 156-character Outlook message id, and the id it stands for.
CREATE TABLE handles (
    handle      TEXT PRIMARY KEY,           -- outlook#9f3a2c1b7e
    full        TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    used_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Skills (agent/skills.py; what they may say, core/skills.py): procedures
-- written from runs of what the user asked, offered when the user asks something like it.
-- Every run tried is a row -- kept (active), refused, or skipped as a one-off
-- -- so it is not tried again; revoked ones stay, revoked.
CREATE TABLE skills (
    id              INTEGER PRIMARY KEY,
    title           TEXT NOT NULL,
    when_to_use     TEXT,
    steps           TEXT,                   -- JSON list
    tools           TEXT,                   -- JSON list
    source_message  INTEGER,                -- their request it was written from
    source_session  INTEGER,
    status          TEXT NOT NULL,          -- active | refused | skipped | revoked
    why             TEXT,                   -- the review's reason, or why it was refused
    uses            INTEGER NOT NULL DEFAULT 0,
    last_used_at    TEXT,
    revoked_at      TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE skill_uses (
    id          INTEGER PRIMARY KEY,
    skill_id    INTEGER NOT NULL REFERENCES skills(id),
    session_id  INTEGER,
    at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Their notes, from the desk's voice (agent/notes.py): a sentence ("hey Steve,
-- note that..."), a dictation, or a lecture or meeting transcribed on the
-- laptop. Text only: no sound is kept. A lecture's transcript stays in a file
-- under data/notes/; `body` holds its summary once it has one.
CREATE TABLE notes (
    id              INTEGER PRIMARY KEY,
    kind            TEXT NOT NULL,          -- quick | dictation | lecture
    title           TEXT,
    body            TEXT NOT NULL DEFAULT '',
    transcript_path TEXT,                   -- a lecture's, local
    doc_url         TEXT,                   -- a lecture summary's Google Doc
    status          TEXT NOT NULL DEFAULT 'done',  -- open while it is being taken
    started_at      TEXT NOT NULL DEFAULT (datetime('now')),
    ended_at        TEXT
);
