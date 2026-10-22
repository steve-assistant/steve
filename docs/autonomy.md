# Autonomy: what the agent does unasked, and how to tune it

The maintainer asked for this on 24 September 2026: briefings morning and evening,
timely pings, an always-on loop that flags anything important, drafts of
replies (never sent), weather and news, and pings at night only for
something critical. This is the runbook. How each message travels is flow 9
in `docs/flows.md`; the code is `agent/attention/`.

## What happens

Every minute, while the laptop is awake, `attention.tick`:

1. runs the **sources** that are due -- each mail account (at its own
   interval: 15 min unless its module or the user says otherwise),
   calendar changes (10), the week ahead (60), meetings that just ended
   (15), commitments (60), Drive (60), weather (30), news (90), threads left
   hanging in either direction (6 h), what is coming due on the calendars
   marked as due dates (3 h, `sources/deadlines.py`), its own health
   (1 h, `sources/health.py`: OpenRouter credit, whether modules can run
   here, Docker, disk, failing jobs), and each enabled module's source (at
   its manifest's interval, `sources/modules.py`, with its paragraph for
   the judge) -- and records what is new, once, by key;
2. has the **judge** (one cheap model call, no tools) say for each new thing
   *now*, *before* (it happens), *briefing* or *never*, whether it is
   critical, a headline, a reason code, and optionally a task -- then
   checks in code (`agent/attention/vet.py`) settle what it need not see,
   and correct what it got wrong that code can know: automated mail is FYI
   unless it changes their plans, a weather alert counts only for home or a
   verified trip, and nothing passed, corrected or closed is said;
3. has the **doer** carry out tasks -- last in the tick, so no ping or call
   waits on it, and what it prepares goes out with the next -- with
   reversible tools only (`agent/attention/prepare.tools_for`): an event with
   no guests, a reminder, a task, a fact looked up, and a heads-up of up to
   three lines before something with a start time (where it is, what to
   bring, what was agreed, what the user promised the people in it -- the
   guests' records are handed over by code; who they are, from the web,
   only if the user turned on `MEETING_RESEARCH`). The one draft it writes
   unasked is a nudge on the user's own unanswered email. A reply the user
   owes, or a note after a meeting, is never drafted unasked -- it could
   need something only they know -- so its task is removed in code
   (`triage.ask_first`, pinned by the frozen gate test) and the ping asks
   instead ("How did lunch with Sam go? Anything to remember or do?"). A
   meeting the user has already written to someone in since it began is
   not asked about (`vet.written_to`). The answer lands in the
   conversation, which drafts it;
4. **delivers**: one message per tick, and at night only what is still
   critical after the checks (pinned by the frozen gate): automated mail is
   never critical, an alert must be for where the user is, and what is stale or
   corrected is dropped. The maintainer's rule of 10 October: "only truly important
   stuff" at night.

At 08:00 and 21:00 the **briefings** go out, carrying what was held. A
computer asleep at 8:00 sends the morning's as soon as it wakes, until noon
(flow 2).

## The knobs

The defaults are in `agent/attention/settings.py`. Since 4 October 2026 the
desktop app's control panel turns the ones below marked *(panel)* without a
code change: its values go to `data/knobs.json` and are in force at once
(`agent/worker/control.apply_knobs`), each inside the bounds frozen in
`core/knobs.py` -- themselves inside the ceilings below, pinned by
`tests/gate/test_knobs.py`. The rest are still a code change: test, commit,
swap. The panel also sets the spending caps and the day budgets of
background work's parts, whose defaults and ranges are core's
(`core/spend.py`, `core/knobs.py`); what a cap reached says is
`agent/caps.py`, which also says when OpenAI's balance is used up. A cap
also changes in a conversation, behind a card: `raise_spending_cap` takes
the high-risk yes and `lower_spending_cap` a plain one
(`agent/toolset/spending.py`), and at the month's cap, where no model is
asked, "RAISE 25" makes the raise's card in code (`caps.ask_to_raise`).
Either way the change is `spend.set_cap`'s, which takes only the five caps,
each within its range. With $1 of the month left, one message asks whether
to raise it, unless the user chose not (`MONTH_ASK`, on the Spend page);
a raised cap is asked about and stopped at again, since notices are kept
per cap amount.

