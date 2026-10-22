# Using Steve

For installation and your first answer, start with [the README](../README.md).
This guide covers everyday use. [The privacy statement](../PRIVACY.md)
lists what leaves your computer, where it goes and how to stop it.

## The app

Steve opens on today's page: what you said and what Steve did, as a day's
journal. What needs you is a note beside it. The menu at your name opens
Accounts, Channels, Settings and the other pages.

Closing the window leaves Steve running in the system tray. Quit from the
tray stops the app and its backend. If you want Steve to start when you sign
in to Windows, turn on start at login in Settings.

The desk card is Steve's mark on your desktop. Point at it for what Steve
is doing, what's next and what needs you. Click for today; hold to talk.
Settings turns it off, or hides its words on a screen others can see.

## Talking and typing

Type in the day's conversation. To speak, hold Win+Alt+S or the microphone
button, then let go. Tapping the shortcut opens typing instead. If another
program holds that shortcut, Steve tries Win+Alt+J or Ctrl+Alt+S; Settings
shows which shortcut it uses and lets you change it.

By default your words go to OpenAI for transcription, and Steve's reply is
spoken with OpenAI's voice, under the same spend caps. The microphone is
open only while you hold. Hands-free voice is not in this version.

For a source checkout, the optional local Whisper and Kokoro add-on keeps
desk speech processing on this computer; a CPU is enough. Install it with
`uv sync --extra accounts --extra voice` and set `STEVE_LOCAL_VOICE` to `1`
in your user environment. An installed Steve does not offer this add-on
yet. Voice notes from your phone still go to OpenAI.

## Approvals and spend

Read a card's action and targets before approving it. High-risk actions
also ask for your passphrase or Windows Hello in the app. A phone approval
for a high-risk action also takes an authenticator code.

Setup asks for a monthly spend cap. The Spend page shows what Steve has
spent and lets you change its caps. Give Steve a provider project of its
own as well: Steve's cap and your provider's budget are separate controls.

## Extensions

Open Extensions to see the modules available to Steve, their switches and
the access each has. A module's card names its grants and hosts. Read those
before approving it; turn a module off or remove it when you no longer
want it to run. Steve can build a tool when you ask for something it cannot
do yet. Its code runs in a sandbox and its approval card comes back to you.
"Approve and turn on" installs it ready to use; "Approve, leave off"
installs it switched off. To change a tool Steve built, press Improve on
its card and say what it should do better: Steve builds a new version,
checks it again, and asks you to approve that one too.

The [module guide](modules.md) describes the sandbox and the grants. The
[build guide](self-upgrade.md) describes how Steve writes and tests a module.


## Your keys

Setup puts your keys in the Windows Credential Manager, under the service
`steve`, and never in a file. **Accounts**, under **Your keys**, says which
are kept, and gives or replaces one: a new key is checked with its service
before it is kept, and Steve's next request uses it. Without the app, use a
terminal. Each `set` asks for the value without echoing it. From this
repository:

```powershell
python -m core.secrets set OPENAI_API_KEY
python -m core.secrets set OPENROUTER_API_KEY
```

Installed, Steve's own Python does the same, from any terminal:

```powershell
& "$env:LOCALAPPDATA\Programs\Steve\backend\python.exe" -I -m core.secrets set OPENAI_API_KEY
```

```powershell
& "$env:LOCALAPPDATA\Programs\Steve\backend\python.exe" -I -m core.secrets set OPENROUTER_API_KEY
```

OpenAI's key is for the models, web search and memory. OpenRouter's is for
Jev alone, the decision model that screens what Steve reads and checks what
it does on its own. Setup lets you skip it, and Steve still runs, but much
of it works only in part:

- Mail, pages and other outside text reach the model only as extracts,
  unless the optional **Local readers** are installed, from **What Steve
  may do**. Steve makes the extracts on your OpenAI key, so it spends a
  little more there.
- A new extension, a tool Steve builds among them, cannot be installed
  unless the local readers are: no one can screen its own words.
- What Steve would do on its own waits for your yes, and so do clicks on
  sites it is logged in to.
- On a mailbox or calendar that cannot make a change conditional on what
  Steve read, every change asks you first, even to Steve's own drafts.
- Background tasks are not split into parts.
- Meetings get no follow-up question, unless you ask for one after every
  meeting.

Steve says once a day that Jev has no key.

An optional key:

| Key | What it adds |
|---|---|
| `BRAVE_API_KEY` | Web search through Brave instead of OpenAI, billed separately by Brave |

See what is set, without the values:

```powershell
python -m core.secrets status
```

## From your phone

Steve can reach you, and take your answers, away from the computer. Set
one up in **Channels**, in the app's menu at your name:

- **Signal** (recommended): the app downloads signal-cli and the Java it
  runs on (about 184 MB, each file checked against its pinned hash), then
  shows a QR code to scan with your phone. Steve becomes one of your linked
  devices, and reads only Note to Self.
- **Telegram:** make a bot with @BotFather, paste its token in the app, and
  send the bot the code the app shows. Telegram is not end-to-end
  encrypted: its servers read every message.

