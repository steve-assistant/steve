/**
 * What only the desktop shell can do (ui/src-tauri/src/lib.rs): start, stop
 * and restart the backend it owns (backend.rs), draw the tray's waiting dot,
 * and start at login. In a plain browser (`npm run dev`) each says so and
 * does nothing.
 */

import { isTauri } from "./api";

export type ServiceAction = "status" | "start" | "stop" | "restart";

/** The backend as the app sees it: its own process, not the agent's answer. */
export interface ServiceStatus {
  /** `external`: one started by hand, which the app found answering. */
  state: "running" | "starting" | "stopped" | "failing" | "external" | "damaged";
  pid: number | null;
  /** Starts after a death, in the last ten minutes. */
  restarts: number;
  /** The last lines of its log: when it keeps stopping, those it ended with. */
  log_tail: string[];
  /** When the install's files are not the ones shipped: which, and how. */
  damaged: string | null;
}

export async function service(
  action: ServiceAction,
): Promise<{ ok: boolean; status: ServiceStatus | null; text: string }> {
  if (!isTauri()) {
    return { ok: false, status: null, text: "Only the desktop app can start or stop the agent." };
  }
  const { invoke } = await import("@tauri-apps/api/core");
  try {
    return { ok: true, status: await invoke<ServiceStatus>("backend_control", { action }), text: "" };
  } catch (err) {
    return { ok: false, status: null, text: String(err) };
  }
}

export async function serviceStatus(): Promise<ServiceStatus | null> {
  return (await service("status")).status;
}

/**
 * Whether the main window is on screen; `changed` is called whenever that
 * changes. Returns the function that stops watching.
 *
 * The app starts hidden at login and lives in the tray, and it polled as if
 * someone were looking: seven endpoints every five seconds and
 * the keeper's status -- a Python start and a PowerShell -- every thirty,
 * all day. Measured 5 October 2026: ~145 ms of the worker's event loop and
 * ~75 KB of JSON per cycle, 1.4 s of CPU per service check, and through
 * `/status` a Docker probe every minute that kept Docker Desktop from ever
 * pausing. Hidden, the app now asks only for what the tray shows.
 *
 * Not `visibilitychange` alone: whether WebView2 reports a hidden window as
 * hidden depends on how it was hidden, so the window itself is asked as well,
 * on focus changes and every few seconds.
 */
export function watchOnScreen(changed: (on: boolean) => void): () => void {
  let alive = true;
  let last: boolean | null = null;
  const check = async () => {
    let on = document.visibilityState === "visible";
    if (on && isTauri()) {
      try {
        const { getCurrentWindow } = await import("@tauri-apps/api/window");
        const main = getCurrentWindow();
        on = (await main.isVisible()) && !(await main.isMinimized());
      } catch {
        /* if it cannot be asked, assume on: a busy poll beats a frozen panel */
      }
    }
    if (alive && on !== last) {
      last = on;
      changed(on);
    }
  };
  const onEvent = () => void check();
  void check();
  document.addEventListener("visibilitychange", onEvent);
  window.addEventListener("focus", onEvent);
  window.addEventListener("blur", onEvent);
  const timer = window.setInterval(onEvent, 3000);
  return () => {
    alive = false;
    document.removeEventListener("visibilitychange", onEvent);
    window.removeEventListener("focus", onEvent);
    window.removeEventListener("blur", onEvent);
    window.clearInterval(timer);
  };
}

/** The tray's dot and tooltip: how many cards wait for them. */
export async function setWaiting(count: number): Promise<void> {
  if (!isTauri()) return;
  const { invoke } = await import("@tauri-apps/api/core");
  await invoke("set_waiting", { count }).catch(() => {});
}

/** Show the quick bar (ui/src/QuickBar.tsx), or hide it if it has focus. */
export async function showQuickbar(): Promise<void> {
  if (!isTauri()) return;
  const { invoke } = await import("@tauri-apps/api/core");
  await invoke("show_quickbar").catch(() => {});
}

/** The hotkey held (`on`) or let go: the quick bar shown, and told to
 *  listen or to send what it heard (`quickbar_talk`). */
export async function quickbarTalk(on: boolean): Promise<void> {
  if (!isTauri()) return;
  const { invoke } = await import("@tauri-apps/api/core");
  await invoke("quickbar_talk", { on }).catch(() => {});
}

/** From the quick bar: hide it and bring the main window forward. */
export async function openAppFromQuickbar(): Promise<void> {
  if (!isTauri()) return;
  const { invoke } = await import("@tauri-apps/api/core");
  await invoke("quickbar_open_app").catch(() => {});
}

/** The title bar in the page's `desk` colour (ui/src-tauri/src/caption.rs). */
export async function setCaption(theme: "paper" | "ink"): Promise<void> {
  if (!isTauri()) return;
  const { invoke } = await import("@tauri-apps/api/core");
  await invoke("set_caption", { theme }).catch((err) => console.warn("caption:", err));
}

/** "Quit Steve": the app and its backend, as the tray's Quit. */
export async function quit(): Promise<void> {
  if (!isTauri()) return;
  const { invoke } = await import("@tauri-apps/api/core");
  await invoke("quit").catch((err) => console.warn("quit:", err));
}

