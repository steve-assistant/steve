# Flows: where information goes, hop by hop

Each flow below is traced through the code as it is: every hop named by file
and function, what can go wrong at that hop and what happens when it does,
and the tests that prove the flow holds. `docs/architecture.md` has the
processes and the code map; this is what moves between them.

A flow is only as good as its least-tested hop. Where one is known to be
weak, it says so under **Limits**.

---

## 1. The user sends a Signal message, and Steve answers

Telegram goes the same way, from its own step 1 (below).

1. **signal-cli on this computer** receives it. The worker starts it as a
   duty of its own, once its add-ons' files check out and an account is
   linked (`agent/worker/links.start_signal`), and tries a failed start
   again (`keep_signal`). `channels/signal.py`
   `SignalChannel.receive` → `_to_incoming`: only Note to Self; a sync message
   from their phone; a reaction carries the timestamp of the message it is on.
   Anything else is ignored, and the attachments signal-cli downloaded for it
   are deleted as it arrives (`_discard_attachments`). What came with a Note
   to Self is sorted (`channels/photos.py` `sort`): a voice note to hear; up
   to four photos (JPEG, PNG, WebP, GIF, up to 20 MB each) to show the model;
   anything else named in a few words, as not opened. A photo with no words
   is a message. The files are kept a day, then deleted
   (`signal.sweep_attachments`, hourly, `background.attachment_sweep`).
2. **The bridge** — `channels/bridge.py` `ChannelBridge._handle`: marks it
   read, turns a 👍/👎 into `YES <word>`/`NO <word>` *only* if the reacted-to
   message was a card (`reaction_as_answer`, via `SignalChannel.sent_text`)
   and otherwise records it as feedback on that reply
   (`answering.note_feedback`), resolves a voice note (`_resolve_text`: heard
   by OpenAI's speech-to-text, `core/hearing.py`, billed under chat to the
   conversation it joins, and echoed back as `heard: ...`; one that cannot be
   heard is answered with why, and a request to type it), shows 👀
   while working, and hands the text to
   `agent/worker/answering.voice_reply(text, channel)`, with the photos
   (`images`) when there are some, or marked `spoken` when it was a voice
   note: a voice note answers a card only by saying its word ("yes
   maple"), since anyone's forwarded recording can say "yes". The text
   the turn is given, and the conversation records, begins with a line
   for what came with it
   (`photos.note`: "[1 photo attached]", "[Not opened: lease.pdf, a file
   Steve cannot open]"). A message of only unopened files is answered with
   that, and no turn. "unlink" typed on its own is not a turn: the goodbye
   goes, then the channel unpairs (`unpair`, below).
3. **Approvals first, before any model** — `answering.intercept_approval`,
   for the user's own channels only (`APPROVAL_CHANNELS`).
   Otherwise `answering.conversation(recent_session(channel), channel)` and
   `agent/loop.Conversation.send`, which resumes today's session for that
   channel, builds the agent (`build_agent`, instructions on every turn) and
   runs it; effects go through `agent/actions.act`. Photos are read once
   (`photos.load`, each sent as the format its bytes show, and a file that is
   not a photo left out) and go to the model after the turn's text, on every
   hop of that turn and no later one: the history carries the line, not the
   photo (`agent/history.py` `_kept`).

   **What the turn reads** (10 October 2026). Before the model, their words
   close what they name (flow 17). Then, on their own channels
   (`call_policy.PRESENT_CHANNELS`), the history is rebuilt from the database
   (`agent/context.py` `history`): everything in the order it was said,
   their last few exchanges from the last few hours whole, with what their
   tools did and, for the latest, a digest of what they found, and the rest
   of the day as one settled block. A ping or pre-event brief says where it
   came from -- source, place, the judge's reason (`context._came_from`, from
   `agent/attention/told.py`). Their message carries the clock and then Open
   now (`closing.open_now`): what is owed, waiting or lately closed, and how.
   Every other run keeps the history it holds in memory
   (`history.compact`).

   Before the answer is accepted, `Conversation._verified` checks it
   (`agent/verify.py`): a claimed effect ("added to your calendar", "sent")
   needs a matching tool call in the conversation (`unbacked_claims`), and a
   queued send is not a sent one. A claim that fails earns one correction
   pass, spliced out of the history. Reflection grades the same thing: the
   judge sees the recent tool results (`reflect.transcript`), not just their
   names.

   A turn that breaks -- a tool that raised, a provider that gave up --
   raises `TurnFailed` after recording the question and a plain "something
   broke" answer in the conversation, so "try again" has something to try.
   `voice_reply` sends that answer as the reply. An executor that raises is
   caught in `act` and read by the model as a failure.
4. **Back** — `ChannelBridge._send_reply` → `_deliver` sends it, retrying
   once if Signal refuses. `SignalChannel.send` addresses it to their own
   number with `notifySelf`, an ordinary message that buzzes their phone;
   `noteToSelf` is signal-cli's silent path. Its own message coming back is
   dropped by `_to_incoming`, by the timestamp it was sent under.

**Setting it up, in the app** (`agent/worker/pairing.py`, setup's channel
page, or Channels under Connections). "Set up Signal" downloads signal-cli and its Java
(`core/addons.install`): each archive checked against its pinned hash
before it is unpacked, and the unpacked files before every start. "Show a
code to scan" asks `/signal/link`: `signal.start_link` reads the
`sgnl://linkdevice` link while signal-cli waits for the phone, and the app
gets it as a QR code in SVG, written nowhere. `signal.finish_link` waits
for the scan, up to five minutes, and Signal starts. Unlinking, from the
app or by "unlink" in Note to Self (`pairing.unlink_signal`), stops the
bridge, deletes this device's keys (`signal.forget`), and stops the
messages still waiting for Signal (`outbox.stop_channel`); the goodbye says
how to remove the device from the phone's list, which a linked device
cannot do itself. Unlinking also takes back what the chat set going
(`approvals.forget_chat`): the cards asked there and still waiting are
withdrawn, the routines and goals approved from it are paused, and its
yeses are undone: those still in their hold, and its approvals of what was
paused. Turned on again in the app, a routine or goal asks before each
send; the app says so beside the goodbye. An answer from the chat that was
still being read when it went does not count, even once a chat is paired
again: `approvals.decide` asks `links.answers_from` as it records one,
which compares the bridge and the pairing the answer was read over
(`bridge.READ_OVER`) with those up now.

**Telegram, in place of Signal** (`channels/telegram.py`). The user's own
bot, from BotFather: its token is checked with Telegram and kept in the
keychain (`pairing.telegram_token`), and the worker long-polls the bot
(`TelegramChannel.receive`), keeping its place, so nothing is read twice and
there is no inbound port. "Pair your chat" shows six digits
(`pairing.telegram_pair`); the first private chat to send them to the bot
within ten minutes is paired, and every other chat is ignored. Voice notes
and photos are fetched from Telegram and go to the bridge as Signal's do;
photos sent together come as one message. A card comes with Yes and No
buttons, and a press is read as "YES WORD" (`_pressed`); a high-risk card
has none, since it needs the code. "/unpair" in the chat, or the app's
button, forgets the chat and its files (`pairing.unpair_telegram`), and
takes back what it set going, as unlinking Signal does. Signal
wins when both are set up: messages go to one remote channel
(`links.remote_channel`).

**Tests:** `tests/unit/test_bridge.py`, `test_approval_replies.py`,
`test_approval_intercept.py`, `test_loop.py`, `test_context.py`,
`test_channels.py`, `test_photos.py`, `test_telegram.py` (against a fake Bot
API), `test_signal_pairing.py`, `test_signal_link.py` (against
a fake signal-cli, `tests/fakes/signal_cli.py`);
`tests/gate/test_addons.py`, `tests/gate/test_hearing.py`.

**Limits:** replies are text, to voice notes too, until step 7. Hearing a
voice note, or seeing a photo, needs an OpenAI key. A photo is seen on its
own turn only: asked about it later, Steve knows one came but cannot look
again. Files other than photos (a PDF, a document) are not
read. Signal drops a linked device left off for
about a month; the panel then offers a new code. Telegram keeps a message
for a computer that is asleep for only a day, and photos sent together are
joined only when they arrive in one poll.

## 2. The computer is asleep

Nothing of Steve's runs while the computer sleeps, and nothing else answers
for it. What happens when it wakes:

1. **The heartbeat notices** (`background.heartbeat`, `awake.tick`). It beats
   every minute, and a beat that finds the last one more than `awake.GAP`
   (three minutes) old, by the wall clock, starts a new stretch awake. The
   gap was the computer asleep if this process beat before it, or the
   backend not running if not. Diagnostics shows "Awake since 07:45 · asleep
   22:10–07:45" (`awake.status`, in `/control/overview`).
2. **Every loop's pause ends** (`runtime.woke`). The loops pause on
   `runtime.nap`, never `asyncio.sleep`, so each takes its next round at once:
   - **a reminder** whose time passed while it slept goes off
     (`reminders.due` returns every active one whose time has passed);
   - **a briefing** that slept through its time is sent, unless it is more
     than `briefing.LATE_BY` (four hours) late (`briefing.due`);
   - **the outbox** retries what it could not send.
