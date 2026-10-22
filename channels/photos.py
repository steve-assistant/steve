"""Photos the user sends, shown to the model with their message.

Until 13 October 2026 nothing did. A photo sent to Note to Self with words
reached the turn as the words alone, and the model answered as if it had
seen nothing; a photo sent without words was dropped, with no reply at all.

What holds now:
- a photo in a format the model reads (JPEG, PNG, WebP, GIF) goes to it
  with the message, up to `MOST_IMAGES` of them, each at most `MOST_BYTES`;
- anything else that came is named in a few words (`Sorted.unopened`), so
  the turn knows it was sent and the user is told it was not opened;
- the bytes are checked as well as the type the channel reported, so a
  file named a photo is not sent as one.

The files themselves are the channel's, kept a day and then deleted
(channels/signal.py `sweep_attachments`). Which came with which message is
kept (`link`), so the day shows them on it while they last; the app's own
come through agent/worker/app_photos.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from core import db

# What OpenAI reads as an image, by the type the channel reports.
FORMATS = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif"})
# A phone's photo is a few megabytes; OpenAI takes up to 20 MB an image.
MOST_BYTES = 20_000_000
# A message of many photos is an album to file, not to read in one turn.
MOST_IMAGES = 4


@dataclass
class Sorted:
    images: list[Path] = field(default_factory=list)
    unopened: list[str] = field(default_factory=list)


def sort(attached: list[tuple[Path, str, str | None]]) -> Sorted:
    """Each of `attached` -- a file, the type the channel says it is, and
    the name it was sent under, if any -- as a photo to show, or a few words
    on why it is not shown."""
    out = Sorted()
    for path, content_type, name in attached:
        kind = (content_type or "").lower()
        if not kind.startswith("image/"):
            why = "a file Steve cannot open"
        elif kind not in FORMATS:
            why = f"a photo in a format Steve cannot read ({kind})"
        elif _size(path) > MOST_BYTES:
            why = f"a photo over {MOST_BYTES // 1_000_000} MB"
        elif len(out.images) >= MOST_IMAGES:
            why = f"a photo past the {MOST_IMAGES} Steve looks at in a message"
        else:
            out.images.append(path)
            continue
        out.unopened.append(f"{name}, {why}" if name else why)
    return out


def note(images: list[Path], unopened: list[str]) -> str:
    """The line the message carries for what came with it, as the
    conversation records it: "[1 photo attached]", "[Not opened: ...]"."""
    lines = []
    if images:
        lines.append("[1 photo attached]" if len(images) == 1
                     else f"[{len(images)} photos attached]")
    if unopened:
        lines.append(f"[Not opened: {'; '.join(unopened)}]")
    return "\n".join(lines)


def link(conn, message_id: int, images: list[Path]) -> None:
    """Keep which photos came with the user's message `message_id`, in
    order, so the day shows them on it (agent/worker/journal.py)."""
    with db.transaction(conn):
        conn.executemany("INSERT INTO message_photos (message_id, n, path) VALUES (?, ?, ?)",
                         [(message_id, n, str(path)) for n, path in enumerate(images)])


def load(images: list[Path]) -> list:
    """The photos as the model is given them (pydantic-ai's
    `BinaryContent`), each by the format its bytes show; one whose bytes
    are not a photo is left out. BLOCKING: it reads the files."""
    from pydantic_ai import BinaryContent

    shown = []
    for path in images:
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if (kind := format_of(data)) is not None and len(data) <= MOST_BYTES:
            shown.append(BinaryContent(data=data, media_type=kind))
    return shown


def format_of(data: bytes) -> str | None:
    """The image format `data`'s first bytes show, or None."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return None


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0
