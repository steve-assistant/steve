"""The doors every process a test starts shuts first.

tests/gate/fences.py shuts the doors in a test's own process. A supervisor,
worker or script a test starts is a new interpreter: it inherits the test's
environment and nothing else, so neither the port check nor the keychain
fence was there, and a worker a gate test started read the real `steve`
keychain (the audit of 12 October 2026). Python imports `sitecustomize`
from PYTHONPATH as it starts, before anything else runs, so `live_services`
puts this folder there and every Python child shuts the same doors:

- every connection off this computer, and the personal edition's port on
  it, by an audit hook for plain sockets and a wrapper for asyncio's on
  Windows, whose `ConnectEx` raises no audit event. Until step 5 only the
  port was refused, and step 3's mutation run reached OpenAI with a dummy
  key, a request no fence stopped;
- every keychain service but Steve's; Steve's own reads as it does in a
  test (API keys as dummies, the rest unset), and nothing is written or
  deleted. In a fake world (`STEVE_TEST_WORLD`, tests/fakes/world.py) it is
  the world's file instead: the run's canaries, and what the backend keeps;
  and the world's settings are applied to the backend's modules as they
  are imported (`apply_the_world`);
- the Credential Manager, where `core/credstore.py` keeps module secrets
  through pywin32: an empty one in memory instead, holding only Steve's
  names, so a module's sign-in a test set up is written nowhere real;
- Docker, by the same audit hook: no process that runs one of Docker's own
  programs starts. Modules and self-upgrade use none since step 6
  (06-modules.md, "No Docker"), so every test of them passes with Docker
  absent; the browser's calls are fenced at their own door as well. Until
  step 6 only those doors were shut: a process started anywhere else could
  reach the developer's Docker Desktop, and a module path that used Docker
  would pass on a computer that has it and fail on one that has not. The
  code tool runs in Docker on purpose, and its own tests, marked `docker`,
  are let through in their own process (fences.live_services); a process
  any test starts is refused it still.

fences.py uses the same checks, so the two cannot drift apart. Standard
library only, apart from keyring and pywin32 where they are installed: a
child may be any Python.
"""

from __future__ import annotations

import importlib.util
import ipaddress
import os
import sys
from pathlib import PureWindowsPath

PERSONAL_PORT = 8765
KEYCHAIN = "steve"
# Docker's own programs, and the shells whose command line may name one, by
# name without ".exe".
DOCKER = frozenset({"docker", "docker-compose", "com.docker.cli"})
SHELLS = frozenset({"cmd", "powershell", "pwsh", "bash", "sh"})
# Whether this process refuses Docker. Only a test marked `docker` opens it,
# for itself and until it ends (fences.live_services).
docker_shut = True


def refuse_personal_port(address) -> None:
    if isinstance(address, tuple) and len(address) >= 2 and address[1] == PERSONAL_PORT:
        raise RuntimeError(
            f"a test tried to reach port {PERSONAL_PORT}, where the personal edition "
            "listens; Steve's supervisor takes a port the system chooses")


def on_this_computer(host) -> bool:
    """A loopback address, or `localhost`. A name is not resolved here: a
    test that connects by any other name is reaching for the network."""
    if not isinstance(host, str):
        return False
    name = host.strip("[]").split("%", 1)[0].lower()
    if name == "localhost":
        return True
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        return False
    if address.version == 6 and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_loopback


def refuse_off_this_computer(address) -> None:
    """Raise for a connection a test may not make. An address that is not a
    (host, port) tuple is a pipe's or a Unix socket's path: this computer."""
    if not (isinstance(address, tuple) and len(address) >= 2):
        return
    refuse_personal_port(address)
    if not on_this_computer(address[0]):
        raise RuntimeError(
            f"a test tried to reach live {address[0]}:{address[1]}, off this computer. "
            "Inject a fake instead -- see tests/gate/fences.py")


def program(executable, command) -> str:
    """The program a process start runs: `executable` if given, else the
    first word of `command`, a list or a command line."""
    if executable:
        return os.fsdecode(executable)
    if isinstance(command, (str, bytes)):
        line = os.fsdecode(command).strip()
        if line.startswith('"'):
            return line[1:].split('"', 1)[0]
        return (line.split() or [""])[0]
    first = next(iter(command or ()), "")
    return os.fsdecode(first) if isinstance(first, (str, bytes, os.PathLike)) else ""


