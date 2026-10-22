# Sticky notes: what needs you

A sticky note is how Steve says "this needs you". It is stuck beside the page, apart from the stream, and stays until it's dealt with. It's the only thing in the app that asks for attention, so it has to earn its place and stay quiet while it waits.

The idea is a real sticky note: something you can't miss because of where it is, not because it shouts. It is not a yellow square.

## What earns a note

A note is for something with a cost or a deadline if it's ignored.

| Kind | Kicker | From | Answered by |
|---|---|---|---|
| **A yes** | Needs you | an approval card (outbox kind `card`) | Yes · No |
| **A yes with a code** | Your code | a high-risk card: spending money | Yes, then the six digits or Windows Hello |
| **Now** | Now | a reminder going off; a critical notice (one that would be sent at night) | Done · Later |
| **Stopped** | Stopped | something that stops Steve working until the user acts: the backend keeps stopping, a spending cap reached, OpenAI's balance used up, the Google sign-in ended, Signal unlinked | the one action that fixes it ("Start", "Raise the cap", "Sign in") · Not now |

What is **not** a note:

- questions Steve asks in passing ("How did lunch with Sam go?");
- updates, results, briefings;
- anything already answered, expired or withdrawn.

Those are entries in the stream (`journal.md`). If it's unclear whether something is a note, it isn't.

## Anatomy

```
          ◆                 ← the pin: the logo's red diamond, 10px, on the top edge
  ┌─────────────────────┐
  │ Needs you           │   ← kicker, real handwriting, ink-soft
  │                     │
  │ Send your reply to  │   ← what, in `small`, ink
  │ Dana Ortiz?         │
  │ "Re: Saturday" —    │   ← the detail that matters, ink-soft
  │ full text in your   │
  │ Gmail drafts.       │
  │                     │
  │ [ Yes ]   No        │   ← actions
  │                     │
  │ 10:41 · MAPLE       │   ← meta: when, and the word for answering from the phone
  └─────────────────────┘
```

- **Surface:** `note`, with corner `r-note`, shadow `lift-note`, and the paper grain.
- **Size:** 264px wide in the margin. In the pile at the top of a narrow page, the measure's width.
- **Tilt:** between −1° and +1°, but never under 0.4° either way, chosen from the note's id so it never changes on redraw. A note at exactly 0° looks printed, not stuck.
- **The pin:** the logo's red diamond, half above the top edge. It is the note's one colour.
- **The actions:**
  - the main one is a small ink block (`ink` fill, `paper` text, corner `r-slip`, 32px tall);
  - the other is a text button that gets the drawn underline on hover;
  - "Later" opens a short list: in 15 minutes, in an hour, this evening, tomorrow morning.
- **The word:** the card's word ("MAPLE") in `meta`. Hovering shows "Or reply YES MAPLE from your phone." The same note can be answered on Signal, and the word ties the two together.
- **Detail:** for an email, who it's to and its subject, never its body. The draft is where the user reads it (flow 4 in `docs/flows.md`). For a payment, the amount and the payee, in `code`.

### A yes with a code

- **Yes** turns the note's lower half into six underlined boxes for the authenticator's digits (in `code`), or a "Use Windows Hello" button once Hello is set up.
- **The kicker** reads "Your code".
- **A wrong code** says so in `red-ink` under the boxes, and the boxes clear.

### Stopped

- The kicker reads "Stopped". The body says, in a sentence, what stopped and what the user loses until it's fixed: "Steve's backend keeps stopping. Nothing runs until it starts." The end of its log sits behind "Details".
- **"Not now"** puts the note away for an hour. Then it comes back.

## Where notes sit

### Wide window (1180px or more)

In the right margin.

- **When its line is on screen,** a note sits level with the line in the stream that raised it, with an arrow mark between the two.
- **When that line scrolls away,** the note sticks at the top of the margin, 24px down. That is what makes it sticky in both senses: it can't scroll out of sight.
- **Several notes** stack and fan out: each one 8px lower, alternately tilted. Up to three show. More become a pile with "+2" on the top note's corner. Clicking the pile spreads it out as a column.

### Narrower windows

- Notes gather at the top of the page, under the heading, as one folded strip on `tint-red`: the pin, "2 notes", and the top note's first line.
- Clicking it unfolds the notes there, full width, one under another.
- The strip stays stuck at the top of the scroll.

### Order

1. Stopped, since nothing else works until it's fixed.
2. Now.
3. Needs you, soonest deadline first, then oldest first.

### Elsewhere

- **The sidebar:** a red diamond and the count beside the day.
- **The desk card:** the top note's edge peeks out behind it (`presence.md`).
- **The quick bar:** the notes are listed under its input, and can be answered there.
- **The tray icon:** a red diamond badge.

## How a note comes and goes

- **Arriving:** it drops 12px into place, settles to its tilt (220ms), and the pin arrives last, 80ms after. If the window is hidden, the arrival happens the next time it's shown, so it isn't missed.
- **Answered here:**
  - the main button shows a drawn check for 300ms;
  - the note peels up and away (180ms);
  - the line in the stream becomes what happened ("You said yes · sent 10:42").
- **Answered elsewhere** (on Signal, in the quick bar): the same peel happens wherever it's shown, and the stream says where it was answered.
- **Expired:** it fades without peeling (300ms). The stream says "Expired · nothing was sent".
- **No escalation on screen.** A note waiting an hour looks the same as a new one: no colour change, no shaking, no badge growing. Reaching the user harder (Signal, a retry) is the backend's job, and stays calm in the app.

## Keyboard and screen readers

- **Ctrl+J** goes to the first note. The arrow keys move between notes, and Tab moves within one.
- **Enter** activates the focused button. **No single key approves anything:** a note is never answered by a stray "y" typed into the wrong window.
- **Esc** returns to the composer.
- **The notes are a region** named "Needs you, 2".
  - A new note is announced politely: "New note, needs you: send your reply to Dana Ortiz?"
  - A "Now" or "Stopped" note is announced at once.
- The tilt, the pin and the handwriting are decoration. The kicker's text and the actions carry the meaning.

## The night theme

- The note is `note` (`#1b2c47`) on navy paper, with chalk text. The pin stays red.
- Its shadow is darker, and the grain stays.
- Not a cream note on the navy page: at night it would be the brightest thing on the screen.