3. **The computer is held up** for `awake.HOLD_AFTER_WAKE` (five minutes) by a
   power request (`wake.kept_awake`), so Windows does not put a machine the
   wake timer woke back to sleep before that work has gone.
4. **Their messages,** waiting on Signal's servers (or Telegram's, which
   keep them only a day), are received and each is answered as in flow 1. A reply to one sent before the wake, and read more
   than ten minutes after it was sent, begins "Sorry for the wait: the
   computer was asleep until 07:45." (`awake.waited_line`, through the
   bridge's `waited`): once per wake, from code.
5. **The night**, if the computer slept through its window's opening, starts
   only while a night cut short may still be taken up (`night.take_up`), and
   otherwise waits for the next one (`background.until_the_night`), rather
   than running beside the user's morning.

Two switches, both off by default (core/knobs.py):
- **`WAKE_TIMER`** (`agent/wake.py`) asks Windows to wake the computer when the
  next thing is due. Its task runs the backend's own `pythonw -c pass`: the
  wake is the point.
- **`KEEP_AWAKE`** holds a power request while the computer runs on mains
  power, so an idle one does not sleep (`awake.on_mains`). It does not stop
  the lid or the user's Sleep, and on battery it lets go.

**Tests:** `tests/unit/test_awake.py` (the heartbeat, the wake ending every
loop's pause, a reminder and the morning briefing delivered on waking with the
clock pinned, the night slept through, the holds, the "sorry" line),
`test_reminders.py`, `test_attention_briefing.py` (when a briefing is due,
and when it is too late), `test_wake.py`.

**Limits:** a wake is noticed by the next beat, up to a minute after it.
"Asleep" is a frozen process, so a worker whose event loop blocked for
three minutes would read as having slept.

## 3. Steve tells the user something

1. Anything unprompted — a card, a briefing, a reminder, a job's result —
   calls `agent/worker/links.notify_user(text, kind=...)`.
2. `notify_user` writes it to the outbox (`agent/outbox.add`) and returns
   its row. It never sends inline. The row goes to the app at once over
   `/ws` (`{"type": "inbox", ...}`), and the app asks for the day again:
   the row is an entry on the day page (`agent/worker/journal.py`).
   - With the window in front of the user, the page is the news;
     otherwise Windows shows a toast (`ui/src-tauri/src/toast.rs`): the
     text while Windows is unlocked, only the kind ("Steve: a reminder")
     while it is locked, since the lock screen shows toasts to anyone at
     the desk. Silent: the phone is the buzz. A click opens the day.
3. With Signal set up and linked (`links.remote_channel`), the row is due a
   send too. The outbox duty (`background.outbox_loop`), woken by the new
   row or every `OUTBOX_EVERY_S`, sends what is due (`links.send_due`).
   - One Signal did not take is tried again after 1, 2, 5, 15 and 30
     minutes, then hourly (`outbox.RETRY_AFTER`), until its `until`: a
     briefing's `LATE_BY`, otherwise a day. Then it stops, and says why.
   - One that goes brings forward the rest waiting for Signal: it is back.
   - A card answered before it could be sent is not sent.
4. **Told means told.** Once Signal took it (`outbox.sent`), or the app
   showed it (`/control/looked` → `links.seen_in_app`, for every row
   written by the moment the user looked at the day), the text joins the
   conversation of the place it reached, the one their reply will land in:
   `answering.note_sent` → `Conversation.note_said`, stored as an
   assistant message marked `(unprompted)` and added to the loaded history.
   So "yes, add it" in reply to a ping reaches a model that has read the
   ping. Its lines in `told` are said then too. A caller can record more
   than it sent (`notify_user(text, noted=...)`): a reminder the user sees
   as "Mop" is recorded as "[reminder 27 went off] Mop".

**Tests:** `tests/unit/test_outbox.py` (in the app at once, retried on
the schedule, stopped with why, told only once the user has it),
`tests/unit/test_attention.py` (a ping is in the conversation),
`tests/unit/test_job_results.py` (a result the outbox could not take is
written later, once), `tests/unit/test_obligations.py` (a reminder reaches
them once Signal is back).

**Limits:** A build run from a checkout
shows its toasts under PowerShell's name and icon: Windows shows a toast
only for an app it knows, and only an installed one is.

## 4. The agent wants to do something that needs a yes

1. A tool calls `agent/actions.act` → `core/monitor.gate`: gated. Every
   decision in this flow is the frozen monitor's; `act` and
   `agent/approvals.py` are its plumbing. In an unattended session the
   action critic (`core/critic.py`, `actions._critic`) reviews the action
   against the session's mandate first; in shadow it only records, and
   enforcing, an action it does not plainly allow arrives here as a card.
2. `agent/approvals.request` records the card (one card per identical
   request, and one high-risk card open at a time: `core/monitor.may_ask`),
   picks a word, and `_ask` puts it in the outbox through `notify_user`
   (flow 3), naming the action, so a card answered first is not sent.
   Only the card says its word: a reply that said it could pose as the
   card. No model is sent a waiting card's word: each request has it taken
   out of all it holds (`agent/provider.Unworded`), and the conversation
   records the card without it. `agent/cardwords.py` also takes it out of
   the model's replies and every other message to a channel, except the
   code's own lines for the card, each of which says the card's whole first
   line with its word: a list's line (the briefing, several cards waiting,
   "pending"), the briefing's question, and the question back to a reply
   that may be about the card. On a phone it is taken out of the text as
   sent, once its Markdown is off and the characters no one sees are gone
   (`channels/plaintext.shown`), so nothing left over can spell it. A card
   keeps its own word and no other's: the model wrote parts of it, an
   event's title or an email's subject, each on one line after its label.
3. The user answers: `YES MAPLE`, a whole-message "yes" if nothing has been
   said since the card, 👍 on the card itself, or its Yes button in
   Telegram (flow 1 steps 1–5). A typed yes approves only when it says
   nothing else: each word is a yes, the card's word, a filler such as
   "please", or the code in ASCII digits. A reply that says more, asks
   ("Send MAPLE?", a question mark of any script), or names the card with
   no yes or no ("what is maple") is asked about, and told what the card
   is. A polite word at the ends of a bare yes leaves it a yes ("yes
   please"); beside a lone verb ("send thanks") it is asked about.
   `approvals.parse_reply` reads it; `answering.decide_action` →
   `approvals.decide` (`core/monitor.may_decide`) →
   `jobs.enqueue(kind="action")`.
   **High-risk** -- spending money (`tier: high` in `core/consent.yaml`) --
   takes the six digits from their authenticator app as well: "YES MAPLE
   482913". `core/monitor.accept_code` checks them against the laptop's
   secret (`core/totp.py`) and binds the 30-second step to this card, once.
   A reaction or a bare yes cannot carry a code, so neither answers one. In
   the app it takes the passphrase instead, typed with the click
   (`routes.action_decide` → `core/monitor.accept_passphrase`), or Windows
   Hello's signature over a challenge naming the card and its arguments
   (`/signin/challenge`, then `core/monitor.accept_hello`). Any answer in
   the app also needs the sign-in's live session (`agent/worker/signin.py`).
   Each yes is
   recorded as the channel it came from, and judged by that channel's
   ceiling (`core/monitor.CEILINGS`, lowered by the user's settings): a yes
   from anywhere not in the table approves nothing.
4. The job queue (a duty) claims it, recording its claimant;
   `agent/worker/handlers.handle_action` → `approvals.execute`: the
   monitor's second look (`core/monitor.may_run`: no action runs on a yes
   its channel may no longer give, and no high-risk one without its second
   factor, for the arguments it was given for), a hold for anything irreversible (`UNDO MAPLE` works
   in it, and not once the call has begun, `approvals._calling`, nor past the hold and
   `approvals.CALL_MARGIN_SECONDS`, marked or not), exactly-once claim, `actions.perform`, and a reported failure
   counts as a failure.
5. `notify_user("Done — …")` or `"Couldn't do it — …"` with the reason.

**An email from Outlook** -- or from any password account, the same way
(`core/engines/imap.py`: `make_draft`, `send_draft` over IMAP
and SMTP) -- takes two steps (`mail._send_via_drafts`), so that nothing
leaves those mailboxes without the user's explicit yes.
1. The email is saved to their Outlook Drafts (`draft_email`, free and
   reversible: `outlook.draft_new`, or `outlook.draft_reply` for a reply).
2. A `send_draft` card, "Send from your Work account" (the account as the
   user named it), with the recipient and subject, says the full email is in
   their Drafts: the
   draft is where the user reads all of it, and the card quotes none of it
   (`approvals.quoted_body`).

On their yes, the draft goes only as its card showed it
(`core/writes.send_draft`, bound to the draft's digest): one changed since
-- a recipient added, a word -- is not sent, and they are asked again with
what it now says. That holds up to the send itself: Gmail sends the message
read for that check, not the draft by its id, and classic Outlook reads the
draft again in the call that sends it (`modules/outlook_classic/outlook.py`).
A message already sent is refused. Without a card, a
send goes only on the monitor's verdict that it is free (`core/monitor.py`'s
`Gate`). A note to their own address, from any of their accounts, is free
(`send_email`), as it is from Gmail.

**A Gmail email the user is asked about** goes the same two steps, since
4 October 2026 (`mail.send_email`): its card, cut at 300 characters, used
to show the first 180 as if they were all of it. Only when the user would be asked
(`actions.asks`): a note to themselves, or a send their approved routine may
make, goes as it did.

**Tests:** `tests/unit/test_approvals.py`, `test_approval_replies.py`,
`test_approval_intercept.py`, `test_server_actions.py`, `test_outlook_send.py`,
`tests/gate/test_every_tool_is_classified.py` (every gated tool routes
through `act`), `tests/gate/test_high_risk_approval.py` (a forged yes from
whoever holds a remote channel approves and runs nothing).

## 5. The tray (the desktop app)

Since 4 October 2026 the tray is an installed desktop app (`ui/`, Tauri),
started by Windows at login with `--hidden` if setup was told to, and
living in the tray; its control panel is described in
`agent/worker/control.py`.

1. The app's Rust side reads the run file, has the backend prove it holds
   the token (`core/localapi.py`), and hands its windows the address and the
   token. The app → `http://127.0.0.1:<port>` or `ws://127.0.0.1:<port>/ws`,
   with the token on each. Once a passphrase is set, the main window shows
   the sign-in first (`ui/src/SignIn.tsx`): Windows Hello, asked at once
   where it is set up, or the passphrase, each → `/signin/…` → a session the
   shell holds for every window and keeps alive each minute
   (`ui/src-tauri/src/signin.rs`). The shell ends it, and tells every
   window why, when Windows locks, on sleep, after 30 minutes without
   input, and on quit; signed out, the quick bar and the desk card show
   only the way to the app.
2. `core/supervisor.py`: `core/localonly.LocalOnly` refuses a foreign Origin
   or Host, or a request without the token; `proxy` / `ws_proxy` forward to
   the active worker (never its `/_…` control routes).
3. `agent/worker/routes.ws_session` or `routes.chat` →
   `answering.conversation(…, "tray")`. A note's Yes and No, in the window
   or the quick bar, post to `/actions/decide` → `routes.action_decide` →
   `answering.decide_action`.
4. The control panel reads and turns `/control/…` (`agent/worker/control.py`);
   a knob it sets goes to `data/knobs.json` through `core/knobs.write` and is
   in force at once (`control.apply_knobs`). Start, stop and restart are the
   shell's own (`backend_control` in `ui/src-tauri/src/lib.rs`), on the
   backend it owns (`ui/src-tauri/src/backend.rs`): a stopped agent cannot
   start itself. The shell also draws the tray's red dot while a note waits
   (`set_waiting`). The tray's menu: Open Steve, Talk (the quick bar,
   listening until Enter), Show the desk card, Take notes on what's
   playing, Quit.
