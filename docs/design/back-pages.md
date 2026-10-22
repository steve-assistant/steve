# The back pages, onboarding and forms

Everything that isn't the journal is behind the menu at the foot of the sidebar: settings, accounts, spend and the rest. They are the back pages of the journal. They're rarely visited, so they're plainer than the journal, but they're made of the same paper, type and marks.

## The menu

Opened from the user's name at the foot of the sidebar. A list on `note` with `lift-note`, in three groups, each under its title in the handwriting face:

**About you**
- What Steve knows: facts and memories, to read, correct and forget
- Profile: name, pronouns, zone, home, work, quiet hours, briefing times

**Connections**
- Accounts: mail, calendar, files
- Channels: Signal, Telegram
- Browser logins (with the sandboxed browser)

**Steve**
- What Steve may do: autonomy, and what it may use on this computer
- Routines and goals
- Extensions (step 6)
- Spend: caps, by feature, forecast
- Activity: the full record of what Steve did, for auditing
- Diagnostics: the backend's state, Start / Restart / Stop, the end of its log
- Settings, including Reading: the text face, handwriting on or plain, and size (`foundations.md`, Accessibility)

Then a rule, and "Quit Steve".

This replaces today's eleven tabs (`ui/src/App.tsx`).

- Chat and Home become the journal.
- Approvals become notes.
- Tasks become Ongoing in the sidebar.
- The rest move here.
- Upgrades become Extensions when step 6 lands.

## A back page

- It opens in place of the journal, in the same window, with "← Today" at the top left to return. Esc also returns.
- **The title** is the group's name in the handwriting face ("Connections"), with the page's name below in `display`.
- **Sections** have a `title` heading and a short sentence in `ink-soft` saying what the section is for and what changing it does.
- **Measure:** the page keeps the journal's 38em column. Settings are read, not scanned like a dashboard.
- **Feedback:** changes save as they're made, and say so in place with a drawn check and "Saved", which fades. There is no Save button, except where a change costs money or reaches outward. Those have a button and say what will happen.

## Forms

| Control | Looks like |
|---|---|
| Text input | no box: a `rule-strong` underline, the label above in `label`, `ink-soft`. Focused: the underline becomes 2px `ink` |
| Secret (an API key) | the same, shown as dots after saving, with "Replace" beside it. A key is never shown again in full |
| Checkbox | a square in the ink's line. Checked, a check in the same line, inside it and the same in every box. Used instead of switches: a switch is a phone's control, a tick is paper's |
| Choice of a few | the options as words in a row; the chosen one gets the drawn underline |
| Choice of many | a native-feeling list under an underline, opening on `note` |
| Number with bounds (a knob) | the number in `code`, with − and + either side, and its bounds in `ink-faint` below ("between 1 and 10") |
| Main button | ink block: `ink` fill, `paper` text, corner `r-slip`, 36px tall |
| Other buttons | text, with the drawn underline on hover |
| Destructive | text in `red-ink`, and a second step that names what will be lost: "Forget everything Steve knows about Theo?" |
| Error | under the field in `red-ink`, saying what to do: "That key was refused by OpenAI. Check it was copied whole." |

## Tables and figures

Spend, activity and diagnostics are mostly figures.

- Rows are separated by hairlines (`rule`). There's no zebra striping and no outer box.
- Figures are in `code`, right-aligned, and tabular.
- Charts are drawn in `ink` alone, with one series emphasised and others in `ink-faint`. An arrow mark and a few words in the handwriting face point out what matters ("Most of this month's spend was the night"). There are no legends where a label can sit on the line.

## Onboarding

Its look:

- **One question per page,** on `paper`, centred, at the journal's measure.
- **The mark** at 120px above the first page only, with "Hello" in real handwriting and one line in `display`: "I'm Steve. Let's get me working."
- **Progress:** a row of small outline diamonds at the top. Each fills as its step is done, and the current one turns slowly (reduced motion: drawn bolder).
- **Each page** has:
  - a question in `display`;
  - one or two sentences in `ink-soft` on why Steve needs it and what it costs;
  - the form;
  - "Continue" as an ink block;
  - "Skip for now" as text where a step is optional.
- **Keys** (the OpenAI key, the OpenRouter key): each page says where to get the key, checks it at once, and shows the result in place ("Works. $18.40 of credit left.").
- **Signal:** the QR code on a `note` card with `lift-note`, untilted, with the steps on the phone beside it. The current flow is `ui/src/Signal.tsx`.
- **Between onboarding's parts,** the dinkus.
- **The last page** opens the journal's first day (`journal.md`, "First day").

## The night theme

The back pages follow the theme like everything else. Ink blocks invert: the button is `ink` (chalk) with `paper` (navy) text.
