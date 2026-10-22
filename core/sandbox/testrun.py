"""A module's own tests, run where its code will run: in a sandbox.

Run in the backend, a module's tests would run code from outside with the
user's files, network and secrets all in reach, before anyone approved it,
and would prove it works with what it will never have (06-modules.md,
section 5). So they run in an AppContainer, on the runtime that will run
the module, with its files read-only to them, so that a test cannot change
what it tests, and under a job's caps on memory, processes and time.

Not in the module's own container: a module imported under an installed
one's id, waiting for the card that would replace it, would run with the
installed one's grants, its state among them. Each run has a container of
its own (`profiles.for_trial`), deleted after.

pytest and its few dependencies are in the runtime's `testing/` folder,
which nothing but this run puts on the path, so a module never imports them
when it runs for the user.

The coding pipeline that builds a module runs its checks through
`for_drafts`, the one runner it is given: pytest here, with only the
arguments the pipeline's own calls use, and ruff in the backend, which
reads the files and runs none of them. Anything else is refused. The
pipeline once ran `python -m pytest` in the repository it changed; a
draft's conftest would have run in the backend with everything in reach.
"""

from __future__ import annotations

import re
import secrets
import subprocess
import threading
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path

from core.sandbox import grants, launch, profiles, runtime
from core.sandbox import job as jobs
from steve_sdk.manifest import ID

TIMEOUT_S = 120.0
LIMITS = jobs.Limits(memory=512 << 20, processes=1, cpu_cores=1, ui=True,
                     die_on_unhandled=True)
MAX_SAID = 4_000  # the end of pytest's report, which the card shows
MAX_DRAFT_SAID = 200_000  # what the pipeline reads failures from; its tools cut it again

# Started with the module's id, its folder, the runtime's `testing/` and
# pytest's own arguments. Above the module's folder is only what the
# container may not read, and pytest looks upward twice: for a config, so
# `-c` names the manifest, which has no pytest table; and at the folder
# above each one it collects, so it collects `tests/` and looks for
# conftests no higher. Given the module's folder, it stopped at a denied
# stat of the folder above. No cache: the folder is read-only, and a cache
# would be one run's way to leave something for the next.
#
# The verdict is the line it prints once pytest has returned, with the seal
# the run gave it on stdin, read before any of the module's code: a test
# that ended the process itself (`os._exit(0)`) exited 0 as a pass does,
# and passed (step 11's audit, B5). Code that dug the seal out of the
# interpreter's frames could still forge it, and is not code a review passes.
#
# Nothing here opens NUL, which a less-privileged container on Windows
# Server may not: CI's runner refused it, and every module's tests failed
# before the first one ran (22 October 2026). pytest opens it twice, for
# its descriptor capture's stdin and as its logging plugin's file, so
# output is captured at sys.stdout and sys.stderr, and the logging plugin,
# with its `caplog`, is off.
CHILD = """
import sys
from pathlib import Path
seal = sys.stdin.readline().strip()
name, folder, testing = sys.argv[1:4]
sys.path.insert(0, testing)
from steve_sdk.mount import mount
mount(name, Path(folder))
import pytest
tests = str(Path(folder) / "tests")
code = int(pytest.main(["-p", "no:cacheprovider", "-p", "no:logging", "--capture=sys",
                        "--import-mode=importlib",
                        "--rootdir", folder, "-c", str(Path(folder) / "manifest.toml"),
                        "--confcutdir", tests, *sys.argv[4:]]))
print()
print(seal, code, flush=True)
sys.exit(code)
"""
# Said in place of a verdict the run never gave.
UNSEALED = "[the run ended before pytest gave its verdict]"

# pytest's exit codes: every test passed; none was found; the arguments
# were wrong, which is also what a refusal here answers.
PASSED, NO_TESTS, USAGE = 0, 5, 4

# What the pipeline's calls pass pytest, and nothing more: a path or node
# id under tests/, a -k of plain words, and the report's shape. A `-p`
# would load a plugin, a `--rootdir` or `-c` would move where it looks,
# and any of them is the draft's to write.
TARGET = re.compile(
    r"^tests(/(?!\.{1,2}(/|$|::))[A-Za-z0-9_.\-]+)*(::[A-Za-z0-9_.\[\]\-]+)*$")
