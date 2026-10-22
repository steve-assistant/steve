# The journal: the main window

The main window is one journal, one page per day. Everything Steve and the user said and did that day is on it, in order, whichever channel it came through: the app, the quick bar, Signal or voice.

## Layout

```
┌──────────────┬───────────────────────────────────────────────┬───────────────┐
│ the mark     │  Tuesday                         All Talk Log │               │
│ Steve        │  14 October                                   │   ◆ Needs you │
│ Reading mail │                                               │   ┌─────────┐ │
│              │  ◇ 07:40  Good morning, Maya. Three things…   │   │ Send the│ │
│ Search       │                                               │   │ reply to│ │
│              │     09:12               ┌──────────────────┐  │   │ Dana?   │ │
│ Today      2 │                         │ can you move my  │  │   │ Yes  No │ │
│ Yesterday    │                         │ 3pm to thursday  │  │   └─────────┘ │
│ Sunday       │                         └──────────────────┘  │               │
│ Saturday     │  ◆ I moved "Design review" to Thursday 3:00.  │               │
│ October      │                                               │               │
│              │  ◇ 10:05  Theo's flight lands at 18:40…       │               │
│ Ongoing      │                                               │               │
│ ◇ Flights    │  ──────────────────────────────────────────── │               │
│ ◇ Tax return │  Write to Steve                       ⊕  ●    │               │
│              │                                               │               │
│ Maya       ▾ │                                               │               │
└──────────────┴───────────────────────────────────────────────┴───────────────┘
   sidebar 240         the page: measure 38em, gutter 72           margin ≥ 288
```

| Window width | Layout |
|---|---|
| 1180px or more | sidebar, page, and the margin for notes |
| 760 to 1180 | sidebar, page; notes pile at the top of the page |
| under 760 | the sidebar folds to a 64px rail (the mark, Today, search, menu) |
| minimum 420 × 560 | one column; the rail opens as an overlay |

- **Title bar:** the native Windows title bar, its colour set to `desk` (Windows 11 allows a caption colour). It stays native rather than custom-drawn, which keeps snap layouts and the system buttons working as people expect.
- **Default size:** 1100 × 760, remembered.

## The sidebar

On `desk`, from the top:

1. **The mark** (40px), with "Steve" beside it in `title`, and below that one line in `ink-faint` saying what Steve is doing: "Reading new mail", "Listening", "Asleep since 23:50", "Idle". States are in `presence.md`.
2. **Search** (Ctrl+K).
3. **The days.** The day in view sits on `paper-deep`: the newest at the foot of the page, otherwise the one across the window's middle. Clicking Today, or Ctrl+T, goes to the foot of today's page, its newest exchange, wherever the page was. Today (with the unread count), Yesterday, then the weekdays of this week, then earlier weeks folded by month. It's the familiar list of past chats, with days in place of chats. A red diamond with a number beside a day means notes from that day are still waiting.
4. **Ongoing.** Goals Steve works on over days, routines and watches, and background tasks running now, one line each, with their signifier. Clicking one shows the journal filtered to that thread, across days.
5. **Voice at hand**, two switches above the name: **Talk**, and **Call notes** once the voice add-on is installed. With Talk off, a hold of the hotkey or the desk card is a tap, and the composer has no microphone. Without the add-on, Settings says what call notes need instead of offering them.
6. **The user's name** at the foot, opening the menu to the back pages (`back-pages.md`). No avatar.

## The day page

### Heading

- The weekday is in real handwriting ("Tuesday", or "Today" and "Yesterday"), about 40px tall. The date is set below it in `small`.
- **The lenses** sit on the right of the heading, as plain words: All · Talk · Log.
  - **All:** everything.
  - **Talk:** what the user and Steve said to each other, typed or spoken.
  - **Log:** what Steve did and noticed on its own.
  - Ctrl+1, 2 and 3. Notes show whatever the lens.

### Where a day begins

A page turns at 4 a.m. in the user's zone, so a conversation at 1 a.m. stays on the day it belongs to. An open window doesn't jump when the page turns. The new day starts above the composer, and yesterday folds once the user scrolls past it.

### Scrolling back

Scrolling up past the top of a page reaches the day before, folded into a band of `paper-deep`:

- the weekday and date;
- one line: the first sentence of that evening's briefing, or, without one, the count of exchanges and things done;
- the count of entries.

Clicking a folded day opens it in place. Days are loaded as they come into view, so a year of pages costs nothing until it's read.

## Entries

Each entry has its time in the gutter (`meta`, `ink-faint`), shown once per exchange rather than on every line, and its signifier (`foundations.md`). A channel other than the app is named beside the time: "Signal", "voice", "quick bar".

### Briefings

- **Morning:** the page's opening, written as a short letter in `body`. Its items are a list with outline diamonds as bullets. An item that needs an answer has its note in the margin, and a small arrow mark from the item to the note.
- **Evening:** closes the page the same way.
- **The end of a page** is the dinkus: the three diamonds in their colours, small and centred (`foundations.md`).
- A briefing held because the computer was asleep goes where it was delivered, not where it was due. Its time says both: "08:00 · sent 09:14".

### Conversation

- **The user's words:** on a `slip`, aligned right, at most 75% of the measure, with corner `r-slip`. No tail and no avatar.
- **Steve's words:** unboxed, at the measure, in `body`. Plain text on the page is what makes Steve read as a writer rather than a chat partner.
- **Steve's formatting** within a reply:
  - lists with outline diamonds;
  - tables with hairline rows and figures in `code`;
  - code on a `slip`;
  - links underlined 1px in `ink-soft`, 3px below the text;
  - a research answer's citations as small raised numbers in `meta`, with its sources listed below in `ink-faint`;
  - the hand's marks in the reply's own ink, on what the reply bolds or quotes (`ui/src/Prose.tsx`): a line under a bold of up to 48 characters, and a bracket down a quote's left side. Until 19 October a short bold was circled in `red-ink`; the circles crowded the words beside them and read as corrections. Three at most a reply, the first in reading order; past them bold stays bold and a quote keeps a hairline. They draw in on a reply not yet seen and are simply there on one already read. The quick bar's answers carry them too; search results and updates don't.
- **While Steve works on a reply:** one line where the reply will be. It shows the turning outline diamond and what Steve is doing when the worker says ("Checking your calendar"), otherwise nothing but the diamond. No dots.
- **A photo:** shown as a print, up to 240px wide, with a 4px `note` border and `lift-note`, untilted. Click to see it full size.
- **Reacting to a reply:** hovering over one of Steve's replies shows two marks in the gutter, a check ("right") and a cross ("not right"). These are the 👍 and 👎 the failure ledger reads (`agent/issues.py`). The chosen mark stays drawn.

### Spoken exchanges

A conversation by voice is long, so it folds.

- **Folded:**
  - the block on `tint-cyan`, with the cyan diamond;
  - "Spoken · 4 min" in `meta`;
  - beneath, a summary of one or two sentences in `small`, for example: "Talked through Friday. You'll take the 8:10 train, and I'll remind you tonight to book a table for Saturday." Without a summary yet, it shows the first thing the user said.
- **Opened:** the whole transcript, in the conversation's style one size smaller, with each line's time.
- **Anything done during it** (a reminder set, an event moved) is listed under the summary as done entries, so it's visible while the talk stays folded.
- **A voice note sent on Signal** is spoken too. It folds the same way, with its transcript.

### What Steve noticed (updates)

Updates are the outbox's notices: a heads-up before a meeting, a flight that moved, an email worth knowing about.

- One line or two in `small`, `ink-soft`, behind an outline diamond.
- Hovering shows "why?" in `ink-faint`. It opens Steve's reason inline: what it saw, from where, and why it told you. That is "why did you tell me that?" (`agent/attention/told.py`).
- **Several in a row** while the user was away are gathered under a bracket mark in the gutter: "While you were away · 5". The first two are shown, the rest folded.
- **A question** (for example "How did lunch with Sam go?") is an update, not a note: answering is welcome but nothing is lost if it isn't. A "Reply" text button under it puts the cursor in the composer, quoting it.

### Done, failed and undo