def _name(path: str) -> str:
    return PureWindowsPath(path.strip("\"'")).name.lower().removesuffix(".exe")


def refuse_docker(executable, command, *, shell: bool = False) -> None:
    """Raise if the process would run Docker: as its program, or, for a
    shell, anywhere in its command line -- Windows starts `shell=True` as
    `cmd.exe /c "docker ps"`."""
    found = program(executable, command)
    names = {_name(found)}
    if shell or names & SHELLS:
        line = os.fsdecode(command) if isinstance(command, (str, bytes)) else " ".join(
            os.fsdecode(part) for part in command or () if isinstance(part, (str, bytes)))
        names |= {_name(word) for word in line.replace('"', " ").split()}
    if names & DOCKER:
        raise RuntimeError(
            f"a test tried to start live Docker ({found}). Nothing in the module path uses "
            "it; inject a fake instead -- see tests/gate/fences.py")


def _audit(event: str, args: tuple) -> None:
    if event in ("socket.connect", "socket.sendto", "socket.sendmsg"):
        refuse_off_this_computer(args[1])
    elif event == "subprocess.Popen" and docker_shut:
        refuse_docker(args[0], args[1])
    elif event == "os.system" and docker_shut:
        refuse_docker(None, args[0], shell=True)


def shut_the_network() -> None:
    """Once per process: an audit hook cannot be removed, so a second would
    only check twice."""
    if getattr(sys, "_steve_network_shut", False):
        return
    sys._steve_network_shut = True
    sys.addaudithook(_audit)
    if sys.platform != "win32":
        return  # the selector loop connects through socket.connect
    from asyncio import windows_events

    real = windows_events.IocpProactor.connect

    def connect(self, conn, address):
        refuse_off_this_computer(address)
        return real(self, conn, address)

    windows_events.IocpProactor.connect = connect


def dummy_or_unset(key: str) -> str | None:
    """What Steve's keychain holds in a test: an API key a client can be
    built with (never one a provider accepts), and nothing else."""
    return f"test-{key.lower()}" if key.endswith("_API_KEY") else None


# The folder of the fake world a test started this process in
# (tests/fakes/world.py), whose `keychain.json` is the keychain: its
# secrets are the run's canaries, each a value of its own, and what the
# backend keeps (a key given in setup) is kept there, for its next read.
WORLD_ENV = "STEVE_TEST_WORLD"


def _world_keychain() -> str | None:
    folder = os.environ.get(WORLD_ENV)
    return os.path.join(folder, "keychain.json") if folder else None


def shut_the_keychain() -> None:
    try:
        import keyring
    except ImportError:
        return
    import json

    kept = _world_keychain()

    def held() -> dict:
        with open(kept, encoding="utf-8") as file:
            return json.load(file)

    def read(service: str, key: str) -> str | None:
        if not str(service).startswith(KEYCHAIN):
            raise RuntimeError(
                f"a process a test started read keychain service {service!r}, "
                f"which is not Steve's ({KEYCHAIN!r})")
        return held().get(f"{service}/{key}") if kept else dummy_or_unset(key)

    def write(service: str, key: str, value: str | None = None) -> None:
        if not kept:
            raise RuntimeError("a process a test started tried to change the real keychain")
        if not str(service).startswith(KEYCHAIN):
            raise RuntimeError(f"a process a test started wrote keychain service {service!r}")
        secrets = held()
        if value is None:
            secrets.pop(f"{service}/{key}", None)
        else:
            secrets[f"{service}/{key}"] = value
        with open(kept, "w", encoding="utf-8", newline="\n") as file:
            json.dump(secrets, file)

    keyring.get_password = read
    keyring.get_credential = lambda service, username: read(service, username) and None
    keyring.set_password = write
    keyring.delete_password = write