KEYWORD = re.compile(r"^[A-Za-z0-9_ ]{1,400}$")
_PYTEST_FLAGS = {"-q", "--tb=short", "-rf", "-rfE"}
# Ruff's, which reads the files it is given and runs none of them: the
# draft's own paths, never one above it.
# Never starting with a dash, which ruff would read as one of its options.
_PART = r"(?!\.{1,2}(/|$))(?!-)[A-Za-z0-9_.\-]+"
_LINT_PATH = re.compile(rf"^(\.|{_PART}(/{_PART})*)$")
_RUFF_FLAGS = {"--fix", "--quiet"}


@dataclass(frozen=True)
class Ran:
    """How a module's tests went: whether every one passed, and the end of
    what pytest said."""

    ok: bool
    said: str


def run(module_id: str, folder: Path, *, lpac: bool = True,
        timeout_s: float = TIMEOUT_S) -> Ran:
    """Run the tests in `tests/` among the module `module_id`'s files in
    `folder`, in a container made for this run alone."""
    if not (folder / "tests").is_dir():
        return Ran(False, "it has no tests")
    code, said, timed_out = _pytest(module_id, folder, ["-q", "tests"], lpac=lpac,
                                    timeout_s=timeout_s)
    if len(said) > MAX_SAID:
        said = f"[the first {len(said) - MAX_SAID} characters left out]\n{said[-MAX_SAID:]}"
    if timed_out:
        return Ran(False, f"its tests ran past {timeout_s:.0f} seconds and were stopped")
    if code == NO_TESTS:
        return Ran(False, "it has no tests")
    return Ran(code == PASSED, said)


def _pytest(module_id: str, folder: Path, args: list[str], *, lpac: bool,
            timeout_s: float) -> tuple[int | None, str, bool]:
    root = runtime.ready()
    python = runtime.python(root)
    container = profiles.ensure(profiles.for_trial())
    err: list[bytes] = []
    seal = secrets.token_hex(16)
    try:
        grants.grant(folder, container.sid, grants.READ)
        argv = [str(python), *runtime.FLAGS, "-c", CHILD, module_id, str(folder),
                str(runtime.testing(root)), *args]
        with launch.start(argv, container=container, cwd=folder, limits=LIMITS,
                          env=launch.environment([python.parent]),
                          capabilities=runtime.capabilities(lpac), lpac=lpac) as process:
            process.stdin.write(f"{seal}\n".encode())
            process.stdin.close()
            drain = threading.Thread(target=lambda: err.append(process.stderr.read()),
                                     daemon=True)
            drain.start()
            with process.deadline(timeout_s):
                out = process.stdout.read()
                code = process.wait(timeout_s)
            drain.join(5)
            timed_out = process.timed_out
    finally:
        grants.revoke(folder, container.sid)
        profiles.delete(container.name)
    if not timed_out:
        code, out = _verdict(code, out, seal)
    return code, (out + b"".join(err)).decode("utf-8", "replace").strip(), timed_out


def _verdict(code: int | None, out: bytes, seal: str) -> tuple[int | None, bytes]:
    """pytest's exit code, if the run's last line is the seal's with the
    code the process exited with, and what it said without that line; None,
    and why, if not."""
    said, _, last = out.rstrip().rpartition(b"\n")
    if code is not None and last.strip() == f"{seal} {code}".encode():
        return code, said
    return None, out.rstrip() + f"\n{UNSEALED}".encode()


# -- the coding pipeline's runner --------------------------------------------


def pytest_args(args: list[str]) -> list[str] | None:
    """`args` as the pipeline passed them, if they are only what its calls
    use; None if anything else is there. `-p no:cacheprovider` is dropped:
    the child always passes it."""
    kept, i = [], 0
    while i < len(args):
        arg = args[i]
        following = args[i + 1] if i + 1 < len(args) else None
        if arg in _PYTEST_FLAGS or TARGET.match(arg):
            kept.append(arg)
        elif arg == "-p" and following == "no:cacheprovider":
            i += 1
        elif arg == "-k" and following is not None and KEYWORD.match(following):
            kept += [arg, following]
            i += 1
        elif arg.startswith("--ignore=") and TARGET.match(arg.removeprefix("--ignore=")):
            kept.append(arg)
        else:
            return None
        i += 1
    return kept


