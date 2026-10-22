"""The mark and the simple marks, drawn once from their sources.

The mark is the logo without its rounded square (docs/design/presence.md).
Copied into the app by hand, its shapes drift from the logo, and each
diamond has to be found again to move it on its own. So this reads
docs/brand/steve-logo.svg: the body's paths, and each diamond's centre and
size, written to ui/src/marks.ts for the app to draw.

The simple marks stand in for drawn ones until a writer's scans exist
(foundations.md, Marks). Each is a filled stroke from a centre line and a
width profile, light where the pen lands, fuller in the middle, lifting at
the end. They are not roughened or sketch-rendered, which reads as a filter.
Each has two or three variants, so a page of checks isn't one check stamped.

The tray's icons are drawn from the same shapes, at the sizes Windows asks
for, to PNGs the app embeds (`--tray`, through Edge).

`python -m devtools.marks` writes ui/src/marks.ts, and
tests/unit/ui/test_marks.py checks the file is what this writes. Change the
shapes here, never by hand there.
"""

from __future__ import annotations

import argparse
import bisect
import math
import re
import sys
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LOGO = REPO / "docs" / "brand" / "steve-logo.svg"
OUT = REPO / "ui" / "src" / "marks.ts"
TRAY = REPO / "ui" / "src-tauri" / "icons" / "tray"

# A square about the body, which spans x 17.2 to 582.8 and y 49.3 to 588.8
# (measured in Edge): the logo's square, without the rounded corners.
VIEW_BOX = (16.0, 35.0, 568.0, 568.0)
# At 16 to 24 px each diamond is drawn smaller about its own centre, so
# the gaps between it and the body stay open (presence.md, Sizes).
SMALL = 0.8

NAVY, CHALK = "#0d1c34", "#f0ece3"
COLOURS = {"red": "#ed1c24", "yellow": "#f9ed32", "cyan": "#00aeef"}

Point = tuple[float, float]


@dataclass(frozen=True)
class Diamond:
    x: float
    y: float
    r: float  # half the diagonal

    def path(self, scale: float = 1.0) -> str:
        r = self.r * scale
        return (f"M{self.x:.2f} {self.y - r:.2f}L{self.x + r:.2f} {self.y:.2f}"
                f"L{self.x:.2f} {self.y + r:.2f}L{self.x - r:.2f} {self.y:.2f}Z")


@dataclass(frozen=True)
class Logo:
    body: list[str]  # in full: the outline diamond cut out of the body
    body_small: list[str]  # each shape's outline alone, without the cut
    centre: Diamond
    diamonds: dict[str, Diamond]


def _transformed(point: Point, transform: str) -> Point:
    """A point through an SVG transform list, as the logo uses them:
    translate and rotate, applied right to left."""
    x, y = point
    for name, args in reversed(re.findall(r"(\w+)\(([^)]*)\)", transform)):
        values = [float(v) for v in re.split(r"[\s,]+", args.strip())]
        if name == "rotate":
            a = math.radians(values[0])
            x, y = x * math.cos(a) - y * math.sin(a), x * math.sin(a) + y * math.cos(a)
        elif name == "translate":
            x, y = x + values[0], y + (values[1] if len(values) > 1 else 0.0)
        else:
            raise ValueError(f"the logo uses a transform this doesn't know: {name}")
    return x, y


