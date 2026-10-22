"""The local injection detector: PIGuard, on the laptop's CPU, when Jev cannot answer.

Since Phase D (11 October 2026) the injection screen enforces: what Jev flags
is extracted or withheld (`core/screen.py`). An item Jev could not answer for
-- an outage, no key -- was read as it came unless a pattern caught it, and an
attacker who knows the patterns writes around them. The user's answer for that
case (11 October 2026): "Run a local open source prompt injection detector
model" in Jev's place. The screen asks this when Jev did not answer and no
pattern flagged the item.

PIGuard (Li et al., ACL 2025; MIT) is DeBERTa-v3-base trained to tell an
injection from ordinary text that merely uses an injection's words --
"ignore", "instructions", "system" -- which mail and pages are full of: the
paper found detectors trained without that flag such text at close to
chance. It runs here as the ONNX export filip-w/PIGuard-onnx, checked against
the published weights (leolee99/PIGuard, `UPSTREAM`) before it was pinned: the
same tokens on 34 texts, logits within 1.1e-5. The revision and each file's
hash are pinned. `python -m core.detector setup` installs them once; nothing
fetches them at the moment they are needed, because an outage that took Jev
away may have taken the network with it.

Links are read short. As they came, PIGuard read tracking links -- a long
encoded token, a mail gateway's wrapper -- as injections, and flagged
sixteen of thirty of their mails. So it is given each link as its host and the
words in it, dropping only what has a digit in it: encodings, not words.
The words stay so that an instruction written into a link is still read.

A 512-token window takes about 1.9 seconds on one laptop core, and a
typical page is two to five windows. Quantized to int8, the model was
barely faster and changed 8 of the red team's 30 answers, so it runs as
exported.

Frozen with the screen, so the agent cannot loosen the detector it is judged
by.
"""

from __future__ import annotations

import functools
import hashlib
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

from core import addons, paths

REPO = "filip-w/PIGuard-onnx"
REVISION = "571ef1d438f3119ec596e6000f44442414a02fdf"
FILES = {
    "model.onnx": "327a210290e469bc2cb1c2467a2d5f9dd36ef8f07bf642af42f1bb979d8e5fb5",
    "tokenizer.json": "5124ef2ead1a10a717703bc436de7f353da76d6340e4587719b42b1693707964",
}
# The weights the export was checked against, and the revision checked.
UPSTREAM = ("leolee99/PIGuard", "dd78b24e330193a22d2293ac66922dd4f982f563")

# A window scoring at least this is an injection. The argmax (0.5) flags
# ordinary text; 0.9 is the operating point the export's authors measured.
THRESHOLD = 0.9
# What the model reads at once, in tokens, its two markers included. A longer
# item is read in windows overlapping by `OVERLAP`, so an injection sitting
# across a seam is whole in one of them -- as Jev's parts overlap.
WINDOW = 512
OVERLAP = 128
# Past this many windows the item is not read and counts as flagged: at about
# 1.4 seconds a window, more would hold a reply for half a minute. Sixteen
# cover the longest page the agent reads (20,000 characters) and nearly all
# mail; a long forward is extracted instead, as Jev's `MAX_CHUNKS` does.
MAX_WINDOWS = 16
# Of their sixteen; the desk's audio shares the rest.
THREADS = 2


@dataclass
class Reading:
    """What the detector made of one item."""

    p_injection: float | None  # the highest any window scored; None if not read
    windows: int  # how many it read before deciding
    too_long: bool  # more than `MAX_WINDOWS`: not read, and flagged

    @property
    def flagged(self) -> bool:
        return self.too_long or (self.p_injection or 0.0) >= THRESHOLD


def home() -> Path:
    """Where the pinned files live: beside the embedding model, in the
    model cache (`STEVE_MODEL_CACHE`, else data/models)."""
    return paths.models_dir() / "piguard" / REVISION[:12]


def installed() -> bool:
    return all((home() / name).is_file() for name in FILES)


def windows(ids: list[int], first: int, last: int) -> list[list[int]]:
    """The item as the model reads it: whole, between its two markers, if it
    fits in one window; else in windows that overlap by `OVERLAP` tokens and
    together cover every token."""
    size = WINDOW - 2
    if len(ids) <= size:
        return [[first, *ids, last]]
    step = size - OVERLAP
    parts = []
    for start in range(0, len(ids), step):
        parts.append([first, *ids[start:start + size], last])
        if start + size >= len(ids):
            break
    return parts


# A link: a scheme or "www.", up to whitespace or the bracket or quote that
# closes it.
LINK = re.compile(r"(?:https?://|www\.)[^\s<>\"')\]]+", re.IGNORECASE)


