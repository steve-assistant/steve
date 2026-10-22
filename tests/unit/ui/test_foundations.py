"""The design's foundations hold in the shipped screens, under every setting
Windows and Settings can give them.

Each is a promise to someone who can't use the default: a reader with low
vision who chose Atkinson or the larger size, someone whom motion makes
ill, someone who reads only in a contrast theme. None of those people is
the one who looks at a screen while building it, so each promise is tested
in Edge, on the screens built from this checkout, against what the page
actually computes.
"""

from __future__ import annotations

import itertools
import re

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

pytestmark = [pytest.mark.processes, pytest.mark.xdist_group("ui")]

# The design's classes, on the persona's names (Latin Extended included).
PROBE = """
<p class="t-body" id="body">Maya Lindqvist wrote to Zoë Dvořák.</p>
<p class="hand-day" id="day">Thursday, with Łucja</p>
<p class="t-meta" id="meta">09:41 Şahin</p>
<div class="grain lifted-note note-arrives" id="note"
     style="--tilt-of: 1; background-color: var(--note)">a note</div>
<div class="arrives" id="entry">an entry</div>
<div class="opens lifted-window" id="window">a window</div>
<div class="draws" id="mark">a mark</div>
<svg class="turning" id="working" width="16" height="16">
  <rect id="diamond" width="10" height="10" style="fill: var(--paper); stroke: var(--ink)"/></svg>
<div id="surface" style="background-color: var(--paper); color: var(--ink)">the page</div>
<div id="system" style="background-color: Canvas; color: CanvasText">the system's colours</div>
"""


def _nothing():
    async def anything(request):
        return JSONResponse({"detail": "not in this test"}, status_code=404)

    return Starlette(routes=[Route("/{path:path}", anything, methods=["GET", "POST"])])


def _probe(page, **root):
    """Puts the probe on the page, with <html>'s attributes set first, so
    its animations start under the settings being tested."""
    page.evaluate(
        """([root, html]) => {
            const settings = document.documentElement.dataset;
            document.getElementById('probe')?.remove();
            for (const a of ['theme', 'face', 'hand', 'size']) delete settings[a];
            Object.assign(settings, root);
            const div = document.createElement('div');
            div.id = 'probe';
            div.innerHTML = html;
            document.body.appendChild(div);
        }""",
        [root, PROBE],
    )
    page.evaluate("document.fonts.ready.then(() => true)")
    # Settled: every arrival over, so what is read is where things rest.
    page.evaluate("Promise.all(document.getAnimations()"
                  ".filter(a => a.effect.getComputedTiming().iterations !== Infinity)"
                  ".map(a => a.finished)).then(() => true)")


def _style(page, element: str, *props: str) -> dict[str, str]:
    return page.evaluate(
        "([id, props]) => { const s = getComputedStyle(document.getElementById(id));"
        " return Object.fromEntries(props.map(p => [p, s.getPropertyValue(p)])); }",
        [element, list(props)],
    )


def _first_face(family: str) -> str:
    return family.split(",")[0].strip().strip('"')


def test_the_page_follows_windows_light_or_dark_setting_unless_settings_hold_it(screen):
    """The night theme is for night; a page that stays cream when Windows
    went dark glares. Settings can still hold either theme."""
    page = screen(_nothing())
    seen = {}
    for scheme, held in itertools.product(("light", "dark"), (None, "paper", "ink")):
        page.emulate_media(color_scheme=scheme)
        _probe(page, **({"theme": held} if held else {}))
        seen[scheme, held] = _style(page, "surface", "background-color")["background-color"]

    cream, navy = "rgb(255, 249, 238)", "rgb(13, 28, 52)"
    assert seen == {
        ("light", None): cream, ("light", "paper"): cream, ("light", "ink"): navy,
        ("dark", None): navy, ("dark", "paper"): cream, ("dark", "ink"): navy,
    }


def test_the_bundled_faces_load_with_latin_extended_and_nothing_is_fetched_elsewhere(screen):
    """A person's name in a fallback face, half its letters in another
    font, looks broken. The faces ship in the app and its CSP lets it fetch
    nothing, so a face that isn't bundled is a face that never loads."""
    page = screen(_nothing())
    _probe(page, face="atkinson")
    _probe(page)

    loaded = page.evaluate(
        "[...document.fonts].filter(f => f.status === 'loaded')"
        ".map(f => f.family + (f.unicodeRange.startsWith('U+0') ? ' latin' : ' latin-ext'))"
    )
    for face in ("Newsreader", "IBM Plex Mono", "Playpen Sans", "Atkinson Hyperlegible Next"):
        assert {f"{face} latin", f"{face} latin-ext"} <= set(loaded), face
    origin = page.evaluate("location.origin")
    fetched = page.evaluate("performance.getEntriesByType('resource').map(e => e.name)")
    assert [url for url in fetched if not url.startswith(origin)] == []