def logo(source: Path = LOGO) -> Logo:
    """The body and diamonds of the logo, by the classes it fills them
    with: navy for the body and the centre diamond, then red, yellow, cyan."""
    root = ET.parse(source).getroot()
    styles = "".join(s.text or "" for s in root.iter("{http://www.w3.org/2000/svg}style"))
    fills = dict(re.findall(r"\.(cls-\d+)\s*\{\s*fill:\s*(#[0-9a-f]{6})", styles))
    named = {fill: name for name, fill in {"body": NAVY, **COLOURS}.items()}
    body: list[str] = []
    diamonds: dict[str, Diamond] = {}
    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1]
        part = named.get(fills.get(element.get("class", ""), ""))
        if part is None:
            continue
        if tag == "path":
            body.append(element.get("d", ""))
        elif tag == "rect":
            x, y = float(element.get("x", 0)), float(element.get("y", 0))
            w, h = float(element.get("width", 0)), float(element.get("height", 0))
            cx, cy = _transformed((x + w / 2, y + h / 2), element.get("transform", ""))
            diamonds["centre" if part == "body" else part] = Diamond(
                round(cx, 2), round(cy, 2), round(math.hypot(w, h) / 2, 2))
    small = [next(p for p in re.split(r"(?=M)", d.strip()) if p) for d in body]
    return Logo(body, small, diamonds.pop("centre"), diamonds)


# The simple marks.

SAMPLES = 36


def stroke(centre: Callable[[float], Point], width: Callable[[float], float],
           samples: int = SAMPLES) -> str:
    """One filled stroke: the centre line offset either side by half the
    width at each point, out along one side and back along the other."""
    ts = [i / (samples - 1) for i in range(samples)]
    points = [centre(t) for t in ts]
    left, right = [], []
    for i, t in enumerate(ts):
        (x0, y0), (x1, y1) = points[max(i - 1, 0)], points[min(i + 1, samples - 1)]
        length = math.hypot(x1 - x0, y1 - y0) or 1.0
        nx, ny = -(y1 - y0) / length, (x1 - x0) / length
        half = width(t) / 2
        x, y = points[i]
        left.append((x + nx * half, y + ny * half))
        right.append((x - nx * half, y - ny * half))
    return "M" + "L".join(f"{x:.1f} {y:.1f}" for x, y in left + right[::-1]) + "Z"


def pressure(start: float, peak: float, end: float,
             at: float = 0.4) -> Callable[[float], float]:
    """A pen's width along a stroke: light where it lands, fullest at
    `at`, lifting off at the end."""
    def width(t: float) -> float:
        if t <= at:
            return start + (peak - start) * math.sin(t / at * math.pi / 2)
        return peak + (end - peak) * (1 - math.cos((t - at) / (1 - at) * math.pi / 2))
    return width


def bezier(p0: Point, p1: Point, p2: Point, p3: Point) -> Callable[[float], Point]:
    def at(t: float) -> Point:
        u = 1 - t
        return (u**3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t**3 * p3[0],
                u**3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t**3 * p3[1])
    return at


def underline(rise: float, sag: float, peak: float) -> list[str]:
    line = bezier((2, 7 + rise), (40, 7 + sag), (80, 6 - sag), (118, 6 - rise))
    return [stroke(line, pressure(0.6, peak, 0.8, 0.35))]


def strike(rise: float, peak: float) -> list[str]:
    line = bezier((1, 7 + rise), (40, 6.5), (80, 6), (119, 6 - rise))
    return [stroke(line, pressure(0.8, peak, 1.0, 0.3))]


def circle(start: float, overshoot: float, wobble: float, tilt: float) -> list[str]:
    """An ellipse drawn once round, running on past where it began."""
    def centre(t: float) -> Point:
        a = start + t * (2 * math.pi + overshoot)
        r = 1 + wobble * math.sin(2 * a + 0.7) + 0.04 * t
        x, y = 46 * r * math.cos(a), 25 * r * math.sin(a)
        return (50 + x * math.cos(tilt) - y * math.sin(tilt),
                30 + x * math.sin(tilt) + y * math.cos(tilt))
    return [stroke(centre, pressure(0.5, 3.0, 0.7, 0.3), samples=72)]


def check(kink: Point, end: Point) -> list[str]:
    def centre(t: float) -> Point:
        # Down to the kink in the first third, then up and out.
        if t < 0.3:
            k = t / 0.3
            return (6 + (kink[0] - 6) * k, 21 + (kink[1] - 21) * k**1.2)
        k = (t - 0.3) / 0.7
        return (kink[0] + (end[0] - kink[0]) * k, kink[1] + (end[1] - kink[1]) * k**0.9)
    return [stroke(centre, pressure(1.0, 3.6, 0.6, 0.35), samples=40)]