class _OnImport:
    """Runs code on a module as it is imported. `win32cred` is fenced so:
    importing pywin32 into every child to patch it costs about 45 ms each,
    and most never use it. A world's settings are applied so too
    (`apply_the_world`)."""

    def __init__(self, after: dict) -> None:
        self.after = after

    def find_spec(self, fullname, path=None, target=None):
        then = self.after.get(fullname)
        if then is None:
            return None
        sys.meta_path.remove(self)
        try:
            spec = importlib.util.find_spec(fullname)
        finally:
            sys.meta_path.insert(0, self)
        if spec is None or spec.loader is None:
            return spec
        load = spec.loader.exec_module

        def exec_module(module):
            load(module)
            then(module)

        spec.loader.exec_module = exec_module
        return spec


def shut_the_credential_manager() -> None:
    if "win32cred" in sys.modules:
        fence_win32cred(sys.modules["win32cred"])
    elif not any(isinstance(finder, _OnImport) and "win32cred" in finder.after
                 for finder in sys.meta_path):
        sys.meta_path.insert(0, _OnImport({"win32cred": fence_win32cred}))


def apply_the_world() -> None:
    """A world's settings for the backend it runs: each module's statements
    in its `world.json`'s `after_import`, run in the module as it is
    imported. A scenario's yes waits out a hold of seconds, not the
    product's 30; a reminder fires within a second of its time; the mail
    and calendar fakes stand in for the internet (tests/fakes/world.py).
    The product has no knob for any of these, and should not grow one for
    a test."""
    folder = os.environ.get(WORLD_ENV)
    if not folder:
        return
    import json

    try:
        with open(os.path.join(folder, "world.json"), encoding="utf-8") as file:
            given = json.load(file).get("after_import") or {}
    except FileNotFoundError:
        return

    def run(code: str):
        return lambda module: exec(code, module.__dict__)  # noqa: S102 - the test's own

    sys.meta_path.insert(0, _OnImport({name: run(code) for name, code in given.items()}))


def fence_win32cred(win32cred) -> None:
    """pywin32's Credential Manager calls, which only core/credstore.py
    makes, answered from a dict that starts empty. Names outside Steve's
    are refused, as the keychain's are."""
    if getattr(win32cred, "_steve_fenced", False):
        return
    import pywintypes

    entries: dict[str, dict] = {}

    def mine(target) -> str:
        if not str(target).startswith(KEYCHAIN):
            raise RuntimeError(
                f"a test reached the Credential Manager for {target!r}, which is not Steve's")
        return str(target)

    def missing(call: str):
        return pywintypes.error(1168, call, "Element not found.")

    def read(TargetName, Type=win32cred.CRED_TYPE_GENERIC, Flags=0):  # noqa: N803 - pywin32's
        if mine(TargetName) not in entries:
            raise missing("CredRead")
        return dict(entries[TargetName])

    def write(Credential, Flags=0):  # noqa: N803
        # As the real one, measured live on 15 October 2026: the blob only as
        # text, stored as UTF-16, at most 2,560 bytes of it. This stand-in once
        # took bytes, and so passed a store the real one refused.
        blob = Credential.get("CredentialBlob", "")
        if not isinstance(blob, str):
            raise TypeError(f"Objects of type '{type(blob).__name__}' can not be converted "
                            "to Unicode.")
        stored = blob.encode("utf-16-le")
        if len(stored) > 2560:  # CRED_MAX_CREDENTIAL_BLOB_SIZE
            raise pywintypes.error(1783, "CredWrite", "The stub received bad data.")
        entries[mine(Credential["TargetName"])] = {**Credential, "CredentialBlob": stored}

    def delete(TargetName, Type=win32cred.CRED_TYPE_GENERIC, Flags=0):  # noqa: N803
        if entries.pop(mine(TargetName), None) is None:
            raise missing("CredDelete")

    def enumerate_(Filter=None, Flags=0):  # noqa: N803
        prefix = mine((Filter or "").rstrip("*"))
        found = [dict(v) for k, v in entries.items() if k.startswith(prefix)]
        if not found:
            raise missing("CredEnumerate")
        return found

    win32cred.CredRead = read
    win32cred.CredWrite = write
    win32cred.CredDelete = delete
    win32cred.CredEnumerate = enumerate_
    win32cred._steve_fenced = True


if __name__ == "sitecustomize":
    shut_the_network()
    shut_the_keychain()
    shut_the_credential_manager()
    apply_the_world()
