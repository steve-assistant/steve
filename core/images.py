"""Which image Docker runs for Steve: the one Steve built, by its ID.

Steve's images were found and run by tag (`steve/browser-gate:<hash>`,
`steve/pyexec:2`), and a tag is a name that any account allowed to use
Docker can point at an image of its own. The browser's tag is a hash of
public source, so it can be worked out in advance. Such an image would
have been run as Steve's browser, given a saved login's cookies, and typed
the user's passwords (step 11's audit, C4). An account that may use Docker
can do worse on its own: it is an administrator of the machine in all but
name, and docs/security-model.md counts it as one. But Steve should not run
what it did not build.

So a build writes the image's ID to a file (`docker build --iidfile`), the
ID is kept in the data folder in a file that is the user's alone (`RECORD`),
and a container starts from that ID, never from the tag. An image Steve did
not build is never found; the tag is a label for people.

The bases are pinned by digest and the Python packages by hash (F2), in
the Dockerfiles and requirements files that `tag` hashes, so one source
builds one image whichever day it is built, and a change to any of it is a
new tag rather than a stale image.

Docker is always reached through the caller's runner: the browser's
(`core/browser/sandbox._docker`) and the code tool's, which the test fences
already cover. This module has no runner of its own to fence.
"""

from __future__ import annotations

import hashlib
import subprocess
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path

from core import identity, paths, settingsfile

Runner = Callable[[list[str], float], subprocess.CompletedProcess]

RECORD = "docker-images.json"
STEPS = (settingsfile.stamped,)
_RECORDING = threading.Lock()


def tag(name: str, context: dict[str, bytes]) -> str:
    """`steve/<name>:<hash>`, the hash of every file the image is built from."""
    digest = hashlib.sha256()
    for file, data in sorted(context.items()):
        digest.update(file.encode() + b"\0" + data + b"\0")
    return f"{identity.DOCKER_IMAGES}{name}:{digest.hexdigest()[:12]}"


def _path() -> Path:
    return paths.data_dir() / RECORD


def _recorded() -> dict[str, str]:
    try:
        held = settingsfile.load(_path(), STEPS) or {}
    except (OSError, ValueError):
        return {}
    images = held.get("images")
    if not isinstance(images, dict):
        return {}
    return {k: v for k, v in images.items() if isinstance(k, str) and isinstance(v, str)}


def built(image_tag: str, run: Runner) -> str | None:
    """The ID of the image Steve built here as `image_tag`, if Docker still
    has it; None when Steve never built it, or it has been removed. What the
    tag points at now does not matter."""
    image_id = _recorded().get(image_tag)
    if not image_id:
        return None
    found = run(["image", "inspect", "--format", "{{.Id}}", image_id], 30)
    return image_id if found.returncode == 0 and found.stdout.strip() == image_id else None


def remember(image_tag: str, image_id: str) -> None:
    """Record `image_id` as the image Steve built as `image_tag`."""
    with _RECORDING:
        held = _recorded()
        held[image_tag] = image_id
        settingsfile.save(_path(), {"images": held}, STEPS, private=True)


def build(image_tag: str, context: dict[str, bytes], run: Runner, *, timeout: float) -> str:
    """Build `context` (file name to bytes) as `image_tag`, record the ID the
    build made, and return it. RuntimeError when the build fails."""
    with tempfile.TemporaryDirectory(prefix="image-build-", dir=paths.scratch_dir()) as tmp:
        folder = Path(tmp) / "context"
        folder.mkdir()
        for file, data in context.items():
            (folder / file).write_bytes(data)
        id_file = Path(tmp) / "id"
        done = run(["build", "--iidfile", str(id_file), "-t", image_tag, str(folder)], timeout)
        if done.returncode != 0:
            raise RuntimeError(f"could not build {image_tag}: {done.stderr[-800:]}")
        try:
            image_id = id_file.read_text(encoding="utf-8").strip()
        except OSError:
            image_id = ""
    if not image_id.startswith("sha256:"):
        raise RuntimeError(f"the build of {image_tag} gave no image ID")
    remember(image_tag, image_id)
    # Older images, about 2 GB each for the browser: all but the one this
    # replaces. That one is kept: a build in a worktree runs ahead of the
    # live worker, which goes on using the previous image until it is
    # swapped, and on 1 October removing it left the live browser "not
    # built" until then. `docker images` lists newest first.
    repo = image_tag.split(":", 1)[0]
    older = [old for old in run(["images", repo, "--format", "{{.Repository}}:{{.Tag}}"],
                                30).stdout.split() if old != image_tag]
    for old in older[1:]:
        run(["rmi", old], 120)
    return image_id