Approving a purchase from your phone also takes a code from an
authenticator app, set up in **Settings**.

## What Steve may use

Anything that reaches beyond a conversation is off until you turn it on. In
the app's menu, open **What Steve may do** and find **What Steve may use
on this computer**:

| Switch | Needs |
|---|---|
| The sandboxed browser | Docker Desktop |
| Browsing in background tasks | the browser |
| Running code for analysis | Docker Desktop |
| Wake the computer for reminders | nothing |
| Keep the computer awake while it is plugged in | nothing |

Accounts:

- **Outlook:** added in the app, as any account is. Steve reads the
  classic Outlook already signed in on this computer and holds no token of
  its own; Steve asks Outlook nothing until you add it, and Outlook may ask
  you once whether to allow it.
- **Google Cloud (Drive and Docs, Tasks, a calendar with guests, Gmail
  through its API):** your own OAuth client, a Desktop app in your own
  Google Cloud project. The app's setup shows how to make one, takes its
  file, and signs you in in the browser for only what you chose; the
  address that signs in must be the account's. Steve keeps the client's id
  and secret and the sign-in's refresh token in the Credential Manager, and
  no file. A client left in Testing asks you to sign in again every week;
  Steve says so the day before.
- **A mailbox with a password:** Gmail takes an app password; iCloud,
  Fastmail and Yahoo have their servers filled in; **Other** takes any
  IMAP and SMTP server, at home or on this computer too, under Advanced.
  Steve tries the address and password against the server before it keeps
  them, in the Credential Manager. Steve reads and writes these over TLS
  only.
- **A calendar's private link:** a course site's, or a calendar someone
  shares, read only, at most every ten minutes. Mark it as due dates when
  its events are deadlines rather than time taken.
- **Outlook.com, Hotmail, Live and MSN:** through an MCP server on this
  computer that holds the account's sign-in; the first Steve knows is
  Softeria's ms-365-mcp-server. Steve installs it from npm at a pinned
  version, held to a reviewed lockfile, and runs it in a container of its
  own that reaches only Microsoft's sign-in and Graph. You can also run the
  server yourself and type its address (`127.0.0.1` and a port); any
  program you run could use that one too, so that way is for trying it
  out. Steve checks the server's tools against the ones it knows, keeps a
  fingerprint of them when you test the account, and stops the account if
  they change, until you test it again. Changing or deleting an event asks
  you every time, since the server cannot make a change conditional on what
  Steve read.

Every account is added in the app, under **Accounts** in its menu, starting from
its address. What has been tried, and how:

| Account | Against a real account | Against test servers |
|---|---|---|
| Outlook (classic) | the module itself: counted, every calendar entry read again by its id, and a draft with Cc and Bcc made and thrown away; sending, not yet | yes |
| Google Cloud | its code ran daily against real accounts before this release; added through the app, not yet | yes |
| Gmail with an app password | the engine itself: read and counted, a draft thrown away, a draft sent to the account's own address, an event made and deleted | yes |
| iCloud, Fastmail, Yahoo, Other | not yet | yes |
| Outlook.com, through an MCP server | the server installed from npm in its container; signing in, not yet | yes, a server built from Softeria's tool list |
| A calendar's private link | not yet | yes |

## Where your data lives

Everything Steve writes goes in one folder: `data\` in this repository when
you run it from here, `%LOCALAPPDATA%\Steve` once installed, or wherever
`STEVE_DATA_DIR` points. Your conversations, memories and settings stay
there, on this computer. An install keeps the program apart from it, in
`%LOCALAPPDATA%\Programs\Steve`.

## Updates

The app has an updater. It looks for a newer release in this project's
GitHub releases and installs it only if it is signed with the update
key built into the app; your data stays as it was. You can also run a
newer installer over the old one. An older installer does not install over
a newer one: run silently it stops, and in its window it offers only to
remove the newer version first.

## Uninstalling

In Windows' Settings, under Apps, Installed apps, choose Steve, then
Uninstall. If Steve is running, the uninstaller offers to close it. Its
window has a box, **Delete the application data**:

- **Not ticked:** the program goes, with its start at login, its sandboxes'
  profiles, its wake-up task and the browser's containers. Your data folder
  and your keys stay, for a reinstall.
- **Ticked:** your data folder goes too (from its usual place; a folder
  `STEVE_DATA_DIR` chose stays), with the browser's images and every key
  and login Steve saved in the Credential Manager.

Each thing goes by a name Steve gave it, never by a pattern another
program's names could match. To uninstall without the window, keeping your
data:

```powershell
& "$env:LOCALAPPDATA\Programs\Steve\uninstall.exe" /S
```

If you linked Signal to Steve, your phone still lists it as a linked
device. Remove it there, in Signal's settings.

## While the computer sleeps

Steve works while the computer is awake. It catches up on reminders and
other waiting work when Windows wakes. Optional switches can keep the
computer awake while plugged in or wake it for reminders. See
[Keeping it running](always-on.md).