def cross(lean: float) -> list[str]:
    return [stroke(bezier((8, 7), (16, 15 + lean), (24, 25 - lean), (33, 34)),
                   pressure(0.8, 3.4, 0.7)),
            stroke(bezier((32, 6 + lean), (24, 14), (17, 24), (7, 33 - lean)),
                   pressure(0.8, 3.2, 0.6))]


def arrow(bend: float, head: float) -> list[str]:
    tx, ty = 56, 15 - bend / 3
    return [stroke(bezier((3, 17 + bend), (20, 21 + bend), (38, 12), (tx, ty)),
                   pressure(0.6, 2.6, 1.2, 0.3)),
            stroke(bezier((tx - 11, ty - head), (tx - 6, ty - head / 2), (tx - 2, ty - 1),
                          (tx, ty)), pressure(0.6, 2.4, 1.0, 0.5)),
            stroke(bezier((tx - 10, ty + head), (tx - 6, ty + head / 2), (tx - 2, ty + 1),
                          (tx, ty)), pressure(0.6, 2.4, 1.0, 0.5))]


def bracket(hook: float) -> list[str]:
    return [stroke(bezier((10, 3), (3 - hook, 4), (3, 50), (4, 97)),
                   pressure(0.6, 2.6, 0.6, 0.5), samples=40),
            stroke(bezier((4, 97), (6, 97.5), (8, 97.5), (10.5, 97 - hook)),
                   pressure(1.6, 1.8, 0.6, 0.2), samples=12)]


def wave(phase: float, amplitude: float) -> list[str]:
    def centre(t: float) -> Point:
        swell = 0.7 + 0.3 * math.sin(t * math.pi)
        return (2 + 116 * t, 8 + amplitude * swell * math.sin(phase + t * 6 * math.pi))
    return [stroke(centre, pressure(0.6, 2.2, 0.8, 0.3), samples=90)]


def mic(lean: float, sag: float) -> list[str]:
    """A microphone: the head drawn once round with flat sides, the cradle
    under it, the stem and the foot. The button was a rounded outline that
    read as a nought."""
    def head(t: float) -> Point:
        a = -math.pi / 2 + t * (2 * math.pi + 0.35)
        c = math.cos(a)
        return (20 + lean * (1 - t) + 6.5 * math.copysign(abs(c) ** 0.45, c),
                13.5 + 10 * math.sin(a))
    return [stroke(head, pressure(0.9, 3.0, 1.0, 0.3), samples=72),
            stroke(bezier((10.5, 15), (10.5, 32 + sag), (29.5, 32 - sag), (29.5, 14.5)),
                   pressure(0.9, 2.8, 0.8, 0.4), samples=40),
            stroke(bezier((20, 28), (20.2, 30), (19.9, 32), (20, 34.5)),
                   pressure(1.4, 2.8, 1.8, 0.5), samples=12),
            stroke(bezier((13.5, 35.5 + lean / 4), (17, 35), (23, 35.2), (26.5, 34.6)),
                   pressure(0.9, 2.8, 0.8, 0.4), samples=16)]


