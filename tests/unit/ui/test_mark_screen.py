"""Steve's mark, drawn in Edge, is the logo, and each state shows the one
thing the design gives it.

The mark is how Steve says what it's doing without words: working, voice,
needing you, asleep, stopped (docs/design/presence.md, "The mark"). A state
that changes the wrong thing, or two things, reads as another state. These
tests draw the mark on the specimen sheet (ui/specimen.html, built for the
run), set its state as the app will, and read what Edge computes.
"""

from __future__ import annotations

import base64
import re
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

pytestmark = [pytest.mark.processes, pytest.mark.xdist_group("ui")]

REPO = Path(__file__).resolve().parents[3]


def _nothing():
    async def anything(request):
        return JSONResponse({"detail": "not in this test"}, status_code=404)

    return Starlette(routes=[Route("/{path:path}", anything, methods=["GET", "POST"])])


@pytest.fixture
def sheet(screen):
    page = screen(_nothing(), path="/specimen.html")
    page.wait_for_selector("#live svg.mark")
    # The paper's grain is noise: two pictures of one shape would differ.
    page.evaluate("document.querySelector('main').classList.remove('grain')")
    return page


def _show(page, fresh: bool = False, **props):
    """The live mark with these props, drawn: `fresh` as when it first
    appears, otherwise as a change to the mark already showing."""
    page.evaluate("([props, fresh]) => window.__mark(props, fresh)", [props, fresh])
    page.evaluate("new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")


def _settle(page):
    """Every move over. A loop never finishes, held still or not, so only
    the moves with an end are waited for."""
    page.evaluate("Promise.all(document.getAnimations()"
                  ".filter(a => a.effect.getComputedTiming().iterations !== Infinity)"
                  ".map(a => a.finished)).then(() => true)")


# Each part of the live mark: shown filled, as an outline, or both while it
# fills in; then any animation running on it, and any scale.
_DESCRIBE = """() => {
  const svg = document.querySelector('#live svg.mark');
  const shown = (e) => getComputedStyle(e).display !== 'none';
  const look = (g) => {
    if (!g) return null;
    const fill = shown(g.querySelector('.fill')), line = shown(g.querySelector('.line'));
    const parts = [fill && line ? 'catching' : fill ? 'filled' : 'outline'];
    for (const a of g.getAnimations({ subtree: true }))
      if (a.playState === 'running') parts.push(a.animationName);
    const scale = getComputedStyle(g).scale;
    if (scale !== 'none') parts.push('scale ' + scale);
    return parts.join(' ');
  };
  const gems = svg.querySelectorAll('.gems .diamond');
  return {
    body: getComputedStyle(svg.querySelector('.body > path')).opacity,
    centre: look(svg.querySelector('.body .diamond')),
    red: look(gems[0]), yellow: look(gems[1]), cyan: look(gems[2]),
  };
}"""

_AT_REST = {"body": "1", "centre": "filled", "red": "filled", "yellow": "filled",
            "cyan": "filled"}

# presence.md's States table: what each state changes, and nothing else.
_STATES = {
    "idle": {},
    "working": {"yellow": "filled steve-tick"},
    "listening": {"cyan": "filled scale 1.15"},
    "speaking": {"cyan": "filled scale 1.15"},
    "asleep": {"red": "outline", "yellow": "outline", "cyan": "outline"},
    "catching-up": {"red": "catching steve-fill-in", "yellow": "catching steve-fill-in",
                    "cyan": "catching steve-fill-in"},
    "starting": {"body": "0.45", "centre": "filled steve-tick"},
    "stopped": {"body": "0.45", "centre": "outline", "red": "outline", "yellow": "outline",
                "cyan": "outline"},
}


def test_each_state_changes_only_what_the_design_gives_it(sheet):
    """Each state changes one thing so it reads at a glance; one that also
    moved another diamond would read as two states at once."""
    seen = {}
    for state in _STATES:
        _show(sheet, fresh=True, size=120, state=state)
        seen[state] = sheet.evaluate(_DESCRIBE)
    assert seen == {state: {**_AT_REST, **changes} for state, changes in _STATES.items()}


def test_waking_fills_the_diamonds_in_from_left_to_right(sheet):
    """Catching up after sleep reads as progress only if it runs one way."""
    _show(sheet, fresh=True, size=120, state="catching-up")
    delays = sheet.evaluate(
        "[...document.querySelectorAll('#live .gems .fill')]"
        ".map(e => e.getAnimations()[0].effect.getTiming().delay)")
    lefts = sheet.evaluate(
        "[...document.querySelectorAll('#live .gems .line')]"
        ".map(e => e.getBoundingClientRect().left)")
    assert lefts == sorted(lefts)
    assert delays == sorted(delays) and len(set(delays)) == 3


