# Presence: the mark, the desk card and the quick bar

Steve is always on, so it needs a presence beyond its window. It has three forms, all built from the same mark:

- **the mark** shows Steve's state, wherever it appears;
- **the desk card** sits small on the desktop, like a desk pet that is useful: what Steve is doing, what's next, what needs you;
- **the quick bar** comes up on a hotkey, over whatever the user is doing, to ask or tell Steve something and go back to work. Like Quick Look or PowerToys Run.

## The mark

The logo without its rounded square: the navy body, the three diamonds, and the outline diamond on the body. It's built from `docs/brand/steve-logo.svg`, whose diamonds are separate shapes, so each can move on its own.

### Sizes

| Size | Where | Drawn |
|---|---|---|
| 16, 20, 24 | tray, quick bar | simplified: the centre diamond dropped, the gaps widened so the diamonds don't merge |
| 40 | the sidebar | in full |
| 56 | the desk card, on its rounded square | in full |
| 120 | onboarding | in full |

### States

Each state changes one thing. The diamonds have meanings: **red** needs you, **yellow** is working, **cyan** is voice. The centre diamond is Steve itself, used only when the backend is starting or stopped.

| State | The mark | With reduced motion | The sidebar's line |
|---|---|---|---|
| Idle | as the logo | | "Idle" |
| Working (a reply, a task, the night) | the yellow diamond turns a quarter turn every 1.4s | the yellow diamond drawn as an outline | what it's doing: "Checking your calendar" |
| Listening | the cyan diamond grows and shrinks with the microphone's level | the cyan diamond 15% larger, still | "Listening" |
| Speaking | the cyan diamond follows the voice's level | the same, still | "Speaking" |
| Needs you | a drawn ink circle around the red diamond, drawn in once, staying | the circle, shown at once | "2 things need you" |
| Quiet hours | the diamonds at 55% | | "Quiet until 07:00" |
| Asleep, then catching up | the diamonds as outlines; on waking they fill in left to right as Steve catches up | they fill in at once | "Catching up after sleep" |
| Starting | the body faint, the centre diamond turning | the centre diamond as an outline | "Starting" |
| Stopped | the body faint, every diamond an outline | | "Stopped" |

- States combine where they can: working during quiet hours is the turning yellow at 55%.
- **A short reconnect** (a worker swap takes under a second) shows nothing. It only shows as Starting if it lasts over five seconds.

### Small life

The mark is a little alive, never busy:

- when an entry arrives, the body nods once, 2px down and back (200ms);
- on hover, the diamonds lift 1px.

Nothing else moves unless a state is happening.

## The desk card

A small window that lives on the desktop. In its smallest form it is the app's own icon, alive.

### Forms

**The tile**, 56 × 56. The mark on its rounded cream square (navy in the night theme), with `lift-window`. This is how it sits most of the time.

**The card**, 320 wide, 72 to 132 tall. The tile, with up to three lines beside it on `note`:

```
┌──────┬──────────────────────────────┐
│      │ Checking your calendar       │   now: what Steve is doing
│ mark │ Next · Design review, 15:00  │   next: the next event or reminder within 3 hours
│      │ ◆ 2 need you                 │   notes: a count, the pin, and the top note's edge
└──────┴──────────────────────────────┘    peeking out behind the card
```

- **The card opens on its own** for something worth a glance, then closes again after 8 seconds unless the pointer is on it:
  - a new note;
  - an event starting within 10 minutes ("Standup in 10 min · Room 4");
  - a reply arriving after the quick bar was closed.
- **Hovering over the tile** opens the card. Hovering over the card adds Steve's last entry, in one or two lines.
- **The note's edge** stays peeking from behind the tile while notes wait, even when the card is closed.

### What it does

| Action | Does |
|---|---|
| Click | opens the journal at today, or at the note if a note was clicked |
| Press and hold | push-to-talk, as with the hotkey. The mark shows Listening, and the card shows the words as they're heard |
| Right-click | a menu: Talk · Open Steve · Hide for an hour · Hide until tomorrow · Desk card settings |
| Drag | moves it. It snaps to the screen's edges and corners, 16px in, and remembers its place per monitor |
| Answer a note | the card's note line opens the note in the quick bar, where it can be answered |

### When it hides

- When a full-screen app, a game or a presentation is in front: Windows reports this as the user being busy, and the card waits until that ends.
- When the user chooses "Hide", for the time chosen.
- When Windows is locked.

