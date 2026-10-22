# Foundations

The parts everything else is built from. Values here are the tokens. The app's stylesheet defines each once, by these names, and nothing else picks its own colour, size or timing.

## Colour

### The brand

From `docs/brand/steve-logo.svg`, the source of truth. The current stylesheet's values were sampled from a compressed image and are slightly off; these replace them.

| Name | Hex | Use |
|---|---|---|
| navy | `#0d1c34` | ink: text, the mark's body, lines |
| cream | `#fff9ee` | paper |
| red | `#ed1c24` | the mark; what needs you |
| yellow | `#f9ed32` | the mark; the highlighter |
| cyan | `#00aeef` | the mark; voice |

**The colours sit mostly on navy.** In the logo, red, yellow and cyan sit on the navy body, and that is their home. On cream they're allowed as accents, under three rules:

1. **Never text.** On cream, yellow almost disappears (1.2:1) and cyan is faint (2.4:1). Red text is `red-ink`. Cyan lines and rings are `cyan-ink`.
2. **Never the only signal.** A colour on cream decorates, or repeats what words or a shape already say. The red diamond beside "Asked you about…" is fine. A red dot alone is not.
3. **Few at once.** One or two coloured accents on screen, not a scatter.

Where they appear on cream:

| Accent | Where |
|---|---|
| red | a note's pin; the needs-you signifier |
| cyan | the spoken signifier, beside the word "Spoken" |
| yellow | the highlighter behind ink text (`highlight`) |
| all three | the dinkus: the three diamonds in a row, small and centred, closing a day page, between onboarding's parts and on empty pages. It's the brand's flourish, like a printer's ornament |

### Darker cream and tints

A darker cream, and pale tints of the brand colours, can mark an area of the page without a box or a line. Their rule: `ink` and `ink-soft` only on them (`ink-faint` drops under 4.5:1 on some), and only for the uses listed.

| Token | Paper | Ink theme | Use |
|---|---|---|---|
| `paper-deep` | `#ede2cb` | `#152540` | the selected day in the sidebar; folded days when scrolling back; the quick bar's rows of notes; a back page's section of figures |
| `tint-red` | `#fde3da` | `#2c1c32` | the folded strip of notes in a narrow window; a message that couldn't be sent |
| `tint-cyan` | `#e0f0ee` | `#0b304e` | a spoken exchange's folded block; the composer while listening |
| `tint-yellow` | `#fef6c5` | `#293534` | a search result's line |

On paper, `ink` is at least 12:1 on all of these, and `ink-soft` at least 5.4:1.

### Paper (the day theme)

| Token | Hex | On paper | Use |
|---|---|---|---|
| `desk` | `#f4ecdc` | | the sidebar, the window's ground around the page |
| `paper` | `#fff9ee` | | the page |
| `note` | `#fffdf7` | | sticky notes, floating windows |
| `slip` | `#f6eedf` | | the user's own entries; code |
| `ink` | `#0d1c34` | 16.3:1 | text |
| `ink-soft` | `#515a68` | 6.7:1 | secondary text, updates |
| `ink-faint` | `#646c77` | 5.1:1 | times, channel names, hints; at least 4.5:1 on every surface |
| `rule` | `#e2ded8` | | hairlines |
| `rule-strong` | `#cfcdc9` | | input underlines, dividers that must be seen |
| `red-ink` | `#b8141c` | 6.4:1 | text that reports a failure |
| `cyan-ink` | `#00739e` | 5.1:1 | voice marks and the listening ring on paper |
| `highlight` | `#fdf4a7` | | yellow at 38% on paper, behind ink text (15:1) |

### Ink (the night theme)

The same page at night: navy paper, cream "chalk" text. It follows Windows' dark setting. Settings can also choose paper always, ink always, or ink during quiet hours.

| Token | Hex | On paper | Use |
|---|---|---|---|
| `desk` | `#08131f` | | |
| `paper` | `#0d1c34` | | |
| `note` | `#1b2c47` | | lifted surfaces |
| `slip` | `#16263f` | | |
| `ink` | `#f0ece3` | 14.5:1 | text |
| `ink-soft` | `#b6b7b6` | 8.5:1 | |
| `ink-faint` | `#9ea1a4` | 6.6:1 | at least 5.4:1 on `note` |
| `rule` | `#2f3b4e` | | |
| `rule-strong` | `#46526a` | | |
| `red-ink` | `#ff6b6f` | 6.2:1 | |
| `cyan-ink` | `#00aeef` | 6.7:1 | the brand cyan works on navy |
| `highlight` | `#414a34` | | yellow at 22% on navy (7.9:1 under `ink`) |

**The mark in the ink theme:** the body is drawn in `ink` (chalk). The diamonds keep their colours. The centre outline diamond is drawn in navy.

**Status colours:** there are no separate green, amber or red for status. Done is a filled ink diamond. Failed is `red-ink` words. In progress is a ticking outline. Status is shape and words, never colour alone.

## Type

Three families, all under the SIL Open Font License, bundled with the app and never fetched. A font request to a CDN would tell a third party each time the app opens.

