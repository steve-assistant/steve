"""Every settings file says which shape it is in, so an update carries the
user's choices over (core/settingsfile.py).

No settings file carried a version before step 9. A setting renamed in a
release would have been read as a name nobody knows and dropped, and the
user's choice lost on update without a word. These pin the steps a file is
read through, the version it is written at, and that a newer release's file
is never written over after a downgrade.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time

import pytest

from agent import appsettings, models, models_switch
from agent.toolset import linkedin
from agent.worker import desk
from core import knobs, passphrase, settingsfile


def _renamed(settings: dict) -> dict:
    """Version 1 to 2 in the tests' made-up release: `colour` became `theme`."""
    out = dict(settings)
    if "colour" in out:
        out["theme"] = out.pop("colour")
    return out


TWO_STEPS = (settingsfile.stamped, _renamed)


def _raw(path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_a_file_from_before_versions_is_read_as_it_was_and_written_back_at_version_one():
    """Every user who updates has files with no version: they must read as
    they always did, or the first update after this one loses what they set."""
    knobs.knobs_path().parent.mkdir(parents=True, exist_ok=True)
    knobs.knobs_path().write_text(json.dumps({"MAX_DAY_PINGS": 5}), encoding="utf-8")

    assert knobs.read() == {"MAX_DAY_PINGS": 5}
    knobs.write({"MORNING": "09:30"})

    assert _raw(knobs.knobs_path()) == {"version": 1, "MAX_DAY_PINGS": 5, "MORNING": "09:30"}


def test_a_renamed_setting_is_carried_over_by_its_step_not_dropped(tmp_path):
    """The failure this exists for: a release renames a setting, and the old
    name, read as unknown, is left out, so the user's choice silently goes."""
    held = tmp_path / "app.json"
    held.write_text(json.dumps({"version": 1, "colour": "ink", "size": "larger"}),
                    encoding="utf-8")
    assert settingsfile.load(held, TWO_STEPS) == {"theme": "ink", "size": "larger"}

    # A file from before versions goes through every step, the stamp and the rename.
    held.write_text(json.dumps({"colour": "paper"}), encoding="utf-8")
    assert settingsfile.load(held, TWO_STEPS) == {"theme": "paper"}

    settingsfile.save(held, settingsfile.load(held, TWO_STEPS), TWO_STEPS)
    assert _raw(held) == {"version": 2, "theme": "paper"}


def test_a_file_from_a_newer_steve_is_read_as_far_as_understood_and_never_written_over():
    """After a downgrade, writing the file in the older shape would lose the
    newer release's settings for good, and the user may go back up."""
    path = knobs.knobs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    newer = json.dumps({"version": 7, "MAX_DAY_PINGS": 5, "A_KNOB_FROM_LATER": 1})
    path.write_text(newer, encoding="utf-8")

    assert knobs.read() == {"MAX_DAY_PINGS": 5}
    with pytest.raises(knobs.BadKnob, match="newer Steve"):
        knobs.write({"MORNING": "09:30"})
    assert path.read_text(encoding="utf-8") == newer

    appsettings.path().write_text(json.dumps({"version": 7, "theme": "ink"}), encoding="utf-8")
    with pytest.raises(appsettings.BadChoice, match="newer Steve"):
        appsettings.write({"theme": "paper"})
    assert appsettings.current()["theme"] == "ink"


@pytest.mark.parametrize("version", ["1", -1, True, 1.5, None])
def test_a_version_that_is_not_a_count_makes_the_file_unreadable_not_guessed(tmp_path,
                                                                             version):
    """A version that is not a count can't say which steps to run; guessing
    one could run a rename over a file already renamed."""
    held = tmp_path / "knobs.json"
    held.write_text(json.dumps({"version": version, "MAX_DAY_PINGS": 5}), encoding="utf-8")

    with pytest.raises(ValueError):
        settingsfile.load(held, TWO_STEPS)
    assert knobs.read(held) == {}


def test_every_settings_file_is_written_with_its_version():
    """A writer that skipped the version would leave a file read as version 0
    by the next release, and run through steps it has already been through."""
    knobs.write({"MAX_DAY_PINGS": 5})
    assert passphrase.choose("correct horse battery") is None
    asyncio.run(desk.switch(False, by="a test"))
    linkedin.Pacer()._save()
    # Only written while a switch holds; its shape is models_switch.switch's.
    models_switch._write_switches({"chat": {"candidate": {}, "since": "2026-10-18 09:00:00"}})
    appsettings.write({"theme": "ink"})

    files = [knobs.knobs_path(), passphrase.path(), desk.state_file(), linkedin.pacer_file(),
             models.active_path(), appsettings.path()]
    # Each at its last version: one step for most, two for app.json since the
    # quick bar's own hotkey went (appsettings._one_hotkey).
    assert {path.name: _raw(path).get("version") for path in files} == {
        path.name: 1 for path in files} | {appsettings.path().name: len(appsettings.STEPS)}


def test_two_threads_of_one_process_can_write_one_settings_file_at_once(tmp_path):
    """Its scratch file was named by the process alone, so two threads of
    the worker shared it, and the second to replace found it gone and
    raised: a passphrase guess went uncounted that way (the review of
    steps 7 to 9, finding 6). Each write has a scratch file of its own."""
    path = tmp_path / "settings" / "app.json"
    start = threading.Barrier(8)
    raised: list[Exception] = []

    def write(n: int) -> None:
        start.wait()
        for _ in range(20):
            try:
                settingsfile.save(path, {"theme": f"ink {n}"}, TWO_STEPS)
            except Exception as exc:  # noqa: BLE001
                raised.append(exc)

    writers = [threading.Thread(target=write, args=(n,)) for n in range(8)]
    for thread in writers:
        thread.start()
    for thread in writers:
        thread.join()

    assert raised == []
    assert _raw(path)["theme"].startswith("ink ")
    assert [p.name for p in path.parent.iterdir()] == ["app.json"], "no scratch file is left"


def test_threads_of_one_process_replace_a_settings_file_one_at_a_time(tmp_path, monkeypatch):
    """Each write reads the file and then replaces it, and Windows refuses a
    replace while another holds the file. Eight threads writing at once on a
    loaded machine refused each other past every retry, and the test above
    failed in a full run. So a process writes one at a time, and only
    another process's write needs the retries."""
    real = os.replace
    counting = threading.Lock()
    inside = most = 0

    def replace(src, dst) -> None:
        nonlocal inside, most
        with counting:
            inside += 1
            most = max(most, inside)
        try:
            time.sleep(0.005)
            real(src, dst)
        finally:
            with counting:
                inside -= 1

    monkeypatch.setattr(settingsfile.os, "replace", replace)
    path = tmp_path / "app.json"
    start = threading.Barrier(8)

    def write(n: int) -> None:
        start.wait()
        for _ in range(5):
            settingsfile.save(path, {"theme": f"ink {n}"}, TWO_STEPS)

    writers = [threading.Thread(target=write, args=(n,)) for n in range(8)]
    for thread in writers:
        thread.start()
    for thread in writers:
        thread.join()

    assert most == 1


def test_a_settings_file_is_on_the_disk_before_it_replaces_the_old_one(tmp_path, monkeypatch):
    """Replaced while its text was still in the cache, a power cut could
    keep the replace and lose the text: an empty passphrase.json reads as
    no passphrase at all, and setup asks for a new one."""
    order: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd: int) -> None:
        order.append("fsync")
        real_fsync(fd)

    def replace(src, dst) -> None:
        order.append("replace")
        real_replace(src, dst)

    monkeypatch.setattr(settingsfile.os, "fsync", fsync)
    monkeypatch.setattr(settingsfile.os, "replace", replace)
    settingsfile.save(tmp_path / "passphrase.json", {"hash": "x"}, TWO_STEPS)

    assert order == ["fsync", "replace"]
