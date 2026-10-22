/**
 * The app's sign-in, as its pages see it. The backend says whether one is
 * needed and checks it (agent/worker/signin.py); the shell holds the session
 * for every window and ends it on lock, sleep, idle and quit
 * (src-tauri/src/signin.rs); Windows Hello signs (src-tauri/src/hello.rs).
 *
 * Not the browser's logins (`signinTyped` and the rest in desktop.ts),
 * which are sites' sign-ins, not Steve's.
 */

import { isTauri, postJSON, request, sessionNow } from "./api";

export { sessionNow };

export interface SigninState {
  /** A passphrase is set, so the app signs in before it shows anything. */
  required: boolean;
  /** A Hello key is registered. */
  hello: boolean;
  /** The name of this install's Hello key, for the shell. */
  hello_name: string;
  /** One was set, and its file is gone or changed: it is locked until
   *  reset (core/passphrase.state). */
  gone?: boolean;
}

/** Why a session ended, as the shell says (signin.rs's `Why`). */
export type Ended = "locked" | "slept" | "idle" | "lapsed" | "ended";

type Said = { ok: boolean; error?: string; session?: string };

async function send(path: string, body: unknown): Promise<Said> {
  try {
    return (await (await postJSON(path, body)).json()) as Said;
  } catch {
    return { ok: false, error: "Steve didn't answer. Check it is running, then try again." };
  }
}

async function shell<T>(cmd: string, args: Record<string, unknown> = {}): Promise<T> {
  const { invoke } = await import("@tauri-apps/api/core");
  return await invoke<T>(cmd, args);
}

/** The backend's last real answer, for every window, kept for when it
 *  can't say: in this webview's storage, since the backend is what is
 *  missing then. Lost with a reinstall, when there is nothing to hide. */
const KNOWN = "signin.last-known";

/** Whether a sign-in is needed; null where the backend can't say. At the
 *  app's first start it then opens as before: a sign-in that can't be read
 *  must not lock anyone out of their own data (the backend still refuses
 *  an answer without it). Once known, a null changes nothing (`signedOut`,
 *  App.tsx's `onSession`). */
export async function signinState(): Promise<SigninState | null> {
  try {
    const response = await request("/signin/state");
    if (!response.ok) return null;
    const state = (await response.json()) as SigninState;
    localStorage.setItem(KNOWN, state.required ? "required" : "not");
    return state;
  } catch {
    return null;
  }
}

/** Whether this window must stay closed until the main window signs in:
 *  a passphrase is set and the shell holds no session. The quick bar and
 *  the desk card ask it; they never sign in themselves. Where the backend
 *  can't say, its last answer stands: read as "no sign-in needed", a
 *  session ended while it restarted opened the desk card on what it held. */
export async function signedOut(): Promise<boolean> {
  const [state, held] = await Promise.all([signinState(), sessionNow()]);
  const required = state ? state.required : localStorage.getItem(KNOWN) === "required";
  return required && !held;
}

/** Every change of the session, in every window. */
export async function onSession(handler: (live: boolean, why?: Ended) => void):
  Promise<() => void> {
  if (!isTauri()) return () => {};
  const { listen } = await import("@tauri-apps/api/event");
  return await listen<{ live: boolean; why?: Ended }>(
    "session", ({ payload }) => handler(payload.live, payload.why));
}

/** Sign out, or drop a session the backend refused. */
export async function endSession(): Promise<void> {
  if (isTauri()) await shell("session_end").catch(() => {});
}

async function keep(said: Said): Promise<Said> {
  if (said.ok && said.session && isTauri()) await shell("session_keep", { secret: said.session });
  return said;
}

export async function signInWithPassphrase(passphrase: string): Promise<Said> {
  return await keep(await send("/signin/passphrase", { passphrase }));
}

/** Whether this laptop has Windows Hello for its user. */
export async function helloAvailable(): Promise<boolean> {
  if (!isTauri()) return false;
  return await shell<boolean>("hello_available").catch(() => false);
}

/** The backend's challenge, signed by Hello: what an answer carries. */
export async function helloSigned(name: string,
                                  what: { for: "signin" | "reset" }
                                    | { for: "yes"; action_id: number }
                                    | { for: "yes"; admission: number }):
  Promise<{ nonce: string; signature: string } | { error: string }> {
  const asked = (await send("/signin/challenge", what)) as Said & { nonce?: string; message?: string };
  if (!asked.ok || !asked.nonce || !asked.message) return { error: asked.error ?? "Steve didn't answer." };
  try {
    const signature = await shell<string>("hello_sign", { name, message: asked.message });
    return { nonce: asked.nonce, signature };
  } catch (err) {
    return { error: String(err) };
  }
}

export async function signInWithHello(name: string): Promise<Said> {
  const signed = await helloSigned(name, { for: "signin" });
  if ("error" in signed) return { ok: false, error: signed.error };
  return await keep(await send("/signin/hello", signed));
}

/** Make this install's Hello key and register it, with the passphrase. */
export async function setUpHello(name: string, passphrase: string): Promise<Said> {
  let publicKey: string;
  try {
    publicKey = await shell<string>("hello_enrol", { name });
  } catch (err) {
    return { ok: false, error: String(err) };
  }
  return await send("/approvals/hello", { public_key: publicKey, passphrase });
}

/** A forgotten passphrase replaced, with a Hello glance in place of the old
 *  one (core/hello.reset_passphrase). */
export async function resetPassphrase(name: string, words: string): Promise<Said> {
  const signed = await helloSigned(name, { for: "reset" });
  if ("error" in signed) return { ok: false, error: signed.error };
  return await send("/approvals/passphrase/reset", { new: words, ...signed });
}

/** A forgotten passphrase replaced on this Windows account's password,
 *  typed into Windows' own dialog, which the backend shows
 *  (core/winsignin.py). */
export async function resetPassphraseByWindows(words: string): Promise<Said> {
  return await send("/approvals/passphrase/reset/windows", { new: words });
}

export async function forgetHello(passphrase: string): Promise<Said> {
  return await send("/approvals/hello/forget", { passphrase });
}

/** Said on the sign-in page, for why the last session ended. */
export const ENDED: Record<Ended, string> = {
  locked: "Signed out when Windows locked.",
  slept: "Signed out while the laptop slept.",
  idle: "Signed out after 30 minutes away.",
  lapsed: "Signed out: Steve restarted.",
  ended: "Signed out.",
};
