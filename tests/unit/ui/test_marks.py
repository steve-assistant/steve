"""The mark and the hand-drawn marks are generated, and stay what the
logo and the design say.

Both are written by `devtools/marks.py`: the mark from the logo's own file,
the tray's pictures from the mark, the hand-drawn marks from their centre
lines. A generated file edited by hand, or a source changed without running
the generator, drifts from the logo quietly: it still looks like a mark,
just not quite Steve's. These tests hold the files to their source, and the
design's table to the marks the app has.
"""

from __future__ import annotations

import re
import struct
from pathlib import Path

from devtools import marks

REPO = Path(__file__).resolve().parents[3]
TRAY = REPO / "ui" / "src-tauri" / "icons" / "tray"


def test_the_marks_module_is_what_the_logo_and_the_marks_source_write_now():
    """marks.ts is generated; a hand edit, or a logo changed without
    running the generator, would ship a mark that isn't the logo."""
    shipped = (REPO / "ui" / "src" / "marks.ts").read_text(encoding="utf-8")
    assert shipped == marks.written(), "run `python -m devtools.marks`"


def test_each_hand_drawn_mark_has_the_variants_the_design_gives_it():
    """foundations.md's Marks table is what the app's screens are designed
    from: a mark it names that the app lacks is a hole in a screen, and a
    count that differs means neighbours repeat more often than designed."""
    design = (REPO / "docs" / "design" / "foundations.md").read_text(encoding="utf-8")
    table = design.split("### Marks", 1)[1].split("\n\n", 2)[1]
    designed = {name: int(count) for name, count in
                re.findall(r"^\| (\w+) \| (\d+) \|", table, flags=re.MULTILINE)}
    assert designed == {name: len(variants) for name, (_, variants) in marks.MARKS.items()}


def test_every_hand_drawn_mark_is_a_closed_filled_shape():
    """A pen's line swells and thins; one stroke width is what makes a drawn
    mark look fake (foundations.md, Marks). The marks are filled outlines,
    and an outline left open fills across its gap."""
    for name, (_, variants) in marks.MARKS.items():
        for paths in variants:
            for d in paths:
                assert d.startswith("M") and d.endswith("Z"), name


def _png_size(path: Path) -> tuple[int, int]:
    head = path.read_bytes()[:24]
    assert head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR", path.name
    return struct.unpack(">II", head[16:24])


def test_the_tray_has_a_picture_for_every_taskbar_state_and_size_and_no_other():
    """The app picks a picture by the taskbar's theme, its state and the
    display's scale (trayicon.rs). The Rust table, the generator and the
    folder have to agree: a name the table has and the folder lacks fails
    the app's build, and one the folder has that nothing names is stale."""
    drawn = {marks.tray_name(theme, state, size): size for theme in marks.TRAY_THEMES
             for state in marks.TRAY_STATES for size in marks.TRAY_SIZES}
    assert {p.name for p in TRAY.iterdir()} == set(drawn)
    for name, size in drawn.items():
        assert _png_size(TRAY / name) == (size, size), name

    rust = (REPO / "ui" / "src-tauri" / "src" / "trayicon.rs").read_text(encoding="utf-8")
    table = rust.split("drawn![", 1)[1].split("];", 1)[0]
    assert {f"{n}.png" for n in re.findall(r'"([a-z]+-[a-z]+-\d+)"', table)} == set(drawn)