5. **The quick bar** (`ui/src/QuickBar.tsx`): a second, small window over
   whatever the user is doing, on the one hotkey (Win+Alt+S, or the first of
   `HOTKEY_FALLBACKS` in `ui/src/api.ts` that Windows lets the app have;
   Settings changes it). The main window registers it (`bindTapOrHold`): a
   tap asks the shell for `show_quickbar`, a hold for `quickbar_talk`
   (`ui/src-tauri/src/lib.rs`), which shows the bar and tells it to listen,
   then, on release, to send. The bar talks on its own socket, as a tray
   conversation with the same rights, so the words heard and the answer show
   in it; it starts a new conversation after half an hour unused. Before
   typing it lists the notes (`/control/needs`, answered in place), the next
   event or reminder (`/control/next`) and Steve's last entry
   (`/control/day`). A hotkey that cannot be registered says so in Settings.
   **The desk card** (`ui/src/DeskCard.tsx`) is a third window, wanted once
   setup is done. It asks the same three routes, and hears on its own socket
   what any window's turn or any job is doing (`journal.TurnLine`,
   `journal.tell`). An answer that reaches a closed quick bar goes to it
   (`deskcard-reply`). The shell shows it without activating it, and hides
   it for a full-screen app, the lock or the user's Hide
   (`ui/src-tauri/src/deskcard.rs`).
6. **Toasts** (`ui/src-tauri/src/toast.rs`, asked for by the main window on
   each `inbox` message): a note (a card, a reminder going off) or a
   briefing always makes one, so it is in Windows' notification centre too;
   anything else only while the window is not in front and the desk card is
   not on the screen.

**Tests:** `tests/gate/test_zero_downtime.py` (proxy, websocket, origin
checks, control routes), `tests/gate/test_local_only.py`,
`tests/gate/test_knobs.py`, `tests/unit/test_control.py`,
`tests/unit/ui/test_quickbar_screen.py`, `tests/unit/ui/test_app_settings_screen.py`,
`tests/unit/ui/test_signin_screen.py`, `tests/unit/test_signin.py`.

## 6. Memory

Two stores. **Facts** (`agent/facts.py`, the `facts` table) are typed
lines -- `subject` (user, `person:<id or name>`, `org:<domain>`, world),
`attribute`, `value`, `source`, `valid_until`, `status` -- numbered from
`facts.FIRST_ID` (1000). **Notes** are the markdown memories: their corrections
of how the agent works, project state, research, the judge's lessons.

1. `remember` (`agent/toolset/memory.py`), off the loop:
   - kind `user` → `keep_fact` → `facts.add`. A fact about them that holds
     someone else's address is refused, with where it belongs
     (`facts.misfiled`; memory 223). Source `user_words` on their channels,
     `inferred:<channel>` elsewhere, and `inferred:paste` when their message
     reads as material passed on rather than their words (`facts.pasted`: long,
     or many lines; message 2584). The same subject and attribute supersede the old row; nothing
     is deleted. `replaces=<n>` → `facts.correct`; `forget=<n>` →
     `facts.forget_number`. The numbers kept go in `Deps.turn_cache`
     (`facts.TURN_KEPT`), and the reply says them (`facts.acknowledged`).
   - any other kind → `memory.remember`: embedded first, then a markdown
     file in `data/memories` (its own git repo), committed alone (`git add
     -- <its path>`, never `-A`), and indexed. A memory about a date carries
     `until`, its last day (frontmatter and `memories.until`).
2. Every turn's instructions carry who the user is (`loop._about_user`):
   `facts.profile_text` -- five facts or fewer, about them, their own words or
   confirmed, their plans for the next two weeks first, none past its last
   day. Notes of kind `user` are found by `recall`, never shown unasked. On
   their channels (`context.builds_here`), their message also
   carries the facts it calls for (`Conversation._facts_for` →
   `offloop.with_own_conn(facts.relevant_text)`, at most `FACTS_WAIT_S`):
   linked by a name or a domain in it first, then by meaning at or above
   `memory.FACT_FLOOR`, at most six lines and 600 characters, each
   with why, after their words and before Open now (`context.turn_text`).
3. `recall` (`agent/toolset/memory.py` `recalled`): the facts first (`facts.search`),
   then the notes at or above `memory.RELEVANCE_FLOOR`, without the research
   notes and the judge's lessons (`memory.archived`) unless `about="how"`,
   and without what is past its day unless asked for the past.
4. "FORGET MEMORY <n>", answered before any model
   (`answering.FORGET_MEMORY_RE` → `facts.forget_number`): a held fact from
   1000 up, else the memory the index numbers so.
5. The facts carried over from the memories were proposed once, ticked by
   them and kept as `confirmed` on 10 October 2026
   (data/audits/2026-10-10/facts-proposed.md). `python -m agent.facts`
   lists what is held.

The night tidies (`learning._tidy_memory`): `memory.reconcile` indexes
files the index lost, `memory.expire` forgets what is a week past its
`until`; and `learning._learn_from_user_words` keeps facts (`facts.add`,
source `user_words`, the message they came from) from their own messages --
only with a quote that is theirs, checked by `tools/evidence.ground_claims`,
under whom they are about, a fact about them holding no one else's address
(`facts.misfiled`) -- listed in the morning with "FORGET MEMORY <n>". In
the profile, what the user confirmed comes before what the night heard. It never
reads a pasted message (`learning._pasted`), and nothing reads their calendar
for facts about them.

**Tests:** `tests/unit/test_facts.py`, `test_offloop.py`,
`test_memory_dates.py`, `test_night_heard.py`, `test_memory_kinds.py`.

## 7. The night, and the briefings

1. `agent/worker/background.night_loop` (a duty) sleeps until 03:00, then
   `agent/night.run_night`: `learn` (mail and calendar ingest, people,
   commitments, facts about the user checked by `tools/evidence.ground_claims`),
   `research`, `improve` — the modules the night builds, while
   `self_upgrade.agent_initiated` is on; off, it says why in the morning
   (docs/self-upgrade.md).
   The report is stored in `jobs` (`night._store`).
