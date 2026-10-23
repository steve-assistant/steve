# Steve

Steve is a personal assistant that runs on your Windows computer. It reads
the mail and calendars you connect, keeps what you tell it, searches the
web and brings you what needs your attention. It uses your own API keys.
There is no Steve server between you and your accounts or model providers.

**Status: early beta.** Download the installer from the
[latest release](https://github.com/steve-assistant/steve/releases/latest),
build it yourself with [the contributing guide](CONTRIBUTING.md), or
[run from a clone](#run-from-a-clone) of this repository.

## What you need

- Windows 10 or 11, 64-bit.
- An OpenAI API key. A separate project key with its own budget is best.
- An OpenRouter key for Jev, the decision model that checks what Steve
  reads and what it does on its own. Steve runs without one, but much of it
  then works only in part.
- Optional: Signal or Telegram for reaching you away from the computer,
  and Docker Desktop for the contained browser and running code.

An installed Steve carries its own Python. You do not need developer tools
to run it.

## Install

Download the installer, `Steve_<version>_x64-setup.exe`, from the
[latest release](https://github.com/steve-assistant/steve/releases/latest)
and run it. It installs for your Windows account alone, without
administrator rights, into `%LOCALAPPDATA%\Programs\Steve`. Your data lives
separately in `%LOCALAPPDATA%\Steve`, so an update or reinstall keeps it.

The installer is not code-signed yet. SmartScreen may say "Windows
protected your PC": choose **More info**, check that the installer came
from this project's releases, then **Run anyway**. Smart App Control, where
enabled, blocks unsigned programs, including Steve's Python; Steve cannot
run there yet.

## Run from a clone

To run Steve from its source instead of the installer, build the app from
a clone. You need Python 3.13, uv, Node.js, Rust and the Windows tools
Tauri requires. In PowerShell:

1. Clone the repository and go into it:

   ```powershell
   git clone https://github.com/steve-assistant/steve.git; cd steve
   ```

2. Install the backend's packages:

   ```powershell
   uv sync --extra dev --extra accounts
   ```

3. Install the app's packages, then build the app, pointed at this folder
   ([more on this build](CONTRIBUTING.md#the-desktop-app)):

   ```powershell
   cd ui; npm install
   ```

   ```powershell
   $env:STEVE_ROOT = (Resolve-Path ..).Path; npm run tauri build -- --no-bundle
   ```

4. Put your keys in a file named `.env`, in the repository's folder or the
   folder above it. Copy `.env.example`, which lists the keys Steve takes,
   and fill in the values. Git ignores `.env`.
5. Start `ui\src-tauri\target\release\Steve.exe`.
6. On setup's key page, Steve names the keys it found in the file, never
   their values. Choose **Use these keys**: each is checked with its
   service, and kept in Windows' Credential Manager like a key typed in.
   Steve does not read the file again. Then go on from step 2 of
   [Your first answer](#your-first-answer).

## Your first answer

1. Open Steve. Setup saves your progress if you close it partway through.
2. Enter your OpenAI key and choose a monthly spend cap, then your
   OpenRouter key. Setup lets you skip it, but then mail and pages reach
   the model only as extracts, what Steve would do on its own waits for
   your yes, and new extensions cannot be installed. The
   [user guide](docs/user-guide.md#your-keys) lists the rest. Either key
   can be added or changed later, under **Accounts**.
3. Give Steve the name to use for you and set a passphrase for high-risk
   approvals. Follow the remaining setup pages. You can connect accounts
   and a phone channel now or return to them later.
4. Choose whether Steve starts with Windows. Read **Before you start**,
   which explains what is sent off the computer, before finishing setup.
5. On today's page, type "Hello. What can you help me with?" and send it.
   A reply is your first working conversation; no connected mailbox is
   needed for it.

To speak, hold Win+Alt+S or the microphone button, then release it. Settings
shows the shortcut if another program already uses that key combination.
The microphone opens only while you hold it.

Closing the window keeps Steve in the system tray. Choose **Quit** from
the tray to stop it. Steve works while the computer is awake and catches
up on waiting work when Windows wakes.

## Your data and your say

Your conversations, memories and settings stay in Steve's data folder.
Your model requests and the information needed to answer them go to
OpenAI; Jev's checks go through OpenRouter when configured. Connected
accounts and optional services receive what they need to do their work.
Steve sends no telemetry. Read [Privacy](PRIVACY.md) for destinations,
retention and how to turn each part off.

Actions that need approval appear as cards. Read their action and targets
before saying yes. High-risk approvals also need a passphrase, Windows
Hello or the phone's second factor. The [security model](docs/security-model.md)
and [known security gaps](SECURITY.md) explain the boundaries.

## Known gaps

- The installer is not code-signed. SmartScreen warns and Smart App Control
  can block it.
- The local voice add-on is not offered in an install. Desk voice uses
  OpenAI by default, and hands-free voice is not in this version.
- Some account sign-in paths have only been tested against fake servers.
  The [user guide](docs/user-guide.md#what-steve-may-use) lists which ones.

## Next

- [User guide](docs/user-guide.md): the app, accounts, phone channels,
  spend, extensions, updates and uninstalling.
- [Contributing](CONTRIBUTING.md): running from source, building and tests.
- [Security](SECURITY.md): reporting a vulnerability.

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
