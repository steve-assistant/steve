/**
 * The window's frame: the sidebar, the menu to the back pages, and a back
 * page's head (docs/design/journal.md, Layout and The sidebar;
 * back-pages.md, The menu and A back page).
 *
 * Eleven tabs across the top asked a stranger to learn the app's map before
 * anything else: which tab held a reminder, which a yes, which the backend's
 * state. Now one page is open at a time. Today is the journal, and the rest
 * are back pages reached from the user's name, each with its way back.
 *
 * The widths are the stylesheet's (shell.css): the sidebar folds to a rail
 * under 760px, and opens over the page under 520px. The title bar takes
 * the theme's `desk` colour from App (`watchTheme`, ui/src-tauri/src/caption.rs).
 */
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { HandMark } from "./HandMark";
import { Mark, type MarkState } from "./Mark";
import { Search } from "./Search";
import "./shell.css";

/** Where the sidebar folds to a rail (shell.css). */
const FOLDED = "(max-width: 759px)";

export interface MenuPage {
  key: string;
  name: string;
  /** How many wait on that page: shown beside its name. */
  count?: number;
  /** A word beside the count, as "building". */
  note?: string;
}

export interface MenuGroup {
  group: string;
  pages: MenuPage[];
}

export interface Ongoing {
  key: string;
  text: string;
}

export interface Presence {
  state: MarkState;
  needsYou: boolean;
  /** The sidebar's line under "Steve". */
  line: string;
}

/** What the mark and its line say (docs/design/presence.md, The mark). */
export function presence(seen: {
  service: string | null;
  connected: boolean;
  /** Not connected for over five seconds: a worker swap takes under one,
      and shows nothing. */
  longGone: boolean;
  listening: boolean;
  /** The speakers playing Steve's voice (level.ts). */
  speaking: boolean;
  doing: string | null;
  thinking: boolean;
  needs: number;
}): Presence {
  const needsYou = seen.needs > 0;
  if (seen.service === "stopped" || seen.service === "failing" || seen.service === "damaged") {
    return { state: "stopped", needsYou, line: "Stopped" };
  }
  if (!seen.connected && seen.longGone) return { state: "starting", needsYou, line: "Starting" };
  if (seen.listening) return { state: "listening", needsYou, line: "Listening" };
  if (seen.speaking) return { state: "speaking", needsYou, line: "Speaking" };
  if (seen.doing) return { state: "working", needsYou, line: seen.doing };
  if (seen.thinking) return { state: "working", needsYou, line: "Writing a reply" };
  if (needsYou) {
    const one = seen.needs === 1;
    return { state: "idle", needsYou,
             line: `${seen.needs} ${one ? "thing needs" : "things need"} you` };
  }
  return { state: "idle", needsYou, line: "Idle" };
}

/** A back page's head: its group in the hand, its name below, and the
    way back to the journal. */
export interface BackPageHead {
  group: string;
  name: string;
}

interface ShellProps {
  presence: Presence;
  /** The user's name, at the sidebar's foot; empty before setup gives one. */
  name: string;
  ongoing: Ongoing[];
  onOngoing: (key: string) => void;
  menu: MenuGroup[];
  /** The back page open, or null for Today. */
  page: BackPageHead | null;
  onPage: (key: string) => void;
  onToday: () => void;
  /** This week's days that held anything, newest first, today among them. */
  days?: { date: string; label: string }[];
  /** Today's entries the user hasn't seen. */
  unread?: number;
  /** The notes: a red diamond and their count beside the day (notes.md, Elsewhere). */
  needs?: number;
  onDay?: (date: string) => void;
  /** The day the page shows, marked in the list; null for today. */
  inView?: string | null;
  /** Under the days: Coming up, Extensions and Call notes, each with what
   *  it holds. Behind the menu, nobody found them (agent/worker/places.py). */
  places?: MenuPage[];
  /** A search's line chosen: its day, at its entry, or the day alone. */
  onFound?: (date: string, key: string | null) => void;
  onQuit: () => void;
  /** "Show me around": the tour again, from the menu (Tour.tsx). */
  onTour?: () => void;
  /** Voice at hand, above the name: Talk, and Call notes once the voice
   *  add-on is installed. Settings held them, out of reach. */
  switches?: ReactNode;
  /** The notes' margin, beside the page in a wide window (StuckNotes.tsx). */
  margin?: ReactNode;
  children: ReactNode;
}