2. `background.briefing_loop` (a duty) sends the morning briefing at 08:00
   and the evening look-ahead at 21:00 (`agent/attention/briefing.py`):
   `gather` reads the calendar, the kept forecast, the cards waiting, the
   commitments due, what the attention loop held (flow 9) and, in the
   morning, the night's report (`NightReport.from_dict`). `render` is code -- times and card
   words exact; a model writes only a one-line lead (`lead`), and the
   briefing goes without it if that fails.

   The news is summarised, not listed (`briefing.compose` →
   `attention/newsdesk.for_briefing`):
   - Each story the judge kept is found again by its headline, since a
     Google News link is a JavaScript redirect with nothing to read, and
     that article is read.
   - It joins the key points of new newsletters, from
     `newsdesk.NEWSLETTERS` senders only. Articles and newsletters reach
     the summary's model as the injection screen allows (`newsdesk._screened`).
   - Summarised in a few lines with the outlets; the headlines go instead
     if that fails or the day's budget is spent.
   - Cached by its sources. A newsletter is carried by one briefing
     (`newsletters_said`).
   - On request, `news_summary` does the same, fresh.

   Sent once a day each (`sync_cursors['briefing:<kind>']`), the morning's
   until noon if the computer slept through 8:00 (flow 2); what it carried
   is marked told once it is in the outbox, which stops trying the phone
   once it is too late to be worth reading. An evening with nothing
   tomorrow sends nothing.

**Tests:** `tests/unit/test_night.py`, `test_attention_briefing.py`,
`test_server_actions.py` (cards lead the briefing).

## 8. Reminders

A duty loop in `agent/worker/background.py` that ends in `notify_user`. A
reminder that checks on something asks the model through
`answering.voice_reply(channel="reminder")` — a channel that cannot answer a
card. Reminders fire whatever the hour: the user asked for them.

A reminder going off joins their conversation marked as one (flow 3), and
`list_reminders` shows what went off or was cancelled in the last twelve
hours after what is still scheduled (`reminders.recent`). Without both, "both
complete" after two reminders had nothing to refer to, and the model took it
to mean the week's gym and run reminders and cancelled them.

A plain reminder is marked gone off once it is in the outbox
(`background._fire_reminder`), which keeps trying the phone (flow 3). It
used to be marked done before it was sent.

**Tests:** `tests/unit/test_reminders.py`, and
`tests/unit/test_attention.py` for how a reminder is recorded,
`tests/unit/test_obligations.py` for delivery.

## 9. Something happens, and the agent notices

The attention loop, `agent/attention/`, run every minute by
`background.attention_loop` (a duty). One row per thing in `observations`
(`agent/attention/store.py`) carries it from noticed to told. The steps are
in the order an item takes them; a tick (`attention.tick`) runs the doer
(3) last, so that no ping or call waits on its tasks, and what it prepares
goes out with the next tick.

