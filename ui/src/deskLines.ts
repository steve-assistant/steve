/**
 * What the desk card says (DeskCard.tsx), worked out apart from drawing it.
 *
 * The card's words can be read by anyone passing the screen. With "Show
 * what Steve is doing on the desk card" off (`words` false), each line
 * here drops what the user didn't choose to show: the next event keeps
 * its time and loses its name and place (presence.md, Privacy).
 */

import type { Next } from "./control";
import { clock } from "./journal";

/** The card opens on its own for an event this close (presence.md). */
export const SOON_MS = 10 * 60_000;
/** The card's next line looks this far ahead. */
export const NEXT_WITHIN_MS = 3 * 60 * 60_000;
/** The page turns at 4 a.m. (docs/design/README.md, settled by step 9's
 *  plan): "Hide until tomorrow" lasts until then, so "tomorrow" means what
 *  the journal means by it. */
export const DAY_TURNS_AT = 4;

type Soon = { at: number; what: string | null; where: string | null; event: boolean };

/** The sooner of the next event and the next reminder, if within three
 *  hours; one that started a minute ago still counts. */
function soonest(next: Next | null, now: number): Soon | null {
  const found: Soon[] = [];
  if (next?.event) {
    found.push({ at: new Date(next.event.start).getTime(), what: next.event.summary,
                 where: next.event.location, event: true });
  }
  if (next?.reminder) {
    found.push({ at: new Date(next.reminder.at).getTime(), what: next.reminder.text, where: null,
                 event: false });
  }
  const near = found.filter((s) => s.at >= now - 60_000 && s.at - now <= NEXT_WITHIN_MS);
  return near.sort((a, b) => a.at - b.at)[0] ?? null;
}

/** "Next · Design review, 15:00"; "Standup in 8 min · Room 4" once it is
 *  close; "Next · 15:00" without the words. Null with nothing within three
 *  hours. */
export function nextLine(next: Next | null, now: number, words: boolean): string | null {
  const soon = soonest(next, now);
  if (!soon) return null;
  const time = clock(new Date(soon.at).toISOString());
  if (!words) return `Next · ${time}`;
  const what = soon.what?.trim() || (soon.event ? "An event" : "A reminder");
  if (soon.at - now <= SOON_MS) {
    const minutes = Math.round((soon.at - now) / 60_000);
    const when = minutes <= 0 ? "now" : `in ${minutes} min`;
    return [`${what} ${when}`, soon.where?.trim()].filter(Boolean).join(" · ");
  }
  return `Next · ${what}, ${time}`;
}

/** The event the card opens for, once: its start and name, while it is
 *  within ten minutes and hasn't begun; else null. */
export function soonEvent(next: Next | null, now: number): string | null {
  const event = next?.event;
  if (!event) return null;
  const at = new Date(event.start).getTime();
  return at >= now && at - now <= SOON_MS ? `${event.start} ${event.summary ?? ""}` : null;
}

/** "◆ 2 need you", or that nothing does. */
export function notesLine(count: number): string {
  if (count === 0) return "Nothing needs you";
  return `${count} ${count === 1 ? "needs" : "need"} you`;
}

/** The end of the card's "Hide for an hour" or "Hide until tomorrow". */
export function hideUntil(kind: string, now: number): number {
  if (kind === "hour") return now + 60 * 60_000;
  const turn = new Date(now);
  turn.setHours(DAY_TURNS_AT, 0, 0, 0);
  if (turn.getTime() <= now) turn.setDate(turn.getDate() + 1);
  return turn.getTime();
}
