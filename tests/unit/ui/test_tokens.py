"""The app's colours are the design's, and every pair it puts together reads.

A colour is chosen once, in docs/design/foundations.md, where its contrast
was worked out, and painted from ui/src/tokens.css. The two drift apart
quietly: a token nudged by eye in the stylesheet still looks fine on the
screen it was nudged for, and fails on the tint or the night theme nobody
looked at. Contrast is arithmetic, so it is checked here, for every pair
the design lists, in both themes, rather than by eye in a review.
"""

from __future__ import annotations

import re

import pytest

from tests.unit.ui import rig

TOKENS = rig.UI / "src" / "tokens.css"
FOUNDATIONS = rig.REPO / "docs" / "design" / "foundations.md"
THEMES = ("paper", "ink")

# Where text sits: the four surfaces of the page.
SURFACES = ("desk", "paper", "note", "slip")
# Where only ink and ink-soft may sit (foundations.md, Darker cream and tints).
AREAS = ("paper-deep", "tint-red", "tint-cyan", "tint-yellow", "highlight")
TEXT = ("ink", "ink-soft", "ink-faint", "red-ink")
# Shapes that carry meaning, and what they sit on: the diamonds and the
# needs-you mark, the pin on the folded strip of notes, and the voice's
# marks, including the listening ring on the composer while it listens.
SHAPES = [(shape, ground) for shape in ("ink", "red", "cyan-ink") for ground in SURFACES]
SHAPES += [("red", "tint-red"), ("cyan-ink", "tint-cyan")]


def _root_block(css: str) -> str:
    """The first `:root { ... }`: the tokens, before any theme or setting
    changes them."""
    start = css.index(":root {")
    return css[start:css.index("\n}", start)]


def tokens() -> dict[str, dict[str, str]]:
    """{theme: {token: '#rrggbb'}} for every colour token in tokens.css.
    A token is `light-dark(paper, ink)`, or one hex for both."""
    themes: dict[str, dict[str, str]] = {t: {} for t in THEMES}
    root = _root_block(TOKENS.read_text("utf-8"))
    for name, value in re.findall(r"^\s*--([a-z-]+):\s*([^;]+);", root, re.M):
        pair = re.fullmatch(r"light-dark\((#[0-9a-f]{6}),\s*(#[0-9a-f]{6})\)", value.strip())
        if pair:
            themes["paper"][name], themes["ink"][name] = pair.groups()
        elif re.fullmatch(r"#[0-9a-f]{6}", value.strip()):
            themes["paper"][name] = themes["ink"][name] = value.strip()
    return themes


def _sections() -> dict[str, list[list[str]]]:
    """foundations.md's tables, as rows of cells under each ### heading."""
    sections: dict[str, list[list[str]]] = {}
    heading = ""
    for line in FOUNDATIONS.read_text("utf-8").splitlines():
        if line.startswith("#"):
            heading = line.lstrip("#").strip()
        elif line.startswith("|") and not line.startswith("|---"):
            cells = [c.strip().strip("`") for c in line.strip("|").split("|")]
            sections.setdefault(heading, []).append(cells)
    return sections


def designed() -> tuple[dict[str, dict[str, str]], dict[str, dict[str, float]]]:
    """The design's colours, {theme: {token: hex}}, and the contrast it
    states for each on the page, {theme: {token: ratio}}."""
    sections = _sections()
    colours: dict[str, dict[str, str]] = {t: {} for t in THEMES}
    stated: dict[str, dict[str, float]] = {t: {} for t in THEMES}
    # The brand's section has a second table, of where each accent goes.
    for name, hexcode, *_ in sections["The brand"][1:]:
        if not re.fullmatch(r"#[0-9a-f]{6}", hexcode):
            continue
        for theme in THEMES:
            colours[theme][name] = hexcode
    for name, paper, ink, *_ in sections["Darker cream and tints"][1:]:
        colours["paper"][name], colours["ink"][name] = paper, ink
    for theme, heading in (("paper", "Paper (the day theme)"), ("ink", "Ink (the night theme)")):
        for name, hexcode, on_paper, *_ in sections[heading][1:]:
            colours[theme][name] = hexcode
            if m := re.fullmatch(r"([\d.]+):1", on_paper):
                stated[theme][name] = float(m.group(1))
    return colours, stated


def luminance(hexcode: str) -> float:
    channels = [int(hexcode[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    r, g, b = (c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    """WCAG 2's contrast ratio of two colours."""
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_the_app_paints_exactly_the_colours_the_design_gives_in_each_theme():
    """A colour in the stylesheet that the design doesn't have, or has
    differently, is one nobody worked out the contrast of."""
    colours, _ = designed()
    assert tokens() == colours


@pytest.mark.parametrize("theme", THEMES)
def test_text_meets_wcag_aa_on_every_surface_it_may_sit_on(theme):
    """4.5:1 is what WCAG AA asks of body text. The faint ink for times and
    hints is where a design slips under it first, and the tints are where
    only the two darkest inks are allowed."""
    colour = tokens()[theme]
    pairs = [(text, ground) for text in TEXT for ground in SURFACES]
    pairs += [(text, ground) for text in ("ink", "ink-soft") for ground in AREAS]
    failing = {f"{t} on {g}": round(contrast(colour[t], colour[g]), 2)
               for t, g in pairs if contrast(colour[t], colour[g]) < 4.5}
    assert failing == {}


@pytest.mark.parametrize("theme", THEMES)
def test_the_shapes_that_carry_meaning_stand_out_at_three_to_one(theme):
    """A diamond that says something needs you, or a ring that says Steve
    is listening, has to be seen by someone who sees colour weakly: WCAG's
    3:1 for meaningful graphics."""
    colour = tokens()[theme]
    failing = {f"{s} on {g}": round(contrast(colour[s], colour[g]), 2)
               for s, g in SHAPES if contrast(colour[s], colour[g]) < 3}
    assert failing == {}


@pytest.mark.parametrize("theme", THEMES)
def test_the_contrast_the_design_states_is_the_contrast_its_colours_give(theme):
    """foundations.md argues from these numbers; a number that was never
    true, or stopped being, misleads the next person to choose a colour."""
    colour = tokens()[theme]
    _, stated = designed()
    assert stated[theme], "foundations.md states no contrast to check"
    actual = {name: round(contrast(colour[name], colour["paper"]), 2) for name in stated[theme]}
    wrong = {name: (said, actual[name]) for name, said in stated[theme].items()
             if abs(actual[name] - said) > 0.06}
    assert wrong == {}


def test_a_colour_that_fails_on_a_surface_is_caught():
    """The arithmetic itself: black on white is 21:1, and the yellow the
    design forbids as text on cream is what it says, about 1.2:1."""
    assert contrast("#000000", "#ffffff") == pytest.approx(21)
    assert contrast("#f9ed32", "#fff9ee") == pytest.approx(1.2, abs=0.05)
