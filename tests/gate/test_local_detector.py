"""The local injection detector reads every token of an item, stops at the
first window it reads as an injection, and installs only the files pinned.

core/detector.py decides when Jev cannot answer and no pattern flagged the
item (the maintainer, 11 October 2026: "Run a local open source prompt injection
detector model"). These pin what must hold whatever model sits behind it:

- an item is read whole, in windows that overlap, so an injection past the
  first 512 tokens -- where a detector that cut the item short would never
  look -- or placed across a seam is whole in one of them;
- a window at the threshold flags the item, and reading stops there;
- an item past the last window is flagged unread: padding buys nothing;
- a link is read as its host and the words in it: its encodings dropped,
  its words kept and unescaped, and a link the parser refuses read as it
  came;
- one item is read at a time, so a run of them during an outage holds two
  cores, not two for each;
- setup installs a file only if it hashes as pinned, from the pinned
  revision.

The model is checked outside the suite, where it is installed: `python -m
core.screen redteam --local` runs the attack set through it.
"""

from __future__ import annotations

import hashlib
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from core import detector

FIRST, LAST = 1, 2
# A fake injection: these three tokens, together in one window.
ATTACK = [997, 998, 999]


class _Tokenizer:
    """A token per word; "ATTACK", punctuation aside, is the three tokens of
    `ATTACK`. Keeps the texts it was given."""

    def __init__(self):
        self.texts: list[str] = []

    def encode(self, text, add_special_tokens=True):
        assert not add_special_tokens, "the windows carry the markers"
        self.texts.append(text)
        ids = []
        for word in text.split():
            ids.extend(ATTACK if word.strip("()<>.,:") == "ATTACK" else [10])
        return SimpleNamespace(ids=ids)

    def token_to_id(self, token):
        return {"[CLS]": FIRST, "[SEP]": LAST}[token]


class _Session:
    """Scores a window an injection if it holds all of `ATTACK` in a row:
    what it was asked, and how many windows it read at once at most."""

    def __init__(self, delay=0.0):
        self.windows: list[list[int]] = []
        self.delay = delay
        self.active = self.most = 0
        self._count = threading.Lock()

    def run(self, outputs, feed):
        with self._count:
            self.active += 1
            self.most = max(self.most, self.active)
        time.sleep(self.delay)
        window = [int(i) for i in feed["input_ids"][0]]
        self.windows.append(window)
        found = any(window[i:i + 3] == ATTACK for i in range(len(window)))
        with self._count:
            self.active -= 1
        return [np.array([[0.0, 9.0] if found else [9.0, 0.0]])]


@pytest.fixture
def session(monkeypatch):
    s = _Session()
    monkeypatch.setattr(detector, "_load", lambda: (_Tokenizer(), s))
    return s


def _words(n: int) -> str:
    return " ".join(["word"] * n)


def test_an_item_is_read_whole_in_windows_that_overlap():
    ids = list(range(100, 5_100))

    parts = detector.windows(ids, FIRST, LAST)

    assert all(p[0] == FIRST and p[-1] == LAST and len(p) <= detector.WINDOW for p in parts)
    inner = [p[1:-1] for p in parts]
    assert [i for i in ids if not any(i in part for part in inner)] == [], "every token is read"
    for one, two in zip(inner, inner[1:], strict=False):
        assert one[-detector.OVERLAP:] == two[:detector.OVERLAP]
    assert detector.windows([5, 6], FIRST, LAST) == [[FIRST, 5, 6, LAST]]


def test_an_injection_past_the_first_window_is_read(session):
    """A detector that read only the first 512 tokens -- as a tokenizer set
    to truncate would have it -- passes an injection behind a page of
    padding."""
    reading = detector.read(f"{_words(3_000)} ATTACK {_words(50)}")

    assert reading.flagged and reading.windows > 1


def test_an_injection_across_a_seam_is_whole_in_one_window(session):
    size = detector.WINDOW - 2
    # The attack's three tokens straddle the end of the first window.
    reading = detector.read(f"{_words(size - 1)} ATTACK {_words(size)}")

    assert reading.flagged


def test_reading_stops_at_the_first_window_over_the_threshold(session):
    reading = detector.read(f"ATTACK {_words(4_000)}")

    assert reading.flagged and reading.windows == 1 and len(session.windows) == 1


