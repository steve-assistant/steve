/**
 * The desk card (docs/design/presence.md, The desk card): Steve's mark,
 * small on the desktop, opening into a card that says what Steve is
 * doing, what's next and what needs you.
 *
 * - It opens on its own only for what is worth a glance: a new note, an
 *   event within ten minutes, a reply that reached a quick bar the user had
 *   closed. Opened for every change, it would be one more thing tugging at
 *   the eye. It closes after eight seconds unless the pointer is on it.
 * - Its words can be read by anyone passing the screen: with "Show what
 *   Steve is doing on the desk card" off, it shows the mark, the next
 *   event's time and the count of notes, nothing more (deskLines.ts).
 * - It is wanted only once setup is done. On a fresh install it would sit
 *   on the desktop with nothing to say, beside a window asking for a key.
 * - A click, a hold and a drag all start with the button down, and are
 *   told apart here by time and distance. Handed to Windows' own move loop
 *   (Tauri's startDragging), a drag would end without the page hearing
 *   the release, and the release is where the card snaps and its place is
 *   kept.
 *
 * The window itself (its place, its size, and when Windows' state hides
 * it) is the shell's: ui/src-tauri/src/deskcard.rs.
 */

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import type { PointerEvent as ReactPointerEvent } from "react";
import { AgentSocket, type ServerEvent } from "./api";
import { applyAppearance, readAppSettings, saveAppSettings, type AppSettings } from "./appSettings";
import { control, type Need, type Next } from "./control";
import { deskcard, onShell, tellWindows, type Corner } from "./desktop";
import { hideUntil, nextLine, notesLine, soonEvent } from "./deskLines";
import { firstLine, stevesLine } from "./journal";
import { useVoice } from "./level";
import { Mark, type MarkState } from "./Mark";
import { onSession, signedOut } from "./session";
import { loadSetup } from "./Setup";
import "./deskcard.css";

/** Held this long without moving, the press is push-to-talk. */
const HOLD_MS = 250;
/** Moved this far with the button down, the press is a drag. */
const MOVE_PX = 4;
/** Opened on its own, it closes after this, unless the pointer is on it. */
const OPEN_MS = 8000;
/** How often it asks what needs the user and what's next. A new note
 *  comes sooner, on the socket's "inbox". */
const LOOK_MS = 30_000;
/** The window's transparent edge, as deskcard.rs `PAD`. */
const PAD = 16;
/** The pointer gone this long, the card closes. While the window moves and
 *  grows to open the card, the page can hear the pointer leave for a frame;
 *  closed at once, the card shrank the window back under it, and opened
 *  again. */
const LEAVE_MS = 200;

interface Press {
  x: number;
  y: number;
  timer: number;
  mode: "down" | "talk" | "drag";
  onNote: boolean;
}