def test_with_animation_effects_off_the_mark_holds_still_and_still_says_its_state(sheet):
    """Someone whom motion makes ill still needs to see that Steve is
    working. A turning diamond held still says nothing, so it is drawn as an
    outline; waking fills in at once; nothing nods or lifts."""
    sheet.emulate_media(reduced_motion="reduce")
    seen = {}
    for state in ("working", "starting", "catching-up"):
        _show(sheet, fresh=True, size=120, state=state)
        _settle(sheet)
        seen[state] = sheet.evaluate(_DESCRIBE)
    assert seen == {
        "working": {**_AT_REST, "yellow": "outline"},
        "starting": {**_AT_REST, "body": "0.45", "centre": "outline"},
        "catching-up": {**_AT_REST, "red": "catching", "yellow": "catching", "cyan": "catching"},
    }
    assert _nod(sheet) == 0
    assert _lift(sheet) == 0


def _nod(page) -> float | None:
    """How far the body dips, in pixels, halfway through the nod an entry
    starts; None if no nod starts at all."""
    _show(page, fresh=True, size=120, arrivals=1)
    _show(page, size=120, arrivals=2)
    return page.evaluate("""() => {
        const body = document.querySelector('#live .body');
        const nod = body.getAnimations()[0];
        if (!nod) return null;
        const top = () => body.getBoundingClientRect().top;
        nod.pause(); nod.currentTime = 0;
        const before = top();
        nod.currentTime = nod.effect.getComputedTiming().duration / 2;
        const dip = top() - before;
        nod.cancel();  // a paused move never finishes, and `_settle` would wait
        return Math.round(dip * 100) / 100; }""")


def _lift(page) -> float:
    """How far the red diamond rises, in pixels, under the pointer."""
    where = "document.querySelector('#live .gems .fill').getBoundingClientRect().top"
    page.mouse.move(0, 0)
    before = page.evaluate(where)
    page.hover("#live svg.mark")
    _settle(page)
    return round(before - page.evaluate(where), 2)


def test_the_mark_is_a_little_alive_a_nod_for_an_entry_and_a_lift_under_the_pointer(sheet):
    """The small life (presence.md): a nod when an entry arrives, never for
    the entries already there when the mark appears."""
    _show(sheet, fresh=True, size=120, arrivals=5)
    nods = "document.querySelector('#live .body').getAnimations().map(a => a.animationName)"
    assert sheet.evaluate(nods) == []
    assert _nod(sheet) == 2
    assert _lift(sheet) == 1


def test_the_circle_draws_in_when_something_starts_needing_you_and_not_on_load(sheet):
    """A mark draws in at the moment its event happens (foundations.md): a
    circle drawn at every load would say "new" about something old."""
    draws = ("[...document.querySelectorAll('#live .circle')]"
             ".flatMap(e => e.getAnimations().map(a => a.animationName))")
    _show(sheet, fresh=True, size=120, needsYou=True)
    assert sheet.evaluate(draws) == []
    _show(sheet, fresh=True, size=120)
    _show(sheet, size=120, needsYou=True)
    assert sheet.evaluate(draws) == ["steve-draw"]
    _settle(sheet)
    around = sheet.evaluate("""() => {
        const c = document.querySelector('#live .circle').getBoundingClientRect();
        const d = document.querySelector('#live .gems .fill').getBoundingClientRect();
        return c.left < d.left && c.top < d.top && c.right > d.right && c.bottom > d.bottom;
    }""")
    assert around


_COMPARE = """async ([a, b]) => {
  const load = (png) => new Promise((ok, fail) => {
    const image = new Image(); image.onload = () => ok(image); image.onerror = fail;
    image.src = 'data:image/png;base64,' + png; });
  const pixels = (image) => {
    const canvas = Object.assign(document.createElement('canvas'),
      { width: image.width, height: image.height });
    const g = canvas.getContext('2d'); g.drawImage(image, 0, 0);
    return g.getImageData(0, 0, image.width, image.height).data; };
  const [x, y] = await Promise.all([load(a), load(b)]);
  if (x.width !== y.width || x.height !== y.height) return 1;
  const p = pixels(x), q = pixels(y);
  let off = 0;
  for (let i = 0; i < p.length; i += 4)
    if (Math.max(Math.abs(p[i] - q[i]), Math.abs(p[i + 1] - q[i + 1]),
                 Math.abs(p[i + 2] - q[i + 2])) > 64) off++;
  return off / (p.length / 4);
}"""


