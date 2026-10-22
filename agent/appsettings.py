"""The app's own choices, kept by the backend: the hotkey, update checks,
the theme and Reading.

They lived in the window's localStorage, inside WebView2's folder. A
reinstall, or a repair that cleared the webview's files, lost them without
a word, and nothing but the window could read them, so no test could see a
choice survive a restart. Now they are a settings file in the data folder
(`app.json`), versioned like the others (core/settingsfile.py), and the
window moves what its storage still holds here once (ui/src/appSettings.ts).

Each value is checked against what the app offers: a hand-edited file
can't hand the window a theme it has no styles for, or a hotkey that is a
paragraph.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from core import paths, settingsfile

log = logging.getLogger(__name__)


def _one_hotkey(settings: dict) -> dict:
    """Version 1 to 2: the quick bar's own key is gone. One hotkey opens it
    on a tap and talks on a hold (ui/src/api.ts, bindTapOrHold)."""
    return {name: value for name, value in settings.items() if name != "quickbar_hotkey"}


# Each change of the file's shape, oldest first (core/settingsfile.py).
STEPS = (settingsfile.stamped, _one_hotkey)

# The choices with a fixed set of answers, the first the default.
CHOICES: dict[str, tuple[str, ...]] = {
    # Follow Windows' light or dark setting, or hold one theme, or ink
    # during quiet hours (foundations.md, the ink theme).
    "theme": ("auto", "paper", "ink", "ink-in-quiet-hours"),
    # Reading (foundations.md): the text face, the hand, the size.
    "face": ("newsreader", "atkinson"),
    "hand": ("hand", "plain"),
    "size": ("normal", "larger"),
    # Each check tells the release's server this computer's address.
    "update_checks": ("on", "off"),
    # The first day's note that shows what a note is (journal.md, The first
    # day). Kept here, not in the window's storage, so a reinstall doesn't
    # bring it back.
    "sample_note": ("show", "dismissed"),
    # The desk card (presence.md): on after setup; its words, which anyone
    # passing the screen can read; on top of other windows.
    "desk_card": ("on", "off"),
    "desk_card_text": ("on", "off"),
    "desk_card_on_top": ("on", "off"),
    # Hold-to-talk, switched at the sidebar's foot. Off, a hold of the
    # hotkey or the desk card is taken as a tap, and the composer has no
    # microphone: someone who never talks to Steve isn't left a button that
    # opens the microphone.
    "talk": ("on", "off"),
    # The tour (ui/src/Tour.tsx): made due by setup's end, and not due
    # again once taken or skipped, so it shows once per install. "Show me
    # around" opens it without this.
    "tour": ("off", "due"),
}
# Accelerators as the shell's shortcut plugin reads them ("Alt+Space",
# "CommandOrControl+Shift+J"); unset, the window's default applies.
HOTKEYS = ("hotkey",)
_ACCELERATOR = re.compile(r"[A-Za-z0-9+`\-=\[\];',./\\]{1,64}")


class BadChoice(ValueError):
    pass


def path() -> Path:
    return paths.data_dir() / "app.json"


def defaults() -> dict[str, str]:
    return {name: answers[0] for name, answers in CHOICES.items()}


def checked(name: str, given: object) -> str:
    if name in CHOICES:
        if given not in CHOICES[name]:
            raise BadChoice(f"{name} is one of {', '.join(CHOICES[name])}")
        return str(given)
    if name in HOTKEYS:
        if not isinstance(given, str) or not _ACCELERATOR.fullmatch(given):
            raise BadChoice(f"{name} is not a shortcut")
        return given
    raise BadChoice(f"there is no setting {name!r}")


def read() -> dict[str, str]:
    """What the user chose, each value checked again; one that fails is left
    out, and its default stands."""
    try:
        raw = settingsfile.load(path(), STEPS) or {}
    except (OSError, ValueError):
        log.warning("%s is unreadable; the app's choices at their defaults", path())
        return {}
    out = {}
    for name, given in raw.items():
        try:
            out[name] = checked(name, given)
        except BadChoice as exc:
            log.warning("ignoring %s in %s: %s", name, path().name, exc)
    return out


def current() -> dict[str, str]:
    """Every choice: what the user chose over the defaults."""
    return defaults() | read()


def write(changes: dict) -> dict[str, str]:
    """Merge `changes` into what was chosen (None puts one back to its
    default) and return every choice. BadChoice, and nothing written, if
    any change will not do, or the file is a newer Steve's."""
    chosen = read()
    for name, given in changes.items():
        if given is None:
            if name not in CHOICES and name not in HOTKEYS:
                raise BadChoice(f"there is no setting {name!r}")
            chosen.pop(name, None)
        else:
            chosen[name] = checked(name, given)
    try:
        settingsfile.save(path(), chosen, STEPS, indent=2)
    except settingsfile.Newer as exc:
        raise BadChoice(str(exc)) from exc
    return defaults() | chosen