export function Shell({ presence: shown, name, ongoing, onOngoing, menu, page, onPage, onToday,
                        days = [], unread = 0, needs = 0, onDay, inView = null, places = [],
                        onFound, onQuit, onTour, switches, margin, children }: ShellProps) {
  const [menuOpen, setMenuOpen] = useState(false);
  // The day being read is the one marked. Today was marked whatever the
  // page showed, yesterday's words in view among them.
  const todayDate = days.find((d) => d.label === "Today")?.date;
  const reading = inView ?? todayDate;
  const marked = (date: string | undefined) => (!page && date === reading ? "page" : undefined);
  // The whole sidebar where it is folded: over the page in a window too
  // narrow for a rail, in the rail's place while searching from one.
  const [overSidebar, setOverSidebar] = useState(false);
  const [searching, setSearching] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);
  const openForSearch = useCallback((open: boolean) => {
    if (window.matchMedia(FOLDED).matches) setOverSidebar(open);
  }, []);

  // The keys the shell owns (journal.md, Keyboard). The journal's own
  // (the lenses, the days, the first note) come with it.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.defaultPrevented) return;
      if (event.key === "Escape") {
        if (menuOpen) setMenuOpen(false);
        else if (overSidebar) setOverSidebar(false);
        else if (page) onToday();
        else return;
        event.preventDefault();
      } else if (event.ctrlKey && !event.altKey && event.key.toLowerCase() === "t") {
        event.preventDefault();
        setMenuOpen(false);
        onToday();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [menuOpen, overSidebar, page, onToday]);

  // A click anywhere but the menu closes it.
  useEffect(() => {
    if (!menuOpen) return;
    const away = (event: MouseEvent) => {
      if (!menuRef.current?.contains(event.target as Node)) setMenuOpen(false);
    };
    window.addEventListener("mousedown", away);
    return () => window.removeEventListener("mousedown", away);
  }, [menuOpen]);

  const choose = (go: () => void) => {
    setMenuOpen(false);
    setOverSidebar(false);
    go();
  };

  return (
    <div className="steve" data-margin={margin ? "" : undefined}
         data-over={overSidebar ? "" : undefined}>
      <button className="sidebar-open" aria-label="Open the sidebar"
              onClick={() => setOverSidebar(true)}>
        <Mark size={24} state={shown.state} needsYou={shown.needsYou} />
      </button>
      {overSidebar && <div className="scrim" onClick={() => setOverSidebar(false)} />}

      <nav className="sidebar" aria-label="Steve" data-searching={searching ? "" : undefined}>
        <div className="presence" role="status">
          <Mark size={40} state={shown.state} needsYou={shown.needsYou} />
          <div className="presence-words">
            <div className="presence-name">Steve</div>
            <div className="presence-line">{shown.line}</div>
          </div>
        </div>

        {onFound && (
          <Search onFound={(date, key) => choose(() => onFound(date, key))}
                  onSearching={setSearching} onOpen={openForSearch} />
        )}

        {/* The days and Ongoing scroll; the menu above the name must not be
            cut off by that, so the sidebar itself doesn't scroll. */}
        <div className="sidebar-middle">
        <ol className="days">
          <li>
            <button className="day" aria-current={marked(todayDate ?? reading)}
                    onClick={() => choose(onToday)}>
              Today
              {unread > 0 && <span className="day-count">{unread}</span>}
              {needs > 0 && (
                <span className="day-needs" aria-label={`${needs} ${needs === 1 ? "note" : "notes"}`}>
                  <span className="needs-count">
                    {needs}
                    <HandMark name="circle" id="needs" className="needs-circle" />
                  </span>
                </span>
              )}
            </button>
          </li>
          {days.filter((d) => d.label !== "Today").map((d) => (
            <li key={d.date}>
              <button className="day" aria-current={marked(d.date)}
                      onClick={() => choose(() => onDay?.(d.date))}>
                {d.label}
              </button>
            </li>
          ))}
        </ol>

        {places.length > 0 && (
          <nav className="places" aria-label="Places">
            <ul>
              {places.map((p) => (
                <li key={p.key}>
                  <button className="place" aria-current={page?.name === p.name ? "page" : undefined}
                          onClick={() => choose(() => onPage(p.key))}>
                    {p.name}
                    {(p.count ?? 0) > 0 && <span className="day-count">{p.count}</span>}
                    {p.note && <span className="place-note">{p.note}</span>}
                  </button>
                </li>
              ))}
            </ul>
          </nav>
        )}

        {ongoing.length > 0 && (
          <section className="ongoing" aria-label="Ongoing">
            <h2>Ongoing</h2>
            <ul>
              {ongoing.map((o) => (
                <li key={o.key}>
                  <button onClick={() => choose(() => onOngoing(o.key))}>
                    <span className="signifier" aria-hidden>◇</span>{o.text}
                  </button>
                </li>
              ))}
            </ul>
          </section>
        )}
        </div>

        <div className="foot" ref={menuRef}>
          {menuOpen && (
            <div className="menu" role="menu" aria-label="Back pages">
              {menu.map((g) => (
                <div className="menu-group" key={g.group} role="group" aria-label={g.group}>
                  <div className="menu-title">{g.group}</div>
                  {g.pages.map((p) => (
                    <button key={p.key} role="menuitem" onClick={() => choose(() => onPage(p.key))}>
                      {p.name}
                      {(p.count ?? 0) > 0 && <span className="count">{p.count}</span>}
                    </button>
                  ))}
                </div>
              ))}
              <hr />
              {onTour && <button role="menuitem" onClick={() => choose(onTour)}>Show me around</button>}
              <button role="menuitem" onClick={() => choose(onQuit)}>Quit Steve</button>
            </div>
          )}
          {switches && <div className="foot-switches">{switches}</div>}
          <button className="who" aria-haspopup="menu" aria-expanded={menuOpen}
                  onClick={() => setMenuOpen((open) => !open)}>
            <span className="who-name">{name || "Menu"}</span>
            <span className="who-rail" aria-hidden>≡</span>
            <span className="caret" aria-hidden>▾</span>
          </button>
        </div>
      </nav>

      <main className="page">
        {page ? (
          <article className="back-page">
            <div className="back-column">
              <button className="back" onClick={onToday}>← Today</button>
              <header className="back-head">
                <p className="back-group">{page.group}</p>
                <h1 className="back-name">{page.name}</h1>
              </header>
              <div className="back-body">{children}</div>
            </div>
          </article>
        ) : children}
      </main>

      {margin && <aside className="margin" aria-label="Needs you">{margin}</aside>}
    </div>
  );
}
