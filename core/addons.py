"""Programs Steve downloads when the user chooses a feature: signal-cli, and
the Java it runs on. Node and the local decision model's runtime are pinned
the same way where they are used (`core/sandbox/runtime.py`,
`core/decider.py`).

Signal needs signal-cli, which is a Java program, and Java 25 to run it.
The personal edition had both installed by hand: signal-cli unpacked into
the data folder, and Java at a fixed Adoptium path that only one laptop
had. A product install has neither, and nothing checked what it ran: any
process of the user's that could write to that folder could replace a jar,
and the next start would run it holding the user's Signal keys.

So each is pinned here, frozen, by its URL, its size and the SHA-256 of
its archive, and of the files the archive unpacks to (`tree_hash`):

- **Installed when asked.** `install` downloads an archive, checks its size
  and hash before anything is unpacked, unpacks it beside the install
  (`core/paths.addons_dir`), and checks the unpacked files' hash before
  moving them into place. A download that does not match is deleted.
- **Checked before every start.** `verify` hashes the unpacked files again,
  and anything that does not match is not run: the caller says so instead.
- **Not shipped in the installer.** Only a user who chooses Signal needs
  the 180 MB download.

The pins were taken on 13 October 2026: signal-cli's archive hashes as
GitHub records for the release, and Java's as Adoptium publishes.

Blocking, all of it: call it off the event loop.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import tarfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from core import paths

log = logging.getLogger("addons")


@dataclass(frozen=True)
class Addon:
    """One pinned download."""

    name: str
    version: str
    url: str
    size: int  # of the archive, in bytes
    sha256: str  # of the archive
    top: str  # the one folder the archive holds, or "" for files at its top
    tree: str  # of the files it unpacks to (`tree_hash`)
    what: str  # for the user: "signal-cli", "Java"


SIGNAL_CLI = Addon(
    name="signal-cli",
    version="0.14.8",
    url="https://github.com/AsamK/signal-cli/releases/download/v0.14.8/signal-cli-0.14.8.tar.gz",
    size=125_387_917,
    sha256="ccd408e831eff7e41ebaaf309704840bb00d78a7869f35ad700dbae5b5a5bb65",
    top="signal-cli-0.14.8",
    tree="5eefe8ab76012ab63d54fae39335ddccd77024da21aa733a612c91a49dc8882e",
    what="signal-cli",
)

# Eclipse Temurin's Java 25 runtime for Windows x64: signal-cli 0.14.8 is
# compiled to class file 69, which nothing older runs.
JAVA = Addon(
    name="java",
    version="25.0.4.1+1",
    url="https://github.com/adoptium/temurin25-binaries/releases/download/"
        "jdk-25.0.4.1%2B1/OpenJDK25U-jre_x64_windows_hotspot_25.0.4.1_1.zip",
    size=58_475_080,
    sha256="4c95451cea98556def2c54f7782933f52a26d4a36bd85e1d59f0364464828b07",
    top="jdk-25.0.4.1+1-jre",
    tree="c377c70ba9ca73204ff60f0541fb0c84119af3a3ce2bac75bc359c52c10f767d",
    what="Java",
)

ALL = (SIGNAL_CLI, JAVA)

TIMEOUT_S = 120.0


class AddonError(RuntimeError):
    """An add-on could not be installed, and why, as the app can show it."""


def home(addon: Addon) -> Path:
    """Where `addon` is unpacked: its archive's one folder, renamed for
    its version, so a new version never mixes with an old one's files."""
    return paths.addons_dir() / f"{addon.name}-{addon.version}"


def tree_hash(folder: Path, *, skip: frozenset[str] = frozenset()) -> str:
    """One SHA-256 over every file under `folder`: each file's path, as
    written with forward slashes, and its own SHA-256, in path order. A
    file changed, added, removed or renamed changes it. A folder named in
    `skip` is passed over wherever it is (a checkout's `__pycache__`)."""
    digest = hashlib.sha256()
    files = sorted((path.relative_to(folder).as_posix(), path)
                   for path in folder.rglob("*")
                   if path.is_file() and not skip & set(path.relative_to(folder).parts))
    for name, path in files:
        digest.update(f"{name}\0{file_sha256(path)}\n".encode())
    return digest.hexdigest()