def ruff_args(args: list[str]) -> list[str] | None:
    """`args` after `ruff`, likewise: `check`, the draft's own paths, and
    the few options the pipeline uses."""
    if args[:1] != ["check"]:
        return None
    kept, i, rest = [], 0, args[1:]
    while i < len(rest):
        arg = rest[i]
        following = rest[i + 1] if i + 1 < len(rest) else None
        paths = (following or "").split(",")
        if arg in _RUFF_FLAGS or _LINT_PATH.match(arg):
            kept.append(arg)
        elif ((arg == "--output-format" and following == "concise")
              or (arg == "--extend-exclude" and all(_LINT_PATH.match(p) for p in paths))):
            kept += [arg, following]
            i += 1
        else:
            return None
        i += 1
    return kept


def draft_id(folder: Path) -> str:
    """The id the draft's manifest gives it, which its tests import it as;
    "" if it has none yet, or one no module may have."""
    try:
        found = tomllib.loads((folder / "manifest.toml").read_text(encoding="utf-8")).get("id")
    except (OSError, ValueError):
        return ""
    return found if isinstance(found, str) and ID.match(found) else ""


def ruff_binary() -> str | None:
    """Ruff's own program, found by its package; None where it is not
    installed. Never `python -m ruff` from the draft: with the draft as the
    working folder, Python would look there first for `ruff`."""
    try:
        from ruff.__main__ import find_ruff_bin
        return str(find_ruff_bin())
    except (ImportError, FileNotFoundError):
        return None


def for_drafts(*, lpac: bool = True, ruff=ruff_binary):
    """The runner `core.coder.pipeline.run` is given for a module draft:
    `(argv, cwd, timeout_s=) -> core.coder.tools.Ran`. `ruff` finds ruff's
    program, or None; a fake in tests."""
    from core.coder.tools import Ran as Done

    def refused(why: str) -> Done:
        return Done(USAGE, f"refused: {why}", 0.0)

    def runner(argv: list[str], cwd, timeout_s: float = 600) -> Done:
        folder, started = Path(cwd), time.monotonic()
        program = argv[2] if len(argv) > 2 and argv[1] == "-m" else ""
        if program == "pytest":
            args = pytest_args(list(argv[3:]))
            if args is None:
                return refused("pytest takes only a test path under tests/, -k and the "
                               "report's options here")
            module_id = draft_id(folder)
            if not module_id:
                return refused("the module needs a manifest.toml with its id before its "
                               "tests can import it")
            code, said, timed_out = _pytest(module_id, folder, args, lpac=lpac,
                                            timeout_s=timeout_s)
            if len(said) > MAX_DRAFT_SAID:
                said = said[-MAX_DRAFT_SAID:]
            return Done(USAGE if code is None else code, said, time.monotonic() - started,
                        timed_out)
        if program == "ruff":
            args = ruff_args(list(argv[3:]))
            if args is None:
                return refused("ruff takes only `check`, the module's own paths and the "
                               "report's options here")
            binary = ruff()
            if binary is None:
                # Not a failure the coder could fix: say so, and let the
                # suite and the judge decide.
                return Done(0, "lint unavailable: ruff is not installed with Steve, so "
                               "nothing was linted", 0.0)
            # `--isolated`: no configuration from the draft, which could
            # turn every rule off; `--no-cache`: nothing left in the module.
            try:
                done = subprocess.run([binary, "check", "--isolated", "--no-cache", *args],
                                      cwd=folder, capture_output=True, timeout=timeout_s,
                                      stdin=subprocess.DEVNULL)
            except subprocess.TimeoutExpired:
                return Done(USAGE, "ruff ran too long and was stopped",
                            time.monotonic() - started, True)
            said = (done.stdout + done.stderr).decode("utf-8", "replace").strip()
            return Done(done.returncode, said, time.monotonic() - started)
        return refused("only pytest and ruff run here")

    return runner