| Role | Family | Why |
|---|---|---|
| Text | **Newsreader** (variable, optical sizes 6–72) | a serif made for reading on screens; its display sizes have the warmth of the mockups' headline, and its text sizes stay clear at 15px |
| Data | **IBM Plex Mono** | times, amounts, codes ("MAPLE"), keys, figures in tables; it sets apart what is exact |
| Hand | a readable handwriting face, chosen at build (see The hand) | headings, kickers and annotations: never a sentence of body text |

There is no sans. Buttons, labels and settings are set in Newsreader too, which gives the app its voice. A sans for the interface is what makes most apps look alike. The one exception is a reading option (see Accessibility).

### Scale

At Windows' 100% text size. Everything follows Windows' text scaling.

| Token | Family | Size / line | Weight | Use |
|---|---|---|---|---|
| `display` | Newsreader, optical size 72 | 34 / 40 | 500, tracking −0.01em | onboarding, back-page titles |
| `title` | Newsreader | 22 / 28 | 500 | section titles, a task's name |
| `body` | Newsreader | 17 / 27 | 400 | Steve's words |
| `body-user` | Newsreader | 16 / 25 | 400 | the user's words |
| `small` | Newsreader | 15 / 22 | 400 | updates, notes, the quick bar's answers |
| `label` | Newsreader | 13 / 18 | 600 | labels, buttons |
| `meta` | Plex Mono | 11.5 / 16 | 400, tracking 0.02em | times, channels, counts |
| `code` | Plex Mono | 13.5 / 20 | 400 | code and tables of figures |

- The measure (line length) of Steve's text is at most 38em, about 66 characters.
- Figures in tables are tabular (Plex Mono is).
- Italic is for titles of things and quoted words, never for whole paragraphs.

## The hand

Three layers, from most to least used:

1. **A handwriting face** for every hand-set word.
2. **Real handwriting** for a few words that matter most, replacing the face where they appear.
3. **Marks**: underlines, circles, checks and the like.

The rest of the app is typeset. The hand must be readable before it is convincing. It's fine if a reader can tell the face is a font. It's not fine if they have to squint.

### The handwriting face

Chosen at build by setting the candidates side by side: a kicker at 18px, a day heading at 40px, and a sentence of annotation at 16px, on paper and on ink.

- **It must:**
  - read at 16px in a sentence;
  - be under the Open Font License;
  - cover Latin Extended, for names;
  - come in more than one weight;
  - sit comfortably beside Newsreader.
- **Better still:** alternates for repeated letters, so "little" doesn't show two identical l's.
- **Try first:** Playpen Sans, made for legibility with handwriting's variety. Then Kalam (light) and Handlee.
- **Chosen: Playpen Sans** (step 9, from the specimen sheet). It meets every rule, with weights from 100 to 800. Kalam's light weight thins out on ink. Handlee has no Latin Extended, so Ł, ř, Ş and Ō in a name fall back to another face.
- **Out,** because they're what generated designs reach for: Caveat, Indie Flower, Architects Daughter, Permanent Marker, Shadows Into Light, Gloria Hallelujah, and Comic Sans and its relatives.

Where it's used:

- day headings and note kickers, when not real handwriting;
- back-page group titles;
- annotations on charts ("Most of this month's spend was the night");
- the "why?" under an update.

### Real handwriting

A short, closed set of words, written by a real person and replacing the face wherever they appear:

- day headings: the seven weekdays, "Today", "Yesterday";
- note kickers: "Needs you", "Now", "Stopped", "Your code";
- onboarding: "Hello".

That's fourteen words. Each is written several times, and two or three of the best are kept as variants, chosen from the day or the note's id like the marks.

