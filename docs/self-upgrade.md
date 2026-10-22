# Self-upgrade: how Steve builds what it cannot do yet

Steve builds a **module** ([the module guide](modules.md)) when it needs a
tool it does not have. Its coding pipeline writes in a draft folder,
tests the module in the sandbox (`core/sandbox/`), and asks the user to
approve its card before it can run. It does not rewrite the installed
app's code: a new tool gets only the access its card grants it.

## Asking for a build

A build starts from one of three places, its **origin**:

- **`user`:** the user, in a conversation they are in, asks for something
  Steve has no tool for, and the model calls `build_module`
  (`agent/toolset/control.py`). The tool is in the turn only while the
  sandbox can run here (`agent/features.py`).
- **`agent`:** the same tool, called from a turn nobody is in: a routine or
  a job.
- **`night`:** the night's improvement block (`agent/night/tinkering.py`)
  proposes up to two builds, fixes from the failure ledger and features
  from what the user asks for (`agent/night/evidence.py`).

A module Steve built can be improved: `build_module` with its id as
`module` builds its next version. The Extensions page's Improve, on a
module Steve built, asks for one in the conversation with that id in the
message. Any other id is refused, with the ids of the modules Steve built.

The switch is asked first: `self_upgrade.agent_initiated` in
`core/policy.yaml`, off by default. While it is off, only `user` builds
start (`SelfUpgradeConfig.refusal`); the night says in the morning why it
built nothing.

`modbuild.request` writes the build down with the request in the words it
was asked, and its estimate: the median of the last twenty ended builds,
from the first, and before there is one the most one build may spend
(`coder.attempt_budget_usd`). The tool tells the user the estimate.

## The cap

A build is billed under the `extensions` spend category (`core/spend.py`),
or the night's, inside its unit, if the night started it.

- If its estimate is more than that cap has left, it does not start:
  it waits as `needs_allow` until the user presses "Allow this build" in the
  app. That route is the app's alone (`agent/worker/accounts.py`).
- Allowing lets that build pass the category's cap by one attempt's budget
  (`Unit.allowance`), and never the month's cap.
- A build the cap stops mid-way is `stopped`, and keeps its draft. Allowing
  it goes on from what it had.

One build runs at a time. A build left running when Steve stopped is
`stopped` at the next start (`modbuild.recover`).

## The build

`modbuild.build` runs in a thread of its own, with the computer kept awake:

1. **The draft** is `<data>/module_drafts/<build>/`: empty for a new
   module, a copy of the current version for a new version of a module
   Steve built (`paths.module_draft_dir`). Only modules Steve built get new
   versions; a built-in or imported one is someone else's code. A build is
   a new version only of the module `build_module` named, keeping its id;
   any other build whose id is taken is refused, and the pipeline's checks
   tell the coder so while it can still rename it (`lifecycle.screened`).
2. **The coding pipeline** (`core/coder/`) writes the module and checks it:
   - the coder writes it, reading `_reference/`: the SDK with its
     templates (`steve_sdk/`), the module guide (`docs/modules.md`) and the
     built-in modules (`modules/`), none of them writable
     (`core/coder/workspace.py`);
   - an independent test builder, blind to the coder's tests, writes tests
     from the request, unless the module is words alone and runs no code
     (`lifecycle.runs_code`);
   - lint and the tests run through the sandbox's runner for drafts
     (`testrun.for_drafts`): pytest in the module's AppContainer, with only
     a test target and a few flags allowed through, and ruff in the
     backend, configured by nothing in the draft, its safe fixes (imports
     put in order, an unused one dropped) applied before each of the
     coder's checks;
   - with them, what admission would refuse from reading the draft: its
     manifest, its imports and the folders it names
     (`lifecycle.screened`), and its words, held to the rules a skill is
     and screened as admission screens them (`modadmit.words_refused`),
     so the coder can still fix them;
   - the judge reads the change against the request and the user's own
     words when they asked (`modbuild.task`), and asks whether the module
     does what they wanted, used as they will use it: one too narrow for
     that goes back to the coder with what it misses. It rejects a module
     that asks for more than the request needs, reaches for a secret,
     hands outside text to a model unscreened, changes things other than
     through `ctx.act`, or weakens a test (`core/coder/prompts/judge.md`).
3. **Admission** (`modadmit.admit`, origin `built`) makes the card. The card
   shows the request in the words it was asked and where it came from, and
   marks each host, class and read the module asks for that the request's
   words do not name (`lifecycle.marks`). The request is the model's
   argument to `build_module`, which what it read can steer, so beside it
   the card shows the user's own last message of the turn that asked.
4. **The user is told** in the conversation the request came from when the
   card is ready, or why the build ended (`modbuild.notifier`). The night's
   builds are told in the morning report.

## When it cannot be a module

The coder may answer that the request cannot be a module
(`CoderReport.cannot`):

- **`slot`:** it needs a slot Steve does not have;
- **`core`:** it needs a fix in Steve itself.

Either ends the build as `cannot`, with a note on the Extensions page
(`modbuild.reports`), redacted so the user may forward it. Steve sends
neither anywhere.

## What it keeps from v1

- **The coding pipeline,** without git. The base is a snapshot of the
  folder as the attempt began (`core/coder/snapshot.py`); every failure
  after the change is the change's; the last word is one more run of the
  checks, which must pass and leave the folder as it found it. The roles
  have no shell (`tests/gate/test_coder_barriers.py`).
- **The failure ledger** (`agent/issues.py`): what went wrong, counted, for
  the night to choose from. A build for an issue is counted against it
  (`issues.attempted`), so a fix that keeps failing is not paid for night
  after night. Nothing is fixed until the user turns the module on.
- **The frozen list** (`frozen:` in `core/policy.yaml`) and its lock, as
  developer tooling until step 8's signed manifest.

## Where it is written down

- `module_builds`: each build, its origin, estimate, cost, time, how it
  ended and why, and its admission.
- `module_reports`: the notes for what could not be a module.
- `db/migrations/0011_module_builds.sql` has both.