def test_each_reading_option_changes_the_text_face_the_hand_and_the_size(screen):
    """Reading is for someone who can't read the defaults. Each option has
    to reach every word it promises to: the text face everywhere, plain
    hand-set words in that face at the same size, and every size a step up."""
    page = screen(_nothing())

    def read(**root):
        _probe(page, **root)
        body = _style(page, "body", "font-family", "font-size")
        day = _style(page, "day", "font-family", "font-size", "font-weight")
        return (_first_face(body["font-family"]), body["font-size"],
                _first_face(day["font-family"]), day["font-size"], day["font-weight"])

    assert read() == ("Newsreader", "17px", "Playpen Sans", "40px", "400")
    assert read(face="atkinson") == (
        "Atkinson Hyperlegible Next", "17px", "Playpen Sans", "40px", "400")
    assert read(hand="plain") == ("Newsreader", "17px", "Newsreader", "40px", "600")
    assert read(face="atkinson", hand="plain") == (
        "Atkinson Hyperlegible Next", "17px", "Atkinson Hyperlegible Next", "40px", "600")
    assert read(size="larger") == ("Newsreader", "19.125px", "Playpen Sans", "45px", "400")


_ANIMATIONS = """() => document.getAnimations()
    .filter(a => a.effect?.target?.closest('#probe'))
    .map(a => {
        const t = a.effect.target, timing = a.effect.getComputedTiming();
        const was = {state: a.playState, at: a.currentTime};
        a.pause(); a.currentTime = 0;
        const s = getComputedStyle(t);
        const start = [s.translate, s.rotate, s.clipPath, s.opacity];
        a.cancel();
        const r = getComputedStyle(t);
        const rest = [r.translate, r.rotate, r.clipPath];
        return {name: a.animationName, loops: timing.iterations === Infinity,
                duration: timing.duration, ...was, start, rest};
    })"""


def _numbers(value: str) -> list[float]:
    return [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", value)]


def _moves(animation) -> bool:
    """Whether the animation starts anywhere but where its element rests:
    shifted, turned or cut away, as opposed to only faded."""
    for start, rest in zip(animation["start"][:3], animation["rest"], strict=True):
        pairs = itertools.zip_longest(_numbers(start), _numbers(rest), fillvalue=0.0)
        if any(abs(a - b) > 1e-6 for a, b in pairs):
            return True
    return False


def test_with_animation_effects_off_nothing_moves_and_nothing_loops(screen):
    """With Windows' animation effects off, motion can make someone ill. Every
    move becomes a short cross-fade, and a looping state holds still; with
    them on, the same page does move, so the check can see a move."""
    page = screen(_nothing())

    page.emulate_media(reduced_motion="no-preference")
    _probe(page)
    moving = page.evaluate(_ANIMATIONS)
    assert {a["name"] for a in moving if _moves(a)} == {
        "steve-arrive", "steve-window-opens", "steve-draw", "steve-note-arrives"}
    assert [a["state"] for a in moving if a["loops"]] == ["running"]

    page.emulate_media(reduced_motion="reduce")
    _probe(page)
    still = page.evaluate(_ANIMATIONS)
    assert len(still) == len(moving)
    assert [a["name"] for a in still if not a["loops"] and (_moves(a) or a["duration"] > 120)] == []
    assert [(a["state"], a["at"]) for a in still if a["loops"]] == [("paused", 0)]


def test_under_a_contrast_theme_the_systems_colours_apply_with_no_grain_shadow_or_tilt(screen):
    """Windows' contrast themes are for people who need exactly those
    colours. Grain and shadows muddy them, a tilt fights the reader, and an
    SVG drawn in the design's ink, which Edge does not recolour, would keep
    the design's navy on the user's own background."""
    page = screen(_nothing())
    look = ("background-image", "box-shadow", "rotate")

    page.emulate_media(forced_colors="none")
    _probe(page)
    usual = _style(page, "note", *look)
    assert usual["background-image"].startswith("url(") and usual["box-shadow"] != "none"
    assert usual["rotate"] == "1deg"

    page.emulate_media(forced_colors="active")
    _probe(page)
    note = _style(page, "note", *look)
    tilt = _numbers(note["rotate"]) or [0.0]
    assert (note["background-image"], note["box-shadow"], tilt) == ("none", "none", [0.0])
    system = _style(page, "system", "color", "background-color")
    diamond = _style(page, "diamond", "fill", "stroke")
    assert diamond == {"fill": system["background-color"], "stroke": system["color"]}
    surface = _style(page, "surface", "background-color")
    assert surface["background-color"] == system["background-color"]