- **Where they come from** (decision 1 in `README.md`, settled by step 9's plan). The maintainer won't be writing them. Recommended: someone with clear, ordinary handwriting writes them in one pen on plain paper, gives written permission to ship them under the project's licence, and they're scanned at 600 dpi, vectorised and cleaned by hand. Otherwise: public-domain handwriting samples, or generated images of handwriting, vectorised and corrected the same way.
- **Readable first.** A word that can't be read at a glance is rewritten, however nice it looks.
- **Another language** falls back to the handwriting face, then to Newsreader italic.

### Marks

| Mark | Variants | Used for |
|---|---|---|
| underline | 3 | a primary text button on hover; a link's focus; a new day's heading |
| circle | 3 | the mark's red diamond when something needs you; a search hit in the sidebar |
| check | 3 | done, in a task's parts; a note answered |
| cross | 2 | failed; "not right" on a reply |
| strike | 2 | something cancelled or withdrawn |
| arrow | 3 | "see note" from the stream to the margin; send |
| bracket | 2 | grouping a run of updates in the gutter |
| wave | 2 | the asleep divider |
| mic | 2 | the microphone button, held to talk |
| clip | 2 | the paperclip beside it, to add a photo |

Rules for the marks:

- **Filled shapes, not even strokes.** A real pen line swells and thins. A path with one stroke width is what makes drawn things look fake.
- **The variant is chosen from the thing's id,** so the same entry always gets the same mark, and two neighbours rarely match.
- **Drawn in, once.** A mark draws itself in (280ms) at the moment its event happens, never on load or scroll. After that it's still.
- **Ink coloured.** `ink`, or `red-ink` for failures. The circle around the red diamond is the one red mark.
- **Not sketch-rendered.** No library that roughens shapes (that reads instantly as a whiteboard tool).
- **Sourced like the handwriting:** real pen strokes, ideally the same pen and hand as the words, vectorised and cleaned. Or generated, then corrected by hand until each one has a pen's swell.

Every hand-set word and mark carries its text for screen readers, and is never the only way to tell what something is.

## Paper

- **Grain:** one 128px tile of fine paper fibre, at 3% over `paper`, `desk` and `note`. Static, cheap to draw, and invisible unless you look. Removed in Windows' high-contrast mode.
- **No ruled lines,** no margins drawn in red, no holes, no torn edges, no coffee rings.
- **Edges:** only sticky notes and floating windows are lifted. Everything else lies flat on the page.

### Shadows

| Token | Value | Use |
|---|---|---|
| `lift-note` | `0 1px 0 rgb(13 28 52 / .06), 0 8px 16px -8px rgb(13 28 52 / .28)` | sticky notes |
| `lift-window` | `0 2px 4px rgb(13 28 52 / .08), 0 18px 40px -12px rgb(13 28 52 / .35)` | the quick bar, the desk card |

In the ink theme, the same shapes with black at double the opacity.

### Corners

| Token | Value | Use |
|---|---|---|
| `r-note` | 2px | sticky notes: paper has nearly square corners |
| `r-slip` | 6px | the user's slips, inputs, buttons |
| `r-window` | 10px | floating windows, matching Windows 11's rounding |

Nothing is a pill except the round voice button.

## Space

A 4px base: 4, 8, 12, 16, 24, 32, 48, 72.

- Between entries in the stream: 16. Between exchanges: 32.
- The page's left gutter, for times and signifiers: 72.
- The right margin, for notes, when the window is 1180px or wider: at least 288.

## Signifiers

Every entry on a day page starts with a small sign in the gutter, like a bullet journal's, built from the logo's diamond:

| Sign | Means |
|---|---|
| ◇ outline | Steve told you something (an update, a reminder) |
| ◆ filled | something was done |
| ◇ turning | something is in progress (it ticks a quarter turn, see Motion) |
| ◆ red, small | needs you: its note is in the margin |
| ✕ drawn | it failed, or you said "not right" |
| ◆ cyan | spoken, beside the word "Spoken" |
| (none) | your own words |

They are 9px, on the first line's centre: `ink-soft`, except the red and cyan ones, which keep their colour.

## Motion

| Event | What moves | Time | Easing |
|---|---|---|---|
| An entry arrives | fades in, rises 6px | 160ms | ease-out |
| A mark draws in | its mask reveals along the stroke | 280ms | ease-in-out |
| A note arrives | drops 12px into place and settles to its tilt | 220ms | `cubic-bezier(.2,.9,.3,1.2)` |
| A note is answered | peels up 16px, tilts 2° more, fades | 180ms | ease-in |
| In progress | the outline diamond turns 90°, rests, turns again | 400ms turn every 1.4s | ease-in-out |
| A floating window opens | fades in, rises 8px | 120ms | ease-out |
| A day folds | its height closes | 240ms | ease-in-out |

- Nothing bounces, shakes or pulses for attention.
- Only a true ongoing state loops: working, listening, speaking.
- **Reduced motion** (Windows' "Animation effects" off): every move becomes a cross-fade of at most 120ms, and looping states become still variants of themselves (see `presence.md`).

## Icons

There are very few. Each is sourced like the marks, at 20px:

- the microphone;
- attach;
- search;
- the menu;
- send (an arrow mark);
- undo;
- expand and fold (a small chevron).

Where text fits, text replaces an icon. No icon pack.

## Accessibility

- **Contrast:** text meets WCAG AA on every surface it sits on (the tables above). Meaningful shapes meet 3:1.
- **Focus:** a 2px `ink` outline, offset 2px, with corner `r-slip`, on every focusable thing, shown for the keyboard only.
- **Colour is never the only signal:** red always comes with a shape or words.
- **Tilt:** a note's tilt is at most 1°, which doesn't hurt reading. Text is never set at an angle elsewhere.
- **High contrast:** Windows' contrast themes drop the grain, shadows and tilt, and use system colours throughout.
- **Screen readers:** every hand-set word and mark has its text, and the stream is a feed whose new entries are announced politely (notes: `notes.md`).
- **Targets:** at least 32px for anything clicked.
- **Reading, in Settings.** For anyone who reads the defaults with difficulty:
  - **Text face:** Newsreader (the default), or Atkinson Hyperlegible Next, a sans made for low vision. This is the only sans in the app. It replaces Newsreader everywhere, at the same scale.
  - **Handwriting:** on (the default), or plain. Plain sets every hand-set word and real handwritten word in the text face, semibold, at the same size. The marks stay: they're shapes, not words.
  - **Size:** Windows' text size, or one step larger in the app alone.
