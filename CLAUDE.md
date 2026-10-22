# Steve

Steve is a personal assistant for Windows: a Tauri desktop app, and a Python backend (`core/supervisor.py` starting `agent/` workers) that it talks to over 127.0.0.1. The map of the code is `docs/architecture.md`; how it is tested is `docs/testing.md`.

## Identity: never share a name with another install

Steve can run beside another agent built from the same code, as the same Windows user. Anything that user's account holds is shared between them: environment variables, the Credential Manager, loopback ports, Task Scheduler, the Run key, Docker. So every name Steve uses there is its own, and all but the environment variables and the port come from `core/identity.py`:

| What | Steve's |
|---|---|
| Environment variables | `STEVE_*` only, written out in full so a search finds each one, besides the standard ones (provider keys, `PATH`, `LOCALAPPDATA`) |
| Credential Manager service | `steve`; `steve-modules` for each account's secrets (`core/credstore.py`); and `steve-*` for other stores |
| Supervisor port | chosen by the system at each start, on 127.0.0.1, and named with its token in the run file (`core/localapi.py`) |
| Scheduled tasks | `SteveWake.<install>` |
| Run key values | `Steve`, the app's own start-at-login; the app starts the backend |
| Tauri app | identifier `dev.steve.desktop`, product name `Steve` |
| Docker | label `steve.install`, the install's id, on everything Steve starts there, and `steve.browser` on the browser's; images `steve/*`, containers `steve-*` |
| Module sandbox | AppContainer profiles `steve.<install>.m.<id>` (a module's), `.s.<digest>` (an MCP server's, per account) and `.n.<id>` (npm's), capability `steve.runtime`, pipes `\\.\pipe\steve.*` |
| Windows Hello | the key `steve.<install>.hello` |
| Tracing | service name `steve` |

Take a new shared name from `core/identity.py` too. `tests/gate/test_identity.py` checks the tree for the old names, and the test fences refuse a test, and any process it starts, a connection to the other install's port (by socket or asyncio) and any use of another keychain service.

## Where Steve keeps things

- **Data:** everything the backend writes is under one data folder, chosen by `core/paths.py`: `STEVE_DATA_DIR` if set, the checkout's `data/` in a source tree, `%LOCALAPPDATA%\Steve` when installed. Build every such path with its functions. `tests/gate/test_paths.py` fails on a module that joins `"data"` onto its own location.
  - One-off files go in `core/paths.scratch_dir`, not `%TEMP%`: every program of the user shares that, and removing Steve's data would miss it.
  - The desktop app keeps only its webview's files, in Windows' folder for `dev.steve.desktop`.
- **Schema:** a change is a new numbered file in `db/migrations/`, which `core/db.migrate` applies once, in a transaction, with foreign keys off, and fingerprints. Until v0.1 ships, `0001_initial.sql` may be edited in place, and a database made before the edit is then refused (move it aside); from then on, migrations are append-only.
- **Times:** the database stores UTC, as SQLite's `datetime('now')` writes it. `agent/sqltime.py` turns a moment into that text and back.

## Working here

- Files are LF (`.gitattributes`). On Windows, `Path.write_text` needs `newline="\n"`. `tests/unit/test_repo_hygiene.py` fails on any CRLF.
- Do not write Python containing `\n` or other escapes through a shell heredoc: the escapes become real newlines inside string literals. Use an editor, or a script file.
- While working, `python -m devtools.check` runs lint, the frozen lock and the tests a change names, in about a minute. Before each commit, `python -m devtools.check --processes` runs those tests that start processes too. The whole suite (`python -m devtools.check --full`, or `pytest tests/ -q -n 3 --dist loadgroup`, about seven minutes) runs once a series of commits is done, such as a plan's step, and before anything is pushed or merged.
- Tests never reach anything live: `tests/gate/fences.py` shuts every door to a real service and gives each test its own data folder, for unit and gate tests alike. A test that needs a service injects a fake. Extend the fences rather than working around them.
- A guard test must be shown failing on the code from before the fix: `python -m devtools.prove` runs a change's new tests against the old code, and `python -m devtools.mutate` breaks the code on purpose and says which tests noticed.
- Pin the clock in any test of time-dependent code (quiet hours, day boundaries); an unpinned one passes by day and fails at night.
- Frozen files (`frozen:` in `core/policy.yaml`) are hashed in `core/policy.lock`. After editing one, run `python -m core.policy --write-lock` and commit the lock with the change.
- The documents name only files and functions that exist (`tests/unit/test_docs_are_true.py`). Move code, update the map.
- `AGENTS.md` is this file for other coding agents. Edit this one, then copy it over; a test checks they match.

## Conventions

- Module docstrings argue: they name the failure the code prevents, in plain prose.
- Test names are full sentences stating the guarantee, and their docstrings say why it matters. Fakes are small local classes, not mocks.
- Comments explain why, including the approach that was wrong and why.
- Use the shared helpers for the recurring traps rather than solving them again:
  - `agent/offloop.py`: blocking work, threads and the database;
  - `agent/runtime.py`: the worker's live state (never import `agent.server`);
  - `agent/sqltime.py`: a moment as SQLite's UTC text, and as the app reads one (`iso`);
  - `agent/cut.py`: cutting text short, and saying so;
  - `core/localonly.py`: the localhost boundary;
  - `agent/worker/bodies.py`: a route's JSON body, its numbers, and its refusal;
  - `spend.openai_why` and `spend.if_spent_here`: a direct OpenAI call's failure, and a spent balance;
  - `approvals.reported_failure`: reading an executor's result.
- Commit messages are post-mortems: what broke, why, what changed, how it was verified.
