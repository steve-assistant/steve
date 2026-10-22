/* The journal's pages as the window holds them (agent/worker/journal.py).

   Chat kept the conversation in the window's memory: a reply sent on
   Signal, a briefing, a task finished overnight never reached it, and a
   restart emptied it. Now the backend's day is the one record. The page
   asks for what changed since its last answer every five seconds while it
   is on screen, and at once when the socket says something happened, so a
   reply on the phone and one typed here land in the same place. */

import { useCallback, useEffect, useRef, useState } from "react";
import { control, type Day, type DayEntry, type FoldedDay, type Verdict } from "./control";

/** How often the open page asks for what changed, while it is on screen. */
export const DAY_POLL_MS = 5000;
/** Folded days fetched at a time, scrolling back. */
const BATCH = 7;

function byTime(a: DayEntry, b: DayEntry): number {
  const at = (a.at ?? "").localeCompare(b.at ?? "");
  return at !== 0 ? at : a.key.localeCompare(b.key);
}

/** `fresh` entries in place of those with the same key, in time order. */
export function merged(entries: DayEntry[], fresh: DayEntry[]): DayEntry[] {
  const keys = new Set(fresh.map((e) => e.key));
  return [...entries.filter((e) => !keys.has(e.key)), ...fresh].sort(byTime);
}

/** The date `n` days from `date`, both YYYY-MM-DD. */
export function shifted(date: string, n: number): string {
  const [y, m, d] = date.split("-").map(Number);
  const at = new Date(Date.UTC(y, m - 1, d + n));
  return at.toISOString().slice(0, 10);
}

export interface Journal {
  /** The open pages, oldest first; the last is the page the window opened on, or a newer one. */
  pages: Day[];
  /** The days fetched scrolling back, oldest first, each as its band: an
   *  opened one keeps it, so folded again it is back where it was. */
  folded: FoldedDay[];
  /** An empty week came back: further days only when asked. */
  quiet: boolean;
  /** Entries that were new when they arrived: they keep their highlight until it fades. */
  fresh: ReadonlySet<string>;
  /** Fetch the week before the earliest day held. */
  earlier: () => Promise<void>;
  /** A folded day opened in place. */
  open: (date: string) => Promise<void>;
  /** An opened day folded again, to its band. */
  fold: (date: string) => Promise<void>;
  /** A check or a cross on a reply, drawn at once. */
  react: (messageId: number, verdict: Verdict | null) => Promise<boolean>;
  /** An entry gone for good, such as a listening note deleted. */
  drop: (key: string) => void;
}

/**
 * `poke` changes when the socket says something happened (a reply, a task
 * finished): the page asks at once rather than at the next tick.
 */