def test_the_full_mark_at_rest_is_the_logo_without_its_square(sheet):
    """The mark is drawn from the logo's file so that it is the logo; this
    compares them as pictures, in the same browser, at the same size. The
    centre's navy island, the cut around it and every diamond have to land
    where the logo has them."""
    svg = (REPO / "docs" / "brand" / "steve-logo.svg").read_text(encoding="utf-8")
    svg = svg[svg.index("<svg"):]
    view = sheet.evaluate("document.querySelector('#live svg.mark').getAttribute('viewBox')")
    svg = re.sub(r'viewBox="[^"]*"', f'viewBox="{view}"', svg, count=1)
    sheet.evaluate("""(svg) => {
        const div = document.createElement('div');
        div.id = 'logo';
        div.innerHTML = svg;
        const s = div.querySelector('svg');
        s.setAttribute('width', 240); s.setAttribute('height', 240);
        s.style.display = 'block';
        div.insertAdjacentHTML('afterbegin', '<style>#logo .cls-1 { display: none }</style>');
        document.getElementById('live').after(div);
    }""", svg)

    def picture(selector: str) -> str:
        return base64.b64encode(sheet.locator(selector).screenshot()).decode()

    logo = picture("#logo svg")
    _show(sheet, fresh=True, size=240)
    sheet.mouse.move(0, 0)
    rest = sheet.evaluate(_COMPARE, [picture("#live svg.mark"), logo])
    _show(sheet, fresh=True, size=240, state="asleep")
    asleep = sheet.evaluate(_COMPARE, [picture("#live svg.mark"), logo])
    assert rest < 0.002, f"{rest:.2%} of the mark's pixels are not the logo's"
    assert asleep > 0.02  # the comparison sees a state's change


def test_the_small_mark_drops_the_centre_and_draws_the_diamonds_smaller(sheet):
    """At 16 to 24 pixels the full mark's diamonds touch and the centre is a
    smudge (presence.md, Sizes)."""
    def red(size: int) -> float:
        _show(sheet, fresh=True, size=size)
        return sheet.evaluate("document.querySelector('#live .gems .fill').getBBox().width")

    full, small = red(120), red(24)
    assert sheet.evaluate("document.querySelectorAll('#live .body .diamond').length") == 0
    assert small == pytest.approx(full * 0.8, rel=0.01)
    _show(sheet, fresh=True, size=40)
    assert sheet.evaluate("document.querySelectorAll('#live .body .diamond').length") == 1


def test_at_night_the_body_is_chalk_and_in_a_contrast_theme_the_diamonds_stay_visible(sheet):
    """A navy body on navy paper vanishes, so the ink theme draws it in
    chalk (foundations.md). A contrast theme makes the body and the brand's
    colours one system colour; the diamonds become cut-outs, so the mark
    still has them."""
    body = ("getComputedStyle(document.querySelector('#live .body > path')).fill")
    gem = "getComputedStyle(document.querySelector('#live .gems .diamond')).color"
    _show(sheet, fresh=True, size=120)
    sheet.evaluate("document.documentElement.dataset.theme = 'paper'")
    assert sheet.evaluate(body) == "rgb(13, 28, 52)"
    sheet.evaluate("document.documentElement.dataset.theme = 'ink'")
    assert sheet.evaluate(body) == "rgb(240, 236, 227)"

    sheet.emulate_media(forced_colors="active")
    system = sheet.evaluate("""() => {
        const probe = document.createElement('i');
        probe.style.color = 'Canvas'; probe.style.fill = 'CanvasText';
        document.body.appendChild(probe);
        const s = getComputedStyle(probe);
        return [s.color, s.fill]; }""")
    assert [sheet.evaluate(gem), sheet.evaluate(body)] == system
    assert system[0] != system[1]


def test_a_hand_drawn_mark_is_chosen_by_its_things_id_the_same_every_time(sheet):
    """The same entry always gets the same mark, and a run of entries gets
    every variant: chosen at random, a mark would change at every redraw."""
    read = ("Object.fromEntries([...document.querySelectorAll('#hand [data-id]')]"
            ".map(e => [e.dataset.id, e.querySelector('svg').dataset.variant]))")
    first = sheet.evaluate(read)
    sheet.reload()
    sheet.wait_for_selector("#hand svg")
    assert sheet.evaluate(read) == first
    assert set(first.values()) == {"0", "1", "2"}
