/* What every back page needs (docs/design/back-pages.md): its own data,
   asked for while it is on screen, and a line that says what a change did.

   The pages' data lived in App.tsx, one state and one handler per page, so
   App held eleven views' worth and asked for a page's data whichever page
   was open. Now a page asks for its own, from when it opens until it
   closes, and nothing asks while the window is hidden.

   The line replaces the window's toasts: they reported a change in a
   corner, eight seconds, away from what was changed, and a screen reader
   heard none of them. */

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { control } from "./control";

/** Asks `load` when the page opens, then every `everyMs` while `onScreen`.
 *  `again()` asks at once, after a change. Answers to an older ask, or
 *  after the page has closed, are dropped. */
export function usePolled<T>(
  load: (signal: AbortSignal) => Promise<T>,
  { everyMs = 8000, onScreen = true }: { everyMs?: number; onScreen?: boolean } = {},
): { data: T | null; failed: boolean; again: () => Promise<void> } {
  const [data, setData] = useState<T | null>(null);
  const [failed, setFailed] = useState(false);
  const loadRef = useRef(load);
  useLayoutEffect(() => {
    loadRef.current = load;
  });
  const asked = useRef(0);
  const live = useRef<AbortController | null>(null);

  const again = useCallback(async () => {
    const n = ++asked.current;
    const controller = new AbortController();
    live.current?.abort();
    live.current = controller;
    try {
      const got = await loadRef.current(controller.signal);
      if (n === asked.current && !controller.signal.aborted) {
        setData(got);
        setFailed(false);
      }
    } catch {
      // The supervisor may be mid-swap: the last answer stays, marked.
      if (n === asked.current && !controller.signal.aborted) setFailed(true);
    }
  }, []);

  useEffect(() => {
    if (!onScreen) return;
    void again();
    const timer = window.setInterval(() => void again(), everyMs);
    return () => {
      window.clearInterval(timer);
      live.current?.abort();
    };
  }, [again, everyMs, onScreen]);

  return { data, failed, again };
}

export interface Said {
  text: string;
  ok: boolean;
  n: number;
}

/** Says what a change did: `ok` false for a failure, which stays. */
export type Say = (text: string, ok?: boolean) => void;

/** What the page's last change did: "Saved" fades after a few seconds; a
 *  failure stays until the next change, since it says what to do. */
export function useSaid(fadeMs = 4000): [Said | null, Say] {
  const [said, setSaid] = useState<Said | null>(null);
  const seq = useRef(0);
  useEffect(() => {
    if (!said?.ok) return;
    const timer = window.setTimeout(
      () => setSaid((now) => (now?.n === said.n ? null : now)), fadeMs);
    return () => window.clearTimeout(timer);
  }, [said, fadeMs]);
  const say = useCallback((text: string, ok = true) => {
    setSaid({ text, ok, n: ++seq.current });
  }, []);
  return [said, say];
}

/** A knob changed from a page: the error to show, or null. */
export async function saveKnob(name: string, value: number | string | null) {
  const done = await control.knobs({ [name]: value });
  return done.ok ? null : done.error ?? "That wasn't kept.";
}
