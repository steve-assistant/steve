# Protocols

Rules for writing code here, each with the failure that earned it and what
enforces it. Where a rule is enforced by a test, the test is the authority;
where it is not yet, it is a rule you check by hand, and the gap is named.
`docs/architecture.md` explains the processes and machines these refer to.

## 1. Know where the code runs

Before writing a line, name its runtime context (worker loop, worker thread,
supervisor, app, CLI, test — see the architecture table) and what that
context actually has.

- A module run with `-m` is `__main__`; anything importing it by name gets a
  second copy with every global `None`. Nothing imports `agent.server` (it
  aliases itself anyway); anything else started with `-m` and imported back
  must do the same.
  *(Earned: a tool read the second copy's empty state, and reported a
  connection that was up as offline.)*
- The worker's live state is in `agent.runtime` (`conn`, `signal`, `voice`,
  `jobs`), and it is None in a CLI run or a test. Read it there, never from
  `agent.server`, and say what the code does when it is None.

## 2. Never block the event loop

An `async def` never calls anything that blocks: subprocesses, sync HTTP,
Docker, embedding, git, COM, `time.sleep`, anything slow. Use
`asyncio.to_thread`, or `agent.offloop.with_own_conn` when it needs the
database, or `agent.offloop.Probe` for a slow status value.
*(Earned: the tray's 5-second `/status` poll ran `docker info` on the loop;
the watchdog killed healthy workers — crash loops with nothing crashing.)*
**Enforced:** `tests/unit/test_offloop.py` scans every `async def` against a
list of known-blocking calls. When you find a new one, add it to the list.

## 3. Connections never cross threads

A connection belongs to the thread that opened it. In a thread, open your own
to the same file — `offloop.with_own_conn(conn, fn, ...)` does exactly that —
never the loop's `runtime.conn`, and never `db.open_store()` with no path from code a
test might run. *(Earned: every nightly upgrade attempt died on the first real
night with `ProgrammingError`.)* **Enforced:** `tests/unit/test_offloop.py`;
unit tests get their own database via `tests/unit/conftest.py`.