export function useJournal({ connected, onScreen, poke }: {
  connected: boolean;
  onScreen: boolean;
  poke: number;
}): Journal {
  const [pages, setPages] = useState<Day[]>([]);
  const [folded, setFolded] = useState<FoldedDay[]>([]);
  const [quiet, setQuiet] = useState(false);
  const [fresh, setFresh] = useState<ReadonlySet<string>>(new Set());
  // The newest page and the days held, for callbacks that must not wait
  // for a render to see them.
  const newest = useRef<Day | null>(null);
  const held = useRef<{ pages: Day[]; folded: FoldedDay[] }>({ pages: [], folded: [] });
  const asking = useRef(false);
  const fetchingEarlier = useRef(false);

  const keepFresh = useCallback((entries: DayEntry[]) => {
    const now = entries.filter((e) => e.new).map((e) => e.key);
    if (now.length) setFresh((all) => new Set([...all, ...now]));
  }, []);

  const onScreenRef = useRef(onScreen);
  useEffect(() => {
    held.current = { pages, folded };
    onScreenRef.current = onScreen;
  });
  const saw = useCallback((day: Day) => {
    // Looked: only with the window in front of them. The highlight stays
    // with the entries already here (`fresh`) until it has been seen.
    if (onScreenRef.current && document.hasFocus() && day.entries.some((e) => e.new)) {
      void control.looked(day.as_of);
    }
  }, []);

  const ask = useCallback(async () => {
    if (asking.current) return;
    asking.current = true;
    try {
      const last = newest.current;
      if (last === null) {
        const day = await control.day();
        newest.current = day;
        keepFresh(day.entries);
        setPages([day]);
        saw(day);
        return;
      }
      const changed = await control.day(last.date, last.as_of);
      const day = { ...changed, entries: merged(last.entries, changed.entries) };
      keepFresh(changed.entries);
      if (changed.today > last.date) {
        // The page turned at 4 a.m. The open one stays where it is, and
        // the new day starts below it, above the composer.
        const next = await control.day(changed.today);
        keepFresh(next.entries);
        newest.current = next;
        setPages((all) => [...all.slice(0, -1), day, next]);
        saw(next);
        return;
      }
      newest.current = day;
      setPages((all) => [...all.slice(0, -1), day]);
      saw(changed);
    } catch {
      /* the backend between answers: the next tick asks again */
    } finally {
      asking.current = false;
    }
  }, [keepFresh, saw]);

  // Today's page as soon as the backend answers, then what changed.
  useEffect(() => {
    if (!connected) return;
    void ask();
    if (!onScreen) return;
    const timer = window.setInterval(() => void ask(), DAY_POLL_MS);
    return () => window.clearInterval(timer);
  }, [connected, onScreen, ask]);

  useEffect(() => {
    if (poke > 0 && connected) void ask();
  }, [poke, connected, ask]);

  // Back on screen: what arrived while it was hidden is what they're now seeing.
  useEffect(() => {
    if (onScreen && newest.current) saw(newest.current);
  }, [onScreen, saw]);

  const earlier = useCallback(async () => {
    const { pages: open, folded: before } = held.current;
    const first = before[0]?.date ?? open[0]?.date;
    if (!first || fetchingEarlier.current) return;
    fetchingEarlier.current = true;
    try {
      const { days } = await control.days(first, BATCH);
      const older = [...days].reverse();
      setFolded((all) => [...older, ...all.filter((d) => !older.some((o) => o.date === d.date))]);
      setQuiet(days.every((d) => d.count === 0));
    } catch {
      /* asked again when the top comes into view again */
    } finally {
      fetchingEarlier.current = false;
    }
  }, []);

  const open = useCallback(async (date: string) => {
    if (held.current.pages.some((p) => p.date === date)) return;
    try {
      const day = await control.day(date);
      setPages((all) => [...all.filter((p) => p.date !== date), day]
        .sort((a, b) => a.date.localeCompare(b.date)));
    } catch {
      /* the band stays folded; clicking again asks again */
    }
  }, []);

  // An opened day had no way back to its band, so a week read back stayed
  // open, page after page, above today. The newest page never folds: it is
  // the one being written.
  const fold = useCallback(async (date: string) => {
    const { pages: open, folded: bands } = held.current;
    if (open.length < 2 || open[open.length - 1].date === date) return;
    setPages((all) => all.filter((p) => p.date !== date));
    if (bands.some((d) => d.date === date)) return;
    // Opened without a band (the page turning at 4 a.m., a search): its
    // band is the backend's, the one day before the next.
    try {
      const { days } = await control.days(shifted(date, 1), 1);
      setFolded((all) => [...all, ...days.filter((d) => d.date === date)]
        .sort((a, b) => a.date.localeCompare(b.date)));
    } catch {
      /* its band comes with the next week fetched */
    }
  }, []);

  const react = useCallback(async (messageId: number, verdict: Verdict | null) => {
    const marked = (p: Day, to: Verdict | null): Day => ({
      ...p,
      entries: p.entries.map((e) => (e.kind === "reply" && e.id === messageId
        ? { ...e, reaction: to } : e)),
    });
    const mark = (to: Verdict | null) => {
      // The newest page too: the next answer is merged into it.
      if (newest.current) newest.current = marked(newest.current, to);
      setPages((all) => all.map((p) => marked(p, to)));
    };
    const before = held.current.pages.flatMap((p) => p.entries)
      .find((e) => e.kind === "reply" && e.id === messageId);
    mark(verdict);
    const done = await control.react(messageId, verdict);
    if (!done.ok) mark(before?.kind === "reply" ? before.reaction : null);
    return done.ok;
  }, []);

  // A poll only adds and replaces entries: what is gone from the backend
  // is never in an answer, so a deleted note stayed on the page, and a
  // second delete failed with "no note".
  const drop = useCallback((key: string) => {
    const without = (p: Day): Day => ({ ...p, entries: p.entries.filter((e) => e.key !== key) });
    // The newest page too: the next answer is merged into it.
    if (newest.current) newest.current = without(newest.current);
    setPages((all) => all.map(without));
  }, []);

  return { pages, folded, quiet, fresh, earlier, open, fold, react, drop };
}

