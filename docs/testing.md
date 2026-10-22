# Testing

How the suite is laid out, what a test here may and may not pin, and the loop
for running it while you work. Read it before changing a test, a conftest or
`devtools/`. `docs/protocols.md` §12 has the rule that tests touch nothing
live; this is the rest.

The maintainer, 8 October 2026: "a vast majority of our time and effort and tokens are
currently being spent on unfreeze, build, mutation check 20 things, freeze,
run 5000 tests, fail 2 of them for fake reasons and 1 for real". The audit
behind this page counted 135 full runs in ten days (11.3 hours; a quarter of
them failing, mostly on things a twenty-second look catches) and 13 of 22
upgrade attempts since #63 rejected by a failure that would have happened with
no change at all. So the rules below are about accuracy as much as speed.

## One environment

`tests/unit/` and `tests/gate/` (frozen: it is the judge) run in the same
world. `tests/gate/fences.py` shuts the doors both need -- every
connection off this computer, the user's keychain (its reads and its
writes), the Credential Manager where modules' secrets live (an empty
store of each test's own), Signal's paths, Telegram's Bot API,
the add-ons' folder and their downloads, the other install's variables,
Docker, every model client and OpenAI's speech-to-text, the user's
mailboxes -- and both
conftests call it, so a fence cannot exist in one tree only.
Until it did, a gate test read the developer's real OpenRouter key on the
laptop and nothing as the coding sandbox's account, and shadow attempts
#82-#87 and the developer's own request #88 were rejected for it.
A process a test starts is fenced too. The fences make each test process a
Python of its own (`fenced_python`), a venv whose site-packages holds
`tests/gate/childfence/sitecustomize.py`, and set `sys.executable` to it;
they also put `tests/gate/childfence/` on `PYTHONPATH`. A venv's
site-packages is read whatever `-E` and `-I` say, and a worker starts with
`-E -s` and the app's backend with `-I`, both of which drop `PYTHONPATH`:
until step 9 a worker a gate test started ran unfenced. So every Python a
test starts, and every one that starts in turn, imports that
`sitecustomize.py` first, which shuts the network beyond this computer, the
other install's port, the keychain and the Credential Manager in that
process, and refuses it Docker's own programs, however they are named or
started. The test's own process is fenced by the same hook, so the module
and self-upgrade paths pass with Docker absent. `tests/gate/test_fences_hold.py` proves each fence from the
caller's side. A new door to something live goes in `fences.py`. A door named
in code the night's coder may change (`tools/`, `agent/`) is skipped
when its name has gone, not an error at setup in every test of both trees: the
file is frozen, so the change that renamed it could not mend it. The
doors it could not find are listed by `tests/unit/test_live_guards.py` (its
`MOVED` table), where a change can say where a door went. Doors in `core/`
stay strict. A module's doors (Google's client, its sign-in and its held
token) are listed as `agent/`'s are, since step 5 moved them into `modules/`.

Markers (registered in both conftests; `pyproject.toml` is frozen):
`live` hits real APIs and is never run here; `processes` starts real
processes, Docker or the module sandbox (the supervisor tests, the
sandbox walls, real-Docker code execution) and is what the fast tier
leaves out; `docker` lets a test's own process start the real Docker,
for the code tool's tests in `tests/unit/test_code_exec.py`, and no
other test or child process; `appcontainer` (gate only) may make AppContainer profiles
and run a module's code in them, which `tests/gate/test_module_sandbox.py`
does to try each wall from inside, and
`tests/gate/test_module_broker_sandbox.py` each way out through the
broker, `tests/gate/test_module_admission_sandbox.py` a module's own
tests in a container made for the run, and `tests/gate/test_module_tools.py`
a model's call to a module's tool, deleting them after.
Nothing but the loop below ever deselects a marker: the final gate and the
night's gate run everything.

## The loop