The same goes for a cached client that holds an open socket, unless its
library says it is thread-safe (httpx's clients are; `httplib2.Http` is not).
Two threads on one TLS socket do not raise an error: they corrupt OpenSSL's
heap, and the process dies later, somewhere else, with no Python traceback.
The Google clients are cached, so `modules/google_cloud/common.RetryingRequest` hands
each thread its own connection. *(Earned: six native worker crashes in eight
days, 22–30 September 2026, before faulthandler's dump named the four
threads inside Google calls.)* **Enforced:**
`tests/unit/test_google_common.py`.

## 4. Assume there is another copy of you

Two workers overlap during every swap. Anything that must happen once is a
duty (see the architecture doc) and starts in `worker/duties.take_up_duties`, never
directly in the lifespan. Anything claimed from shared state records its
claimant and is reclaimed only when the claimant is dead. *(Earned: the new
worker re-ran the old one's job; both held the one connection to the phone
and took it from each other.)*
**Enforced:** `tests/gate/test_duty_handover.py`, `tests/unit/test_duties.py`.

## 5. Starting processes

- Long-lived processes are started by Task Scheduler or WMI, never
  `Start-Process` from a session: a session's job object kills its children.
- Console programs inherit the supervisor's windowless console; do not start
  the supervisor as `pythonw`. *(Earned: flashing PowerShell windows.)*
- Never `stdout=PIPE` unless you read it to the end; send output to a file.
  *(Earned: a supervisor frozen by a full pipe buffer, twice.)*
- Every subprocess has a timeout. Kill trees (`taskkill /T`), not stubs — the
  venv's `python.exe` is a launcher with the real interpreter as its child.
- A launch is not a success until you have seen the thing running. The app
  shows the backend as starting until it proves itself.
**Enforced:** by hand; the app's job object by its Rust tests
(`ui/src-tauri/src/backend.rs`).

## 6. Liveness is not health

A process that answers is not a process that works. Judge each layer only
from directly above it, with its own health route; report what a worker is
doing (`/health` has `duties` and its uptime), not what the repo says now.
*(Earned: the keeper the app replaced killed a supervisor mid-rollback.)*

## 7. Time

SQLite's `datetime('now')` is UTC; Python's `datetime.now()` is local and
naive. Compare like with like — both SQLite (`created_at > asked_at`) or both
converted (`datetime(created_at, 'localtime')`). A Python moment becomes
SQLite's text through `agent/sqltime.py` (`stamp`, and `moment` back), not
by hand. Any test whose outcome depends on the clock (quiet hours
22:00–08:00, day boundaries) pins the clock. A model never converts between
time zones: it passes the time as its source wrote it and the zone, and
`agent/zones.py` (`in_user_time`) does the sum. *(Earned: spend "today" rolled over at 8 p.m.; a card TTL expired four
hours early; a test that failed only at night; "4pm MST" put on their calendar
at 4 pm Eastern.)*

## 8. Approvals can only be answered by the user, before any model

- Decided only in code that runs before a model sees the text:
  `worker/answering.intercept_approval`. A model that could read an approval
  could write one.
- Only the user's channels (`signal`, `voice`, `phone`) may answer. A new input
  path gets its own channel name; never reuse `signal` for something that is
  not them typing.
- A bare "yes" is the whole message, with one card waiting, and nothing said
  since. Anything else names the card.
- An intercept forwards what it does not own. *(Earned: an intercept
  swallowed every approval that was not its own; "send me the notes" could send a pending email; a 👍
  on any message approved the waiting card.)*
**Enforced:** `tests/unit/test_approval_replies.py`,
`tests/unit/test_approval_intercept.py`, `tests/unit/test_bridge.py`,
`tests/gate/test_call_policy.py`.

## 9. Every boundary checks who is on the other side

- Localhost is not private: browsers reach it. Both the supervisor and
  workers apply `core.localonly.LocalOnly`; a new ASGI app on this machine
  must too. **Enforced:** `tests/gate/test_local_only.py`.
- A URL a model chose is fetched only through `core/egress.py`: never a
  private address, a bare IP or a local name, on any redirect hop; and in a
  run nobody is watching, only a URL it was shown (a search result, or their
  own words), as in a conversation they are in once it has read outside
  text (`agent/toolset/web.py` `unshown`). A new tool that fetches what a model names goes through it
  too. *(Earned: the doer could be steered by an email into a GET that
  carried their data.)* **Enforced:** `tests/gate/test_injection_containment.py`.

## 10. Failures are reported, never absorbed

- Executors return a dict and never raise; so every caller checks for a
  reported failure (`approvals.reported_failure`) instead of treating "it
  returned" as "it worked". *(Earned: a bounced email announced as "Done".)*
- No `except Exception: pass` without a log line at WARNING or above, and a
  comment saying why continuing is right.
- The worker configures logging (`_configure_logging`); a new process must
  too, or its INFO lines vanish and its last lines before a crash are lost to
  buffering. *(Earned: every worker log was empty.)*
- A send is not a delivery. Every message to the user is a row in the
  outbox (`notify_user`, `agent/outbox.py`) before anything sends it, and
  no caller sends or retries one itself. It joins a conversation, and its
  lines in `told` are said, only once the remote channel took it or the app
  showed it; one that stopped trying says why. *(Earned: cards and digests
  counted as delivered when Signal was down; with Signal down, a reminder
  went nowhere at all.)* **Enforced:** `tests/unit/test_outbox.py`,
  `tests/unit/test_attention.py`.

## 11. Third-party contracts are verified, not guessed

Before relying on a field of a third-party API, confirm it against the
provider's spec or a captured real payload, and cite which in a comment or a
test fixture. A guessed field fails silently: the value is ignored and nothing
errors. On an error, surface the provider's own message, never a truncated
status code. *(Earned: three of a phone carrier's field names ignored in
silence; a 403 that hid "account level D60 may only call verified
numbers".)*

## 12. Tests never touch anything live

- Unit tests: `tests/unit/conftest.py` shuts every door — Google (below
  `service`), Outlook, Signal, the running agent's processes and files, the
  live database. Extend it rather than working
  around it, and prove each guard from the caller's side
  (`tests/unit/test_live_guards.py`). *(Earned: tests that read Gmail, the
  Exchange cache, and took the running agent's connection to the phone from
  it; a Google guard that patched a name nobody read.)*
- Gate tests that start processes run them from a sandbox copy of the tree
  (`sandbox_repo` in `tests/gate/test_zero_downtime.py`) with `STEVE_INERT=1`.
  *(Earned: a gate test wrote a broken
  `agent/server.py` over the live one.)*
- A guard test must fail against the unfixed code. Check it: restore the old
  code, run, put it back.

## 13. Schema changes

A change to the schema is a new numbered file in `db/migrations/`, applied once, in a
transaction, by `core/db.migrate`. Until v0.1 ships, `0001_initial.sql` may be edited in
place, because no user database exists yet; from then on migrations are append-only.
*(Earned: before migrations, `CREATE TABLE IF NOT EXISTS` never altered a live table,
and `jobs.claimed_by` would never have reached the live database.)*

## 14. Done means running

A committed fix is not a deployed fix, and a swapped worker is not a
restarted supervisor. After a change, confirm the running process has it:
the worker's `/health` uptime or `/_sup/health`. Worker code needs a swap;
supervisor code needs the app's Restart.

## 15. Cut text says it was cut; long ids never reach a model

`text[:N]` decides silently that the rest does not matter. Where a person
decides from what is left, or a model reasons from it, cut with
`steve_sdk/cut.cut` instead: at a word or a sentence, with a mark that says how
much is missing -- and where a tool can read on, say how (`read_email`'s
`from_char`). A card the user approves is never cut: it shows whole what the yes
binds them to and quotes no message body (`approvals.quoted_body`). JSON is
shortened value by value,
never mid-string (`loop._args_kept`). An id longer than
`handles.LONGER_THAN` is never handed to a model: every tool's results and
arguments pass through `agent/handles.py`, and so must any prompt that lists
ids outside a tool (`prepare.describe`). Log lines, ledger excerpts and error
summaries may stay plain slices. *(Earned, 4 October 2026: a mangled
156-character Outlook id made the canary revert three innocent upgrades; a Gmail
card showed 180 characters of an email as if they were all of it; Outlook bodies
stopped at 4,000 characters with nothing to say so.)*