- **Done:** a filled diamond and one line: "Added 'Dentist' to Thursday, 15:00."
- **Undo:** while an action's hold lasts, "Undo · 0:24" sits beside it in `meta`, counting down, and disappears when the hold ends.
- **Failed:** a drawn cross, the first words in `red-ink`, and the reason and what to do in `ink`: "**Couldn't send.** Gmail refused the sign-in. Sign in again in Accounts."

### Background tasks

A task Steve runs in parts (flow 12 in `docs/flows.md`) is a block:

- its name in `title`, with the turning diamond while it runs;
- its parts as a short list below, each getting a drawn check as it finishes;
- when it's done, the block folds to a done entry with the result's first line, and opens to show the parts and the full result.

The night's work folds as a spoken exchange does: "Overnight · 4 min" in `meta`, on `paper-deep`, and beneath it two sentences read from the night's report: what it did, then what it took or why it stopped early. Opened, it lists the report's parts under their headings.

A goal worked on over days adds one entry to each day it moved, and lists them all under Ongoing.

### Asking for a yes

When Steve needs a yes, the note goes in the margin (`notes.md`), and the stream gets one line where it arose, with a small red diamond: "Asked you about sending the reply to Dana", plus an arrow mark toward the note.

Once it's answered, the line becomes what happened: "You said yes · sent 10:42", or "You said no", or "Expired · nothing was sent". If the answer came from Signal, that's said too ("You said yes on Signal").

### Asleep

When the computer slept, a quiet divider marks the gap:

- a short drawn wave, with "Asleep 23:50 – 07:12" in `meta`, centred;
- what Steve caught up on after waking is gathered under it, like updates;
- a reply late because of the sleep keeps Steve's own "sorry for the wait" line (`agent/awake.py`).

### New since you last looked

Entries that arrived while the window was hidden get a yellow highlighter swipe (`highlight`) behind their first line. A drawn shape, not a rectangle. Each fades out over a second, three seconds after it has been on screen. Apart from the dinkus, it's the one place yellow appears on the page.

## The composer

At the foot of the page, the width of the measure, on the page itself. No box: a hairline (`rule`) above it, then the text.

- **Placeholder:** "Write to Steve", in `ink-faint`.
- **Enter** sends, and **Shift+Enter** adds a new line. The send arrow mark appears at the right only when there is text.
- **Attach:** the drawn paperclip.
- **The voice button:** a 36px circle, the only round control. Hold it, or the push-to-talk key, to talk. While listening, its ring turns `cyan-ink`, follows the voice's loudness, and the composer's ground turns `tint-cyan`. Release to send.
- **While Steve is busy,** the composer still takes the next message.
- **A message that couldn't be sent** stays in its slip, turned `tint-red`, with "Not sent · Retry" in `red-ink` beneath it.

## Search

Ctrl+K puts the cursor in the sidebar's search. Results replace the day list. Each is a day, with the matching lines and the matched words highlighted (`highlight`). Choosing one opens that day at the line. Esc returns to the day list.

## Keyboard

| Key | Does |
|---|---|
| Ctrl+K | search |
| Ctrl+J | go to the first note |
| Ctrl+1 / 2 / 3 | All / Talk / Log |
| Ctrl+↑ / Ctrl+↓ | the previous or next day |
| Ctrl+T | back to today |
| Esc | back to the composer |

## First day

Before anything has happened, the page shows its heading and Steve's first entry, written by the app rather than a model:

> Hello, Maya. I'll keep everything we do here, one page a day. Write below, or hold the microphone to talk. What I notice on my own will appear here too, and anything I need from you will be stuck to the side, like this.

Then one sample note in the margin explains notes, and goes away when it's dismissed.

## What the backend needs to supply

The current app shows a chat transcript and an inbox separately. The journal needs:

1. **One day's entries in order** across every conversation and channel (the app, the quick bar, Signal, voice), the outbox, actions and jobs, with each entry's kind, channel, time and read state, split at the user's 4 a.m.
2. **Spoken exchanges as one entry,** with their transcript and a summary of one or two sentences. That is a small model call, billed under the caps, made once when the exchange ends.
3. **A folded day's line**: the evening briefing's first sentence, or the counts. No extra model call.
4. **What Steve is doing,** as a short phrase over the socket while it works, for the reply line and the sidebar.
5. **Read state for every entry**, not only the outbox's rows.