def _shortened(match: re.Match[str]) -> str:
    raw = match.group(0)
    try:
        parts = urlsplit(raw if "://" in raw else f"http://{raw}")
    except ValueError:
        # Read as it came: a link the parser refuses must not be a way past
        # the detector.
        return raw
    # Decoded twice: a mail gateway's wrapper carries the link it wraps
    # encoded, and that link's own escapes encoded again.
    rest = unquote(unquote(f"{parts.path} {parts.query} {parts.fragment}"))
    words = [w for w in re.split(r"[\W_]+", rest) if w.isalpha()]
    host = parts.hostname or "a site"
    return f"(link to {host}: {' '.join(words)})" if words else f"(link to {host})"


def _shorten_links(text: str) -> str:
    """The text with each link as its host and the words in it (the module's
    docstring says why)."""
    return LINK.sub(_shortened, text)


# One item at a time: screens run on threads, and two items read at once
# would hold twice the cores for the same total time.
_lock = threading.Lock()


@functools.cache
def _load():
    """The tokenizer and the model, loaded at the first item and kept for the
    process (about 1 GB): an outage sends items in a run, and loading takes
    seconds. `tests/gate/fences.py` refuses it."""
    if not installed():
        raise FileNotFoundError(f"PIGuard is not installed in {home()} "
                                f"(python -m core.detector setup)")
    import onnxruntime
    from tokenizers import Tokenizer

    tokenizer = Tokenizer.from_file(str(home() / "tokenizer.json"))
    # Every token is read: a tokenizer that cut at 512 would hide anything past
    # the first window.
    tokenizer.no_truncation()
    tokenizer.no_padding()
    options = onnxruntime.SessionOptions()
    options.intra_op_num_threads = THREADS
    options.inter_op_num_threads = 1
    options.log_severity_level = 3
    session = onnxruntime.InferenceSession(str(home() / "model.onnx"), options,
                                           providers=["CPUExecutionProvider"])
    return tokenizer, session


def _score(session, window: list[int]) -> float:
    """P(injection) for one window: the softmax of the model's two logits,
    benign and injection."""
    import numpy as np

    ids = np.array([window], dtype=np.int64)
    logits = session.run(None, {"input_ids": ids, "attention_mask": np.ones_like(ids)})[0][0]
    exp = np.exp(logits - logits.max())
    return float(exp[1] / exp.sum())


def read(text: str) -> Reading:
    """How likely the text is to be an injection: its windows read in order,
    stopping at the first that crosses `THRESHOLD`, its links read short.
    Blocking -- seconds of CPU a window -- and one item at a time. Raises if
    the detector is not installed or fails."""
    with _lock:
        tokenizer, session = _load()
        ids = tokenizer.encode(_shorten_links(text), add_special_tokens=False).ids
        parts = windows(ids, tokenizer.token_to_id("[CLS]"), tokenizer.token_to_id("[SEP]"))
        if len(parts) > MAX_WINDOWS:
            return Reading(None, 0, True)
        best = 0.0
        for number, window in enumerate(parts, 1):
            best = max(best, _score(session, window))
            if best >= THRESHOLD:
                return Reading(best, number, False)
        return Reading(best, len(parts), False)


def _download(url: str, to: Path) -> str:
    """One file, streamed to disk; its SHA-256. `tests/gate/fences.py`
    refuses it."""
    import httpx2

    digest = hashlib.sha256()
    with httpx2.stream("GET", url, follow_redirects=True, timeout=120.0) as response, \
            to.open("wb") as out:
        response.raise_for_status()
        for block in response.iter_bytes(1 << 20):
            digest.update(block)
            out.write(block)
    return digest.hexdigest()


def setup() -> Path:
    """Install the pinned files in `home()`, each checked against its hash
    before it is moved into place. A file already there and whole is kept."""
    target = home()
    target.mkdir(parents=True, exist_ok=True)
    for name, sha in FILES.items():
        path = target / name
        if path.is_file() and addons.file_sha256(path) == sha:
            continue
        partial = path.with_name(name + ".part")
        got = _download(f"https://huggingface.co/{REPO}/resolve/{REVISION}/{name}", partial)
        if got != sha:
            partial.unlink()
            raise RuntimeError(f"{name} from {REPO} hashed {got}, not the pinned {sha}")
        partial.replace(path)
    return target


def _main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="The local injection detector.")
    parser.add_argument("what", choices=["setup"])
    parser.parse_args()
    print(f"PIGuard installed in {setup()}")


if __name__ == "__main__":
    _main()
