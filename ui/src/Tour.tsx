/**
 * The tour: six notes, each pinned to what it explains, with Next and
 * "Skip the tour". Under two minutes, once per install, and again from the
 * name's menu ("Show me around").
 *
 * On 18 October a review of the running app found Steve's best work out of
 * sight: the hotkey, the desk card, the extensions Steve builds itself and
 * the switches that let it browse and run code were each a thing to know
 * about already. Setup asks for what Steve needs and says little of what
 * it does. The tour shows where each thing is, on the screen it is on.
 *
 * Nothing here is modal: the page under a note still works, and Esc ends
 * the tour. Setup's end makes it due (app setting `tour`); taken or
 * skipped, it is not due again.
 */
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { hotkeyLabel } from "./api";
import "./tour.css";

/** A stop: what its note points at, and what it says. */
export interface Stop {
  /** The element explained, found when the note shows; null, or not on
   *  screen, and the note sits where `corner` says. */
  find: () => Element | null;
  title: string;
  text: string;
  /** With nothing to point at: the window's corner nearest the thing, as
   *  the desk card's, which is in the screen's corner, not this window. */
  corner?: "bottom-right";
}

const GAP = 14;
const EDGE = 12;
/** The desk card's window, square from the screen's corner, as it first
 *  shows (`deskcard` in ui/src-tauri/tauri.conf.json). */
export const DESK_CARD = 88;

function last(selector: string): Element | null {
  const all = document.querySelectorAll(selector);
  return all.length ? all[all.length - 1] : null;
}

function shown(el: Element | null): Element | null {
  if (!el) return null;
  const box = el.getBoundingClientRect();
  return box.width > 0 && box.height > 0 ? el : null;
}

/** The six stops, in the order a stranger meets them. */
export function stops(hotkey: string | null): Stop[] {
  const key = hotkey ? hotkeyLabel(hotkey) : null;
  return [
    {
      find: () => last(".day-sheet .day-head"),
      title: "One page a day",
      text: "Everything happens here: what you ask, what Steve does for you, and what it "
        + "hears. A new page starts each morning, and the days before fold up above it.",
    },
    {
      find: () => shown(document.querySelector(".margin"))
        ?? shown(document.querySelector(".notes-strip"))
        ?? last(".day-sheet .day-head"),
      title: "Notes stuck to the side",
      text: "When Steve needs a yes, or has a question, it sticks a note beside the page. "
        + "Answer it where it is; nothing that spends or sends goes without your yes.",
    },
    {
      find: () => document.querySelector(".composer"),
      title: key ? `${key}, over any window` : "A hotkey, over any window",
      text: key
        ? `Tap ${key} wherever you are for the quick bar, and ask without leaving what you're `
          + "doing. Hold it to talk."
        : "Choose a hotkey in Settings, and Steve's quick bar opens over any window.",
    },
    {
      find: () => null,
      corner: "bottom-right",
      title: "The card in the corner",
      text: "A small card sits in the corner of your screen: what Steve is doing, and a red "
        + "diamond when something needs you. Point at it to see more.",
    },
    {
      find: () => [...document.querySelectorAll(".places .place")]
        .find((el) => el.textContent?.startsWith("Extensions")) ?? document.querySelector(".who"),
      title: "Tools Steve builds",
      text: "Ask Steve for a tool it doesn't have, and it builds one, tests it, and asks "
        + "before turning it on. They're all under Extensions.",
    },
    {
      find: () => document.querySelector(".who"),
      title: "Off until you say",
      text: "The browser, running code and waking the laptop for a reminder are switches "
        + "in What Steve may do, under your name. Each is off until you turn it on.",
    },
  ];
}

interface Place {
  /** The note's corner, in the window. */
  left: number;
  top: number;
  /** The ring around what it explains, if anything. */
  ring: DOMRect | null;
}

/** Where a note of this size goes beside `target`: right of the sidebar,
 *  below or above anything else, and at the corner with nothing. */