def verify(addon: Addon, folder: Path | None = None) -> str | None:
    """Why `addon` may not be run, or None if its files are the pinned
    ones. `folder` is where it was unpacked, if not its `home`."""
    folder = folder or home(addon)
    if not folder.is_dir():
        return f"{addon.what} is not installed"
    if tree_hash(folder) != addon.tree:
        return (f"{addon.what} has changed since it was installed, so it is not run;"
                " set it up again to replace it")
    return None


def install(addon: Addon, *, progress: Callable[[int, int], None] | None = None,
            folder: Path | None = None) -> Path:
    """`addon` in place and checked: its home, or `folder` if given (the
    module runtime unpacks Node into its own, core/sandbox/runtime.py).
    Downloaded only if what is there is not the pinned files;
    `progress(bytes so far, of)` is told as the download goes. `AddonError`
    if it cannot be."""
    folder = folder or home(addon)
    if verify(addon, folder) is None:
        return folder
    archive = paths.scratch_dir() / f"{addon.name}-{addon.version}.download"
    unpacked = folder.parent / f".{addon.name}-{addon.version}.partial"
    try:
        got = _download(addon.url, archive, most=addon.size, progress=progress)
        if got != addon.sha256 or archive.stat().st_size != addon.size:
            raise AddonError(f"{addon.what} did not download as published (its hash was"
                             f" {got[:12]}, not {addon.sha256[:12]}), so it was not installed")
        shutil.rmtree(unpacked, ignore_errors=True)
        _unpack(archive, unpacked)
        if not (unpacked / addon.top).is_dir():
            raise AddonError(f"{addon.what}'s archive did not hold {addon.top}")
        if tree_hash(unpacked / addon.top) != addon.tree:
            raise AddonError(f"{addon.what} unpacked to files other than the pinned ones,"
                             " so it was not installed")
        if folder.exists():
            shutil.rmtree(folder)
        (unpacked / addon.top).replace(folder)
    except AddonError:
        raise
    except Exception as exc:  # noqa: BLE001 - the app shows why
        raise AddonError(f"{addon.what} could not be installed: {_why(exc)}") from exc
    finally:
        archive.unlink(missing_ok=True)
        shutil.rmtree(unpacked, ignore_errors=True)
    log.info("installed %s %s in %s", addon.name, addon.version, folder)
    return folder


def _unpack(archive: Path, into: Path) -> None:
    """Every file of a .zip or .tar.gz under `into`, and nothing outside
    it. The archive's hash is checked before this runs, so its content is
    known; this refuses a path that climbs out all the same."""
    into.mkdir(parents=True)
    root = into.resolve()
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as zipped:
            for member in zipped.infolist():
                if not (root / member.filename).resolve().is_relative_to(root):
                    raise AddonError(f"the archive names a path outside its folder:"
                                     f" {member.filename}")
            zipped.extractall(root)
        return
    with tarfile.open(archive, "r:gz") as tarred:
        # `data` refuses links and paths that leave the folder, and devices.
        tarred.extractall(root, filter="data")


def file_sha256(path: Path) -> str:
    """A file's SHA-256, read in blocks rather than whole."""
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def _download(url: str, to: Path, *, most: int,
              progress: Callable[[int, int], None] | None = None) -> str:
    """One archive, streamed to disk: its SHA-256. Stopped past `most`
    bytes, which is the pinned size. `tests/gate/fences.py` refuses it."""
    import httpx2

    digest = hashlib.sha256()
    done = 0
    with httpx2.stream("GET", url, follow_redirects=True, timeout=TIMEOUT_S) as response, \
            to.open("wb") as out:
        response.raise_for_status()
        for block in response.iter_bytes(1 << 20):
            done += len(block)
            if done > most:
                raise AddonError(f"the download from {url} ran past its {most:,} bytes")
            digest.update(block)
            out.write(block)
            if progress is not None:
                progress(done, most)
    return digest.hexdigest()


def _why(exc: BaseException) -> str:
    import httpx2

    if isinstance(exc, httpx2.TimeoutException):
        return f"the download stalled for {TIMEOUT_S:.0f} seconds"
    if isinstance(exc, httpx2.HTTPStatusError):
        return f"the server answered {exc.response.status_code}"
    if isinstance(exc, httpx2.TransportError):
        return "the server could not be reached"
    if isinstance(exc, OSError):
        return f"the files could not be written ({exc.strerror or exc})"
    return f"{type(exc).__name__}: {exc}"[:200]
