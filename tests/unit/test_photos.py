"""Photos sent to Steve reach the model, and are deleted a day later.

Until 13 October 2026 neither held: a photo sent to Note to Self with words
reached the turn as the words alone, a photo sent without words was dropped
unanswered, and every attachment stayed in the data folder for good.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from agent import runtime
from channels import photos
from core import db

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
WEBP = b"RIFF\x10\x00\x00\x00WEBPVP8 " + b"\x00" * 8
GIF = b"GIF89a" + b"\x00" * 16


def _file(folder, name: str, data: bytes):
    path = folder / name
    path.write_bytes(data)
    return path


# --------------------------------------------------------------------------
# What is shown, and what is said not to be
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("data", "kind"), [
    (JPEG, "image/jpeg"), (PNG, "image/png"), (WEBP, "image/webp"), (GIF, "image/gif"),
    (b"%PDF-1.7", None), (b"", None), (b"RIFF\x10\x00\x00\x00WAVEfmt ", None),
])
def test_a_photos_format_is_read_from_its_bytes(data, kind):
    assert photos.format_of(data) == kind


def test_a_photo_is_shown_as_its_bytes_say_whatever_the_channel_called_it(tmp_path):
    """The type is the phone's word for it; the model is sent what the
    file is."""
    shown = photos.load([_file(tmp_path, "a", PNG)])

    assert [(item.media_type, item.data) for item in shown] == [("image/png", PNG)]


def test_a_file_that_is_not_a_photo_is_not_shown_as_one(tmp_path):
    """Sent as a photo, a PDF named image/jpeg would fail the whole turn
    at the model, and a missing file would fail it here."""
    shown = photos.load([_file(tmp_path, "a", b"%PDF-1.7 not a photo"), tmp_path / "gone",
                         _file(tmp_path, "b", JPEG)])

    assert [item.media_type for item in shown] == ["image/jpeg"]


def test_what_steve_cannot_open_is_named_with_why(tmp_path, monkeypatch):
    """Named, so the turn knows it came and can say so; not silently left
    out, as every attachment was."""
    monkeypatch.setattr(photos, "MOST_BYTES", 2_000_000)
    small = _file(tmp_path, "small", JPEG)
    large = _file(tmp_path, "large", JPEG + b"\x00" * 2_000_000)

    out = photos.sort([
        (tmp_path / "pdf", "application/pdf", "report.pdf"),
        (tmp_path / "heic", "image/heic", None),
        (large, "image/jpeg", None),
        (small, "IMAGE/JPEG", None),
    ])

    assert out.images == [small]
    assert out.unopened == [
        "report.pdf, a file Steve cannot open",
        "a photo in a format Steve cannot read (image/heic)",
        "a photo over 2 MB",
    ]


def test_steve_looks_at_four_photos_a_message_and_says_which_it_did_not(tmp_path):
    """Six photos in one message is an album: each one is billed as
    hundreds of words, and the turn is about one question."""
    sent = [(_file(tmp_path, f"p{n}", JPEG), "image/jpeg", f"p{n}.jpg") for n in range(6)]

    out = photos.sort(sent)

    assert out.images == [path for path, _, _ in sent[:4]]
    assert out.unopened == ["p4.jpg, a photo past the 4 Steve looks at in a message",
                            "p5.jpg, a photo past the 4 Steve looks at in a message"]


@pytest.mark.parametrize(("images", "unopened", "line"), [
    (1, [], "[1 photo attached]"),
    (3, [], "[3 photos attached]"),
    (0, ["report.pdf, a file Steve cannot open"],
     "[Not opened: report.pdf, a file Steve cannot open]"),
    (1, ["a", "b"], "[1 photo attached]\n[Not opened: a; b]"),
    (0, [], ""),
])
def test_the_line_a_message_carries_says_what_came_with_it(tmp_path, images, unopened, line):
    assert photos.note([tmp_path] * images, unopened) == line


# --------------------------------------------------------------------------
# To the conversation
# --------------------------------------------------------------------------


async def test_a_message_with_photos_hands_them_to_the_conversation(tmp_path, monkeypatch):
    """The last link before the model: the bridge's photos went nowhere if
    `voice_reply` dropped them."""
    from agent.worker import answering

    photo = _file(tmp_path, "p", JPEG)
    sent = []

    class _Conversation:
        session_id = 7

        async def send(self, text, **kw):
            sent.append((text, kw))
            return SimpleNamespace(text="A heron.", role=SimpleNamespace(value="converse"),
                                   cost_usd=0.0, tools_used=[], deferred_jobs=[])

    async def not_an_approval(text, channel, card=None, spoken=False):
        return None

    monkeypatch.setattr(answering, "intercept_approval", not_an_approval)
    monkeypatch.setattr(answering, "recent_session", lambda channel: 7)
    monkeypatch.setattr(answering, "conversation", lambda session, channel="tray": _Conversation())
    # The reply is read against the cards waiting (agent/cardwords.py).
    store = db.open_store(tmp_path / "worker.db")
    monkeypatch.setattr(runtime, "conn", store)

    text, _ = await answering.voice_reply("[1 photo attached]\nwhat bird", images=[photo])
    await answering.voice_reply("no photo")
    store.close()

    assert text == "A heron."
    assert sent == [("[1 photo attached]\nwhat bird", {"images": [photo]}), ("no photo", {})]


# --------------------------------------------------------------------------
# Kept a day
# --------------------------------------------------------------------------


NOW = 1_790_000_000.0  # pinned: the sweep is about a file's age


def _aged(folder, name: str, hours: float):
    folder.mkdir(parents=True, exist_ok=True)
    path = _file(folder, name, JPEG)
    os.utime(path, (NOW - hours * 3600, NOW - hours * 3600))
    return path


def test_an_attachment_is_deleted_a_day_after_it_came_and_not_before(tmp_path, monkeypatch):
    """The maintainer, 13 October 2026: keep photos and voice notes a day,
    then delete them. Kept for good, the data folder became a second copy
    of everything the user ever sent."""
    from channels import signal

    monkeypatch.setenv("STEVE_SIGNAL_DATA", str(tmp_path / "signal"))
    received = tmp_path / "signal" / "attachments"
    old_photo = _aged(received, "old.jpg", 25)
    old_note = _aged(signal.attachment_dir(), "old.m4a", 24.5)
    fresh = _aged(received, "fresh.jpg", 23)
    (received / "a folder").mkdir()

    assert signal.sweep_attachments(now=NOW) == 2

    assert not old_photo.exists() and not old_note.exists()
    assert fresh.exists() and (received / "a folder").is_dir()


def test_sweeping_before_signal_was_ever_set_up_deletes_nothing_and_does_not_fail(
        tmp_path, monkeypatch):
    from channels import signal

    monkeypatch.setenv("STEVE_SIGNAL_DATA", str(tmp_path / "never"))

    assert signal.sweep_attachments(now=NOW) == 0


def test_the_sweep_is_one_of_the_workers_duties(monkeypatch):
    """Written and never started, it would delete nothing: started here,
    as every duty is."""
    from agent import issues, modbuild, runtime
    from agent.worker import background, duties

    begun = []

    def create_task(coroutine):
        begun.append(coroutine.cr_code)
        coroutine.close()

    monkeypatch.setattr(duties, "asyncio", SimpleNamespace(create_task=create_task))
    monkeypatch.setattr(duties, "_duties", {"state": "standby"})
    monkeypatch.setattr(duties, "_duty_tasks", [])
    monkeypatch.setattr(runtime, "jobs", SimpleNamespace(start=lambda: None))
    monkeypatch.setattr(issues, "record_deaths", lambda conn: 0)
    monkeypatch.setattr(modbuild, "recover", lambda conn: 0)

    duties.take_up_duties()

    assert background.attachment_sweep.__code__ in begun