export function place(target: DOMRect | null, note: { width: number; height: number },
                      win: { width: number; height: number },
                      corner?: "bottom-right"): Place {
  const clampX = (x: number) => Math.max(EDGE, Math.min(win.width - note.width - EDGE, x));
  const clampY = (y: number) => Math.max(EDGE, Math.min(win.height - note.height - EDGE, y));
  if (!target) {
    if (corner === "bottom-right") {
      // Above the card, not in the corner itself: the card is a window of
      // its own, over this one when this one fills the screen, and on the
      // first install (22 October 2026) it covered the note's Next.
      return { left: win.width - note.width - EDGE * 2,
               top: Math.max(EDGE, win.height - note.height - DESK_CARD - EDGE), ring: null };
    }
    return { left: clampX((win.width - note.width) / 2), top: clampY((win.height - note.height) / 2),
             ring: null };
  }
  // Beside: a thing wholly in the window's left third, as the sidebar's.
  // Any thing starting there, the day's heading went beside too, over the
  // notes' margin, rather than below where its page is.
  if (target.right < win.width / 3 && target.right + GAP + note.width <= win.width - EDGE) {
    return { left: target.right + GAP, top: clampY(target.top - 8), ring: target };
  }
  if (target.bottom + GAP + note.height <= win.height - EDGE) {
    return { left: clampX(target.left), top: target.bottom + GAP, ring: target };
  }
  if (target.top - GAP - note.height >= EDGE) {
    return { left: clampX(target.left), top: target.top - GAP - note.height, ring: target };
  }
  // Too tall for either, as the notes' margin: on its left.
  return { left: clampX(target.left - GAP - note.width), top: clampY(target.top + 24), ring: target };
}

/** The stop the tour is at is the app's (`at`), not the tour's: a stop
 *  points at a page's place in the sidebar, the user goes there, and the
 *  tour shown only on the home page vanished, then began again at its first
 *  stop on the way back (the first install, 22 October 2026). */
export function Tour({ hotkey, at: asked, onAt, onEnd }: {
  hotkey: string | null; at: number; onAt: (at: number) => void; onEnd: () => void;
}) {
  const all = useMemo(() => stops(hotkey), [hotkey]);
  const at = Math.min(asked, all.length - 1);
  const stop = all[at];
  const note = useRef<HTMLDivElement>(null);
  const next = useRef<HTMLButtonElement>(null);
  const [where, setWhere] = useState<Place | null>(null);

  // Measured after each render, and again while the page moves under it:
  // the places load, a note arrives, the window is resized.
  useLayoutEffect(() => {
    const measure = () => {
      const box = note.current?.getBoundingClientRect();
      if (!box) return;
      const target = stop.find()?.getBoundingClientRect() ?? null;
      const now = place(target, box, { width: window.innerWidth, height: window.innerHeight },
                        stop.corner);
      setWhere((was) => (was && was.left === now.left && was.top === now.top
        && was.ring?.x === now.ring?.x && was.ring?.y === now.ring?.y
        && was.ring?.width === now.ring?.width && was.ring?.height === now.ring?.height
        ? was : now));
    };
    measure();
    const timer = window.setInterval(measure, 250);
    window.addEventListener("resize", measure);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener("resize", measure);
    };
  }, [stop]);

  useEffect(() => {
    next.current?.focus();
  }, [at]);

  // Esc ends it, before the page's own Esc: in the capture phase.
  useEffect(() => {
    const key = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.preventDefault();
      e.stopPropagation();
      onEnd();
    };
    window.addEventListener("keydown", key, true);
    return () => window.removeEventListener("keydown", key, true);
  }, [onEnd]);

  const lastStop = at === all.length - 1;
  return (
    <div className="tour" data-stop={at + 1}>
      {where?.ring && (
        <div className="tour-ring" aria-hidden style={{
          left: where.ring.left - 4, top: where.ring.top - 4,
          width: where.ring.width + 8, height: where.ring.height + 8 }} />
      )}
      <div ref={note} key={at} className="tour-note grain lifted-note note-arrives"
           role="dialog" aria-modal="false" aria-labelledby="tour-title"
           style={{ left: where?.left ?? -9999, top: where?.top ?? -9999,
                    visibility: where ? "visible" : "hidden" }}>
        <p className="tour-count t-meta">{at + 1} of {all.length}</p>
        <h2 id="tour-title" className="tour-title hand-kicker">{stop.title}</h2>
        <p className="tour-text t-small">{stop.text}</p>
        <div className="tour-actions">
          {!lastStop && (
            <button type="button" className="tour-skip t-small" onClick={onEnd}>Skip the tour</button>
          )}
          <button ref={next} type="button" className="tour-next t-small"
                  onClick={() => (lastStop ? onEnd() : onAt(at + 1))}>
            {lastStop ? "Done" : "Next"}
          </button>
        </div>
      </div>
    </div>
  );
}