def test_a_window_at_the_threshold_flags_the_item_and_one_below_does_not():
    assert detector.Reading(detector.THRESHOLD, 1, False).flagged
    assert not detector.Reading(detector.THRESHOLD - 0.01, 1, False).flagged


def test_an_ordinary_item_is_read_to_its_end_and_passed(session):
    reading = detector.read(_words(2_000))

    assert not reading.flagged and reading.windows == len(session.windows) > 1


def test_an_item_past_the_last_window_is_flagged_unread(session):
    """Seconds a window: past `MAX_WINDOWS` the reply would wait half a
    minute. Flagged, the item is extracted, as Jev's `MAX_CHUNKS` has it."""
    step = detector.WINDOW - 2 - detector.OVERLAP
    reading = detector.read(_words(step * (detector.MAX_WINDOWS + 1)))

    assert reading.flagged and reading.too_long and session.windows == []


def test_a_link_is_read_as_its_host_and_words_without_its_encodings(monkeypatch):
    """As they came, PIGuard read tracking links -- a long encoded token, a
    mail gateway's wrapper -- as injections, and flagged sixteen of thirty
    of their mails."""
    tokenizer = _Tokenizer()
    monkeypatch.setattr(detector, "_load", lambda: (tokenizer, _Session()))

    detector.read("Your order: https://t.example/ltrk/eyJ0eXAiOiJKV1Qi.Zm9vYmFy9?utm_source=mail2"
                  " and <https://gw.example/?url=https%3A%2F%2Fshop.example%2Fsale>.")

    assert tokenizer.texts == ["Your order: (link to t.example: ltrk utm source) and "
                               "<(link to gw.example: url https shop example sale)>."]


def test_an_instruction_written_into_a_link_is_still_read(session):
    """Only encodings are dropped: what an attacker writes in a link reaches
    the detector as words, its escapes undone -- twice, as a mail gateway
    wraps a link that was escaped already."""
    assert detector.read("Details: https://x.example/please/ATTACK/now").flagged
    assert detector.read("Details: https://x.example/?q=please%20ATTACK%20now").flagged
    assert detector.read("Details: https://gw.example/?url=https%3A%2F%2Fx.example"
                         "%2F%3Fq%3Dplease%2520ATTACK").flagged


def test_a_link_the_parser_refuses_is_read_as_it_came(session):
    """Were it to raise, the item would go unread, and an item the detector
    cannot read passes as it came: one stray bracket would be a way past
    it."""
    assert detector.read("Click https://[x ATTACK now").flagged


def test_one_item_is_read_at_a_time(monkeypatch):
    """Screens run on threads; the detector holds `THREADS` cores for an
    item, and two items at once would hold twice as many beside their desk's
    audio."""
    slow = _Session(delay=0.02)
    monkeypatch.setattr(detector, "_load", lambda: (_Tokenizer(), slow))

    threads = [threading.Thread(target=detector.read, args=(_words(1_200),)) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert slow.most == 1 and len(slow.windows) > 3


def test_setup_installs_only_files_that_hash_as_pinned(monkeypatch, tmp_path):
    """A file is moved into place only after its hash is checked, and it is
    asked for at the pinned revision, never at whatever the repository holds
    today."""
    monkeypatch.setenv("STEVE_MODEL_CACHE", str(tmp_path))
    good = {"model.onnx": b"weights", "tokenizer.json": b"vocabulary"}
    monkeypatch.setattr(detector, "FILES",
                        {name: hashlib.sha256(body).hexdigest() for name, body in good.items()})
    served = dict(good, **{"model.onnx": b"tampered"})
    asked: list[str] = []

    def download(url, to):
        asked.append(url)
        body = served[url.rsplit("/", 1)[1]]
        to.write_bytes(body)
        return hashlib.sha256(body).hexdigest()

    monkeypatch.setattr(detector, "_download", download)

    with pytest.raises(RuntimeError, match="not the pinned"):
        detector.setup()
    assert not detector.installed() and list(detector.home().glob("*.part")) == []
    assert asked[0] == (f"https://huggingface.co/{detector.REPO}/resolve/{detector.REVISION}"
                        f"/model.onnx")

    served.update(good)
    detector.setup()
    asked.clear()
    detector.setup()

    assert detector.installed() and asked == [], "a whole file already there is kept"
