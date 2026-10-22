# Steve's design

This folder is the design spec for the desktop app: how Steve looks, reads and behaves on screen. The maintainer agreed the direction on 13 October 2026, and step 9 built it.

## The idea in one paragraph

Steve is one living thing, not a chat app with tabs. Everything it does with the user is written in one place: a journal, one page per day. Conversation, voice, what it did, what it noticed and the briefings are all set down in the order they happened. Anything that needs an answer now is stuck to the page like a sticky note, and stays there until it's dealt with. Away from the app, Steve sits small on the desktop as a desk card, and a hotkey pulls up a quick bar to ask or tell it something. It should feel like good paper and a good pen, and look like something a person designed.

## The files

| File | What it covers |
|---|---|
| `foundations.md` | colour, type, handwriting and marks, paper, space, motion, icons, accessibility, the two themes |
| `journal.md` | the main window: the sidebar, the day page, every kind of entry, voice, the composer, search |
| `notes.md` | sticky notes: what earns one, how it looks, where it sits, how it goes away |
| `presence.md` | the mark and its states, the desk card, the quick bar, the tray and Windows toasts |
| `back-pages.md` | everything behind the menu (settings, accounts, spend and the rest), onboarding's look, forms |

The logo's source is `docs/brand/steve-logo.svg`. Its colours are the brand's exact values.

## Principles

1. **One stream, written in order.** Nothing Steve says lives somewhere else. The backend already works this way: everything unprompted goes through one outbox, and joins the conversation once seen (flow 3 in `docs/flows.md`). The design shows what is true.
2. **Few things ask for attention, and they earn it.** A sticky note is the only loud thing, and it's quiet. If everything stands out, nothing does.
3. **Paper and ink, not screens and glow.** Hierarchy comes from type, space and position, not colour, boxes or effects.
4. **The brand colours are scarce, and mostly on navy.** Navy is the ink and cream is the paper, with a darker cream to mark an area. Red, yellow and cyan live in the mark. On cream they're accents that decorate or repeat what words say: red for what needs you, cyan for voice, a yellow highlighter for what's new, and the three together as a flourish.
5. **The hand is readable first.** A handwriting face for headings, kickers and annotations, and real handwriting for a few words that matter most. Never for body text. Marks are real pen strokes, a small fixed set, not a sketchy-rendering library.
6. **Familiar where it helps.** A sidebar on the left, the conversation in the middle, a composer at the bottom, your words in a slip and Steve's as plain text. Anyone who has used an AI assistant knows where to look.
7. **Calm by default.** Steve is always on, so the app must be pleasant to leave open all day. No motion loops forever except a state that is truly happening (listening, working). No sounds: the phone is the buzz.

## What to avoid

This is the "generated" look. Each of these is out:

- sparkle and star icons (✦) for anything AI;
- gradients, glows, neon, glassy blur behind panels;
- many colours, or colour used for decoration;
- stock icon packs, used everywhere at the same weight;
- system fonts used plainly, or one sans for everything;
- avatar circles with initials, and chat bubbles with tails;
- pill-shaped grey input bars, and big round send buttons;
- blobby background shapes and decorative swooshes;
- emoji as interface;
- "Thinking…" with bouncing dots;
- copy that sounds like a wellness app ("your companion", "What's on your mind today?").

## The two mockups

They point the right way. Keep:

- the cream ground and navy ink;
- a large warm serif for the greeting;
- generous space;
- the mark as Steve's presence;
- the composer at the bottom;
- the date set small and quiet.

Change:

- **Steve's words go unboxed.** Only the user's words sit on a slip.
- **No avatar circles,** for either party.
- **No sparkles,** and no grey pill input.
- **Diamonds on cream only as a flourish, never as status.** The mockup's "Thinking" diamonds are status, and the yellow one is too faint on cream (1.2:1) to carry it. The mark shows state; loose diamonds only decorate.
- **No delivery ticks.**
- **"Library" and "Insights" go.** The journal and its days replace them.
- **No blob shapes** in the background.

## How Steve sounds on screen

- Plain, short sentences. The way a capable assistant writes a note, not a friend texting.
- First person when Steve acts ("I moved it to 3:00"), never cute.
- No exclamation marks, no emoji, no "Great question".
- Say what happened and what's needed: "Couldn't send: Gmail refused the sign-in. Sign in again in Accounts."
- Times in the user's own format and zone. Days as people say them: "Today", "Yesterday", "Tuesday", then dates.
- Examples in these files use the test persona, Maya Lindqvist (`tests/persona.py`), and the people around her.

## Decided

The maintainer, 13 October 2026, on the first draft:

- **Colours** sit mostly on navy, with accents on cream and a darker cream where an area needs marking (`foundations.md`).
- **No sans,** except as a reading option in Settings.
- **Notes** as specified (`notes.md`).
- **One hotkey for the quick bar:** tap to open, hold to talk. Win+Alt+S, if Windows lets an app register it on a clean Windows 11; otherwise the next free combination (`presence.md`).
- **Toasts** for notes and briefings only. The desk card shows the rest quietly (`presence.md`).
- **The handwriting** is not the maintainer's. A readable handwriting face, plus real handwriting for a few words (`foundations.md`).

## Settled by step 9's plan

The four decisions this file left open, as the maintainer answered them on approving step 9's plan:

1. **Where the real handwriting comes from:** someone with clear, ordinary handwriting, with written permission to ship it. Until their scans exist, the face and simple marks stand in (`foundations.md`).
2. **Which handwriting face:** Playpen Sans. The specimen sheet showed no problem beside Kalam and Handlee, and it ships in `ui/src/fonts/` (`foundations.md`).
3. **When a day page turns:** 4 a.m. in the user's zone, so a conversation at 1 a.m. stays on the day it belongs to. Spend's day still turns at midnight (`journal.md`).
4. **Whether the desk card is on by default:** on after setup, with its switch on its own right-click menu and in Settings.
