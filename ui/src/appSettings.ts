/* The app's choices, kept by the backend (agent/appsettings.py): the
   hotkey, update checks, the theme and Reading.

   They lived in this window's localStorage, inside WebView2's folder, where
   a reinstall lost them and no test could read them. What the storage still
   holds is moved to the backend once, each choice on its own, and then
   removed: a value the backend refuses would be refused at every start. */

import { request } from "./api";

export type AppSettings = {
  theme: "auto" | "paper" | "ink" | "ink-in-quiet-hours";
  face: "newsreader" | "atkinson";
  hand: "hand" | "plain";
  size: "normal" | "larger";
  update_checks: "on" | "off";
  sample_note: "show" | "dismissed";
  desk_card: "on" | "off";
  desk_card_text: "on" | "off";
  desk_card_on_top: "on" | "off";
  /** Hold-to-talk, at the sidebar's foot: off, a hold is a tap. */
  talk: "on" | "off";
  /** The tour, due once setup ends (Tour.tsx). */
  tour: "off" | "due";
  hotkey?: string;
};

type Answer = { ok: boolean; settings?: AppSettings; error?: string };

// The window's storage keys before the backend kept them.
const STORED: Record<"hotkey" | "update_checks", string> = {
  hotkey: "steve.hotkey",
  update_checks: "steve.updates.check",
};
// The quick bar's own key, before the one hotkey took its place: dropped.
const DROPPED = ["steve.quickbar-hotkey"];

async function call(init?: RequestInit): Promise<Answer> {
  try {
    const response = await request("/control/app", init);
    return (await response.json()) as Answer;
  } catch (err) {
    return { ok: false, error: String(err) };
  }
}

function changing(changes: Partial<Record<keyof AppSettings, string | null>>): RequestInit {
  return {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ changes }),
  };
}

/** Changes some choices; null puts one back to its default. */
export function saveAppSettings(changes: Partial<Record<keyof AppSettings, string | null>>):
  Promise<Answer> {
  return call(changing(changes));
}

/** Every choice, after moving what the window's storage still held. Null
    while the backend can't be reached. */
export async function loadAppSettings(): Promise<AppSettings | null> {
  for (const key of DROPPED) localStorage.removeItem(key);
  for (const [name, key] of Object.entries(STORED)) {
    const held = localStorage.getItem(key);
    if (held === null) continue;
    try {
      const response = await request("/control/app", changing({ [name]: held }));
      // Moved, or refused for good: either way not sent again.
      if (response.ok || response.status === 400) localStorage.removeItem(key);
    } catch {
      // Not reached: moved at the next start.
    }
  }
  return readAppSettings();
}

/** Every choice, as the backend holds them; null while it can't be
    reached. The quick bar's window reads them; only the main window moves
    what the storage held. */
export async function readAppSettings(): Promise<AppSettings | null> {
  const answer = await call();
  return answer.ok && answer.settings ? answer.settings : null;
}

/** Theme and Reading, as tokens.css reads them: attributes on <html>,
    absent for each default. Ink in quiet hours is ink while `quiet`, the
    backend's word on whether the user's quiet hours are now, and paper
    otherwise (foundations.md, the ink theme). */
export function applyAppearance(settings: AppSettings, quiet = false): void {
  const root = document.documentElement.dataset;
  const set = (name: "theme" | "face" | "hand" | "size", value: string | null) => {
    if (value === null) delete root[name];
    else root[name] = value;
  };
  set("theme", settings.theme === "paper" || settings.theme === "ink" ? settings.theme
    : settings.theme === "ink-in-quiet-hours" ? (quiet ? "ink" : "paper") : null);
  set("face", settings.face === "atkinson" ? "atkinson" : null);
  set("hand", settings.hand === "plain" ? "plain" : null);
  set("size", settings.size === "larger" ? "larger" : null);
  window.dispatchEvent(new Event(THEME_CHANGED));
}

const THEME_CHANGED = "steve-theme";
const DARK = "(prefers-color-scheme: dark)";

/** The theme the page shows: the one chosen, else Windows'. */
export function shownTheme(): "paper" | "ink" {
  const chosen = document.documentElement.dataset.theme;
  if (chosen === "paper" || chosen === "ink") return chosen;
  return window.matchMedia(DARK).matches ? "ink" : "paper";
}

/** Told the theme shown now and at each change, from Settings or from
    Windows: the title bar follows it (`setCaption`). */
export function watchTheme(told: (theme: "paper" | "ink") => void): () => void {
  const media = window.matchMedia(DARK);
  let last: string | null = null;
  const tell = () => {
    const now = shownTheme();
    if (now === last) return;
    last = now;
    told(now);
  };
  tell();
  media.addEventListener("change", tell);
  window.addEventListener(THEME_CHANGED, tell);
  return () => {
    media.removeEventListener("change", tell);
    window.removeEventListener(THEME_CHANGED, tell);
  };
}
