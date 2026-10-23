"""Every name this install shares with the rest of Windows, in one place.

Steve began as a copy of a personal agent that still runs, on its own, on
the machine it was forked on. The two share a user account, so they share
everything that account has: its environment variables, its Credential
Manager, its loopback ports, its Task Scheduler and its Run key. A name
copied unchanged into any of those is a door between them. The copy's
relay client, for one, read the other agent's relay address and token from
the user environment; started by hand, it would have dialled that relay
and, since the newest link wins there, taken the other agent's Signal away.

So each shared name is defined here and imported where it is used, and
tests/gate/test_identity.py checks the whole tree for the old ones.
"""

from __future__ import annotations

import hashlib
import os

# The product's name, and the stem of every other Windows-visible name: the
# wake task, the coding sandbox's account. The desktop app's "start at
# login" (tauri-plugin-autostart) writes a Run key value of this name, and
# the app starts the backend (ui/src-tauri/src/backend.rs).
NAME = "Steve"

# The Credential Manager service every secret is stored under
# (core/secrets.py); other stores add a suffix to it.
KEYCHAIN = "steve"

# The Credential Manager prefix of every module's secrets (core/credstore.py):
# `steve-modules/<module key>/<account key>/<NAME>`, written with win32cred
# under these exact names, so step 8's uninstall finds them by prefix.
MODULE_SECRETS = f"{KEYCHAIN}-modules"

# The service the browser's saved logins are stored under (core/browser/vault.py).
# Every keyring service is named here, so uninstall (core/uninstall.py)
# deletes each one's entries.
BROWSER_LOGINS = f"{KEYCHAIN}-browser-logins"
KEYRING_SERVICES = (KEYCHAIN, BROWSER_LOGINS)


def wake_task(install: str) -> str:
    """The scheduled task that wakes the computer for what is due
    (agent/wake.py): `SteveWake.<install>`. A task's name belongs to the
    Windows user, and registering one replaces any of the same name: one
    `SteveWake` for every install let a second install take the first's
    task, and uninstalling either take both's."""
    return f"{NAME}Wake.{install}"


# The prefix of every environment variable the product reads. Nothing in
# the product reads a variable outside it, except the standard ones
# (provider keys, PATH, LOCALAPPDATA and the like).
ENV_PREFIX = "STEVE_"

# No port: the supervisor's is chosen by the system at each start and handed
# to the app, with a token, in the run file (core/localapi.py). The fixed one,
# 8790, could be taken first by another account's program on the same PC.

# The modules' sandbox (core/sandbox/): AppContainer profiles, which belong to
# the Windows user, so every program of theirs shares the names. A profile is
# named for this install and its module, so two installs never share one, and
# tests, each with an install id of their own, never meet the real ones. The
# capability opens the module runtime to every module's container with one
# grant; the pipes are a local server's one way out (06-modules.md, sections 2
# and 7).
CONTAINER_PREFIX = f"{KEYCHAIN}."
RUNTIME_CAPABILITY = f"{KEYCHAIN}.runtime"
PIPE_PREFIX = rf"\\.\pipe\{KEYCHAIN}."


def container(install: str, module_id: str) -> str:
    """One module's AppContainer profile: `steve.<install>.m.<id>`."""
    return f"{CONTAINER_PREFIX}{install}.m.{module_id}"


def server_container(install: str, module_id: str, key: str) -> str:
    """The profile a local server runs in for one account (06-modules.md,
    section 7): `steve.<install>.s.<digest>`. Its own, so one account's
    server cannot read another's home; a digest of the module and the
    account, since the two together can run past Windows' 64 characters."""
    digest = hashlib.sha256(f"{module_id}/{key}".encode()).hexdigest()[:16]
    return f"{CONTAINER_PREFIX}{install}.s.{digest}"


def npm_container(install: str, module_id: str) -> str:
    """The profile npm installs one module's server in: `steve.<install>.n.<id>`,
    never the server's own, which never writes its installed files."""
    return f"{CONTAINER_PREFIX}{install}.n.{module_id}"


def probe_container(install: str) -> str:
    """The profile the start's check of support runs in (core/sandbox/probe.py)."""
    return f"{CONTAINER_PREFIX}{install}.probe"


def trial_container(install: str, token: str) -> str:
    """The profile one admission's tests run in (core/sandbox/testrun.py):
    `steve.<install>.t.<token>`, never the module's own."""
    return f"{CONTAINER_PREFIX}{install}.t.{token}"


def hello_key(install: str) -> str:
    """The Windows Hello key the app signs with (core/hello.py):
    `steve.<install>.hello`. Hello's keys belong to the Windows user, and
    making one replaces any of the same name, so a second install sharing
    it would leave the first's registered key unable to sign."""
    return f"{CONTAINER_PREFIX}{install}.hello"


# Docker, which belongs to the Windows user too: the label on the browser's
# containers and networks, the one carrying the install's id on everything
# Steve starts there, the prefix of their names, and of the images it builds.
# Without the install's id, uninstalling one install removed another's
# containers, and a code run's carried no label at all and were left.
DOCKER_LABEL = f"{KEYCHAIN}.browser"
DOCKER_INSTALL_LABEL = f"{KEYCHAIN}.install"
DOCKER_NAMES = f"{KEYCHAIN}-"
DOCKER_IMAGES = f"{KEYCHAIN}/"

# How Steve names itself to the services it calls, and in the commits it
# makes. A domain under `.local`, which no one can own, so a commit's author
# reaches no one. The project address identifies Steve in service requests.
PROJECT_URL = "https://github.com/steve-assistant/steve"
GIT_DOMAIN = f"{KEYCHAIN}.local"


def git_identity(role: str) -> tuple[str, str]:
    """The author name and address of a commit Steve's `role` makes."""
    return f"{NAME} {role}", f"{role}@{GIT_DOMAIN}"


# Steve's one version. It was in five places (here, through the installed
# distribution's metadata, and pyproject.toml, tauri.conf.json, Cargo.toml and
# ui/package.json), and they had drifted: pyproject said 0.1.0.dev0 where the
# rest said 0.1.0. The metadata answer was worse: an install made by copying
# the tree has no distribution, so it said "0" in every User-Agent. An update
# compares versions to refuse a downgrade, so two answers to "which version is
# this" are two answers to "is this older". tests/unit/test_one_version.py
# holds the other files to this one, and the build refuses to run when they
# differ. The worker reports it as `agent.version.STEVE_VERSION`.
VERSION = "0.1.1"


def version() -> str:
    return VERSION


def user_agent(purpose: str) -> str:
    """A User-Agent naming the product, its version and why it calls."""
    contact = f"+{PROJECT_URL}; " if PROJECT_URL else ""
    return f"{NAME}/{version()} ({contact}{purpose})"


def scrub_environment(environ=None) -> list[str]:
    """Remove the personal edition's variables (`AGENT_*`) from this process's
    environment, so neither this process nor anything it starts can read them.
    The user's environment holds that edition's relay address and token; a
    child that read them would dial its relay and take its Signal away.
    Windows compares variable names without case, so neither does this.
    Returns the names removed."""
    environ = os.environ if environ is None else environ
    gone = [name for name in list(environ) if name.upper().startswith(_OLD_PREFIX)]
    for name in gone:
        del environ[name]
    return gone


# Built, so that the tree's check for the old prefix does not find it here.
_OLD_PREFIX = "AGENT" + "_"
