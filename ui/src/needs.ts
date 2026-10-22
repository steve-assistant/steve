/* What the notes need besides drawing (StuckNotes.tsx): the app's own notes,
   the window's width, and each note's tilt.

   Two notes come from the app, not the backend: the backend that keeps
   stopping, which a stopped backend can't report, and the first day's
   sample. They are shaped as the backend's are, so the margin draws one
   list and answers each the same way. */

import { useSyncExternalStore } from "react";
import type { LaterWhen, Need } from "./control";
import type { ServiceStatus } from "./desktop";
import { variantOf } from "./variant";

/** A note as drawn: the backend's, or one of the app's own. */
export interface AppNeed extends Need {
  /** The app's notes say what they are in their own words. */
  kicker?: string;
  /** The stopped backend's note: the end of its log, behind Details. */
  log?: string[];
}

export type NoteAnswer =
  | { say: "yes"; passphrase?: string; hello?: boolean; only?: number[] }
  | { say: "no" }
  | { say: "done" }
  | { say: "later"; when: LaterWhen }
  | { say: "not_now" }
  | { say: "fix"; action: string };

export const SAMPLE_ID = "app:sample";
export const BACKEND_ID = "app:backend";

const SAMPLE: AppNeed = {
  kind: "now", id: SAMPLE_ID, kicker: "A note",
  text: "When Steve needs you, a note like this sticks here: a yes to give, a reminder going off, something that stopped.",
  detail: "Answer it here or from your phone, and it comes off.",
  word: null, at: null, actions: ["done"],
};

/** The app's own notes, before the backend's: a stopped backend first,
 *  since nothing else works until it starts. */
export function appNeeds({ service, putAway, sample }: {
  service: ServiceStatus | null;
  /** The stopped backend's note, put away until then ("Not now"). */
  putAway: number;
  sample: boolean;
}): AppNeed[] {
  const out: AppNeed[] = [];
  if (service?.state === "failing" && Date.now() >= putAway) {
    out.push({
      kind: "stopped", id: BACKEND_ID,
      text: "Steve’s backend keeps stopping. Nothing runs until it starts.",
      detail: `It stopped ${service.restarts} times in ten minutes, so the app has stopped starting it.`,
      word: null, at: null, actions: ["start", "not_now"], log: service.log_tail,
    });
  }
  if (sample) out.push(SAMPLE);
  return out;
}

/** A note's tilt, as a share of `--tilt-max`: never under 0.4 either way,
 *  so no note looks printed, and from its id, so a redraw never moves it. */
export function tiltOf(id: string): number {
  const size = 0.4 + 0.05 * variantOf(id, 13);
  return variantOf(`${id}/side`, 2) ? size : -size;
}

// The margin's width (shell.css): notes sit in it from here up, and gather
// at the top of the page below it.
const WIDE = "(min-width: 1180px)";

function watchWidth(changed: () => void): () => void {
  const query = window.matchMedia(WIDE);
  query.addEventListener("change", changed);
  return () => query.removeEventListener("change", changed);
}

export function useWide(): boolean {
  return useSyncExternalStore(watchWidth, () => window.matchMedia(WIDE).matches);
}
