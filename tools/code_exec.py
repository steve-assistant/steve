"""Running code the model wrote, in a container, without bind mounts.

Files go in and come out via `docker cp` rather than a volume mount. That is a
deliberate choice on Windows: WSL2 projects host files through `drvfs` as mode
777, mounts from C:\\ are slow, and permissions have been observed changing
mid-session. Copying sidesteps all of it and costs a few milliseconds.

Defaults are restrictive -- no network, capped memory, capped process count,
read-only root with a small writable workspace. The model asking for network
access has to say so, which makes "this code phones home" a visible decision in
the transcript rather than an invisible default.
"""

from __future__ import annotations

import io
import shutil
import subprocess
import tarfile
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from core import identity, images, paths
from core.browser import sandbox

# By digest: the base the gate is built on too (core/browser/sandbox.py).
DEFAULT_IMAGE = sandbox.GATE_BASE

# Built once the user turns running code on; carries the libraries an
# analysis task actually needs so the model is not tempted to ask for network
# access just to pip install pandas. With pypdf: the sandboxed browser reads a
# quarantined download's text here, with no network, rather than parsing a
# stranger's PDF on the laptop. The packages are pinned with their hashes in
# code_exec_requirements.txt, beside this file, and the image is run by the
# ID its build recorded (`core/images.py`).
ANALYSIS_DOCKERFILE = f"""\
FROM {DEFAULT_IMAGE}
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt
WORKDIR /work
"""
REQUIREMENTS = Path(__file__).with_name("code_exec_requirements.txt")


def _analysis_context() -> dict[str, bytes]:
    return {"Dockerfile": ANALYSIS_DOCKERFILE.encode(),
            "requirements.txt": REQUIREMENTS.read_bytes()}


def analysis_image() -> str:
    """The analysis image's tag, a hash of everything it is built from."""
    return images.tag("pyexec", _analysis_context())


DEFAULT_TIMEOUT_S = 30
MAX_OUTPUT_CHARS = 20_000

WORKDIR = "/work"
OUTDIR = "/work/out"


class DockerUnavailable(RuntimeError):
    """Docker is not reachable. Deliberately not falling back to the host."""


@dataclass
class ExecResult:
    ok: bool
    stdout: str
    stderr: str
    exit_code: int | None
    duration_s: float
    timed_out: bool = False
    artifacts: list[str] = field(default_factory=list)
    artifact_dir: str | None = None
    error: str | None = None

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "exit_code": self.exit_code,
            "duration_s": round(self.duration_s, 2),
            "timed_out": self.timed_out,
            "artifacts": self.artifacts,
            "artifact_dir": self.artifact_dir,
            "error": self.error,
        }