def clip(tilt: float, tail: float) -> list[str]:
    """A paperclip, the composer's way to add a photo: the wire's three
    turns nested, in one stroke, laid at an angle. Drawn upright about the
    box's middle (y down), then turned by `tilt`."""
    wire: list[Point] = []

    def leg(x: float, y0: float, y1: float, n: int = 12) -> None:
        wire.extend((x, y0 + (y1 - y0) * i / n) for i in range(n))

    def turn(cx: float, cy: float, r: float, a0: float, a1: float, n: int = 18) -> None:
        wire.extend((cx + r * math.cos(a0 + (a1 - a0) * i / n),
                     cy + r * math.sin(a0 + (a1 - a0) * i / n)) for i in range(n))

    leg(6, -7 - tail, 8)
    turn(0, 8, 6, 0, math.pi)
    leg(-6, 8, -8)
    turn(-2, -8, 4, math.pi, 2 * math.pi)
    leg(2, -8, 6)
    turn(0, 6, 2, 0, math.pi)
    leg(-2, 6, -4 + tail)
    wire.append((-2, -4 + tail))
    c, s = math.cos(tilt), math.sin(tilt)
    placed = [(20 + x * c - y * s, 20 + x * s + y * c) for x, y in wire]
    along = [0.0]
    for (x0, y0), (x1, y1) in zip(placed, placed[1:], strict=False):
        along.append(along[-1] + math.hypot(x1 - x0, y1 - y0))

    def centre(t: float) -> Point:
        # Evenly along the wire, so the pen's pressure is too.
        d = t * along[-1]
        i = min(max(bisect.bisect_right(along, d) - 1, 0), len(placed) - 2)
        f = (d - along[i]) / ((along[i + 1] - along[i]) or 1.0)
        (x0, y0), (x1, y1) = placed[i], placed[i + 1]
        return (x0 + (x1 - x0) * f, y0 + (y1 - y0) * f)
    return [stroke(centre, pressure(0.9, 2.5, 0.9, 0.5), samples=140)]


# Each mark's box, and its variants, as many as foundations.md's table says.
MARKS: dict[str, tuple[str, list[list[str]]]] = {
    "underline": ("0 0 120 12", [underline(0.6, 1.2, 3.0), underline(-0.5, -0.8, 2.6),
                                 underline(1.2, 0.3, 3.4)]),
    "circle": ("0 0 100 60", [circle(-2.4, 0.5, 0.03, -0.05), circle(-2.0, 0.35, 0.04, 0.04),
                              circle(-2.7, 0.6, 0.02, -0.02)]),
    "check": ("0 0 40 40", [check((15, 31), (36, 6)), check((14, 33), (35, 4)),
                            check((16, 30), (37, 8))]),
    "cross": ("0 0 40 40", [cross(1.5), cross(-1.5)]),
    "strike": ("0 0 120 12", [strike(0.8, 2.6), strike(-0.6, 2.4)]),
    "arrow": ("0 0 60 30", [arrow(2, 6), arrow(-1, 5), arrow(4, 7)]),
    "bracket": ("0 0 14 100", [bracket(1.5), bracket(2.5)]),
    "wave": ("0 0 120 16", [wave(0.0, 3.2), wave(1.2, 2.6)]),
    "mic": ("0 0 40 40", [mic(0.0, 0.8), mic(0.6, -0.6)]),
    "clip": ("0 0 40 40", [clip(math.pi / 4, 0.0), clip(math.pi / 4 + 0.07, 1.0)]),
}


def _diamond_ts(d: Diamond) -> str:
    return f"{{ x: {d.x}, y: {d.y}, r: {d.r} }}"


def written(source: Path = LOGO) -> str:
    """ui/src/marks.ts, as this writes it."""
    shapes = logo(source)
    x, y, w, h = VIEW_BOX
    lines = [
        "// Written by `python -m devtools.marks`, from docs/brand/steve-logo.svg",
        "// and the simple marks' centre lines and widths. Change them there.",
        "",
        "export type Diamond = { x: number; y: number; r: number };",
        "",
        "export const LOGO = {",
        f'  viewBox: "{x:g} {y:g} {w:g} {h:g}",',
        f"  small: {SMALL},",
        "  body: [",
        *(f'    "{d}",' for d in shapes.body),
        "  ],",
        "  bodySmall: [",
        *(f'    "{d}",' for d in shapes.body_small),
        "  ],",
        f"  centre: {_diamond_ts(shapes.centre)},",
        *(f"  {name}: {_diamond_ts(d)}," for name, d in shapes.diamonds.items()),
        "} as const;",
        "",
        "export const MARKS = {",
    ]
    for name, (box, variants) in MARKS.items():
        lines += [f"  {name}: {{", f'    viewBox: "{box}",', "    variants: ["]
        lines += ["      [" + ", ".join(f'"{d}"' for d in paths) + "]," for paths in variants]
        lines += ["    ],", "  },"]
    lines += ["} as const;", "", "export type MarkName = keyof typeof MARKS;", ""]
    return "\n".join(lines)