1. **Sources** — `sources.due` picks the ones whose cadence
   (`settings.CADENCE_S`) has passed since `sync_cursors['attention:<name>']`;
   each runs in a thread on its own connection (`offloop.with_own_conn` →
   `sources.collect`). Each mail account lists what arrived and fetches
   only what is unseen (`MailProvider.arrived`; Gmail's listing also reads
   the headers a mailing system adds, its category, the recipients and the
   RFC Message-ID, kept in the row's `ref` for the checks; the IMAP engine's fetch
   reads the same from the bytes it already has; Outlook's envelopes have
   none of it); from every calendar read as one (`agent/calendars.py`, an
   event on two counted once with the other's guests), calendar changes
   (`CalendarProvider.changed`), the week's horizon (two passes per event),
   meetings that just ended (`calendar.after` via `calendars.ended`:
   one-off, timed, at most four hours, once per event); commitments due,
   files others changed in each files account; the weather at home, once the user has said where that is
   (`sources/weather.py`: Open-Meteo, and NWS alerts for a US home;
   conditions computed in code -- rain during an in-person plan, a notable
   day, a forecast that called its rain off -- and each day kept in
   `weather_days`). The weather is also read where the user is going
   (`attention/trips.py`: calendar events in the week ahead whose location or
   title names a town more than 80 km away, geocoded once into `places`, and
   a trip only once verified (`trips.verify`: the title says the user is going, a
   flight, a hotel, several days, or an address whose own state is the
   place's) -- an unverified one gets no weather at all;
   `weather.trip_observations` keeps each day's line in `trip_weather` for
   the briefing and raises rain, snow, storms, frost or heat on the days the user
   is there, plus official alerts for a US trip two days out); and news
   (`sources/news.py`: a Google News feed per topic the user follows
   in `interests`, none until they follow one, one observation per story
   however many outlets or topics carry it); and threads left hanging (`sources/threads.py`: headers from
   both mailboxes, both directions, written to the contact ledger as read;
   the last message of a thread with a real person, days old, keyed by that
   message so each silence is said once). Every public feed goes through
   `attention/fetch.py`. What a stranger wrote goes through the injection
   screen before the judge reads it: a mail preview and a calendar entry in
   their sources, a headline and a Drive file's name as their source's
   `screen` (`news.screened`, `drive.screened`), which `sources.collect`
   applies to what is new and within the cap.
   `store.record_new` keeps what is new, before any model sees it. Mail is
   one source per mail account (`sources.mail.watch`), which lists what
   arrived and fetches only what the store has not seen
   (`MailProvider.arrived`).
2. **Triage** — `triage.triage`: one BULK call per batch of 40, no tools,
   with today's and tomorrow's calendar and what is known about the user
   (`day_context`, cached ten minutes), and, uncached, what the agent did for
   them in the last six hours, what the user said in the last three, their recent
   notes on how it did, and their thumbs on its pings and pre-event briefs with
   where each line came from (`triage.lately`, `told.reactions`): an alert
   its own action caused, or a follow-up the user handled or declined, is `never`. Each item gets `say` (now / before / briefing / never),
   `critical`, a headline, optionally a task, and a reason code
   (`vet.REASONS`: changes-plans, reply-owed, deadline, money, safety,
   fyi), kept with its sentence in `verdicts` (`store.record_verdict`).
   Invented keys are dropped; forgotten items are `never`. Billed to
   background work's `triage` part and capped per day (`core/spend.py`). A
   reply the user owes and a meeting that just ended never keep a task,
   whatever the judge says (`triage.ask_first`, pinned in
   `tests/gate/test_attention_bounds.py`): they could need something from
   them, so the user is asked, and the ping names who was there so their answer in
   the conversation has a recipient.

   **Checks in code** (`agent/attention/vet.py`), around the call. Before it
   (`vet.before_judging`), what code already knows is settled as `never`
   with its reason: a message judged before under another key (a `mail:`
   row and the `thread:` row for it share the mailbox's id; a copy in another
   mailbox and the original share the RFC Message-ID or, arriving within
   two days, sender, subject and opening words), a second copy in the same
   batch, and a "reply the user owes" to a list or a platform. After it
   (`vet.after_judging`): **who sent it** (`vet.sender_rule`, the maintainer's rule of
   10 October: automated mail is FYI unless it changes their plans or is a
   payment with a stated date; a receipt for something the user did is
   nothing), **where it is** (`vet.location`: a weather alert only for home
   or a verified trip), and **whether it is still true** (`vet.fresh`: the
   event passed, the user corrected it, or `owed.closed_refs` has it closed).
   What each check did is kept in `verdicts` (`store.vetted`); a fault in
   the checks holds the item for the briefing rather than sending it.
3. **Prepare** — `prepare.prepare`: a `Conversation` on channel `initiative`
   (autonomous by `core.call_policy.is_autonomous`), `PREPARE_TOOLS` only —
   private and reversible, drafts but never sends — and, since 11 October
   2026, a browsing worker for a page that needs a real browser (flow 12,
   step 6; `workers.send_browsing`). A task is marked started
   before its turn. Tools used are the evidence; a turn that used none did
   nothing. For an event, the guests (`calendar.people_in`) arrive with
   what their contact ledger knows about each (`prepare.people_block`: who
   they are, the last three exchanges, what the user owes them), and a
   pre-meeting brief may run to three lines, where looking at their own mail,
   Drive or ledger counts as evidence. A brief names nobody it only guessed
   from a first name: `who_is` marks each match `exact` (a whole word of
   the name, address or firm) or `partial` (`people.match_quality`), and
   "Alex appears to be Alexandra Wren" -- the one ledger row with "alex"
   inside it -- is what a partial match did on 5 October. A brief is also
   written again, once: when it is more than a day old and its meeting is
   within a day (`store.refresh_briefs`, run by `prepare.prepare`), and as
   soon as a look at the same event lists different guests
   (`store.record_new`). Either way it waits for the doer like a task just
   judged, so it is not sent empty.
4. **Deliver** — `deliver.deliver`: what is ready is checked once more
   (`vet.recheck`, against what the user said since it was judged: "I'm in
   Denver", "Solve is done"), then goes out as one message through
   `notify_user` (flow 3, so it joins the conversation).
   Day: up to `MAX_DAY_PINGS`. Night (quiet hours, a panel knob): up to
   `MAX_NIGHT_PINGS`, and only what is still critical after the checks
   (pinned by the frozen gate); the rest wait for the morning. Delivered
   once it is in the outbox, and recorded line by line in `told`
   (`told.record`), said once the user has it (flow 3). A `now` item that
   could not go within three hours becomes briefing material; a pre-event
   brief for an event that has started is dropped.
5. **The briefings** (flow 7) are a view of what was held
   (`store.for_briefing`), checked again (`vet.recheck`): **Needs you**, the
   items whose reason needs them (`briefing.needs_user`), each with it, and
   where the obligations ledger will add what the user owes (`Facts.owed`); then
   **FYI**, at most `briefing.MAX_FYI`; then the night's report, a line
   each, what is due, the goals, spending and the news; and last the day
   and its weather, where the eye lands after the top. Each is marked told
   once it is in the outbox, and its lines recorded in `told`. A pre-event brief for a meeting
   before 9:30 is folded into the morning's, under the meeting.

"Why did you tell me that?" is `told.explain`: the line, when and how it
went, the observation and source, the place, the judge's reason and
sentence, and what the checks made of it.

`python -m agent.attention` runs one tick against a copy of the database
and prints what it would send, without doing tasks or sending anything.
`python -m agent.attention vet --dry-run --db <copy>` replays the checks over
the last fourteen days of a copy and says what they would have changed
(`agent/attention/replay.py`), writing nothing.

The user changes what news is followed in conversation: `follow_news`,
`unfollow_news`, `followed_news` (`agent/toolset/news.py` →
`attention/interests.py`).

**Tests:** `tests/unit/test_attention.py`, `test_attention_vet.py` (each
check against its incident), `test_attention_sources.py`,
`test_attention_threads.py`, `test_attention_feeds.py` (real captured payloads in
`tests/unit/fixtures/attention/`), `tests/gate/test_attention_bounds.py`
(what the doer may touch, the night rule, the caps).

**Limits:** nothing is noticed while the computer sleeps; a tick after
waking catches up (flow 2).

## 10. A draft in the user's style

1. **The model asks how they write** -- `writing_style`
   (`agent/toolset/mail.py`) before `draft_email` or `send_email`: the
   style guide learned weekly from their sent mail (`tools/style.py`), up to
   two emails they sent this person (`style.examples`), and what they have
   said about how they write (`facts.stated_style`), which beats both.
2. **The model writes the email** in that style, and the draft saved and
   the card they approve are the text it wrote.

A second pass that rewrote each draft in the user's voice was removed in
October 2026: it ran only from a voice file built once, outside the repo,
from one person's documents, and nothing in the product could build one.

## 11. What went wrong: the failure ledger

`agent/issues.py`, one row per kind of thing (`issues`, keyed by a
fingerprint with its numbers taken out, so a repeat counts up) and one per
occurrence (`issue_events`). Written, never raising, by the code that sees
each failure:

- `agent/loop.py` `Conversation.send`: a message from them that corrects the
  last answer ("wait, why did you…", `issues.is_correction`), with what it
  had said; a turn stopped in a tool-call loop; a reply over a minute
  (`SLOW_TURN_MS`) or one whose model call was retried; `_verified`, an
  answer that claimed what no tool did; `_broke`, a turn that fell over;
  `_store_tool_results`, a tool that reported an error (executors that
  raised arrive this way too, through `act`).
- `agent/attention/sources/__init__.py` `collect`: a source that failed.
- **A worker that died.** The supervisor keeps no database, so its
  watchdog's `core/supervisor.record_death` appends a note -- how it went
  (gone, or alive and not answering), Windows' exit code, the log's last
  lines -- to `data/worker_deaths.jsonl`. The next worker, taking up its
  duties (`worker/duties.take_up_duties`), turns each into a `worker_crashed`
  issue (`issues.record_deaths`), named by the frame faulthandler printed
  for the thread that faulted, and consumes the file.
- **Their thumbs on a reply.** `channels/bridge.py` `_handle`: a 👍 or 👎 on
  one of its own messages that is not a card is feedback, not an answer
  (`feedback_of`), handed to `answering.note_feedback`. Said in words,
  the `note_feedback` tool records it.
  A thumb on a ping or a pre-event brief also reaches the attention judge,
  matched to the lines that message carried (`told.reactions`).

- **Reflection** (`agent/reflect._file_proposal`): a conversation graded
  poor on a rubric dimension files its proposed change here, one issue per
  dimension, rather than as an `upgrade` job the day's job worker would
  claim before the night saw it.
- **The canary's count** (`core/canary.count`): the ledger is also what
  judges a new model. For a day after a model switch, its bad events per
  reply are counted against the two days before (`models_guard.watch_verdict`),
  and a clear rise puts the old model back. It judged every merged
  self-upgrade the same way until 13 October 2026, when Steve stopped
  merging its own changes; that self-upgrade went in step 6
  (docs/self-upgrade.md).

The morning briefing carries one line of it (`issues.briefing_line`):
"noticed about itself: 3 slow replies, 1 thumbs-down".

**The night works from it** when it builds modules
(docs/self-upgrade.md). `issues.candidates` offers the open issues seen in
the last two weeks, weighted so their own signals count most, leaving out
any tried in the last `RETRY_AFTER_DAYS` or `MAX_TRIES` times already. Each
attempt is noted (`issues.attempted`, table `issue_attempts`); none marks
its issue fixed, since a fix is a module the user has not yet turned on.
An issue that happens again after a fix is open again.

What it depends on is watched as an attention source instead
(`sources/health.py`), since those need saying the day they happen.

**Tests:** `tests/unit/test_issues.py`, `tests/unit/test_live_guards.py`.

## 12. A background task, split among workers

Since 30 September 2026. A conversation defers a long task
(`defer_to_background`); the job worker runs it in
`agent/worker/handlers.handle_research`:

1. **Jev decides whether it splits** (`workers.worth_splitting`): one
   predicate through `core/decide.py`, a calibrated P(the task splits into
   independent parts). At or above `SPLIT_AT` the lead is offered workers;
   below, the job runs as it always did. If Jev cannot be asked, the task is
   not split, and the `crews` row says why (30 September: offered workers
   with no answer, a lead split one question three ways).
2. **The lead** is the job's `Conversation` with a `Crew` and
   `workers.lead_note`: name the parts, start one worker per part at once,
   wait, and answer from their reports. Only a lead is given
   `start_worker`, `wait_for_workers` and `stop_worker`
   (`register_tools(lead=True)`), and not `defer_to_background`.
3. **Each worker** (`Crew._run`) is a fresh agent on `WORKER_ROLE`, with
   only `WORKER_TOOLS`, `WORKER_NOTE`, its own turn and tool limits and
   `WORKER_TIMEOUT_S`, and `Deps(worker=True)` with no crew. `actions.act`
   refuses any call of a worker's that is not free, private and reversible,
   and asks the user nothing: drafts and events without guests go through, an
   event with guests or any send does not.
4. **Bounded.** At most `MAX_LIVE` at once, `MAX_WORKERS` a task, and none
   once `CREW_BUDGET_USD` is spent; a replayed `start_worker` for the same
   task is the same worker. When the lead answers, `Crew.close` stops any
   still running. Their usage is billed to the job (`usage_events.job_id`),
   and a `crews` row keeps what Jev said and what the lead did.
5. **Out of calls, it answers.** At `WORKER_TOOL_CALLS` a worker's tools
   are taken away (`toolset.within_budget`, a `PrepareTools` capability on
   every agent `build_agent` makes, reading `Deps.tool_budget`) and it writes
   up what it has, marked as cut short; only `WORKER_BURST` calls past that
   is it stopped with nothing. Their own turns and the lead work the same way
   at `loop.HOP_TOOL_CALLS`, and a turn that needed them all is still a
   `turn_loop` row in the ledger.
6. **A page that needs a real browser** goes to a browsing worker
   (since 4 October 2026, in a task the user asked for, `browser.background` on;
   since 11 October also in a task the agent set itself, and from the
   doer's and the night's research turns, which `workers.send_browsing`
   gives a crew that may only browse -- each starting from what its own
   searches returned):
   `start_browsing_worker` with the pages, which must be ones the lead was
   shown. The worker (`Crew._run` with `Worker.sites`) has only
   `BROWSING_TOOLS` and `BROWSING_NOTE`, its own browser under
   `Deps.browser_key`, kept to those pages' sites by the driver, never
   logged in; the browser is closed and its place among `MAX_BROWSING` given
   back when it ends. A task that does not split still gets a crew for this
   (`Crew.split` False hides `start_worker`; `toolset.offered`).
7. **A site that refuses twice is left alone.** `Deps.refused` counts, per
   host, fetches answered 401, 403, 429, 451 or LinkedIn's 999
   (`tools/web.note_refusal`); from the second, `read_page` and
   `search_and_read` skip that host for the rest of the task
   (`tools/web.refused_before`). The lead and its workers share one count
   (`Crew.refused`); nothing is kept past the task.

The answer is announced like any job's (flow 3).

**Tests:** `tests/unit/test_workers.py`, `tests/unit/test_tool_budget.py`,
`tests/unit/test_web.py`;
`tests/gate/test_workers_stay_private.py` (frozen: what a worker may do,
and that no worker starts one).

## 13. A page in the sandboxed browser

Since 1 October 2026, switched by `browser.enabled` in `core/policy.yaml`
(on, for their own conversations). What holds the line is the browser add-on in docs/security-model.md; this is the path.

1. **Only their own conversations, only when on.** `toolset.offered` hides
   the `browse_*` tools from the model when the browser is off or nobody is
   watching (a worker, the doer, the night, a job), and each tool refuses
   the same way before anything starts (`toolset/browser.refusal`). The
   one exception is a clean-room browsing worker (flow 12, step 6).
2. **A session per conversation** (`agent/browser/session.session_for`):
   `core/browser/sandbox.start` brings up the network, the gate and the
   browser, in a thread; the driver is a `docker exec` subprocess
   (`DriverChannel`), told the gate's address by `start`.
3. **Each step** is one driver command (`BrowserSession.ask`, which never
   raises): `browse_open` (the URL through `egress.refusal` first, then the
   gate's rules on every request), `browse_read` (text and numbered
   elements, read as the injection screen allows, `browser_page`, as is the
   title after any step that lands on a page), `browse_click`,
   `browse_scroll`, `browse_back`, `browse_screenshot` (kept under
   `data/artifacts/browser/`, never handed to the model), `browse_search`
   (a search box only, `driver.is_search_field`). Each is a `browser_steps`
   row. `browse_read(whole=True)` scrolls to the page's end first, for
   sections that load on scroll, and `start` reads past the first 8,000
   characters. What a page did is said in words (`toolset/browser._plainly`):
   a refusal (`blocked`, from the answer's status or a bot wall's title), an
   empty page, and in a logged-in browser a redirect loop or a sign-in wall
   as their saved login gone stale. `browse_open`'s description names their
   saved logins (`toolset/browser.with_saved_logins`), and `read_page`
   refused on one of their sites points to the browser (`login_hint`).
4. **It ends.** `browse_close`; or `background.browser_sweep` (every minute)
   closes one idle `IDLE_S` (three minutes) or older than `MAX_AGE_S` -- or one
   whose page has died (`session.browser_gone`; `alive()` is false) -- unless a card holds it
   (`session.hold`) -- reaps a dead worker's leftovers, and removes
   screenshots after `SCREENSHOT_DAYS`; the worker closes all of its own as
   it stops. At close the gate's log of every host contacted is kept in
   `data/logs/browser/`.
5. **Filling in a form is a card** (`browse_submit`). The driver describes
   the fields and button (`describe`) -- nothing is typed -- and the tool
   refuses a payment-looking page, a plan `driver.check_plan` rejects
   (a password or file field, say). Otherwise `actions.act` raises the
   card: the site, each field and its value whole, however long, and the
   button. The session
   is held for their answer. After their yes (flow 4), the `browse_submit`
   executor sends the stored plan to the driver, which checks the page is
   still the one the user approved -- the address, every label, no payment --
   before typing and pressing; a page that changed, or has closed, is a
   failure the user is told.
6. **Logged in as them.** Since 4 October 2026 the user usually saves a login
   from the desktop app: Browser logins, "Open sign-in window" opens the
   site in a private window of the app's own (`open_signin`), every page it
   passes through fills the login's domains, and Save hands its cookies
   (`signin_cookies`) to `live.import_login`, which keeps only the login's
   domains' cookies, each put back on its domain (the window's cookie
   library drops the leading dot), and only from a page on them. Closing the window is
   "done": the shell reads it (`signin-finished`) and the panel saves, with
   the username and password the user typed into the site's own form
   (`capture_script`, kept only if typed on the login's domains). A saved
   login's rule -- ask first, or just use it -- changes in the list
   (`live.set_tier`) without signing in again. Or the user saves one in the live view
   (`agent/browser/live.py`): their clicks and keys reach a sandbox browser,
   and "save login" writes its name, domains, tier, session and (if the user
   types them into the form) username and password to the vault
   (`core/browser/vault.py`). Then `browse_open` on one of its pages opens
   the conversation's browser for that login (`session.session_for(conv,
   site)`), from the saved session and kept on its domains -- for a login
   The user marked `ask`, only after `browse_use_login`'s card (their yes lasts 30
   minutes, `session.approve_login`). `browse_login` has their username and
   password typed and the sign-in button pressed (`session.fill_login` ->
   the driver's `fill_login`), unseen. A click there runs
   `core/browser/clicks.rule`; one that does something becomes a
   `browse_act` card, carried out by `driver.cmd_act` after their yes. When
   it closes, its refreshed session is saved back if it is no worse
   (`session.kept_state`): the login's own cookies, one of each, and
   nothing from a browser marked stale or signed out.
   `python -m agent.browser.doctor logins` shows what each saved login
   holds -- names, domains, scope, expiry, twins -- never a value.
7. **Files.** `browse_files` lists the browser's downloads and what may be
   uploaded; `read_download` copies one to the quarantine and reads its
   text in the no-network analysis container (`tools/code_exec`, image
   `steve/pyexec`, with pypdf); `browse_upload` raises a card for a file from
   the outbox, quarantine or artifacts (`core/browser/files.py`), and its
   executor uploads the same bytes after their yes.
8. **LinkedIn** (8 October 2026). `toolset/linkedin.py` is read-only and
   offered (`linkedin.offered`, from `toolset.offered`) only where the
   browser is and a saved login covers linkedin.com. A tool builds its
   addresses from validated parts -- a slug matched strictly
   (`profile_slug`, `company_slug`), keywords encoded (`_url`) -- on
   https://www.linkedin.com alone, checked again just before each open
   (`on_linkedin`). `_ready` refuses where the browser may not be used, while
   the tools rest after a refusal, without a login, for an `ask` login whose
   `browse_use_login` card has not been answered (`browser.needs_user_yes`,
   the rule `browse_open` uses) and past the hour's cap; then `_visit` takes
   a turn from the `Pacer` (a few jittered seconds after the last load, thirty
   an hour across the worker, kept in `data/linkedin_pacer.json`), opens
   through `browser._do` -- the same logged-in browser, recorded as
   `browser_steps`, replaced by a fresh one after `RECYCLE_AFTER` loads --
   scrolls the pages to their end, reads (the rest of a page past the
   driver's 8,000 characters is read on with `start=`, not loaded again,
   `Page.reads`), and `_check`s the page. A profile is one load unless a
   section says "Show all" or Experience was never drawn. The read is
   screened as `browser_page` by `_do`. A refusal of any kind (sign-in page,
   checkpoint, redirect loop, 401/403/429/999, empty page, a sign-up or
   security wall, a page of another kind than asked) stops the call, marks
   the browser stale so nothing it holds is saved over their login, closes it
   (`session.close_site`), rests the tools for `HALT_S` (or until the user saves the
   login again) and says their saved login needs signing in again, in the
   sandbox's live view. A browser that dies instead (`TargetClosedError`) is
   not a refusal: `_visit` loads the page once more in a fresh one.
   `browse_open`, a click or a search in the LinkedIn browser, `read_page`
   and `search_and_read` leave linkedin.com to these tools
   (`browser.linkedin_refusal`). The model sends no click and no typing, so
   nothing here can message, connect, follow or post. Calls take turns
   (`toolset/pace.py`, group `linkedin`) and a repeat in one turn is answered
   from the first. There is no persistent browser profile: the vault holds
   the login under DPAPI and every browser starts empty (flow 13, step 6).

**Tests:** `tests/unit/test_browser_session.py`,
`tests/unit/test_browser_submit.py`, `tests/unit/test_browser_live.py`,
`tests/unit/test_linkedin.py`, `tests/unit/test_browser_patchright.py`;
`tests/gate/test_browser_consent.py` (frozen: off means off, unwatched
means no, a hijacked model gets a card and never a keystroke, `ask` means
asked, judged clicks), `tests/gate/test_browser_files.py` (frozen: the
quarantine and uploads), `tests/gate/test_browser_logins.py` (frozen: the
vault and where a login may be typed) and `tests/gate/test_browser_contained.py`
(frozen: the sandbox).

## 14. A goal, worked on over days

1. **The user sets it.** In a conversation, "find three times next week for a call
   with Alex" becomes `start_goal`: their words, what done looks like, a plan
   of steps (`agent/goals.create`). One that may email or invite people
   itself raises an `approve_goal` card first, like a routine.
2. **A session.** `background.goal_loop` claims a due goal once a minute at
   most (`goals.claim`, never in quiet hours) and queues a `goal` job;
   `handlers.handle_goal` runs it as a conversation on the `goal` channel
   with the whole record in its note (`goals.session_note`): plan, findings,
   their notes and answers, its questions, whose mail it waits on, and the
   actions earlier sessions took or asked for, with how each came out.
3. **It works.** It reads, drafts, and records progress with `goal_update`
   (`goals.apply_update`). A send is `act` -> `monitor.goal_gate`: free only
   inside an approved envelope, otherwise a card (flow 4).
4. **After.** `goals.after_session` keeps its cost (what its usage was
   billed), the actions it took, and puts it to sleep if every step waits on
   them. If its reply is new, or it asked a question, the user gets one message
   (`handlers.tell_goal` -> `goals.message`), recorded in their conversation as
   `[goal N "title", status] ... [question 1: ...]` (flow 3).
5. **The user answers.** Their reply reaches their conversation's model, which passes it
   on with `answer_goal`: recorded as their answer or note, and the goal's next
   session is due at once.
6. **Woken sooner** when a card it raised has been answered and run
   (`handle_action` -> `goals.woken_by_action`), or when mail comes from
   someone it waits on (attention's `look` -> `goals.woken_by_mail`) -- not
   within 30 minutes of its last session.
7. **It ends** when a session says done (the user is told the outcome), when the user
   drops it, or at its deadline (`goals.expire`; told once, after quiet
   hours, held until then on the goal: `goals.owe`). Live goals are a
   line each in the morning briefing (`goals.briefing_lines`).

**Tests:** `tests/unit/test_goal_sessions.py`; `tests/gate/test_goals.py`
(frozen: the envelope, who may set or steer a goal, what a session may
change, and STOP GOALS).

## 15. Mail to the agent's own mailbox

1. **The user writes to it.** The user forwards a mail to the address the app
   shows, one of their mail accounts' with `+agent` ("deal with this"), or
   CCs it into a thread. It lands in that account like any mail.
2. **Read.** `background.mailbox_loop` searches the account for mail `to:`
   the alias every two minutes, through the slot; `mailbox.poll` (on its own
   connection) reads the user's Sent folders for copies sent to it, fetches
   each new one's headers (`MailProvider.headers`, the topmost of each name)
   and asks `core/mailbox.from_user` whether it is theirs: from an address
   they opted in, with a copy in their Sent folder, or passing DMARC where the
   provider ties the From to its sign-in, not sent by the agent, and not a
   Message-ID already taken. It is recorded once as their instruction (read
   from the Sent copy), a reply in their thread, or ignored. Mail in their
   name only minutes old waits for its Sent copy.
3. **Their instruction.** `mailbox.take` reads it, splits their note from what the user
   forwarded, and asks a model -- shown the note only -- whether it is a task
   or a goal. A goal is started with that plan and the user is told; a task is a
   `mail` job. Then, if the user wrote to the mailbox alone, it is archived.
4. **The task.** `handlers.handle_mail` runs a conversation on the `mailbox`
   channel with their note as the instruction and the mail as material,
   screened; it may read, draft and raise cards, and never write to its own
   mailbox (`core/monitor.mailbox_refusal`). Its answer is announced to them
   (`handlers._phone_text`), retried until it lands.
5. **Replies in their thread** come round as step 2 again, as `thread`: a task
   that drafts their reply, sent only by card.

**Tests:** `tests/unit/test_mailbox_intake.py`; `tests/gate/test_mailbox.py`
(frozen: whose mail is theirs, and the agent never writing to its own mailbox).

## 16. "Hey Steve", at the desk

The maintainer, 5 October 2026: an always-on assistant at the laptop that the user can
switch on and off, cheap, and without everything the user says going to a model.
Rebuilt on 9 October 2026 after two sound tests in which the hand-over
between the local voice and GPT-Live failed every way it could
(docs/desk-voice-design.md): one state machine now owns the desk, one owner
at a time holds the speakers, and a live conversation opens only when the user
asks for one by name.

**Not offered in v0.1** (step 7): the switch refuses unless a developer sets
`STEVE_HANDS_FREE=1` (`agent/worker/desk.offered`), and the app shows no
control for it. It is rebuilt, and measured against a bar, in its own step
after v0.1. Push-to-talk is the desk's voice: it
hears and speaks through OpenAI, or through the add-on's local models when
the user chooses them (`agent/worker/links._local_voice`).

1. **The switch.** A post to `/voice/listen` -> `agent/worker/routes.voice_listen`
   -> `agent/worker/desk.switch`, kept in `data/listening.json`. A request
   may send `{"toggle": true}` and let the worker decide which way (a
   client's copy of the state can be stale); every request is logged with
   which control sent it, and switching off plays two falling notes. Off until the user
   turns it on. The ear opens and closes in a thread, never on the event
   loop. It is a worker duty: `agent/worker/links.warm_voice` opens it once
   the speech models are warm, and `agent/worker/duties.give_up_duties` closes
   it (`desk.stop(reason="swap")`) before a swap's new worker opens its own.
   The models load at start only when something needs them then; otherwise
   on first use (`agent/worker/links.ensure_voice`). Whisper is run once as
   it loads and again whenever hands-free opens (`voice/stt.Transcriber.warm`,
   `agent/worker/desk._warm_whisper`, in a thread): cold, the first "hey
   Steve" of 9 October 2026 took 2.4 s to transcribe.
2. **The ear** (`voice/listen.py`). `Ears` opens Windows' default microphone
   -- following it when the user plugs another in -- and the speakers' loopback,
   both through WASAPI (soundcard), at 16 kHz, each on its own thread; the
   microphone stays closed while the screen is locked or the loopback is down
   (`Ears.check`, `core/desk.may_listen`), and says so in the log (`ear: mic
   open|closed reason=...`). `Ear` keeps 1.6 s in a ring buffer and runs the
   wake detector (`voice/wake.py`); what else it may hand on is a mode the
   engine sets (`EarMode`: a follow-up window, a note's stretches, a live
   conversation), and without one it hands on nothing but a wake and what
   followed. A near miss of the detector -- 0.4 up, and not the windows
   just after a wake, which score high as the phrase passes -- is a line in
   the log (`ear: near wake score=... over=voice|quiet`), as are blocks it
   lost. While the desk's own voice is audible, the bar for the phrase is
   lower (`voice/listen.over_voice_bar`: the bar the head was measured to
   hold over the desk's voice, or 0.7 against 0.9 for an older head; the
   echo canceller ducks their voice under its own), the laptop's own sound is
   checked at the same bar, and a second detector scores the microphone
   before the canceller, logged beside the cleaned score (`ear: over its
   voice the raw microphone scored ...`); at the bar it is a wake too
   (`voice/listen.RAW_WAKES`), since the canceller can hide their phrase
   under its voice and a loud room can drown the raw one.
3. **The engine** (`voice/engine.py`). The desk is one state -- off, paused,
   idle, capture, transcribe, local, follow, live -- changed in one place
   (`Engine._to`, logged `desk: a -> b why=...`) on the event loop. Every
   turn is an object (`Turn`) that holds the speakers while it runs
   (`voice/floor.py`): only the floor's owner is played, so a turn that has
   been cut cannot make a sound and two voices cannot answer one turn; what
   was audible is measured from the samples played, not inferred from a
   queue. The speaker callback only copies what is queued and notes what it
   played (`voice/audio.Player._callback`); the floor reads that off the
   audio thread, and the canceller is fed from a thread of its own. A stream
   that falls behind keeps its queue and is logged (`floor: the speaker
   stream is late`); one that has stopped -- 5 s, or it says so -- is
   reopened in a thread with what it held dropped (`Floor.check`); and once a
   minute the callbacks' worst gap and run are logged when they were late.
   Every mode has a longest stay; one that outlives it is logged and left.
4. **A wake.** If the loopback's detector heard it too, it is the laptop's
   own sound and ignored (`core/desk.wake_counts`). Otherwise a soft tick
   plays -- the user can hear they were heard -- whatever is talking stops, a live
   conversation ends, and the pre-roll and what follows are gathered until
   The user has finished (the end-of-turn judge, `voice/turns.py`, or 0.8 s of
   quiet) and handed on as a `Heard`, with how much speech the laptop played
   meanwhile. Until the user has said something after the wake, quiet does not end
   it: the user may be waiting for the tick, so it waits `voice/listen.WAKE_WAIT_S`
   (3 s) for their request to begin, and only then is handed on as the phrase
   alone (`Heard.bare`).
5. **The turn** (`agent/worker/desk.DeskShell`). Whisper transcribes it
   locally and the audio is dropped. `core/desk.wake_phrase` must find "hey
   Steve" at its start or it is a false wake, dropped unread. The phrase alone
   gets a chime and a moment to ask -- no chime if the user has already begun: what
   The user starts saying while a bare wake is being transcribed is gathered and
   held for Whisper's verdict (`Engine._verdict`), then answered if the
   phrase was there and dropped unread if not. "Stop listening" and "never mind" are
   answered by code. **A cut-in** -- the wake came while the desk was
   answering (`Heard.barge_in`) -- has its own words taken out first
   (`core/desk.own_words_removed`: what the desk was saying, come back
   through the microphone, is not theirs); a request that is wholly an order to
   be quiet ("stop", "that's enough") is answered by nothing
   (`agent/worker/desk.cut_in_stop`), and one with no word in it is dropped
   (`agent/worker/desk.cut_in_noise`). A yes to a card counts only with the
   laptop quiet (`core/desk.approval_counts`), otherwise it is sent to the
   screen. "Let's talk live" opens a live conversation (step 8). Anything else
   is `VoiceSession.respond` -> `agent/worker/answering.voice_reply` on channel
   `voice` -- the hotkey's thread, rights and approval intercept -- spoken
   locally as it is written (`voice/speaking.py`), never saying the phrase
   (`core/desk.never_spoken`), through the turn's hold on the speakers. A
   lookup that runs long gets "Still on it." at 10 s. What the assistant asks
   in the turn -- notes of the room -- is recorded on the turn
   (`agent/runtime.desk_turn`) and acted on when it is over, never later.
   Push-to-talk (the hotkey held) runs through the same engine
   (`desk.hotkey_turn`).
6. **A follow-up.** For 10 s after a reply has been heard and its echo died
   (`core/desk.FOLLOW_UP_S`), speech starts a capture without the wake word;
   it counts only if the laptop was not itself talking
   (`core/desk.capture_counts`). This is the back-and-forth: a conversation
   in the local voice, for the price of its turns. With a note of the room
   open there is no follow-up window -- what the room says is the note's,
   and a lecture never reaches a model -- so each request then starts with
   "hey Steve". Speech over the desk's
   voice without the phrase is not a request (`core/desk.barge_in_counts`,
   off until the echo canceller has been measured on their speakers).
7. **Notes** (`agent/notes.py`), answered by code with no model: "note that
   ..." is kept as said; "take a note" starts a dictation -- the ear hands on
   every stretch of speech -- written into the note until "end note" or two
   minutes with nothing said (`agent/worker/desk._watch`); words that only
   name notes -- a reminder, a plan, a question, a time that is not now --
   start none (`_NOT_NOW`) and go to the assistant; "take notes on this
   lecture" does the same into a transcript file under data/notes/, until
   "hey Steve, stop taking notes" or three hours, then `agent/notes.summarize`
   -- one call with no tools -- writes a Google Doc. A note under way carries
   on across a worker swap. The user reads and deletes them on the day's page, as
   listening notes; their assistant finds them (`find_notes`). **A call or a
   video** ("take notes on this call", the tray's "Take notes on what's
   playing", or Settings' "Take notes on what the laptop plays")
   is heard on its way out: `voice/listen.Tap` reads the laptop's output
   (WASAPI loopback, any volume, no microphone needed, hands-free on or off)
   and `Segmenter` cuts it into stretches, written as "Them:" lines
   (`agent/worker/desk.tap`, `agent/worker/desk._segment`); with hands-free on,
   their side is added from the mic as "Me:", and the call heard back through
   their speakers is dropped. It ends when the user says, after ten minutes with
   nothing said (the user is told on Signal), or at four hours; then the same
   tool-less summary. Started from the app, nothing is said aloud.
8. **A live conversation** (`voice/desk_call.py`), opened only when the user asks
   for one by name -- "let's talk live", "talk live", "switch to live", "go
   live" (`agent/worker/desk.live_asked`), never a bare "let's talk": the user's
   choice, 9 October 2026 (who may open one is `LIVE_OPENED_BY` in
   `voice/engine.py`; the assistant's `talk_it_through` is not offered under
   it) -- and only within the day's minutes (`DESK_LIVE_MIN`, a knob). It
   starts with the desk thread's recent turns as GPT-Live's own history
   (`session.input`), never as words to say, so nothing the local voice said
   is said again. Three rising notes play the moment it is asked for, before
   the connection is up. Asked with a topic, it is told to answer it
   (`voice/desk_call.ANSWER_NOW`); asked with none, to say it is listening
   (`GREET`) -- told only to answer, on 9 October 2026 it had nothing to
   answer and said nothing for eight seconds. Silent 4 s after the user asked, it
   is told again; silent 10 s after it opened, it is closed, their words are
   answered by the local voice, or the user is told it did not answer. Each
   conversation's end is its own (`LiveCall.end_reason`). Its frames go
   to the speakers through the floor, where only audible ones are its voice,
   and its own transcript is the second witness (`LiveCall.talking`). The ear
   streams every block to it (full duplex on the speakers only while the echo
   canceller works, asked for every block; otherwise its input is muted while
   its voice is audible), and also hands their stretches of speech to local
   Whisper (`Words`): when GPT-Live delegates, those words -- never its
   transcript of the room -- go to `agent/worker/desk.hand_over`, where a yes
   counts only with no other laptop audio and only if the user finished saying it
   after the conversation's own voice and its echo tail
   (`core/desk.heard_user`), then to `agent/worker/answering.voice_reply` on
   channel `voice`. It ends after 20 s with nothing from either side (its
   voice, them by the microphone's voice detector outside its voice, work in
   hand), after 45 s with no word from either, at 15 minutes or the day's
   limit, when "hey Steve" is said (which reaches the engine in every state),
   at the hotkey, or when hands-free is switched off, the screen locks or a
   swap takes the microphone. However it ends, what the user said that nobody
   answered is answered by the local voice -- or, the desk going off or their
   new request taking over, written into the desk's thread as theirs -- and an
   answer the assistant was still working on is said by the local voice.
   Its minutes are one usage row, made as it starts and kept up to date
   every 30 s, on the desk's thread; its transcript is kept there as a
   `live_conversation` row.
   **The log** has one line per hands-free turn (`hands-free turn:
   outcome=... source=... ended_by=... asks=... stt_ms=... first_audio_ms=...`)
   and one per live conversation (`a live conversation ended: reason=...
   handed_over=... unanswered=... frames_s=... audible_s=... events: ...`):
   numbers and outcomes, never words, so a sound test can be read afterwards
   (`DeskShell.log_turn`, `DeskShell.live_ended`).

**Tests:** `tests/gate/test_desk.py` (frozen: nothing gathered before a wake,
the laptop's own wake ignored, no model without the phrase, no follow-up or
yes over the laptop's speech, the desk's own words not a request, speech over
its voice not a request unless switched on, the phrase never spoken, the mic
closed while locked, no sound written; notes reach no model and a lecture's
summary has no tools; a conversation's audio is open to it on the speakers
only while the echo canceller works, its silent frames are not its voice,
"hey Steve" ends it, a yes it hands over needs speech outside its own voice,
and it hands over only their words, as the desk's own Whisper heard them);
`tests/unit/test_desk_scenarios.py` (the whole desk against a simulated room,
laptop and GPT-Live on a virtual clock, `tests/unit/desk_world.py`: both
sound tests of 9 October 2026 replayed, each of that day's failures on its
own, and properties over random rooms); `tests/unit/test_desk_engine.py`,
`tests/unit/test_listen.py`, `tests/unit/test_desk_call.py`.

## 17. Something the user owes, or asked for, and what closed it

The obligations ledger (`tools/obligations.py`, table `obligations`): their
commitments from sent mail, and the tasks the user dictates (`add_task`).
The `commitments` table was carried over with its numbers on 10 October 2026
and is no longer written; `tools/commitments.py` is the view the night, the
briefings and the people tools read. It stays on this computer: the rows
name the people the user owes.

1. **Kept** by the night's extraction (sent mail only) or by `add_task`.
   Each has a `source_ref` or source id, so the same thing twice is one
   row.
2. **Closed by code**, each saying what closed it (`closed_by`) and what came
   of it (`outcome`):
   - their words, before the model reads the turn (`Conversation.send`): a
     short message
     saying something is done or not needed that names exactly one open item
     by a word only it has (`closing.from_words`). "Solve is done" closes the
     Solve task; "both complete" names nothing and closes nothing;
   - their YES or NO to a card asking whether the user still wants it
     (`closing.keep`, `closing.declined`);
   - a tick on their tasks list, and the other way: one closed here is ticked
     there (`taskmirror.reconcile`, hourly; `closing.untrack_soon` at once).
   The model closes one on their word with `commitment_done`. Nothing closes an
   item on an inference from their mail.
3. **Read** by the next turn as one view across the ledger, reminders and
   cards: what is open, and what closed in the last day and how
   (`closing.open_now`).
4. **Asked about** when it has gone quiet (the maintainer, 10 October 2026: reach
   out more, and ask when it needs them): a dated item a day past its date,
   or an undated one that has sat for days (`closing.worth_asking`). The next briefing carries the question under
   "Needs you", each a card made quietly (`closing.briefing_questions`,
   `approvals.queue`): YES keeps it, NO drops it. At
   most `settings.MAX_QUESTIONS_PER_DAY`; never sooner than three days after
   the last ask, and twice at most. No answer is not a yes.

Reminders and cards are not copied into the ledger: each has a table that
already knows when it went off or was answered.

**Tests:** `tests/unit/test_obligations.py`, `tests/unit/test_commitments.py`,
`tests/unit/test_taskmirror.py`, `tests/gate/test_sent_mail_only.py`.
