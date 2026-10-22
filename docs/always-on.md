# Keeping it running

The desktop app owns the backend. It starts the backend when it starts, starts
it again if it dies, and ends it when the app ends. So Steve runs while the
tray icon does. Closing the window only hides it; Quit, from the tray, stops
both. Windows starts the app at login (its own start-at-login, where the user
said yes to it in setup), and the app starts the backend.

Diagnostics shows the backend's state, with Start, Restart and Stop, and so does the day page while it is not running. A stop lasts
until Start is pressed or the app starts again.

To pick up a *worker* code change without turning anything off, swap the
worker:

```bash
python -m core.localapi post /_sup/swap
```

A change to `core/supervisor.py` needs Restart: a swap only replaces the
worker, and the supervisor keeps running the code it started with.

## Without the app

A developer can run the backend in a terminal:

```bash
python -m core.supervisor
```

If the app starts while that one answers, it uses it and starts none of its
own. Stop it with Ctrl+C. Once it has stopped answering for two minutes, a
running app starts its own.

## Asleep and awake

Nothing of Steve's runs while the computer sleeps. When it wakes, a missed
heartbeat says so, and every loop takes its next round at once: a reminder
due in the night goes off, a briefing up to four hours late is sent, and
the outbox retries. A reply to a message that waited on the sleep begins
"Sorry for the wait". Diagnostics shows when Steve was asleep. Flow 2 in
`docs/flows.md` has the detail.

Two switches, both off until the user turns them on, on **What Steve may do**, under
**What Steve may use on this computer**:

- **Keep the computer awake while it is plugged in** (`KEEP_AWAKE`): a
  power request while on mains power, let go on battery. Closing the lid,
  or choosing Sleep, still sleeps. No administrator is needed.
- **Wake the computer for reminders** (`WAKE_TIMER`): a scheduled task,
  `SteveWake.<install>`, wakes the machine before a reminder is due, and the backend
  holds it up for five minutes while the due work goes.

Signal keeps a sleeping computer's messages until it wakes; Telegram keeps
them for only a day.

## Layers

| Layer | Runs as | Started by | Watches | If it dies |
|---|---|---|---|---|
| App | the desktop app, in the tray | the user, or Windows at login | the supervisor, by its proof | the job object ends everything below |
| Supervisor | `python.exe -I -B -m core.supervisor`, windowless | the app, suspended until it is in the app's job | the worker | the app starts it again, up to five times in ten minutes |
| Worker | `python.exe -m agent.server` | the supervisor | — | the supervisor restarts it from the same code, backing off if it keeps failing |

The app's side is `ui/src-tauri/src/backend.rs`:

- **One kill-on-close job object.** The supervisor starts suspended, goes into
  the job, and only then runs, so everything it starts is in the job from its
  first instruction. When the app exits for any reason, a crash included,
  Windows ends the whole tree.
- **Quit drains first.** It sends `POST /_sup/stop` with the token, waits up to
  ten seconds for the workers to drain, then ends whatever is left in the job.
- **Alive is not enough.** After a two-minute grace at each start, the app
  asks the supervisor for its proof every 30 seconds. Four misses in a row and
  it ends the supervisor, which then counts as a death.
- **Five deaths in ten minutes, and it stops trying.** The app then says "Steve's
  backend keeps stopping", with the last lines of its log, until Start is
  pressed.

Each layer judges only the layer directly below it. The app asks the
supervisor to prove itself (`/_sup/prove`, answered by the supervisor
itself), never whether the worker is healthy (`/health`, proxied to the
worker). A supervisor with no healthy worker is busy restarting one, and an
app that ended it for that would reset the failure count its backoff depends
on.

## Where to look when it is down

- Diagnostics, in the app's menu: the state, the restarts in the last ten
  minutes, and the end of its log; when it keeps stopping, the lines it
  ended with, and a note on the day says so too.
- `data/logs/supervisor.log`: the supervisor's own output, emptied in place
  past 5 MB. Every dead worker's last 40 lines are copied here before its log
  can be pruned.
- `data/logs/worker-*.log`: one per worker, the newest 20 kept.
- `python -m core.localapi get /_sup/status`: the active worker, the ones
  draining, and the workers served since the supervisor started.

## Things that were learned the hard way

- **A start mechanism is untested until it has been through a real logon.**
  On 23 September a Run entry that had passed every test, and a start by hand,
  was skipped at the first real logon after it was installed, while twenty
  other Run entries ran. Why is still unknown. The app's start-at-login is a
  Run entry too, so its first real logon is part of its test.
- **Nothing started from a terminal or chat session survives it**, even
  "detached" with `Start-Process`: the child lands in the caller's job object,
  which the session kills on exit. The app now uses the same mechanism on
  purpose.
- **"The launch command returned 0" is not evidence anything is running.**
  The app shows "starting" until the supervisor proves itself.
- **A process with no console makes every console program it starts open a
  window.** That was the flicker of vanishing PowerShells. The supervisor
  starts with `CREATE_NO_WINDOW`, and everything below inherits its windowless
  console.
- **A supervisor alive and not answering is as down as one that exited.** A
  watcher that only waits for an exit never sees it, so the app asks for the
  proof as well.
- **The gate tests run from a copy of the working tree.** They used to run
  the supervisor under test in the live checkout, so one of them wrote a
  deliberately broken `agent/server.py` over the live one for as long as it
  ran. See `sandbox_repo` in `tests/gate/test_zero_downtime.py`.