export function DeskCard() {
  const [settings, setSettings] = useState<AppSettings | null>(null);
  const [setupDone, setSetupDone] = useState(false);
  const [needs, setNeeds] = useState<Need[]>([]);
  const [next, setNext] = useState<Next | null>(null);
  const [doing, setDoing] = useState<Record<string, string>>({});
  const [last, setLast] = useState<string | null>(null);
  const [hover, setHover] = useState(false);
  const [opened, setOpened] = useState<"note" | "soon" | "reply" | null>(null);
  const [reply, setReply] = useState<string | null>(null);
  const [talking, setTalking] = useState(false);
  const voiceNow = useVoice();
  const [corner, setCorner] = useState<Corner>("bottom-right");
  const [now, setNow] = useState(() => Date.now());
  // Signed out (security model, section 4.2): the mark alone, which opens
  // the app to sign in. No line, no peek and no hold to talk: a card that
  // went on showing notes and next events showed them to whoever sat down.
  const [out, setOut] = useState(false);
  const outRef = useRef(false);
  const seenRef = useRef<Set<string> | null>(null);
  const soonRef = useRef<Set<string>>(new Set());
  const openTimer = useRef(0);
  const leaveTimer = useRef(0);
  const press = useRef<Press | null>(null);
  const moving = useRef(false);
  const cardRef = useRef<HTMLDivElement>(null);
  const settingsRef = useRef(settings);
  useEffect(() => {
    settingsRef.current = settings;
  }, [settings]);

  const words = settings?.desk_card_text !== "off";

  const openFor = useCallback((why: "note" | "soon" | "reply") => {
    window.clearTimeout(openTimer.current);
    setOpened(why);
    openTimer.current = window.setTimeout(() => setOpened(null), OPEN_MS);
  }, []);

  // What it shows, and whether it is wanted. The first look only learns
  // the notes already there: a card that opened for them at every start
  // would open for nothing new.
  const look = useCallback(async () => {
    const away = await signedOut();
    outRef.current = away;
    setOut(away);
    const [chosen, setup, needsNow, nextNow] = await Promise.all([
      readAppSettings(),
      loadSetup().catch(() => null),
      away ? null : control.needs().catch(() => null),
      away ? null : control.next().catch(() => null),
    ]);
    if (chosen) {
      setSettings(chosen);
      const quiet = chosen.theme === "ink-in-quiet-hours"
        && ((await control.autonomy().catch(() => null))?.quiet_hours.now ?? false);
      applyAppearance(chosen, quiet);
    }
    if (setup) setSetupDone(setup.done);
    // Signed out, emptied rather than hidden: the peek, the mark's note and
    // the lines all draw from these, so none can show what was there.
    if (away) {
      setNeeds([]);
      setNext(null);
      setDoing({});
      setOpened(null);
      setReply(null);
      setLast(null);
      return;
    }
    if (needsNow) {
      const ids = needsNow.needs.map((n) => n.id);
      const seen = seenRef.current;
      if (seen && ids.some((id) => !seen.has(id))) openFor("note");
      seenRef.current = new Set([...(seen ?? []), ...ids]);
      setNeeds(needsNow.needs);
    }
    const at = Date.now();
    if (nextNow) {
      setNext(nextNow);
      // An event within ten minutes opens it, once for that event.
      const soon = soonEvent(nextNow, at);
      if (soon && !soonRef.current.has(soon)) {
        soonRef.current.add(soon);
        openFor("soon");
      }
    }
    setNow(at);
  }, [openFor]);

  useEffect(() => {
    const every = window.setInterval(() => void look(), LOOK_MS);
    return () => window.clearInterval(every);
  }, [look]);

  // What Steve is doing, from every window's turn and every job; a new
  // note at once. The first look is when the backend answers, and again
  // after it restarts.
  useEffect(() => {
    const socket = new AgentSocket({
      onOpen: () => void look(),
      onClose: () => setDoing({}),
      onEvent: (event: ServerEvent) => {
        if (event.type === "doing") {
          if (outRef.current) return;
          setDoing((all) => {
            const { [event.of]: _gone, ...rest } = all;
            return event.text ? { ...rest, [event.of]: event.text } : rest;
          });
        } else if (event.type === "inbox") {
          void look();
        }
      },
    });
    socket.connect();
    return () => socket.dispose();
  }, [look]);

  // Wanted once setup is done and while the setting is on; the shell
  // shows it when the screen is free.
  const wanted = setupDone && settings?.desk_card === "on";
  const onTop = settings?.desk_card_on_top !== "off";
  useEffect(() => {
    void deskcard.want(wanted, onTop);
  }, [wanted, onTop]);

  // The shell's and the other windows' word.
  useEffect(() => {
    const offs: (() => void)[] = [];
    let gone = false;
    const keep = (off: () => void) => (gone ? off() : offs.push(off));
    void onShell<string>("deskcard-hide", (kind) => {
      void deskcard.hide(hideUntil(kind, Date.now()));
    }).then(keep);
    void onShell("deskcard-show", () => {
      if (settingsRef.current?.desk_card === "off" && outRef.current) {
        // Signed out, the tray's menu is anyone's: the user's setting stays
        // as they left it, and the app opens on its sign-in.
        void deskcard.open();
      } else if (settingsRef.current?.desk_card === "off") {
        // The main window is told too: its Settings went on saying the
        // card was off until the app was opened again.
        void saveAppSettings({ desk_card: "on" }).then(() => {
          look();
          void tellWindows("app-settings", "deskcard");
        });
      }
    }).then(keep);
    void onShell<string>("deskcard-reply", (text) => {
      if (outRef.current) return;
      setReply(text);
      openFor("reply");
    }).then(keep);
    void onShell("app-settings", () => void look()).then(keep);
    void onShell<boolean>("deskcard-showing", (on) => {
      if (on) void look();
    }).then(keep);
    void onSession(() => void look()).then(keep);
    return () => {
      gone = true;
      offs.forEach((off) => off());
    };
  }, [look, openFor]);

  // Steve's last entry, for a pointer on the card.
  useEffect(() => {
    if (!hover || !words || out) return;
    let gone = false;
    void control.day().then((day) => {
      const lines = day.entries.map(stevesLine).filter((l): l is string => !!l?.trim());
      if (!gone) setLast(lines.length ? lines[lines.length - 1].trim() : null);
    }, () => {});
    return () => {
      gone = true;
    };
  }, [hover, words, out]);

  const open = !out && (hover || opened !== null || talking);
  const doingNow = Object.values(doing).at(-1) ?? null;
  const lines = {
    now: talking ? "Listening" : words ? doingNow : null,
    next: nextLine(next, now, words),
    notes: notesLine(needs.length),
    last: !words ? null : opened === "reply" && reply ? firstLine(reply) : hover ? last : null,
  };

  // The window takes the page's size, its corner held; the corner it is
  // in says which way the card opens.
  useLayoutEffect(() => {
    const card = cardRef.current;
    if (!card) return;
    const sized = () => {
      void deskcard.size(card.offsetWidth + 2 * PAD, card.offsetHeight + 2 * PAD)
        .then((at) => at && setCorner(at));
    };
    sized();
    const watch = new ResizeObserver(sized);
    watch.observe(card);
    return () => watch.disconnect();
  }, []);

  const down = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    e.currentTarget.setPointerCapture(e.pointerId);
    const onNote = !!(e.target as HTMLElement).closest(".desk-notes");
    const timer = window.setTimeout(() => {
      // Talk off at the sidebar's foot: the hold stays a click.
      if (press.current?.mode !== "down" || outRef.current
          || settingsRef.current?.talk === "off") return;
      press.current.mode = "talk";
      setTalking(true);
      void deskcard.talk(true);
    }, HOLD_MS);
    press.current = { x: e.screenX, y: e.screenY, timer, mode: "down", onNote };
  };

  const move = (e: ReactPointerEvent<HTMLDivElement>) => {
    const held = press.current;
    if (!held) return;
    if (held.mode === "down" && Math.hypot(e.screenX - held.x, e.screenY - held.y) > MOVE_PX) {
      window.clearTimeout(held.timer);
      held.mode = "drag";
      void deskcard.drag("start");
    } else if (held.mode === "drag" && !moving.current) {
      // Once a frame: the window follows the pointer as Windows reports it.
      moving.current = true;
      requestAnimationFrame(() => {
        moving.current = false;
        void deskcard.drag("move");
      });
    }
  };

  const up = (clicked: boolean) => {
    const held = press.current;
    press.current = null;
    if (!held) return;
    window.clearTimeout(held.timer);
    if (held.mode === "drag") {
      void deskcard.drag("end").then((at) => at && setCorner(at));
    } else if (held.mode === "talk") {
      setTalking(false);
      void deskcard.talk(false);
    } else if (clicked) {
      void (held.onNote && !outRef.current ? deskcard.quickbar() : deskcard.open());
    }
  };

  const state: MarkState = talking ? "listening" : voiceNow === "speaking" ? "speaking"
    : doingNow ? "working" : "idle";
  return (
    <div className="desk" data-corner={corner} data-wanted={wanted ? "yes" : "no"}>
      <div ref={cardRef} className={open ? "desk-card open" : "desk-card"}
           onPointerEnter={() => {
             window.clearTimeout(leaveTimer.current);
             setHover(true);
           }}
           onPointerLeave={() => {
             window.clearTimeout(leaveTimer.current);
             leaveTimer.current = window.setTimeout(() => setHover(false), LEAVE_MS);
           }}
           onPointerDown={down} onPointerMove={move} onPointerUp={() => up(true)}
           onPointerCancel={() => up(false)}
           onContextMenu={(e) => {
             e.preventDefault();
             void deskcard.menu();
           }}>
        {needs.length > 0 && <div className="desk-peek" aria-hidden="true" />}
        <div className="desk-tile">
          <Mark size={44} state={state} needsYou={needs.length > 0}
                label={out ? "Steve: sign in to see more" : "Steve"} />
        </div>
        {open && (
          <div className="desk-lines">
            {lines.now && <div className="desk-line desk-now">{lines.now}</div>}
            {lines.next && <div className="desk-line desk-next">{lines.next}</div>}
            <div className={needs.length ? "desk-line desk-notes waiting" : "desk-line desk-notes"}>
              {needs.length > 0 && <span className="desk-gem" aria-hidden="true">◆ </span>}
              {lines.notes}
            </div>
            {lines.last && <div className="desk-last">{lines.last}</div>}
          </div>
        )}
      </div>
    </div>
  );
}
