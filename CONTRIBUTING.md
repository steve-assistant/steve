# Contributing to Steve

Steve is a Windows desktop app with a Python backend. For a security
problem, follow [SECURITY.md](SECURITY.md) and report it privately. For other
bugs, include your version, the steps to reproduce and what you expected.
Leave keys, passwords and private conversations out of reports.

## Set up a checkout

You need Windows 10 or 11, Python 3.13, git and uv. Building the desktop app
also needs Node.js, Rust and the Windows tools required by Tauri. An
installed Steve carries its own Python; these are developer requirements.

From the repository root:

```powershell
uv sync --extra dev --extra accounts
.venv\Scripts\Activate.ps1
```

The tests use fake services and their own data folders. They need no live
API keys. Running Steve itself needs an OpenAI key; see the
[user guide](docs/user-guide.md#your-keys).

## Start it

Steve's background process is a supervisor that runs a worker. It listens on
127.0.0.1, on a port the system chooses at each start, and every request
needs the token it makes as it starts. Both are in its run file, which only
you can read (`core/localapi.py`). The desktop app starts it, and stops it
when you quit. From this repository, without the app, start it in a
terminal:

```powershell
python -m core.supervisor
```

Say hello from another terminal. `core.localapi` reads the run file, has the
backend prove it holds the token, and only then sends it:

```powershell
python -m core.localapi post /chat message=hello
```

Ctrl+C stops it. An app started while it runs uses it and starts none of its
own (`docs/always-on.md`).

## The desktop app

The app opens on today's page: what you said and what Steve did, as a day's
journal. What needs you is a note beside it, and the rest is in the menu at
your name. It lives in the system tray, and Steve's mark sits on your
desktop as the desk card: point at it for what Steve is doing, what's next
and what needs you; click it for today, hold it to talk. Settings turns it
off, or its words, for a screen others can see. An install carries it. To run it beside this repository instead, build it from `ui/`.
`STEVE_ROOT` is the folder of this repository, read as the app is built:
the app starts the backend from there, with its `.venv`. A release build
without it names no folder of yours, and starts only a backend installed
beside it.

```powershell
npm install
```

```powershell
$env:STEVE_ROOT = "C:\path\to\steve"; npm run tauri build -- --no-bundle
```

Then run `ui\src-tauri\target\release\Steve.exe`. `--no-bundle` makes the
app alone, in about two minutes; the installer needs the update key.

## Building the installer

The installer carries Steve's code, a Python of its own with every library
Steve uses, and the app. Each file is listed with its hash in a manifest,
signed with the update key. The app checks every file against it, and
looks for any it doesn't list, before it starts the backend, and stops at a file
that changed. Updates are signed with the same key.

1. In `ui/`, once:

   ```powershell
   npm install
   ```

2. Make your update key, once. Give it a password, keep the private file
   outside the repository, and back it up: every update must be signed with
   it.

   ```powershell
   npx tauri signer generate -w C:\keys\steve-update.key
   ```

   Put the public key it prints in `ui/src-tauri/tauri.conf.json`, as
   `plugins.updater.pubkey`. The one there now is the project's release
   key: a fork's builds need their own, or the project's releases would
   be the only updates they take.

3. In the terminal you build from, give the signer the key's password:

   ```powershell
   $env:TAURI_SIGNING_PRIVATE_KEY_PASSWORD = [Net.NetworkCredential]::new("", (Read-Host -AsSecureString "Password")).Password
   ```

4. From the repository's root, build Steve's tree and sign its manifest.
   `build` refuses uncommitted changes, and `manifest` refuses a key the
   app does not trust.

   ```powershell
   python -m devtools.package build
   ```

   ```powershell
   python -m devtools.package manifest --key C:\keys\steve-update.key
   ```

5. In `ui/`, build the app and its installer. `STEVE_ROOT` must be unset,
   or the app names this repository's folder, and the remap writes your
   profile's folder as `~` wherever the compiler names a library's source:

   ```powershell
   $env:STEVE_ROOT = $null; $env:CARGO_ENCODED_RUSTFLAGS = "--remap-path-prefix=$env:USERPROFILE=~"; $env:TAURI_SIGNING_PRIVATE_KEY = "C:\keys\steve-update.key"; npm run tauri build -- --config src-tauri/tauri.release.conf.json
   ```

   The installer is
   `ui\src-tauri\target\release\bundle\nsis\Steve_0.1.0_x64-setup.exe`,
   about 100 MB. The build takes about 15 minutes, most of it compressing.

6. From the repository's root, check that the app names none of your
   folders:

   ```powershell
   python -m devtools.package scan ui\src-tauri\target\release\Steve.exe
   ```

7. Write the updater's file, `latest.json`, beside the installer. Every
   install reads it to find a newer version. The command first checks the
   installer's signature as the app will: made by the key the app trusts,
   over the installer's bytes, and naming this version. If any of these
   fails, every install would refuse the update without telling anyone.
   The notes are a text file you write.

   ```powershell
   python -m devtools.package latest ui\src-tauri\target\release\bundle\nsis\Steve_0.1.0_x64-setup.exe --notes notes.md
   ```

8. Publish the release on GitHub, tagged `v` and the version, at the
   commit the installer was built from. Attach the installer, its `.sig`
   and `latest.json`. The app looks for `latest.json` in the latest
   release, which is never a draft or a pre-release, so publishing is what
   offers it to every install.

   ```powershell
   gh release create v0.1.0 --target COMMIT --title "Steve 0.1.0" --notes-file notes.md ui\src-tauri\target\release\bundle\nsis\Steve_0.1.0_x64-setup.exe ui\src-tauri\target\release\bundle\nsis\Steve_0.1.0_x64-setup.exe.sig ui\src-tauri\target\release\bundle\nsis\latest.json
   ```

## Check a change

Run these from the repository root with the environment activated:

```powershell
python -m devtools.check
```

Before a commit, include the affected tests that start processes:

```powershell
python -m devtools.check --processes
```

At the end of a series, and before a push or merge, run the full suite:

```powershell
python -m devtools.check --full
```

For a new guard test, show that it fails against the code from before the
fix. `python -m devtools.prove` does that in a scratch worktree. Use
`python -m devtools.mutate` to check that deliberate breakage is caught.
See [Testing](docs/testing.md) for the full workflow and the sandbox runs.

## Conventions

- Keep files in LF, including files written on Windows. With Python,
  pass `newline="\n"` to `Path.write_text`.
- Keep shared names in `core/identity.py`, use only `STEVE_*` environment
  variables apart from standard provider and system variables, and build
  backend data paths through `core/paths.py`.
- Give tests names that state the guarantee. Explain in their docstrings
  why it matters. Use small local fakes; never reach a live account.
- Pin the clock in tests of time-dependent behaviour.
- Use the shared helpers listed in [AGENTS.md](AGENTS.md). Module
  docstrings explain the failure the code prevents; comments explain why.
- If a frozen file changes, run `python -m core.policy --write-lock` and
  include the lock. Keep `CLAUDE.md` and `AGENTS.md` identical.
- Add schema changes as numbered files in `db/migrations/`. See the
  migration rules in `AGENTS.md` before changing an existing migration.
- Update the docs when behaviour or file names change. Commit messages
  explain what broke, why, what changed and how it was checked.

## Find your way around

- [Architecture](docs/architecture.md): the code map.
- [Testing](docs/testing.md): tests, fences and developer commands.
- [Security model](docs/security-model.md): threats and boundaries.
- [Modules](docs/modules.md): the SDK, grants and sandbox.
- [Building extensions](docs/self-upgrade.md): the module-writing pipeline.
- [Privacy](PRIVACY.md): what Steve sends and how to stop it.

Contributions are under the project's Apache-2.0 license; see
[LICENSE](LICENSE) and [NOTICE](NOTICE).