/** This window brought forward, as the tray's "Open Steve" does. */
export async function comeForward(): Promise<void> {
  if (!isTauri()) return;
  const { getCurrentWindow } = await import("@tauri-apps/api/window");
  const window = getCurrentWindow();
  await window.unminimize().catch(() => {});
  await window.show().catch(() => {});
  await window.setFocus().catch(() => {});
}

/** The tray's "Take notes on what's playing" check and tooltip line. */
export async function setTapping(on: boolean): Promise<void> {
  if (!isTauri()) return;
  const { invoke } = await import("@tauri-apps/api/core");
  await invoke("set_tapping", { on }).catch(() => {});
}

/** A Windows toast for a message from Steve: its text, or only its kind
 *  while Windows is locked (ui/src-tauri/src/toast.rs). */
export async function notify(kind: string, text: string, signedOut: boolean): Promise<void> {
  if (!isTauri()) return;
  const { invoke } = await import("@tauri-apps/api/core");
  await invoke("notify", { kind, text, signedOut }).catch((err) => console.warn("no toast:", err));
}

/** A toast was clicked: open the inbox. */
export async function onOpenInbox(handler: () => void): Promise<() => void> {
  if (!isTauri()) return () => {};
  const { listen } = await import("@tauri-apps/api/event");
  return await listen("open-inbox", handler);
}

/** The install's files are not the ones shipped, so the backend is refused. */
export async function onBackendDamaged(handler: () => void): Promise<() => void> {
  if (!isTauri()) return () => {};
  const { listen } = await import("@tauri-apps/api/event");
  return await listen("backend-damaged", handler);
}

/** The backend kept stopping, and the shell has stopped starting it. */
export async function onBackendFailing(handler: () => void): Promise<() => void> {
  if (!isTauri()) return () => {};
  const { listen } = await import("@tauri-apps/api/event");
  return await listen("backend-failing", handler);
}

/** The tray menu's "Take notes on what's playing" was clicked. */
export async function onTapToggle(handler: () => void): Promise<() => void> {
  if (!isTauri()) return () => {};
  const { listen } = await import("@tauri-apps/api/event");
  return await listen("tap-toggle", handler);
}

/**
 * A page opened in the user's own browser (ui/src-tauri/src/lib.rs
 * `open_link`), never in the app's window: there it would replace the app,
 * with the app's access to the backend, and the window refuses to go there.
 * Only an https address with a host and no password in it is opened. Every
 * link in the app is a button calling this, not an `<a href>`.
 */
export async function openLink(url: string): Promise<{ ok: boolean; error?: string }> {
  if (!isTauri()) return { ok: false, error: "Links open from the desktop app." };
  const { invoke } = await import("@tauri-apps/api/core");
  try {
    await invoke("open_link", { url });
    return { ok: true };
  } catch (err) {
    return { ok: false, error: String(err) };
  }
}

/**
 * The private sign-in window (ui/src-tauri/src/lib.rs `open_signin`): the
 * site opens there, the user signs in as in any browser, and its cookies are read
 * back for the vault. Every page it passes through arrives as
 * `onNavigated`; the page it settles on as `onPage`; its closing as `onClosed`.
 */
export async function openSignin(url: string): Promise<{ ok: boolean; error?: string }> {
  if (!isTauri()) return { ok: false, error: "The sign-in window is part of the desktop app." };
  const { invoke } = await import("@tauri-apps/api/core");
  try {
    await invoke("open_signin", { url });
    return { ok: true };
  } catch (err) {
    return { ok: false, error: String(err) };
  }
}

/** What the user typed into a sign-in form in the window, by the site the user typed it on. */
export interface Typed {
  host: string;
  username: string;
  password: string;
}

export interface SigninState {
  url: string;
  cookies: Record<string, unknown>[];
  typed: Typed[];
  error?: string;
}

/** Who the user has typed in as so far -- never the password itself. */
export async function signinTyped(): Promise<{ host: string; username: string; has_password: boolean }[]> {
  if (!isTauri()) return [];
  const { invoke } = await import("@tauri-apps/api/core");
  return invoke<{ host: string; username: string; has_password: boolean }[]>("signin_typed").catch(() => []);
}

export async function signinCookies(): Promise<SigninState> {
  // In a browser, as every other call here: invoke was no function there.
  if (!isTauri()) {
    return { url: "", cookies: [], typed: [], error: "The sign-in window is part of the desktop app." };
  }
  const { invoke } = await import("@tauri-apps/api/core");
  return invoke<SigninState>("signin_cookies");
}

export async function closeSignin(): Promise<void> {
  if (!isTauri()) return;
  const { invoke } = await import("@tauri-apps/api/core");
  await invoke("close_signin").catch(() => {});
}