### Privacy

The card's text can be read by anyone looking at the screen. In Settings, "Show what Steve is doing on the desk card" is on by default. Off, the card shows the mark, the time of the next event without its name, and the count of notes, nothing more. Whatever the setting, the card is hidden while Windows reports a presentation or a full-screen app (above). Screen sharing in a meeting app isn't reported to other apps, so it is covered only by the setting and by "Hide".

### The window

- A separate window: transparent outside the tile and card, no frame, not in the taskbar, and on top of other windows by default (Settings can turn that off).
- **It never takes the keyboard focus** unless clicked, so it can't interrupt typing.
- **It is on by default** after setup (decision 4 in `README.md`, settled by step 9's plan), and off from its own menu or Settings.

## The quick bar

The quick bar exists today (`ui/src/QuickBar.tsx`, flow 5 in `docs/flows.md`). This is its new form.

### The hotkey

- **Tap** the hotkey to open the quick bar. Tap again, or Esc, to close it.
- **Hold** it to talk (push-to-talk), and release to send. The quick bar opens while listening and shows the words as they're heard.
- One key replaces today's separate quick-bar and push-to-talk keys. The global-shortcut plugin reports both the press and the release, which is what tap-or-hold needs.
- The default is Win+Alt+S, if Windows lets an app register it; otherwise the next free combination, checked on a clean Windows 11. It must not collide with PowerToys Run (Alt+Space), Windows' own shortcuts, input-method switching or the personal edition's keys. Settings changes it, and says when another app holds it.

### Look

- A slip 640px wide on `note`, with corner `r-window` and `lift-window`. It sits centred horizontally, its top at 28% of the height of the monitor the pointer is on.
- Opens in 120ms (a fade and an 8px rise).
- **The input row:** the mark at 24px (showing its state), then the input in Newsreader 20px. Placeholder: "Ask or tell Steve".

### Before typing

Up to four rows under the input:

1. Each note waiting, in one line on `paper-deep`: its kicker, its first line, and its actions. Answered in place, peeling away like a note.
2. "Next · Design review, 15:00".
3. Steve's last entry, one line, in `ink-faint`.

### After sending

- Steve's answer appears beneath, in `small`, with the same working line as the journal while it comes.
- The slip grows to at most 60% of the screen's height, then scrolls.
- The exchange is on today's page too. The quick bar is another way into the one journal, not a separate conversation.

### Keys

| Key | Does |
|---|---|
| Enter | send |
| Shift+Enter | new line |
| Esc | close. Unsent text is kept as a draft for 10 minutes |
| Ctrl+Enter | send, and open the journal at this exchange |
| Tab | into the notes, then their buttons |

### Closing

- Clicking anywhere else closes it, like PowerToys Run.
- A reply still coming carries on. It lands on the page, and the desk card opens to show it.
- **It never opens on its own.** Steve reaches the user through notes, the desk card and toasts, never by putting a window in front of their work.

### Later, not in v0.1

Asking about what's selected: the text or file selected when the hotkey is pressed, offered as context. Reading another app's selection means sending it a copy keystroke, so it needs its own privacy decision first.

## The tray

- **The icon:** the mark at 16px. A small red diamond badge appears while notes wait, which replaces today's red dot (`set_waiting`). It turns faint with outline diamonds when stopped.
- **The menu:** Open Steve · Talk · Show the desk card · Hands-free on/off (with the voice add-on) · Quit.

## Windows toasts

Toasts already exist (`ui/src-tauri/src/toast.rs`). The design narrows when they appear (decided 13 October 2026):

- **Notes** always make a toast, so they're also in Windows' notification centre.
- **Briefings** make one too.
- **Updates and results** don't make a toast while the desk card is showing. The card is the quiet way to see them. With the card hidden or off, they do, as today.
- **The text** is plain: "Steve · Needs you: send your reply to Dana Ortiz?". While Windows is locked, only the kind, as today.
- **Silent,** as today.

## What the backend needs to supply

1. **What Steve is doing,** as a short phrase over the socket (shared with the journal).
2. **What's next:** the next calendar event and the next reminder within three hours, for the card and the quick bar.
3. **The voice's level** while listening and speaking, a few times a second, for the cyan diamond.
4. **Which entries are notes,** and their kind, as a field rather than read from the text (`notes.md`).
5. **Snoozing a reminder** from a note's "Later".
