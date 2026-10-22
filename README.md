# Steve

Steve is a personal assistant that runs on your Windows computer. It reads
the mail and calendars you connect, keeps what you tell it, searches the
web and brings you what needs your attention. It uses your own API keys.
There is no Steve server between you and your accounts or model providers.

**Status: early. No release is published yet.** You can build an installer
or run from source using [the contributing guide](CONTRIBUTING.md).
The steps below describe starting with an installer you built.

## What you need

- Windows 10 or 11, 64-bit.
- An OpenAI API key. A separate project key with its own budget is best.
- Recommended: an OpenRouter key for Jev, the decision model that checks
  what Steve reads and what it does on its own.
- Optional: Signal or Telegram for reaching you away from the computer,
  and Docker Desktop for the contained browser and running code.

An installed Steve carries its own Python. You do not need developer tools
to run it.

## Install

Run `Steve_0.1.0_x64-setup.exe`, made with the
[building instructions](CONTRIBUTING.md#building-the-installer).
It installs for your Windows account alone, without administrator rights,
into `%LOCALAPPDATA%\Programs\Steve`. Your data lives separately in
`%LOCALAPPDATA%\Steve`, so an update or reinstall keeps it.

The installer is not code-signed yet. SmartScreen may say "Windows
protected your PC": choose **More info**, check that this is the installer
you built, then **Run anyway**. Smart App Control, where enabled, blocks
unsigned programs, including Steve's Python; Steve cannot run there yet.

## Your first answer

1. Open Steve. Setup saves your progress if you close it partway through.
2. Enter your OpenAI key and choose a monthly spend cap. Add your OpenRouter
   key if you have one; you can skip it.
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

- No release is published yet, and the updater has no endpoint. For now,
  build a newer installer and run it over the old one.
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