export async function onSignin(handlers: {
  onNavigated: (url: string) => void;
  onPage: (url: string) => void;
  onFinished: (state: SigninState) => void;
  onClosed: () => void;
}): Promise<() => void> {
  if (!isTauri()) return () => {};
  const { listen } = await import("@tauri-apps/api/event");
  const offs = await Promise.all([
    listen<string>("signin-navigated", (e) => handlers.onNavigated(e.payload)),
    listen<string>("signin-page", (e) => handlers.onPage(e.payload)),
    listen<SigninState>("signin-finished", (e) => handlers.onFinished(e.payload)),
    listen("signin-closed", () => handlers.onClosed()),
  ]);
  return () => offs.forEach((off) => off());
}

export async function startsAtLogin(): Promise<boolean | null> {
  if (!isTauri()) return null;
  const { isEnabled } = await import("@tauri-apps/plugin-autostart");
  return isEnabled().catch(() => null);
}

export async function setStartsAtLogin(on: boolean): Promise<void> {
  if (!isTauri()) return;
  const autostart = await import("@tauri-apps/plugin-autostart");
  await (on ? autostart.enable() : autostart.disable());
}

/** A release the update check found (ui/src-tauri/src/update.rs `Offer`). */
export interface UpdateOffer {
  version: string;
  notes: string | null;
  /** This version again, asked for to put back damaged files. */
  repair: boolean;
}

/**
 * Ask for a newer release, or with `repair` for this one too. The release's
 * signature is checked when it is installed, not here.
 */
export async function checkUpdate(repair = false): Promise<{ offer: UpdateOffer | null; error?: string }> {
  if (!isTauri()) return { offer: null, error: "Updates come through the desktop app." };
  const { invoke } = await import("@tauri-apps/api/core");
  try {
    return { offer: await invoke<UpdateOffer | null>("update_check", { repair }) };
  } catch (err) {
    return { offer: null, error: String(err) };
  }
}

/**
 * Download the release the last check found and run its installer. The app
 * stops its backend and exits on the way, so this returns only on a failure.
 */
export async function installUpdate(): Promise<{ ok: boolean; error?: string }> {
  if (!isTauri()) return { ok: false, error: "Updates come through the desktop app." };
  const { invoke } = await import("@tauri-apps/api/core");
  try {
    await invoke("update_install");
    return { ok: true };
  } catch (err) {
    return { ok: false, error: String(err) };
  }
}

/** Which screen corner the desk card sits nearest (src/deskcard.rs). */
export type Corner = "top-left" | "top-right" | "bottom-left" | "bottom-right";

async function deskcardCall<T = null>(cmd: string, args: Record<string, unknown> = {}):
  Promise<T | null> {
  if (!isTauri()) return null;
  const { invoke } = await import("@tauri-apps/api/core");
  return await invoke<T>(cmd, args).catch((err) => {
    console.warn(`${cmd}:`, err);
    return null;
  });
}

/**
 * The desk card's own commands (ui/src/DeskCard.tsx). Its window is moved,
 * sized, shown and hidden by the shell (src/deskcard.rs), which knows the
 * screens and whether Windows is locked or a full-screen app is in front.
 */
export const deskcard = {
  /** Setup is done and the setting is on; on top of other windows or not. */
  want: (on: boolean, onTop: boolean) => deskcardCall("deskcard_want", { on, onTop }),
  /** The page's size, padding included; says which corner it sits in. */
  size: (width: number, height: number) =>
    deskcardCall<Corner>("deskcard_size", { width, height }),
  /** A drag: "start", "move" while the button is down, "end" on release. */
  drag: (phase: "start" | "move" | "end") => deskcardCall<Corner>("deskcard_drag", { phase }),
  /** Hidden until then (milliseconds since 1970). */
  hide: (until: number) => deskcardCall("deskcard_hide", { until }),
  /** The journal at today, in the main window. */
  open: () => deskcardCall("deskcard_open"),
  /** The quick bar, where a note is answered. */
  quickbar: () => deskcardCall("deskcard_quickbar"),
  /** Push-to-talk, as the hotkey's hold. */
  talk: (on: boolean) => deskcardCall("deskcard_talk", { on }),
  /** The right-click menu, drawn by Windows. */
  menu: () => deskcardCall("deskcard_menu"),
};

/** Whether the desk card is on the screen now: updates and results make no
 *  toast while it is (presence.md, Windows toasts). */
export async function deskCardShowing(): Promise<boolean> {
  return (await deskcardCall<boolean>("deskcard_showing")) === true;
}

/** An event from the shell or another window, by name: the desk card's
 *  "deskcard-hide", "deskcard-show", "deskcard-reply", "app-settings"; the
 *  main window's "open-view". */
export async function onShell<T>(event: string, handler: (payload: T) => void):
  Promise<() => void> {
  if (!isTauri()) return () => {};
  const { listen } = await import("@tauri-apps/api/event");
  return await listen<T>(event, ({ payload }) => handler(payload));
}

/** Tells every window, the sender's too, something changed: "app-settings"
 *  after a setting is saved, with the label of the window that saved it,
 *  so the desk card follows Settings at once and Settings the card's tray
 *  item; "deskcard-reply" for a reply that reached a closed quick bar. */
export async function tellWindows(event: string, payload: unknown = null): Promise<void> {
  if (!isTauri()) return;
  const { emit } = await import("@tauri-apps/api/event");
  await emit(event, payload).catch(() => {});
}