# The tray.

TRAY_SIZES = (16, 20, 24, 32)
TRAY_STATES = ("idle", "waiting", "stopped")
# The taskbar's theme, not the app's: a navy body on a dark taskbar is lost.
TRAY_THEMES = {"paper": NAVY, "ink": CHALK}


def tray_svg(theme: str, state: str, size: int) -> str:
    """The mark at a tray size: drawn small (presence.md, Sizes), with a
    red diamond badge while notes wait, faint with outline diamonds when
    stopped (presence.md, The tray)."""
    shapes = logo()
    x, y, w, h = VIEW_BOX
    body = TRAY_THEMES[theme]
    pixel = w / size
    parts = []
    for name, d in shapes.diamonds.items():
        if state == "stopped":
            parts.append(f'<path d="{d.path(SMALL)}" fill="none" stroke="{COLOURS[name]}" '
                         f'stroke-width="{pixel:.1f}"/>')
        else:
            parts.append(f'<path d="{d.path(SMALL)}" fill="{COLOURS[name]}"/>')
    opacity = ' opacity="0.45"' if state == "stopped" else ""
    mark = "".join(f'<path d="{d}" fill="{body}"{opacity}/>' for d in shapes.body_small)
    mark += "".join(parts)
    badge = ""
    if state == "waiting":
        # Bottom right, cut out of the mark by a ring of nothing.
        spot = Diamond(x + w * 0.8, y + h * 0.8, w * 0.2)
        mark = (f'<mask id="cut"><rect x="{x}" y="{y}" width="{w}" height="{h}" fill="white"/>'
                f'<path d="{spot.path(1.35)}" fill="black"/></mask><g mask="url(#cut)">{mark}</g>')
        badge = f'<path d="{spot.path()}" fill="{COLOURS["red"]}"/>'
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{x:g} {y:g} {w:g} {h:g}" '
            f'width="{size}" height="{size}">{mark}{badge}</svg>')


def tray_name(theme: str, state: str, size: int) -> str:
    return f"{theme}-{state}-{size}.png"


def render_tray(out: Path = TRAY) -> list[Path]:
    """Each tray icon to a PNG, drawn by Edge through the screen tests' rig,
    so the icon is what a browser makes of the same shapes."""
    import tempfile

    from playwright.sync_api import sync_playwright

    sys.path.insert(0, str(REPO))
    from tests.unit.ui import rig

    out.mkdir(parents=True, exist_ok=True)
    written_files = []
    with tempfile.TemporaryDirectory(dir=_scratch()) as profile, sync_playwright() as driver, \
            rig.edge(Path(profile), driver) as browser:
        page = browser.new_page()
        for theme in TRAY_THEMES:
            for state in TRAY_STATES:
                for size in TRAY_SIZES:
                    page.set_content('<body style="margin:0;background:transparent">'
                                     + tray_svg(theme, state, size) + "</body>")
                    target = out / tray_name(theme, state, size)
                    page.locator("svg").screenshot(path=str(target), omit_background=True)
                    written_files.append(target)
    return written_files


def _scratch() -> Path:
    from core import paths

    return paths.scratch_dir()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m devtools.marks", description=__doc__)
    parser.add_argument("--tray", action="store_true",
                        help="also draw the tray's icons, through Edge")
    args = parser.parse_args(argv)
    OUT.write_text(written(), encoding="utf-8", newline="\n")
    print(OUT)
    if args.tray:
        for path in render_tray():
            print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
