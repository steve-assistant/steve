/** The voice's level, told by the backend a few times a second while it
    listens or speaks (agent/worker/background.py, `voice_level`).

    Kept outside React's state: eight renders of a whole window a second
    would be waste. The mark and the voice button follow it by a CSS
    variable (`useLevel`), and a window renders again only when what the
    voice is doing changes (`useVoice`). */
import { type RefObject, useEffect, useState } from "react";

export type Voice = "listening" | "speaking" | null;
export type Level = { of: Voice; level: number };

const QUIET: Level = { of: null, level: 0 };
let last: Level = QUIET;
const followers = new Set<(now: Level) => void>();

/** From the socket: the level just told, or quiet when it closes. */
export function heardLevel(now: Level = QUIET): void {
  last = now;
  for (const follow of followers) follow(now);
}

/** Calls `follow` with the level now and with each one told after; returns
    the way to stop. */
export function onLevel(follow: (now: Level) => void): () => void {
  followers.add(follow);
  follow(last);
  return () => {
    followers.delete(follow);
  };
}

/** What the voice is doing, rendering again only when that changes. */
export function useVoice(): Voice {
  const [of, setOf] = useState<Voice>(last.of);
  useEffect(() => onLevel((now) => setOf(now.of)), []);
  return of;
}

/** Sets `--level`, from 0 to 1, on the element while `on` and a level is
    told. Untold, the element's own rest applies: set to 0, a listening mark
    whose level hadn't come yet shrank to idle's size, and read as idle. */
export function useLevel(ref: RefObject<Element | null>, on: boolean): void {
  useEffect(() => {
    const element = ref.current as HTMLElement | SVGElement | null;
    if (!on || !element) return;
    const stop = onLevel((now) => (now.of
      ? element.style.setProperty("--level", String(now.level))
      : element.style.removeProperty("--level")));
    return () => {
      stop();
      element.style.removeProperty("--level");
    };
  }, [ref, on]);
}