Tools in `devtools/` (not `scripts/`, which is frozen, and not `tools/`,
which is the agent's own). Run them from your worktree, never from the live
checkout; `python -m devtools.mutate` and `python -m devtools.prove` refuse it.

1. **Affected tests first.** `python -m devtools.affected` (`devtools/affected.py`) lists the test
   files a change names: those that import a changed module or name it in a
   `monkeypatch` string, those called after it, those that read its path in
   a string other than a docstring, a changed test, and the whole folder of
   a changed conftest -- plus
   twenty-nine repository-wide checks that always run (lint, docs, tool
   classification, the frozen core, the names and hosts no file may
   carry...). A test that scans every file git lists belongs on that list
   (`REPO_WIDE`): no test names a doc, so a guard left off it first runs in
   the full suite. Not "everything reachable by imports":
   that is 68% of the suite for a median commit, because a few modules are
   reached by most tests; naming it is 12%. Given the commit the change
   began from (`devtools.check` gives it), an importer is narrower still
   when the change stays inside the module's functions and classes: it is
   picked only if it names one of them (`affected.touched`). A new
   function in `core/paths.py`, which 74 test files import, picked all 74
   and 633 s of tests on 20 October 2026; it now picks the one test file
   that calls it, and one that reads the module's path.
   A change to the module's own lines (an import, a constant) still picks
   every importer.
2. **The fast tier.** `python -m devtools.check` (`devtools/check.py`) runs `ruff`, then the frozen
   lock against the frozen files (a stale lock stops the run, names the files
   and says to run `python -m core.policy --write-lock` once the change is
   final), then those affected tests without the `processes` ones. About
   two minutes for a change that touches no test's subject: on 21 October
   2026 a change to two documents ran 905 tests in 111 s on two workers,
   most of them those checks. (On 13 October the nineteen of then took 40 s;
   in the live checkout they took 72 s, 58 of them one test
   that walked every session's worktree under `.claude`.) A test that reads the
   whole tree takes its files from `tests/gate/repo_files.py`: git's list,
   tracked and new, so `.claude`, `.venv` and the rest are never entered, and
   each file read and parsed once in a worker, not once a test. It
   never prints dots: each failure is its id and its first `E` line, read from
   pytest's junit file. With `--processes` it keeps the named tests that start
   processes, and that is the check before each commit. A change to a file
   under `ui/src-tauri/` also runs the shell's own tests (`RUST_TESTS`,
   `cargo test --lib` with the app's build features, so they reuse most of
   its crates: 77 tests, 1 min 40 s with the app already built). Tauri and
   its plugins are compiled once more for them, with tauri's mock runtime
   (`test`), and kept, which adds about a minute and a half the first time.
   `--full` always runs them. They need the app's pages built (`npm run
   build` in `ui/`), and the check says so rather than skipping them.

   **A slow or stuck test is named, and where it spent its time is one
   command away.** Every run ends with its tests over five seconds, the
   slowest first, and the command for the slowest:
   `python -m devtools.slow <test>` (`devtools/slow.py`) runs it in one
   process with its stack sampled every 50 ms, and prints, for each test,
   the lines it spent its time at (its own line, the innermost of the
   project under it, and what that waited on; for an async test, the
   coroutine it awaits). A test still running after two minutes is stopped
   by `devtools/stuck.py`, the run goes on, and the check prints the
   stuck test's own frames from the stacks it left. On 14 October 2026 this
   found three causes of the zero-downtime tests' slowness: a supervisor
   that published its port before listening (two seconds per early probe),
   a third of a second per httpx client spent reading certificates
   (`core/localapi.tls`), and a watchdog that took up to fifteen seconds to
   see a killed worker.
3. **One mutation runner.** A guard test is shown to fail when the guarded
   code is wrong: `python -m devtools.mutate spec.json` (`devtools/mutate.py`) takes a list of
   (name, file, old, new, test), refuses a spec whose `old` is not in the file
   exactly once, runs each test unmutated first, then each mutation in a
   scratch worktree (two at a time), and prints CAUGHT, MISSED or BROKEN per
   line. Exit 1 on a miss. It never writes to the tree you are working in.
   A scratch tree shares the checkout's ignored builds, the module runtime
   and the UI's packages (`devtools/worktrees.py` `share_builds`), so the
   sandbox's and the screens' tests run there; a run whose every test
   skipped is BROKEN, not a miss.
   For a new test that came with a change, `python -m devtools.prove`
   (`devtools/prove.py`) is the other half: it runs the tests the change added or altered against the code
   from before it, and says which of them fail without it (they prove the
   change) and which pass (they prove nothing new).
4. **The full suite, once, last:** when a series of commits is done, such as
   a plan's step, and before anything is pushed or merged. Until 13 October
   2026 it ran before every commit, and in the first eight commits of step
   4 running and waiting on tests was a third of the working time; a
   commit's own tests now run with `--processes`, and what the whole
   suite finds is fixed in the next commit. `python -m devtools.check --full` does 1-2,
   then everything under `tests/` with nothing deselected (when the change
   names most of the suite, 2's tests are left to this run rather than run
   twice), then runs each
   failure again, alone. A test that fails alone is a real failure; one that
   passes alone was the load or another test, and is named as such rather than
   hidden or blamed on the change. Exit 0 clean, 1 a real failure, 2 only
   load-sensitive ones. The command in `CLAUDE.md` is the same run without the
   report, and is still the way to run it by hand.

The suite is not run for each edit. `-n 3` is the most it takes of the laptop
(CLAUDE.md); the loop's own runs use two. Every run on workers takes
`--dist loadgroup` (`devtools/check.DIST`): tests marked with one
`xdist_group` go to one worker together, so a module fixture as costly as
test_zero_downtime's supervisor is started once, not on every worker that
draws one of its tests.

The app's screens are tested in `tests/unit/ui/`, under the unit tree's
fences, with Playwright over CDP (`tests/unit/ui/rig.py`). Playwright never
starts a browser itself: its own close of Edge took 30 s and left Edge's
helpers running. The test starts it, puts it in a kill-on-close job, and
attaches.
- **Edge**, as Windows ships it, runs the screens built from this checkout
  for the run, served with a backend of the test's own on one origin. A
  stand-in for the shell answers the page's `backend` command with that
  origin and a token, and records every other command the page sends. It
  holds the app's session as the shell does, across a reload, and ends it
  on the test's word (`signOut`). A test sets its answers on
  `__STEVE_SHELL__.answers` after the page loads, or before, in
  `__STEVE_SHELL_PRESET__` from an init script of its own; an answer that
  is a function answers from the command's arguments, as Windows Hello's
  signature does from a software key.
- **The real app** is the developer's debug build. It runs on the test's
  data folder, with a webview folder of its own, and its backend runs in the
  fenced Python (`STEVE_PYTHON`, which only a developer's build reads).
  Where a test needs OpenAI to answer, it starts `tests/fakes/openai.py`
  and names it in `STEVE_OPENAI_URL`, which only a checkout honours, and
  only for an address on this computer (`core/endpoints.py`).
  These tests skip where the app was never built, and fail where it is
  older than its sources.
- **The exit walk** (`tests/unit/ui/test_exit_walk.py`) is the real app on
  a fresh data folder. It goes through setup to a first answer and a first
  card answered Yes. Then every control on Settings and What Steve may do
  is moved, and read back after the app and backend start again. It found
  what the screen tests, on the backend's own origin, could not: a method
  the supervisor's CORS refused.
- **Three walks beside it** (`tests/unit/ui/test_app_walks.py`) start
  someone already set up, each on a fresh data folder seeded before the
  start: a card answered from the main window's note with the passphrase
  and another from the quick bar with a Yes; a reminder going off and the
  morning's briefing on the day; and the month's cap, said in a reply and
  fixed from its note. What the walks share is `tests/unit/ui/walk.py`.
- **A walk never takes the developer's desktop.** A test's app shows its
  window without asking for the foreground, which Windows refuses it and
  tao would take anyway by pressing Alt (`focus_main` in
  `ui/src-tauri/src/lib.rs`). The quick bar is driven where it is, hidden,
  and told it was shown as the shell tells it. A failure inside
  `rig.desktop_app` carries the app's story: running or gone, its pages,
  their closes and crashes, and the last lines of each log.
- **Neither browser reaches past this computer:** every name resolves to
  nothing and every address goes to a proxy nobody runs, loopback excepted
  (`LOOPBACK_ONLY`).
- **One build per run, one Edge per file:** the tests share the xdist group
  `ui`. Edge lasts a file because Playwright's sync API keeps its event loop
  marked as running, which breaks `asyncio.run` in later tests. The walks
  on the real app need neither, and are the group `app`, on a worker beside
  them: in `ui`, the folder took ten minutes on one worker.
- **Windows' settings are asked of Edge, never changed:** a test emulates
  the dark theme, animation effects off or a contrast theme
  (`emulate_media`), so the developer's own Windows is never touched
  (`tests/unit/ui/test_foundations.py`).
- **A component before its screen** is tested on the specimen sheet,
  `/specimen.html`, which the run's build adds (Vite's `specimen` mode)
  and the app never ships. `window.__mark` sets the live mark's state as
  the app will (`tests/unit/ui/test_mark_screen.py`).
- **What only a person can judge** is the maintainer's, by hand: how each
  page reads, the caption's colour, the tray, toasts, the desk card's
  window, Windows Hello's own prompt, and the voice's level with a real
  voice. The maintainer walks them by hand on a fresh
  data folder.

`npx tsc -b` in `ui/` checks the screens' types. To look at the account
and module screens, `python -m devtools.preview`
(`devtools/preview.py`) serves them in a browser against a backend on the
fakes: the app's own routes, run under pytest from `tests/unit/preview_ui.py`
so the fences hold, with an account in each health state, and modules and
cards in each state the Extensions page shows. Vite's "preview" mode passes the
requests on and adds that backend's token, so the page never holds one.

## What a test here pins

A test fails for a reason about behaviour or it fails for a fake one, and the
fake ones are what the night's coder dies of: the coder cannot edit the gate,
and a judge reads loosening a test as weakening it.

- **Behaviour, not spelling.** Not `"audit.is_revoked(" in getsource(...)`:
  drive the code with a fake and assert what it did. Where it cannot be
  driven, check the syntax with `ast` -- call and keyword names, whatever
  their order or layout (`tests/unit/test_loop.py`, every hop's `Deps`).
- **Properties, not one run of a heuristic.** `tests/unit/test_mail_search_widening.py`
  pins what every set of narrower searches must be (no repeats, their operators
  kept, nothing invented, no bare count), and keeps their Tailspin search as the
  one golden case; the exact lists it replaced made a better heuristic fail.
- **Counted, not timed.** Three calls "run together" is `max in flight == 3`,
  not "under 0.6 s". An upper bound on elapsed time fails on a loaded laptop
  with the code right. A lower bound is load-safe; a wait for something that
  must not take ninety seconds can allow five.
- **Derived, not copied.** The outbound-call prompt check takes its marker
  lines from `agent/prompts/system.md` rather than a phrase that a reword
  would orphan.
- **A new test fails without the change.** Check it (`devtools.prove`,
  `devtools.mutate`).

`tests/unit/test_test_quality.py` is a ratchet over the first three: it counts
a string looked for in source text, an upper bound under a second on measured
time, and a gate test's string-target patch of non-frozen code. Today's
instances are allowed by file and may only fall -- a new one fails with what to
write instead, and a fixed one fails until its count is lowered. It is narrow
on purpose, so that the recommended forms do not fail the coder who writes
them.

`tests/gate/test_no_provider_names.py` is a ratchet of another kind: no
code outside `modules/` names a provider, and each file that still does is
listed with the item of step 5 that removes its names. A count may only
fall, and a fall is written down there.

Wording that is the maintainer's own (the night's prompt, the outbound-call rules) is
pinned deliberately and says so in the test's docstring. Those stay.

**Fakes that behave like the real thing.** On 9 October 2026 every unit test
of the desk passed while the desk failed: the fake GPT-Live sent audio only
when it spoke, and the real one streams a frame every 100 ms. Where parts meet
-- the desk's ear, engine, speakers and live voice -- they are run together
against a simulated room (`tests/unit/desk_world.py`): a script of what the user
says, the acoustics, and fakes of Whisper, the assistant and GPT-Live that do
what the real ones did that day. It runs on the event loop's own virtual clock
(`VirtualLoop`), so a two-minute scenario is milliseconds and no assertion
depends on the machine's load (`tests/unit/test_desk_scenarios.py`). Since
that evening's retest it can also give Whisper a cold first run (`stt_s`),
the end-of-turn judge (`judge`), a speaker stream that stalls (`stalls`), a
"hey Steve" the canceller ducked (`Say.muffled`), and a GPT-Live that stays
silent when told only to answer "let's talk live" and is slow to close.

**Golden outputs, for a move that must change nothing.** Step 5 moves every
connector onto a slot and its code into `modules/`, and each old test fakes a
function the move replaces. So `tests/unit/test_golden_connectors.py` drives
the mail, calendar, Drive and Tasks tools, and one tick of each attention
source, against `tests/unit/golden_world.py`: fakes at each service's own seam
(Google's API client, Outlook's MAPI, a calendar link's feed), and real IMAP and
SMTP servers on loopback for a password account (`tests/fakes/mailrig.py`),
on a pinned clock. What the tools returned, what each would have asked, and what
reached each service is compared with `tests/unit/fixtures/golden/connectors.json`.
It is recorded with `STEVE_GOLDEN_RECORD=1`; as the code moves only the world's
`wire` changes, and a difference made on purpose is recorded again in the
commit that makes it. That commit's message lists every field the diff of
the record changed, each with its reason, for a later review of the
commits; nothing checks the list at the time.

**Accounts in a dict, for the tools on the slot.** A test of the mail,
calendar, Drive or Tasks tools plugs in accounts whose far side is a dict
(`tests/gate/mailfake.py`; `tests/gate/slotfake.py` for calendars, files and
tasks) and lets the real slot, cards and write path run against them. Each
account hands out its own ids and records what was written, and a test can
change an item behind a card, as its owner would elsewhere, to see that the
yes did not reach it.

**Off means original.** `tests/gate/test_off_means_original.py` snapshots
what Steve is given -- the prompt's feature sections, the tools with their
schemas, the attention sources, the worker's duties, the judge's prompt --
with every module off; turns them on with accounts on those fakes, ticks
each source, calls each class's tool and leaves a card; turns them off; and
compares, byte for byte. A second does the same with modules from
outside in every slot -- a built module's tool, prompt words, skill,
attention source with its judge paragraph and routine; an imported mail
module's account; a remote MCP server's tool -- each used, a card left,
then switched off: the card is withdrawn and the surface, the queue and
the cards waiting are as before. `tests/gate/test_module_life.py` follows
one module through its whole life -- built through the pipeline's checks,
admitted with its tests in the sandbox, approved, run, upgraded, rolled
back, exported and imported over itself, switched off and removed -- under
an audit hook, and fails on any write under the install folder outside
the data folder. One test per table in `core/modules.TAGGED` sees
its rows skipped while their account is off, and a schema test fails on a
table that could hold an account's rows and is neither tagged nor core's.
`tests/gate/test_tagged_reads.py` finds every read of a tagged table in
`agent/`, `core/` and `tools/`: it goes through `modules.live`, or is listed
with the reason it may see an account that is off (by id, a count, history).

**Accounts and their secrets** (step 5, item 5).
`tests/gate/test_no_secret_on_disk.py` runs setup end to end through the
app's routes on a test module, stores Google's client and token, and reads
every file in the data folder for any of the secrets, as text and as UTF-16.
`tests/gate/test_only_the_app.py` reads the code: only the app's backend
names an account or secret writer, and only `core/modules.py` writes the
registry's tables in SQL. `tests/gate/test_account_health.py` holds the five
states' sentences, that no other text says them, and when one is said on the
user's channel: once, after six hours down or a day before a known end, never
at night. `tests/unit/test_accounts.py` drives setup, the test, settings,
moving a class and removal over two test manifests and a dict-backed engine.

**The IMAP engine, against real servers on loopback** (step 5, item 6).
`tests/fakes/imap.py` and `tests/fakes/smtp.py` are IMAP and SMTP servers
that speak the protocols over real sockets, with TLS from a test CA
(`trustme`). Each runs in a mode: a standard server, Gmail's labels and its
own Sent copy, one that ignores every search filter (as Google's CalDAV
ignored a UID filter on 11 October), and one without MOVE or CONDSTORE.
`tests/unit/test_imap_engine.py` runs the SDK's mail contract
(`steve_sdk/testing/mail.py`) on each mode, then each preset, STARTTLS
refused when it is not offered, an untrusted certificate, a Bcc kept off the
headers, a Sent copy filed once, and a draft sent only once. No test reaches
a real mail server: the engine's one way out is a socket, refused off this
computer by the fences.

**The CalDAV engine, against Radicale** (step 5, item 7).
`tests/fakes/caldav.py` runs Radicale, a real CalDAV server, in the test's
process; the engine reaches it through `ctx.http` and httpx's WSGI
transport, so every rule of the module's HTTP door still runs. A layer in
front adds what Radicale lacks: Google's habits (a new event stored under
its UID, a calendar-query answered with every event), a server that names
new events itself, and a principal on another host.
`tests/unit/test_caldav_engine.py` runs the SDK's calendar contract
(`steve_sdk/testing/calendar.py`) on each, then each preset's discovery, an
undeclared host that never sees the password, a change that keeps another
app's alarm, and a Gmail account added by app password, end to end through
the slot.

**One contract per class, on a `World`** (step 5, items 6 to 12). A provider
of a class passes that class's suite in the SDK: `steve_sdk/testing/mail.py`,
`steve_sdk/testing/calendar.py`, `steve_sdk/testing/files.py` and
`steve_sdk/testing/tasks.py`. A suite drives the provider through the slot's
methods and checks the far side through a `World`
(`steve_sdk/testing/__init__.py`): it seeds an item, reads what was written,
and changes an item behind the provider's back, as its owner would
elsewhere. Each engine's fake gives one: IMAP's, CalDAV's, a feed's (the
calendar suite's read half, `READS`) and the MCP server's. A suite skips a
check the provider says it cannot do (threads, flags) and expects a card for
an unconditional provider's change. `steve_sdk/testing/api.py` is a strict
web API in memory (a token, versions, `If-Match`, pages) for the templates,
and `tests/unit/test_templates.py` runs each template on its class's suite.
A module from outside cannot run them: they go through core's write path,
which its code may not import. Its own tests, run at admission
(`core/sandbox/testrun.py`), check its code on its own fake, and the suites
hold the template it was copied from.

**The other fakes** (step 5, items 8 to 11).
- `tests/fakes/ics.py` serves feeds on the module's own network, with zones,
  repeats and redirects; `tests/unit/test_ics_engine.py`.
- `tests/fakes/mcp.py` is an MCP server over mail and events shaped as
  Microsoft Graph's, with the tools Softeria's server lists
  (`tests/fakes/mcp_tools/softeria-0.166.0.json`). It can change its tools,
  misdirect, misread, fail a tool, and run a hook after a call.
  `tests/unit/test_mcp_engine.py` runs the mail and calendar suites on it.
- `tests/fakes/google.py` adds a Google account the way the app does
  (`signed_in`), with Google's API faked at its client's `build`.
  `tests/fakes/outlook.py` adds Outlook classic and serves a MAPI namespace
  through core's own context, from the real manifest, so a member not
  listed fails. `tests/fakes/com.py` is a smaller Outlook for the broker's
  `com.call`, recording what it sent and the threads that called it.
- `tests/modules/world/` is the evaluations' account: a module whose world
  the yardstick and the persona eval play on (`agent/autonomy_eval.py`).

**The rehearsal rig: the real desk, in a room that is software**
(`devtools/rehearse/`, 10 October 2026).
- **Why:** the simulator's fakes stand in for the parts that failed by ear: its
  wake detector reads a marker and its echo canceller subtracts a number. The
  10 October retest failed on exactly that: over a live call AEC3 left 0.00 of
  a "hey Steve" the raw microphone scored 0.91.
- **What it runs:** the desk as `agent/worker/desk.switch` builds it -- the
  trained wake detector, Silero, the end-of-turn judge, AEC3, Whisper on the
  GPU, Kokoro and the real `Player`.
- **What it fakes:**
  - the microphone is a room: their lines in Kokoro voices the wake detector
    never trained on, the echo of what was played with delay, reverb and
    small-speaker clipping, noise, and a video heard on the loopback too;
  - the speakers are a sound card's clock that pulls from the player in real
    time and can stall;
  - the assistant and GPT-Live are scripted fakes that stream as the real
    ones do.
- **What it refuses:** real devices, before anything is built (`rig.no_devices`).
  Nothing calls a model unless `--live` asks for the real GPT-Live, which is
  billed and capped per scenario.
- **Scenarios** are scripts in `devtools/rehearse/scenarios/`, written the way
  their sound tests are described ("six seconds into the answer, say hey Steve,
  stop"). The language is in `devtools/rehearse/script.py`.
- **Running it:** `python -m devtools.rehearse all`, `--list`, `--save-audio`.
  It prints a timeline per scenario, with their lines, the desk's transitions,
  wake scores raw and cleaned, transcripts, what was audible and when, and
  its checks. It keeps them in `data/rehearsals/<when>/`.
- **What it can't reproduce:** their room and speakers exactly, the real
  GPT-Live's judgement (without `--live`), and a real assistant's answers. A
  sound test by the user stays the last word.
- **Not part of the suite:** it loads models and takes minutes. Its pure parts
  are tested in `tests/unit/test_rehearse.py`.
- **Unattended:** the night runs it (`agent/night/rehearsal.py`) when the desk's
  code changed since the last rehearsal, or weekly. It runs after the coding
  attempts, never with hands-free on, in a process of its own (fenced from every
  test by `tests/unit/conftest.py`). The morning briefing gets one line, and
  each failed scenario becomes a ledger issue (`rehearsal_failed`).

## Built once, copied

`git init` is 66 ms and each further git command about 40 ms.
`tests/unit/conftest.py` builds a repository once per worker and copies it:
`memories_repo` for an empty one with `tools.memory` pointed at it (and a git
identity of its own, which the coding sandbox's account lacks). Twelve files
had the same four lines of memories repo each; one of them spent 9.3 s of
setup on 45 identical repositories. The copy is a copy: a write in one test
never reaches the template (`tests/unit/test_template_fixtures.py`).

Every test has a data folder of its own, so nearly every one that opens the
store opens a new database. Migrating one from nothing took about 92 ms, a
copy 24 ms (21 October 2026). So the fences of both trees make each new,
empty database a copy of one migrated once per process
(`tests/gate/storecopy.py`); on a sample of 793 unit tests that took the run
from 82 s to 60 s. A database already holding tables, one migrated with other
migrations than the files hold, and every test marked `real_migrations` (the
tests of migrating, `tests/unit/test_store.py`) migrate for real
(`tests/unit/test_storecopy.py`). `tests/gate/storecopy.py` also holds the
fixtures `store` and `conn`, each a new database for a test of what is stored; both trees'
conftests take them from it.

## The installer

The suite reads the installer; it never runs one. `tests/unit/test_installer.py`
checks what the hooks in `ui/src-tauri/windows/hooks.nsh` say, and
`python -m devtools.package smoke` runs the built tree moved, with an empty
environment. A hook is checked by compiling it: makensis on a copy of the
last build's generated script, with its compression set to none, takes about
a minute, against nine for the whole build.

Running an installer needs a clean Windows, since an install on the
developer's own would take the Run value and the data folder for real.
`devtools/sandbox_check.py` builds two installers one patch apart (`build`),
then runs them in Windows Sandbox (`run`): the install, the first start, a
changed file refused, the update, the old installer refused, and both
uninstalls, each with what it left. The update, a reinstall, the old
installer (silently, by its window and with `/P`) and an uninstall each run
again with Steve running, since that is how a user meets them. Its report
is written by `devtools/sandbox_check.report`.
The maintainer's own run, with a real key, is in a sandbox that `session`
opens.

## Time

Quiet hours (22:00-08:00) and day boundaries are pinned in tests, or a test
passes by day and fails at night. A process that must start gets a generous
limit (`tests/gate/test_zero_downtime.py` gives its supervisor 120 s, free
when healthy) and puts its log in the failure message. A wait for something
another process or thread does is a wait for that thing (a poll with a
generous deadline, an `Event`), never a sleep that guesses how long it takes.

## Processes a test starts

Self-upgrade v1 ran the suite inside its coding sandbox, whose whole process
tree could commit 6 GiB, with no bytecode cache. That is where "new worker
never became healthy" came from on 8 October 2026 (upgrade #89, approved,
then rejected at the gate): not a slow worker but a dead one. Its log said "OpenBLAS error: Memory allocation still failed", and
`pytest tests/gate -n 3` was asking for 6.03 GiB.

- **Cap OpenBLAS.** numpy reserves a buffer for each of the machine's threads
  as it loads: 500 MiB of committed memory for `import numpy` here, in each
  pytest worker and each worker a supervisor starts. `tests/gate/fences.py`
  sets `OPENBLAS_NUM_THREADS=1` as the first thing either conftest imports,
  and every process a test starts inherits it. The same run then peaks at
  1.87 GiB (`tests/unit/test_test_memory.py` asks a child process).
- **A fixture that starts a process stops the whole tree.** `terminate()` on
  Windows kills the one process and leaves its children running; the
  supervisor's worker outlived every module that used one, until some later
  supervisor reaped it (`_stop_everything` in
  `tests/gate/test_zero_downtime.py`).
- **A swap's wait is the supervisor's to be told.** `STEVE_WORKER_START_TIMEOUT_S`
  (`core/worker_handle.start_timeout`) lengthens how long a swap waits for a
  new worker; unset, it is the thirty seconds production has always used. The
  gate's supervisor sets it to its own 120 s.
- **A failure prints every log.** The supervisor's output and the end of each
  worker's log go into the message (`_diagnosis`), because the supervisor can
  only say that a worker did not answer.

To see what a change that starts more processes costs, run the suite inside a
Job Object limited to 6 GiB (`core/sandbox/job.py`) and read its peak commit.

## The end-to-end tier

`tests/e2e/` runs Steve's backend as shipped: a supervisor started as a
developer's build of the app starts it (`python -E -s -B -m core.supervisor`
in the checkout, `tests/e2e/backend.py`), which starts its worker, on a data
folder of the test file's own. It is driven as the app drives it, through the
run file's port and token and `/ws`, and through the channels' fakes as a
phone drives it.

- **One backend per file.** A start is about ten seconds, so a file's module
  fixture starts one (`running`) in one world, and each test in it uses its own
  conversation, card and account. `tests/e2e/conftest.py` shuts the doors once
  for the file, with a `MonkeyPatch` of the module's, so the backend and
  everything it starts run under the same fences as a test's own children;
  and again for each test, as the other folders do. The files run in one
  `xdist_group`, so two backends never compete.
- **The fake world** (`tests/fakes/world.py`) starts the fakes a file asks for
  on this computer and gives the environment that points the backend at them
  (`STEVE_OPENAI_URL`, `STEVE_OPENROUTER_URL`, `STEVE_TELEGRAM_URL`, Signal's
  program and folder), each honoured in a checkout only (`core/endpoints.py`).
- **Every secret is a canary.** Each key, the bot's token and the
  authenticator's secret get a value made for the run, held in the world's
  `keychain.json`, which the child fence reads in place of its dummies while
  `STEVE_TEST_WORLD` names the folder; what the backend keeps (a key given in
  setup) is kept there too. After each test, every file in the data folder,
  the backend's log and every request each fake received are scanned, and a
  secret anywhere but its one place (`world.ALLOWED`: a key in its own
  service's `Authorization` header, the local API's token in the run file)
  fails the test, naming the secret and the place. A secret given another
  way (a passphrase typed in setup, a sign-in's session) is watched too
  (`World.keep`): a mailbox's password and a feed's private link among them.
  The mail server's messages, sent and filed, are scanned as well. A file
  is searched for each secret as UTF-8, Latin-1 and UTF-16, and one that
  cannot be read fails the test too (`world.holds`, `world.on_disk`).
- **The real app's leftovers are scanned too.** Each test of the real app
  ends, once the app has gone, with a scan of its data folder (the shell's
  own log among it) and WebView2's folder for the passphrase typed into it
  and the key its backend was given (`rig.left_behind`). The Google
  account's client secret and refresh token, browser logins and an MCP
  server's secret enter no end-to-end scenario; their own tests scan the
  data folder and the logs for them (`tests/gate/test_no_secret_on_disk.py`,
  `tests/gate/test_browser_logins.py`, `tests/unit/test_mcp_servers.py`).
- **The world's settings.** Some of the product's pace is a module constant
  with no knob: the 30 s hold before a yes runs, the reminders' 20 s tick, the
  routines' and the outbox's 30 s, the briefing's 5 minutes, the 4 s a send
  waits for the server's copy in Sent. The world's `world.json` names
  statements to run in a module of the backend's as it is imported
  (`after_import`), which the child fence applies (`apply_the_world`); every
  world has `BRISK` (a 2 s hold, 1 s ticks), and a file can add its own, run
  after the world's.
- **The world's network.** A world asked for `mail`, `calendar` or a `feed`
  serves an IMAP and an SMTP server, Radicale and the feed on loopback, each
  over TLS with a certificate from an authority made for the run, under the
  names a user types in setup (`imap.mail.example`, `caldav.mail.example`).
  The backend reaches a provider only through `core.modctx.Network`, so
  `tests/fakes/worldnet.py` changes that class as `core.modctx` is imported:
  a world name resolves to a public address of its own, a connection to it
  goes to its server's port, and TLS trusts the world's authority. Setup's
  pin of a typed host follows it too (`egress.pin_kind`'s resolver). The
  account is added as setup adds it (`World.mailbox`, `World.feed_account`).
  The mail is served under setup's Fastmail names too, the one preset whose
  mail a plus-address reaches: `World.mailbox(plus=True)` adds it so, for the
  agent's own mailbox, and `World.calendar_account` then adds the calendar
  apart, since that preset fixes its own.
- **The sandbox.** A world asked for `sandbox` lets the worker's start check
  that modules can run here, as shipped (`core/sandbox/probe.py`), in a real
  AppContainer. The probe finds this computer's address by a socket's
  connect, which the fence refuses, so the world gives it the address from
  the host name (`worldnet.own_address`). Its backend makes real profiles,
  so its file is marked `appcontainer`, which lets the test delete them.
- **The model's script** (`tests/fakes/openai.py`'s `Rule`) answers by what
  the request ends with: the user's words, or a tool's result (`after` names
  the tool). A call's arguments can be made from that result, as a model
  passes back an id it was handed; `FakeOpenAI.results` keeps what each tool
  told the model, for a scenario to check. A module's coding roles are told
  apart by the tools they are offered (`edit_file` is the coder's alone)
  or the words they are sent.
- **The scenarios**, one file each: `test_setup.py` (every page, then a
  restart), `test_chat.py` (each tier, and the escalation between them),
  `test_approvals.py` (a yes, a no during the hold that undoes it, a no),
  `test_high_risk.py` (the passphrase, bound to the card as read),
  `test_spend.py` (the month's cap, and an OpenAI account with no credit),
  `test_raise.py` (RAISE at the month's cap, from Telegram, Signal and the
  app's chat, with no model, and the passphrase's yes raising it),
  `test_signal.py` (cards, a wrong word, the authenticator's code and a
  reminder, by Note to Self), `test_telegram.py` (a card's buttons, a
  stranger ignored), `test_briefing.py` (the morning's, once, on the phone
  and the day), `test_mail.py` (read and summarised; a reply in Drafts, sent
  on a yes; a yes to part of a card), `test_calendar.py` (the calendar and a
  feed read as one; an event added, one with a guest refused before a card,
  one deleted on a yes, each checked by its UID), `test_extensions.py` (a
  tool asked for in the chat, built by the scripted coder, tested and
  called in the real sandbox, switched off, and removed with its container),
  `test_red_team.py` (outside text asking for a send, a lasting routine and a
  local page, through a mail, a web page, a calendar invitation and a forward
  to the agent's own mailbox, with every model fooled: cards naming the real
  address, the local page and an unwatched run's unshown pages refused,
  nothing sent, no secret out).
- **Every guarantee from every door:** `tests/e2e/doors.py` holds a table,
  the product's guarantees down the side and its doors across (the main
  window, the quick bar, the desk card, the tray's menu, a toast, the two
  phones, push-to-talk, call notes, scheduled work, the model's tools,
  modules and MCP servers). Each pair names the tests that check it through
  that door, or a reason it cannot be reached that cites the code, or says
  it is open: reachable, with the product's answer still to decide. Every
  route of the supervisor and the worker is placed there too, by the
  guarantees it touches. `tests/gate/test_doors.py` fails on a pair left
  empty, a test that no longer exists, a reason citing no file, and a route
  not placed, so a new door or route arrives examined.
- **The hostile modes:** each fake can misbehave mid-run, as its service
  does. The fake OpenAI's `Rule.fail` cuts a reply off, answers a 500 or a
  429, or a page that is not its answer. Signal's daemon refuses sends
  (`signal_cli.fails`) or dies (`signal_cli.dies`). Telegram answers a
  method with `CONFLICT` or `too_many` (`FakeBotApi.trouble`). The mail
  servers refuse a send (SMTP's `refuse`), drop a read partway
  (`drop_reads`) or no longer take the password. Jev is `down` or slow
  (`slow_s`). `test_hostile_model.py`, `test_hostile_signal.py`,
  `test_hostile_telegram.py` and `test_hostile_mail.py` set one at a time,
  and check the same three things: the user is told plainly, nothing is
  sent twice or lost, and the next ask is answered. A case that sends to a
  phone first waits out the morning briefing (`backend.briefed`), which
  otherwise met its trouble on some mornings only.
- **Waits are on events:** a reply on the socket, a send in a fake's record, a
  row in the database (`backend.until`). Only a check that nothing more
  happens (no retry after a refusal) waits a few seconds out.

## The live smoke

Nothing above reaches a provider, so a model renamed, an endpoint moved or a
reply's shape changed would first meet a user. `python -m devtools.live_smoke`
(`devtools/live_smoke.py`) runs each of Steve's paths to a provider once,
through Steve's own code and with the keys the app keeps: the key's check, a
turn, an escalation, an embedding, a web search, and Jev's one decision when
an OpenRouter key is kept. It is the maintainer's to run, by hand, never in
CI: it spends real money. It asks first (`--yes` doesn't), bills to the data
folder's ledger as background work, and stops at $0.25 on its own count. A
failed step says the provider's status and error code and never its message,
which can carry part of the key. `tests/unit/test_live_smoke.py` runs it on
fake steps.

## CI

`.github/workflows/ci.yml` runs on Windows, the platform Steve ships for,
with read access to the repository and no secrets.

- **Each push and pull request:** lint, the secrets scan, the app built as a
  developer builds it (so its tests run rather than skip), the shell's Rust
  tests by the check's own command, and the suite at
  `-n 3 --dist loadgroup`, as `devtools.check` runs it. The end-to-end tier
  and the app's walks are left out. The `ui` job type-checks and lints the
  app.
- **Each night, or by hand:** the end-to-end tier and the walks, on fakes
  only. A private repository's schedule runs nothing, since its runner
  minutes are counted; there these run by hand.
- **By hand:** the installer's run of `devtools/sandbox_check.py` on the
  runner itself, a clean Windows thrown away after the job. The job makes an
  update key for its run (`build --pubkey`), so the release key never leaves
  the maintainer. The rig shuts its machine down at the end only in Windows
  Sandbox (`STEVE_RIG_STAYS_UP` keeps the runner up for the report).

Nothing that spends on a model runs in CI. The live smoke and the
yardstick's real run are the maintainer's. The workflow has not yet run, as
the repository has no remote. `tests/unit/test_ci_workflow.py` reads it as
GitHub would, so a job or flag can't drift from the suite before then.

## What tests each feature

Each feature the README names, and the tests that reach it, one or two
files a kind: the unit and gate tests of its parts, an end-to-end scenario
(`tests/e2e/`), the app (a real-app walk or a screen test in
`tests/unit/ui/`), and the shell's Rust tests. Six of them are also the
live smoke's: the key, a turn, an escalation, an embedding, a web search
and Jev's decision. Mapped in step 11's audit (20 October 2026); a new
feature takes a row.

| Feature | Unit and gate | End to end | The app | Rust |
|---|---|---|---|---|
| **Installing and the first start** | | | | |
| The installer: for the user alone, the data kept apart, an older one refused | `tests/unit/test_installer.py` | — | — | `ui/src-tauri/src/lib.rs` |
| Each start checks the backend's files against the signed list | `tests/unit/test_package.py` | — | — | `ui/src-tauri/src/integrity.rs` |
| Setup: one question a page, resumed where it was left | `tests/unit/test_setup.py`, `tests/gate/test_setup_bounds.py` | `tests/e2e/test_setup.py` | `tests/unit/ui/test_setup_screen.py`, `tests/unit/ui/test_exit_walk.py` | — |
| The OpenAI key, checked at once; Jev's, which can be skipped | `tests/unit/test_keys.py` | `tests/e2e/test_setup.py` | `tests/unit/ui/test_setup_screen.py` | — |
| Name, pronouns, home and work; the Profile page | `tests/gate/test_who_the_user_is.py` | `tests/e2e/test_setup.py` | `tests/unit/ui/test_back_pages_screen.py` | — |
| The passphrase | `tests/gate/test_who_may_approve.py` | `tests/e2e/test_setup.py` | `tests/unit/ui/test_setup_screen.py` | — |
| The browser and running code, offered with Docker running | `tests/unit/test_setup.py` | — | `tests/unit/ui/test_setup_screen.py` | — |
| Quiet, Balanced, More | `tests/gate/test_setup_bounds.py` | `tests/e2e/test_setup.py` | `tests/unit/ui/test_setup_screen.py` | — |
| Starting with Windows | — | — | `tests/unit/ui/test_setup_screen.py` (the command sent) | — |
| What stays and what is sent; the drive warning | `tests/unit/test_privacy_statement.py` | — | `tests/unit/ui/test_setup_screen.py` | — |
| The first day's tour | — | — | `tests/unit/ui/test_tour_screen.py` | — |
| **Keys, starting and stopping** | | | | |
| Keys in the Credential Manager; `python -m core.secrets` | `tests/gate/test_credstore.py`, `tests/unit/test_store.py` | every scenario's canary scan | `tests/unit/ui/test_app_walks.py` (what is left on disk) | — |
| Web search | `tests/gate/test_websearch.py` | `tests/e2e/test_red_team.py` | — | — |
| The backend on a loopback port, behind its token | `tests/gate/test_local_token.py`, `tests/gate/test_zero_downtime.py` | `tests/e2e/test_world.py` | `tests/unit/ui/test_screens_rig.py` | `ui/src-tauri/src/backend.rs` |
| The app starts and stops the backend; "keeps stopping"; Diagnostics | `tests/gate/test_crash_loops.py` | — | `tests/unit/ui/test_back_pages_screen.py` | `ui/src-tauri/src/backend.rs` |
| **The desktop app** | | | | |
| Signing in, and out on a lock or when away | `tests/unit/test_signin.py` | `tests/e2e/test_high_risk.py` | `tests/unit/ui/test_signin_screen.py` | `ui/src-tauri/src/signin.rs` |
| Windows Hello | `tests/unit/test_signin.py` | — | `tests/unit/ui/test_signin_screen.py` | `ui/src-tauri/src/hello.rs` |
| The day's page, earlier days, search | `tests/unit/test_journal.py` | `tests/e2e/test_chat.py` | `tests/unit/ui/test_day_screen.py` | — |
| A message answered; an escalation | `tests/unit/test_loop.py` | `tests/e2e/test_chat.py` | `tests/unit/ui/test_exit_walk.py` | — |
| Photos in a message | `tests/unit/test_app_photos.py` | — | `tests/unit/ui/test_day_screen.py` | — |
| The notes beside the day | `tests/unit/test_needs.py` | `tests/e2e/test_signal.py` | `tests/unit/ui/test_notes_screen.py` | — |
| Cards: Yes, No, and undo during the hold | `tests/gate/test_approval_binding.py` | `tests/e2e/test_approvals.py` | `tests/unit/ui/test_notes_screen.py` | — |
| A yes to part of a card | `tests/unit/test_grouped_cards.py` | `tests/e2e/test_mail.py` | `tests/unit/ui/test_notes_screen.py` | — |
| A high-risk yes takes the passphrase | `tests/gate/test_high_risk_approval.py` | `tests/e2e/test_high_risk.py` | `tests/unit/ui/test_app_walks.py` | — |
| The quick bar | — | — | `tests/unit/ui/test_quickbar_screen.py` | — |
| The desk card | — | — | `tests/unit/ui/test_deskcard_screen.py` | `ui/src-tauri/src/deskcard.rs` |
| The tray and notifications | — | — | `tests/unit/ui/test_signin_screen.py` | `ui/src-tauri/src/trayicon.rs`, `ui/src-tauri/src/toast.rs` |
| The sidebar | `tests/unit/test_places.py` | — | `tests/unit/ui/test_shell_screen.py` | — |
| What Steve knows: corrected, forgotten | `tests/unit/test_knows.py` | — | `tests/unit/ui/test_back_pages_screen.py` | — |
| What Steve may do | `tests/gate/test_knobs.py` | `tests/e2e/test_setup.py` | `tests/unit/ui/test_exit_walk.py` | — |
| Activity | `tests/unit/test_control.py` | `tests/e2e/test_approvals.py` | `tests/unit/ui/test_back_pages_screen.py` | — |
| Settings: theme, reading, contrast, window size | `tests/unit/test_control.py` | — | `tests/unit/ui/test_back_pages_screen.py` | `ui/src-tauri/src/windowsize.rs` |
| Quiet hours and the morning's time | `tests/gate/test_knobs.py` | `tests/e2e/test_briefing.py` | `tests/unit/ui/test_back_pages_screen.py` | — |
| **The phone** | | | | |
| Signal: its download, the link, unlinking | `tests/unit/test_signal_pairing.py`, `tests/gate/test_addons.py` | — | — | — |
| Signal: cards, a yes with its word, Note to Self alone | `tests/unit/test_channels.py` | `tests/e2e/test_signal.py` | — | — |
| Telegram: the bot's token, the pairing code | `tests/unit/test_telegram.py` | — | — | — |
| Telegram: Yes and No buttons, strangers ignored | `tests/unit/test_telegram.py` | `tests/e2e/test_telegram.py` | — | — |
| The authenticator | `tests/unit/test_approvers.py` | `tests/e2e/test_signal.py` | — | — |
| RAISE, from a phone | `tests/unit/test_spend_caps.py` | `tests/e2e/test_raise.py` | — | — |
| Voice notes from the phone | `tests/gate/test_hearing.py` | — | — | — |
| **What Steve may use** | | | | |
| The browser, in a chat and in background work | `tests/gate/test_browser_contained.py`, `tests/gate/test_browsing_workers.py` | — | — | — |
| The browser's logins | `tests/gate/test_browser_logins.py` | — | `tests/unit/ui/test_back_pages_screen.py` | — |
| Running code | `tests/unit/test_code_exec.py` | — | — | — |
| Waking the computer; keeping it awake | `tests/unit/test_wake.py`, `tests/unit/test_awake.py` | — | — | — |
| Outlook (classic) | `tests/unit/test_outlook_send.py`, `tests/gate/test_module_com.py` | — | — | — |
| Google Cloud: mail, calendars, Drive, tasks | `tests/unit/test_google_cloud_setup.py`, `tests/unit/test_google_contract.py` | — | — | — |
| A mailbox with a password | `tests/unit/test_imap_engine.py` | `tests/e2e/test_mail.py` | — | — |
| A calendar by CalDAV or a private link | `tests/unit/test_caldav_engine.py`, `tests/unit/test_ics_engine.py` | `tests/e2e/test_calendar.py` | — | — |
| Outlook.com, through an MCP server | `tests/unit/test_server_accounts.py` | — | — | — |
| The Accounts page | `tests/unit/test_accounts.py` | `tests/e2e/test_mail.py` | — | — |
| Steve's own mailbox | `tests/gate/test_mailbox.py` | `tests/e2e/test_red_team.py` | — | — |
| Extensions: built, tested, turned off, removed | `tests/unit/test_modbuild.py`, `tests/gate/test_module_sandbox.py` | `tests/e2e/test_extensions.py` | `tests/unit/ui/test_extensions_screen.py` | — |
| MCP servers the user adds | `tests/unit/test_mcp_servers.py` | — | — | — |
| The local readers | `tests/gate/test_local_detector.py`, `tests/gate/test_local_decider.py` | — | `tests/unit/ui/test_back_pages_screen.py` | — |
| The injection screen | `tests/gate/test_injection_screen.py`, `tests/gate/test_screen_enforces.py` | `tests/e2e/test_red_team.py` | — | — |
| **Voice** | | | | |
| Push-to-talk | `tests/unit/test_push_to_talk.py` | — | `tests/unit/ui/test_app_settings_screen.py` | — |
| Spoken replies | `tests/gate/test_voicing.py` | — | `tests/unit/ui/test_quickbar_screen.py` | — |
| The local voice add-on | `tests/unit/test_voice_add_on.py` | — | — | — |
| Call notes | `tests/unit/test_call_notes.py` | — | `tests/unit/ui/test_shell_screen.py` | — |
| **Work Steve does** | | | | |
| Reminders | `tests/unit/test_reminders.py` | `tests/e2e/test_signal.py` | `tests/unit/ui/test_app_walks.py` | — |
| The morning briefing and meeting briefs | `tests/unit/test_attention_briefing.py` | `tests/e2e/test_briefing.py` | `tests/unit/ui/test_app_walks.py` | — |
| Routines | `tests/gate/test_routines.py` | `tests/e2e/test_approvals.py` | `tests/unit/ui/test_back_pages_screen.py` | — |
| Goals | `tests/gate/test_goals.py` | — | — | — |
| Night work | `tests/unit/test_night.py` | — | — | — |
| Spending caps and the Spend page | `tests/gate/test_spend.py` | `tests/e2e/test_spend.py` | `tests/unit/ui/test_app_walks.py` | — |
| **Data, updates, uninstalling** | | | | |
| The data folder | `tests/gate/test_paths.py` | `tests/e2e/test_world.py` | `tests/unit/ui/test_screens_rig.py` | `ui/src-tauri/src/lib.rs` |
| Updates: signed, and never older | `tests/unit/test_installer.py` | — | — | `ui/src-tauri/src/update.rs` |
| Uninstalling, keeping or deleting the data | `tests/unit/test_uninstall.py` | — | — | `ui/src-tauri/src/lib.rs` |

What the map found:

- **Reached by nothing:** starting with Windows for real, since no test
  writes the Run key (the fences refuse it), and an app started while a
  backend started by hand runs, which uses it (`ui/src-tauri/src/backend.rs`).
  Both are checked by hand. The installer's mode for the user alone and the
  `core.secrets` command line were reached by nothing too, and now are.
- **Tested in parts only:** the phone's setup (Signal's link, Telegram's
  pairing, the authenticator), voice notes, the browser, running code,
  waking the computer, Outlook, Google Cloud, Outlook.com, MCP servers,
  the local voice add-on, goals and night work. Most need a live service or
  Docker, which the fences shut; the end-to-end world starts its phones
  already paired.
- **The installer, updates and uninstalling** run whole only in Windows
  Sandbox, by hand (`devtools/sandbox_check.py`, under The installer).