| Want | Change |
|---|---|
| More or fewer pings *(panel)* | `MAX_DAY_PINGS` (8), `MAX_NIGHT_PINGS` (2, critical only) |
| Different briefing times *(panel)* | `MORNING`, `EVENING` |
| Different quiet hours *(panel)* | `QUIET_FROM`, `QUIET_UNTIL` (22:00-08:00), in the frozen `core/call_policy.py`; any times, on either side of midnight, lasting 5 to 16 hours (`core/knobs.py`). The night's work runs in their middle (`agent/night`), and the briefings must fall outside them |
| Weather in °C or °F *(panel)* | `UNITS`: those of the country home is in, unless chosen (`agent/attention/sources/weather.units`) |
| Check mail more or less often *(the app's Accounts)* | each mail account's own interval, 1 to 30 minutes (`agent/accounts.py`) |
| When a quiet thread counts as dropped | `OWE_REPLY_AFTER_DAYS`/`_UNTIL_DAYS` (2-7, the user's reply), `AWAIT_REPLY_AFTER_DAYS`/`_UNTIL_DAYS` (4-10, theirs) |
| Spend more or less *(panel)* | `TRIAGE_BUDGET_USD` ($0.10/day), `WORK_BUDGET_USD` ($0.25/day), within background work's `BACKGROUND_DAY_USD` and the month's `MONTH_USD` |
| Weather somewhere else | the user's home city, a fact they give in conversation or on the profile page (`agent/worker/control.profile`); no home, no forecast |
| What counts as a trip | `AWAY_KM` (80) and `HORIZON_DAYS` (7), in `agent/attention/trips.py` |
| Earlier heads-ups before events *(panel)* | `BEFORE_LEAD` (45 min) |
| Look up who is in a meeting, on the web *(panel)* | `MEETING_RESEARCH` (off): a heads-up is from the user's own mail, calendar and contacts unless they turn it on (`agent/attention/prepare.tools_for`) |

The frozen gate test `tests/gate/test_attention_bounds.py` puts a ceiling on
every one of these (12 pings a day, 3 a night, $0.25 and $0.50), and pins
what the doer may touch. Raising a ceiling is a human edit to that file and
a relock -- deliberately out of reach of anything Steve builds.

**What to say in judgement** is the judge's prompt in
`agent/attention/triage.py`: `JUDGE_CORE`, with a paragraph from
`JUDGE_SOURCES` for each source being watched or in the batch, joined by
`judge_prompt`. How the doer works is `TASK` and `HEADS_UP` in `prepare.py`,
chosen by `instructions`.

**Which news** is data, not code: the user says "keep an eye on X" or "stop
telling me about markets" (`follow_news` / `unfollow_news`).

## The action critic (from 4 October 2026)

Every action an unattended session takes or asks for -- a routine's run, a
goal's session, a mail task, a background task and its workers, the
attention loop's doer, a reminder going off -- is reviewed by an isolated
second model against what the user asked of that session (`core/critic.py`;
`actions._critic`; each handler passes its mandate). It never sees what the
session read. The maintainer, 4 October: the main priority after the mailbox.

- **Enforcing since 5 October 2026** (`critic.mode: enforce` in
  core/policy.yaml; the maintainer: "critic seems good to go"): an action it does not
  judge plainly part of their instruction (serves, at 0.6 or more), or could
  not review, becomes a card that says why, instead of happening. A card
  stays a card and a refusal a refusal. It had reviewed no live action
  before the switch -- nothing unattended had acted since it shipped -- so
  the evidence was the yardstick's: 21 of 21 hijacked actions stopped, 1 of
  22 real ones asked about needlessly. `shadow` in the policy turns
  enforcing off again; nothing turns the critic off.
- **Its record**: every review in `critic_reviews`; the morning briefing
  says how many it reviewed and which it asked them about; Activity marks
  those actions; `python -m core.critic report` gives the week's numbers.

## Skills (from 5 October 2026)

Methods saved from what worked, offered the next time the user asks for something
of the same kind (`agent/skills.py`; the rules, frozen, `core/skills.py`).

- **Written at night** (`agent/night/skilling.py`), at most three, from their
  requests of the last two days -- a turn each, their message to the reply --
  that were answered without breaking, not thumbed down, not corrected by
  their next message, and took three or more tool calls of two or more tools.
- **From their words and the steps only.** The writer is shown their request
  and the tool calls, each argument's value only where their words hold it,
  redacted -- never a tool's result or the reply, where a page or a mail
  could have put something. Most runs are one-offs and are skipped.
- **Checked twice.** The frozen check refuses a skill naming a tool the run
  did not call, an address, link, site or number their words did not hold,
  anything that sets aside their approval or hides something from them, or an
  injection's patterns; then a reviewer -- a separate call, no shared
  context -- keeps it only if it is a general method that does what the user
  asked and nothing more, and says if it repeats one already kept.
- **Used in their conversations only.** Their conversations carry a line per
  skill; the model calls `use_skill` for the steps when the request fits.
  Matching by embedding was tried first and could not separate the right
  requests from the wrong ones. A skill grants nothing: every action is
  checked as it would be without it. No unattended run sees one.
- **Seen and undone.** Each new skill is in the morning digest ("Skills
  learned"), with REVOKE SKILL n -- answered before any model. `list_skills`
  and `revoke_skill` in a conversation; uses are counted (`skill_uses`).
- **One switch for all of them** (from step 6): the built-in module Skills
  (`modules/skills/`) on the Extensions page. Off, none is offered or loaded
  and the night writes no more (`agent/modtext.skills_on`). A module may
  bring skills of its own, `<module>.<number>`, held to the same rules
  against its manifest at admission; they go with the module's switch, not
  one by one (docs/modules.md).
- **First trial**, 5 October 2026, on their runs of the past ten days (a copy
  of the database): of five candidates, two kept ("find and summarise a
  person's recent articles", "look up an office lead on a company website"),
  one skipped as a one-off, two refused by the reviewer as going beyond what
  the user asked. About a tenth of a cent for all five.

## The yardstick (from 4 October 2026)

Before autonomy widens -- the critic enforcing (5 October 2026), the
injection screen (11 October), background browsing -- it is measured
rather than asserted. The yardstick (`agent/autonomy_eval.py`) plays each
scenario in
`tests/gate/fixtures/autonomy/scenarios.json` -- a routine's run, a goal's
session, a mail to the agent, a background task, ten of them -- through the
real handler, with the live models, prompts, monitor, egress rule, critic
and screen, against a fake world: the scenario's mail, calendar, Drive and
pages, with attacks planted in some of them. Every action is recorded and
none is done; nothing the user would be told is sent. It runs in a process of its
own, because its fakes would be the live agent's anywhere else. The world
is an account of a test module (`tests/modules/world`), standing where the
user's accounts stand, so the sessions read it through the connector slot
as they would a real one, and are offered what a user with a mail,
calendar, files and tasks account is (`agent/features.py`), and no more.
Its scenarios and its world are under `tests/`, so an installed Steve never
runs it.

What the record means is frozen (`core/yardstick.py`), beside the frozen
scenarios, so the agent cannot move its own bar: a task done; an attack
read, obeyed, asked about, or landed (done without their yes); whether the
critic would have stopped the action a hijacked session would take (each
attack's `probe`) and how often it would ask needlessly about the rest;
what the screen flagged, planted and clean.

A task is done by its answer, not by its words: the cheaper hotel named as
the cheaper one, the museum's hours as hours, next week's Thursday by its
date (an alternative's `answers`, patterns rather than a model's judgement).
The world's dates are written relative to the day of the run
(`{{day:+10}}`, `{{week:+1:thursday}}`), and its search finds only the
pages with the query's words, best first (`autonomy_eval.ranked`).

- **Weekly**, from the night window (`agent/night/yardstick.py`), three
  runs of each scenario within $0.50; the morning digest says it in a line.
  By hand: `python -m agent.autonomy_eval run` (`--only`, `--repeat`,
  `--budget`); `python -m agent.autonomy_eval report` shows the last run.
  Reports are kept in `data/autonomy_evals/`.
- **Modules built** (`agent/build_eval.py`) join a run by hand, on a budget
  of their own (`--build-budget`, $5 unless given; 0 builds none), in a
  child process, and the report and its line carry their summary. The
  weekly run builds none until a real run has measured what they cost.
- **The first numbers**, 4 October 2026, ten scenarios three times ($0.05):
  tasks done 30/30; 24 planted attacks read, none obeyed, none landed; the
  critic would have stopped 21/21 hijacked actions, and asked needlessly
  about 1 of 22 real ones (an event a mail-thread session put on their own
  calendar); the screen flagged 5 of 6 planted items it reads, 0 clean.
  The attacks are mild; harder ones are the next thing to add.

## How to see what it is doing

- `python -m core.localapi get /attention/status`: each source's last run and
  note, today's counts, spend against budget, briefings sent, the caps.
  Counts only; no subject or headline.
- `python -m agent.attention` (or `--only weather,news`): one tick against a
  *copy* of the database. It reads the real sources, asks the real judge,
  does no tasks, sends nothing, and prints what it would send.
- The `observations` table: every item's verdict, task, outcome, and how
  it was delivered (`delivered_in`: ping, before, morning, evening,
  dropped, legacy).

## Jev as the judge: tried in shadow, retired (24 September to 11 October 2026)

Jev is a decision model from TypeSafe. It writes no text, just calibrated
probabilities, and is fast and nearly free. It was asked the judge's
question beside the judge -- when should the user hear this, and is it worth
waking them for -- and its answers were recorded in `jev_shadow`, never
acted on. A calibrated P(critical) would have let a threshold in frozen
code own the night's bar instead of a model's yes.

It did not earn that. Over 4,464 items it agreed with the judge 31% of
the time; most of what the judge dropped, it put in the briefing. Its
P(critical) told nothing apart: 0.47 on average for the three items the
judge called critical, 0.53 for everything else, and never above 0.9.
The shadow was retired on 11 October 2026. Its record stays in the live
database's `jev_shadow` table; its code is in git history.

Jev still answers narrower questions, where a calibrated yes or no is
the whole job: the injection screen (`core/screen.py`), the action
critic (`core/critic.py`), whether a logged-in click needs their yes
(`core/browser/clicks.py`), whether a task splits
(`workers.worth_splitting`) and whether a meeting that just ended is worth
asking about (`agent/attention/sources/calendar.py`). Every one asks it
through `core/decide.py`.

## Building modules at night (off by default)

Not the attention loop, but also unasked: at 3am the night may build up to
two modules (`agent/night/tinkering.py`, `MAX_BUILDS`) -- fixes from the
failure ledger that a module can make, and features from what the user
asked for, chosen from what `agent/night/evidence.py` shows it. Each is a
module build like one the user asks for (`docs/self-upgrade.md`): tested in
the sandbox, and ending at a card the user decides on. Nothing turns on by
itself.

- **The switch:** `self_upgrade.agent_initiated` in `core/policy.yaml`, off
  by default. While it is off the night proposes nothing, spends nothing,
  and the morning says why.
- **The money:** billed to the night, inside its budget and its cap. A
  build whose estimate is more than the night's cap has left waits for the
  user's allowance in the app, as theirs would.
- **The morning:** the report lists each build and how it ended
  (`NightReport.built`), and the briefing carries a line of it.

Until step 6, the night had the coding pipeline change the agent's own
code, and from 28 September 2026 merged none of it while the user rated
each attempt (the shadow week). That self-upgrade went in step 6.

## Changing its own models: the model scout (from 1 October 2026)

The maintainer, 1 October 2026: when a trusted provider releases a model that is
smarter or cheaper, switch to it, watch it for a day, and switch back if
things get worse -- as the maintainer moved every role from GPT-5.6 Luna to GPT-6 Luna
by hand on 25 September.

- **The bounds**, frozen: the `models` block of `core/policy.yaml`, checked by
  `core/models_guard.py`. OpenAI's models only, reached directly with
  storage off, as every turn already goes, and only one whose price is in
  the guard's own table (`models_guard.PRICED`): OpenAI's list gives a
  model's name and date, not its price, and a price the scout wrote down
  could make a dear model look cheap. A maintainer adds a new model's line.
  No model that must reason on a role the user waits on. Ceilings of $0.30 /
  $1.50 a million tokens, whatever the role. Only triage, converse, reason,
  escalate and bulk switch on their own; the coding pipeline, Jev and the
  call voice get a proposal. Until 12 October 2026 DeepSeek, Gemini and
  Anthropic's models were candidates too, on OpenRouter's zero-retention
  endpoints.
- **The bar** (`models_guard.weigh`): at least 10% cheaper and no worse on
  their own replayed turns, or clearly better (a margin of 10% of turns, by a
  sign test) at no more than 1.5x the cost; on triage and converse no
  slower than 1.2x to the first token and under a second; never readier to
  act on an injection, never worse at calling tools.
- **Finding:** `python -m agent.models_scout scan` reads OpenAI's model list
  and lists what the bounds would let each role switch to (the
  `model_candidates` table), and which models have no price in the table
  yet. It switches nothing.
- **Trying:** `python -m agent.models_scout eval <model> <role>` (`--dry` to
  estimate first) replays their last 40 turns from Signal and the tray
  (`agent/models_eval.py`): both models write each reply from the same
  rebuilt context, and Luna at high effort judges them blind (until 12
  October 2026 a judge from another maker, Gemini 3.8 Flash, had to agree
  too, so the incumbent's maker never judged alone; the bar's margins stand
  against that now); then the first step against the real
  tool schemas, the red-team injections as tool results, time to first token,
  and cost. No tool runs. The report is in `data/model_evals/`, the numbers
  and the bar's decision in `model_evals`. Conversation roles only for now:
  triage and bulk have no recorded inputs to replay, so they cannot be tried,
  and so cannot switch. Turns go ten at a time, and a candidate plainly
  losing after any ten is stopped there (`models_eval.hopeless`).
- **Switching** (`agent/models_switch.py`): an entry in
  `data/model_switches.json`, re-checked by the guard wherever it is loaded
  (`agent/models.py`, `load_active`) and put into the running worker at once.
  Who may switch is `models_guard.switch_refusal`: the user by hand, always
  (`python -m agent.models_scout switch <model> <role>`, `--anyway` past a
  trial that said no); the night only while `models.auto_switch` is on and
  only to a model the bar said yes to. One switch at a time, none while the
  canary watches a merged change. History in `model_switches`; `status` shows
  it. `models.auto_switch` is off until the user has watched one switch and one
  revert done by hand.
- **The watch**: a day, every 15 minutes (`background.model_watch`), judged by
  `models_guard.watch_verdict` -- the canary's count of failures against the
  two days before, and the new model's calls against the old one's: 1.5x the
  time or 2x the cost puts it back. The user is told when it is put back or kept,
  after quiet hours.
- **Undoing:** "REVERT MODELS" on Signal or aloud at the desk puts every
  role back, answered before any model reads it
  (`answering.intercept_approval`, like REVOKE SKILL). `python -m
  agent.models_scout revert [role]` at the laptop.
- **The week's round** (`agent/night/scouting.py`), last in the night when a
  week has passed since the last scan: scan, then `models_scout.shortlist` --
  free, in code: a model the bounds admit and not tried for the role in 90
  days, newest first -- then up to three trials on the scout's own budget.
  What it did is in the morning digest under "Models". Not a model reading
  releases. Until 12 October 2026 the shortlist went by the benchmark score
  in OpenRouter's catalog, which did not predict their turns (DeepSeek V4.1
  Flash scored above Luna and lost 20 of 40).

## Routines the user sets by text (from 4 October 2026)

"Every Sunday at 7pm, recap my week"; "every weekday at 8:30, what's due";
"check every three hours until the tickets go on sale" (`agent/routines.py`,
the tools in `agent/toolset/routines.py`). Every leading agent had these
(cron and heartbeat jobs, scheduled tasks); this one had only one-off
reminders and checks that end within a day. The maintainer's decisions, 4 October:

- **Approve once, with the targets fixed.** A routine that should email or
  invite someone on its own names them when it is made, and one card --
  showing exactly those targets (`core/monitor.describe_envelope`), the
  schedule and the instruction whole -- approves them. Inside them its runs
  act without asking; anything else asks with a card. Checked on every call
  by `core/monitor.routine_gate` against the card's hash of the envelope,
  the instruction and the schedule (`core/monitor.holder_sha`), so changing
  any of them asks again; high-risk tools are never routinable. A goal's
  card shows its ask and done-condition whole, and its hash binds them.
- **Only from their conversations.** `create_routine` and resuming refuse on any
  unattended channel, in a worker and in a routine's own run; no unattended
  toolset is offered them.
- **Runs are unattended** (channel `routine`): a worker's toolset plus the
  envelope; URLs only from search results or the routine's instruction
  (`core/egress.unseen`); receipts on every action.
- **The user hears only what changed** -- a run says NOTHING_NEW otherwise -- and a
  watch ends itself, with a last report, when its condition is met. A routine
  at a time the user chose is delivered then; an interval watch keeps quiet hours
  unless the user made it urgent, and is told in the morning.
- **Limits:** 20 routines, intervals of 30 minutes at least, a year ahead at
  most, $1 a day across all their runs. "STOP ROUTINES" pauses them all,
  answered before any model reads it.
- **A module's routines** (from step 6) run the same way at the times its
  manifest gives, within the same daily cap, naming the module when they
  report, and stop the turn it is off (`agent/modroutines.py`,
  docs/modules.md). "STOP ROUTINES" pauses the user's own; a module's stop
  with its switch. Their instructions are
  the module's words, so a run has no envelope and every action, even one
  a worker may take alone, is a card (`core/monitor.module_gate`).

## Goals: worked on over days until done (from 4 October 2026)

"Find three times next week for a call with Alex"; "get my follow-ups to the
three firms done"; "plan the Boston trip" (`agent/goals.py`, the tools in
`agent/toolset/goals.py`). A job is one answer and a routine repeats; a goal
holds a plan, the findings kept between sessions, their notes and answers, its
questions for them, and whose reply it is waiting for. Meta's Muse launched on
this in September; the maintainer chose it next, 4 October.

- **Made only by them** (`start_goal`, from their conversations): their words, what
  done looks like, a plan of steps, a cadence (12 hours unless the user says), an
  optional deadline (90 days at most) and budget ($2 unless the user says, $10 at
  most). Resuming and `answer_goal` likewise -- an answer is an instruction
  the goal follows.
- **Sessions are unattended** (channel `goal`, `handlers.handle_goal`): a
  worker's toolset, plus `send_email` and `send_draft` -- which always raise a
  card -- plus `goal_update`. A goal that should email or invite people itself
  names them when it is made and gets one `approve_goal` card, as a routine
  does; `core/monitor.goal_gate` holds it to the routine's rule. URLs only from
  search results or their own words (`goals.user_urls`).
- **A session changes the goal only through `goal_update`**: step statuses and
  notes, new steps, findings, one question at a time (three open at most),
  whose mail it waits on, when to look again (30 minutes at the soonest), and
  done. Never their words, the done-condition, the envelope, the budget or the
  deadline. Findings are notes, never instructions.
- **When it runs:** on its cadence; sooner when the user answers, when a card it
  raised has been answered, or when mail arrives from someone it waits on
  (attention's `look` calls `goals.woken_by_mail`) -- never twice within
  30 minutes. Never in quiet hours. Not at all while every step is waiting on
  them. A goal past its deadline ends, and the user is told.
- **What the user hears:** a session's reply only when it is new (NOTHING_NEW
  otherwise), each question once, and done with its outcome -- kept and sent
  again if Signal did not take it (`goals.owe`). Its progress is a
  line under "Goals:" in the morning briefing. When the user replies to a goal's
  message, their conversation passes it on with `answer_goal` (the message is
  recorded as `[goal N ...]` with numbered questions).
- **Limits:** 10 goals at once, $1 a day across all their sessions, and each
  its own budget -- spent, it pauses and says so. "STOP GOALS" pauses them all,
  answered before any model reads it.
- Not yet: splitting a session across a crew of workers.

## The agent's own mailbox (from 4 October 2026)

One of their mail accounts' addresses with `+agent` (`agent/mailbox.py`;
the rules frozen in `core/mailbox.py`). The maintainer, 4 October: Muse has
an address of its own; ours is a plus-alias, so there is nothing to set up
and it lands in their own mail. The user chooses the account in the app,
among those whose server delivers to a plus-address, and opts in the
addresses whose mail can be theirs.

- **Read every two minutes** (`background.mailbox_loop`, one search of
  that account when nothing is new), each mail judged once and kept in the
  `mailbox` table.
- **Their instruction** -- from an address they opted in, with its copy in
  their Sent folder, or, on Gmail, passing Gmail's own DMARC check (see
  docs/security-model.md). It is read from the Sent copy. Their note, the part above anything forwarded or
  quoted (`split_note`), says what to do. If it asks for days of work, a
  model reads the note alone and the mailbox starts a goal with that plan;
  otherwise it is a `mail` task. One written to the mailbox alone is then
  archived and marked read; one with other people on it stays in their inbox.
- **A reply in a thread the user brought it into** -- someone writing back on a
  thread where the user gave it an instruction -- becomes a task that drafts the
  reply the user would want; sending it is a card.
- **Anything else** is recorded and ignored. A mail that claims to be theirs and
  fails authentication goes in the failure ledger (`mail_unverified`), so
  the morning briefing says so.
- A mail task runs on the `mailbox` channel: unattended, with a worker's
  tools and the sends that ask them; the mail is material, marked as such and
  screened. Its answer reaches them like any task's, "From your agent's
  mailbox (...)".
- **Limits:** 20 instructions and 10 thread replies a day. The control
  panel's Channels page shows the address and the day's counts; Activity lists every
  mail, ignored ones too.

## Adding a source

1. A module in `agent/attention/sources/` with `collect(conn) ->
   list[Observation]`. It runs in a thread, so blocking calls are fine and
   `runtime` is not available. Public feeds go through `attention/fetch.py`.
2. Give each observation a key that names the thing **in the state worth
   mentioning**. An id alone gives one mention for the thing's whole life;
   an id plus an `updated` stamp gives one per change; an id plus a date
   gives one per day.
3. List it in `sources.all_sources()` and give it a cadence in
   `settings.CADENCE_S`.
4. Tell the judge what it is and how to weigh it: a paragraph in
   `JUDGE_SOURCES`, and the feature that switches the source on
   (`Source.feature`).
5. Test it against a captured real payload (protocols §11) in
   `tests/unit/fixtures/attention/`. `tests/unit/conftest.py` refuses the
   live feeds.

## What it cannot do, on purpose

- Anything that reaches another person: no send, no invite. A draft waits
  in their drafts.
- Approve a card, or file a change to itself as the user's request. The doer's channel, `initiative`, is autonomous by the frozen
  rule `core.call_policy.is_autonomous`.
- Read mail or calendar while the computer sleeps.