// The page's words and filters, kept from the components that draw them.

export type Lens = "all" | "talk" | "log";

const TALK: ReadonlySet<DayEntry["kind"]> = new Set(["said", "reply", "spoken"]);

/** Talk is what the two of them said; Log is what Steve did and noticed. */
export function inLens(entry: DayEntry, lens: Lens): boolean {
  return lens === "all" || TALK.has(entry.kind) === (lens === "talk");
}

/** A message sent from this window that the page doesn't hold yet. */
export interface Unsent {
  id: number;
  text: string;
  /** The photos it carries, kept to go up again on Retry. */
  photos?: File[];
  /** When it was sent, in ms. */
  at: number;
  failed: boolean;
}

export function clock(iso: string | null | undefined): string {
  if (!iso) return "";
  const at = new Date(iso);
  return Number.isNaN(at.getTime()) ? "" : at.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

export function localDate(date: string): Date {
  const [y, m, d] = date.split("-").map(Number);
  return new Date(y, m - 1, d);
}

/** "Today", "Yesterday", or the weekday. */
export function dayName(date: string, today: string): string {
  if (date === today) return "Today";
  if (date === shifted(today, -1)) return "Yesterday";
  return localDate(date).toLocaleDateString([], { weekday: "long" });
}

/** Steve's first entry, written by the app: the page before anything has happened. */
export function firstWords(name: string | null): string {
  const first = name?.trim().split(/\s+/)[0];
  return `Hello${first ? `, ${first}` : ""}. I'll keep everything we do here, one page a day. `
    + "Write below, or hold the microphone to talk. What I notice on my own will appear here "
    + "too, and anything I need from you will be stuck to the side, like this.";
}

/** The sent messages the page doesn't show yet: each matched to one of today's. */
export function stillUnsent(unsent: Unsent[], entries: DayEntry[]): Unsent[] {
  const said = entries.filter((e) => e.kind === "said");
  const used = new Set<string>();
  return unsent.filter((u) => {
    // Never sent, so never on the page: it waits for Retry.
    if (u.failed) return true;
    const match = said.find((e) => !used.has(e.key) && e.kind === "said"
      && e.text.trim() === u.text && Date.parse(e.at ?? "") >= u.at - 60_000);
    if (match) used.add(match.key);
    return !match;
  });
}

/** Steve's side of an entry, in one line; null for the user's own. */
export function stevesLine(e: DayEntry): string | null {
  switch (e.kind) {
    case "reply":
    case "briefing":
    case "update":
      return e.text;
    case "action":
      return e.line;
    case "task":
      return e.title;
    case "spoken":
      return e.summary ?? e.first_words;
    case "listening":
      return e.title;
    default:
      return null;
  }
}

/** A text's first line, trimmed: the quick bar's and the desk card's
 *  one line of Steve's last word. */
export function firstLine(text: string): string {
  return text.trim().split("\n")[0];
}