def _docker(*args: str, timeout: float | None = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _docker_bytes(
    *args: str, stdin: bytes | None = None, timeout: float | None = 60
) -> subprocess.CompletedProcess:
    """Run docker with binary stdio, for streaming tar in and out."""
    return subprocess.run(
        ["docker", *args], input=stdin, capture_output=True, timeout=timeout
    )


def _tar_bytes(source: Path) -> bytes:
    """Tar a directory's contents (not the directory itself) into memory."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for path in sorted(source.rglob("*")):
            archive.add(path, arcname=path.relative_to(source).as_posix())
    return buffer.getvalue()


# How to ask whether the daemon answers. Not `docker info`, which asked the
# same question and also ran every CLI plugin Docker Desktop installs to
# list them -- thirteen processes and 1.3 s of CPU a time, against 0.06 s
# for this (measured 5 October 2026), and the status page asked once a
# minute all day.
DAEMON_VERSION = ("version", "--format", "{{.Server.Version}}")


def available() -> bool:
    """Is the Docker daemon reachable right now?"""
    if shutil.which("docker") is None:
        return False
    try:
        return _docker(*DAEMON_VERSION, timeout=15).returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def status() -> dict:
    """Explain the sandbox's state, for the status endpoint and the model."""
    if shutil.which("docker") is None:
        return {"available": False, "reason": "docker CLI not found on PATH"}
    probe = _docker(*DAEMON_VERSION, timeout=15)
    if probe.returncode != 0:
        return {
            "available": False,
            "reason": "docker daemon not running (start Docker Desktop)",
            "detail": probe.stderr.strip()[:200],
        }
    return {
        "available": True,
        "server_version": probe.stdout.strip(),
        "analysis_image_built": _analysis_image_id() is not None,
    }


def _run(args: list[str], timeout: float) -> subprocess.CompletedProcess:
    """`_docker` in the shape `core/images` calls it, looked up when called."""
    return _docker(*args, timeout=timeout)


def _analysis_image_id() -> str | None:
    # One image asked about by its ID, rather than every image listed.
    return images.built(analysis_image(), _run)


def ensure_analysis_image(force: bool = False) -> bool:
    """Build the analysis image if Steve has not built it here. Slow once,
    then cached."""
    if not available():
        raise DockerUnavailable("docker daemon not running")

    if _analysis_image_id() is not None and not force:
        return False

    images.build(analysis_image(), _analysis_context(), _run, timeout=900)
    return True


def run_python(
    code: str,
    *,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    files: dict[str, str] | None = None,
    network: bool = False,
    image: str | None = None,
    collect_artifacts: bool = True,
) -> ExecResult:
    """Execute Python in a throwaway container.

    `files` are extra files placed alongside the script. Anything the code
    writes to /work/out is copied back out and listed in `artifacts` -- that is
    the channel for charts and generated documents.

    Never raises for ordinary failures: a traceback or a timeout is a result
    the model should read and react to, not an exception that kills the turn.
    """
    import time

    if not available():
        return ExecResult(
            ok=False,
            stdout="",
            stderr="",
            exit_code=None,
            duration_s=0.0,
            error="Docker is not running. Start Docker Desktop to enable code execution.",
        )

    # Prefer the batteries-included image when it exists, so the model is not
    # tempted to request network access merely to pip install pandas.
    image = image or _analysis_image_id() or DEFAULT_IMAGE
    name = f"{identity.KEYCHAIN}-exec-{uuid.uuid4().hex[:12]}"
    started = time.perf_counter()
    host_tmp = Path(tempfile.mkdtemp(prefix="exec-", dir=paths.scratch_dir()))
    # Outside host_tmp on purpose: the staging directory is deleted in the
    # finally block, and artifacts are the one thing that must survive it.
    artifact_dir = paths.artifacts_dir() / name
    container_made = False

    try:
        (host_tmp / "main.py").write_text(code, encoding="utf-8", newline="\n")
        (host_tmp / "out").mkdir(exist_ok=True)
        for rel, content in (files or {}).items():
            target = host_tmp / rel
            # Keep a malicious or careless relative path inside the staging dir.
            if not target.resolve().is_relative_to(host_tmp.resolve()):
                return ExecResult(
                    ok=False, stdout="", stderr="", exit_code=None, duration_s=0.0,
                    error=f"refusing to write outside the workspace: {rel}",
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="\n")

        # The container idles first and runs the code via `exec` afterwards.
        #
        # The obvious create -> cp -> start sequence does not work alongside
        # the hardening: `docker cp` into a stopped container writes to the
        # rootfs, which --read-only forbids, and the tmpfs at /work does not
        # exist until the container is actually running. Starting an idle
        # container first gives us a live tmpfs to copy into, and has the
        # bonus that the timeout then covers only the code, not image startup.
        create = _docker(
            "create",
            "--name", name,
            "--network", "bridge" if network else "none",
            "--memory", "1g",
            "--memory-swap", "1g",   # equal to --memory disables swap
            "--cpus", "2",
            "--pids-limit", "256",
            "--read-only",           # writable space is granted explicitly below
            "--tmpfs", f"{WORKDIR}:rw,exec,size=256m",
            "--tmpfs", "/tmp:rw,exec,size=64m",
            "--workdir", WORKDIR,
            # A worker ended mid-run leaves the container; uninstall finds it by this.
            *sandbox.install_labels(),
            "--entrypoint", "sleep",
            image,
            "infinity",
        )
        if create.returncode != 0:
            return ExecResult(
                ok=False, stdout="", stderr=create.stderr[-2000:], exit_code=None,
                duration_s=time.perf_counter() - started,
                error=f"could not create container (is the image {image!r} pulled?)",
            )
        container_made = True

        launched = _docker("start", name, timeout=60)
        if launched.returncode != 0:
            return ExecResult(
                ok=False, stdout="", stderr=launched.stderr[-2000:], exit_code=None,
                duration_s=time.perf_counter() - started, error="container failed to start",
            )

        # Files go in as a tar stream through `exec`, not via `docker cp`.
        # `docker cp` inspects the container's read-only flag and refuses
        # before it ever looks at the destination, so it cannot write into the
        # tmpfs even though the tmpfs itself is writable. Piping tar to a
        # process running *inside* the container sidesteps that entirely and
        # lets the rootfs stay read-only.
        copied = _docker_bytes(
            "exec", "-i", name, "tar", "-x", "-C", WORKDIR, stdin=_tar_bytes(host_tmp)
        )
        if copied.returncode != 0:
            return ExecResult(
                ok=False, stdout="",
                stderr=copied.stderr.decode("utf-8", "replace")[-2000:],
                exit_code=None,
                duration_s=time.perf_counter() - started, error="failed to copy sources in",
            )

        timed_out = False
        try:
            run = _docker(
                "exec", "--workdir", WORKDIR, name,
                "python", "-u", f"{WORKDIR}/main.py",
                timeout=timeout_s,
            )
            stdout, stderr, exit_code = run.stdout, run.stderr, run.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            _docker("kill", name, timeout=30)
            stdout, stderr, exit_code = "", f"timed out after {timeout_s}s", None

        artifacts: list[str] = []
        if collect_artifacts and not timed_out:
            # Same constraint in reverse: stream a tar out of the container.
            dumped = _docker_bytes("exec", name, "tar", "-c", "-C", OUTDIR, ".")
            if dumped.returncode == 0 and dumped.stdout:
                with tarfile.open(fileobj=io.BytesIO(dumped.stdout), mode="r") as archive:
                    artifacts = sorted(
                        member.name.removeprefix("./")
                        for member in archive.getmembers()
                        if member.isfile()
                    )
                    artifact_dir.mkdir(parents=True, exist_ok=True)
                    archive.extractall(artifact_dir, filter="data")

        return ExecResult(
            ok=(exit_code == 0) and not timed_out,
            stdout=stdout[:MAX_OUTPUT_CHARS],
            stderr=stderr[:MAX_OUTPUT_CHARS],
            exit_code=exit_code,
            duration_s=time.perf_counter() - started,
            timed_out=timed_out,
            artifacts=artifacts,
            artifact_dir=str(artifact_dir) if artifacts else None,
        )

    finally:
        if container_made:
            _docker("rm", "-f", name, timeout=60)
        shutil.rmtree(host_tmp, ignore_errors=True)
