/**
 * Their saved logins (agent/browser/live.py): the list, forgetting one, and
 * saving one from the desktop app's sign-in window. Never a secret back:
 * the list says only whether a password and a session are kept.
 */

import { postJSON, request } from "./api";

export interface Login {
  name: string;
  domains: string[];
  tier: "ask" | "free";
  has_password: boolean;
  has_session: boolean;
}

export interface Saved {
  ok: boolean;
  error?: string;
  site?: Login;
  cookies?: number;
  left_behind?: number;
}

async function post<T>(path: string, body: unknown): Promise<T> {
  try {
    const response = await postJSON(path, body);
    return (await response.json()) as T;
  } catch (err) {
    return { ok: false, error: String(err) } as T;
  }
}

export const logins = {
  list: async (): Promise<Login[]> => {
    const response = await request("/browser/logins");
    return ((await response.json()) as { logins: Login[] }).logins ?? [];
  },
  forget: (name: string) => post<{ ok: boolean }>("/browser/logins/forget", { name }),
  setTier: (name: string, tier: "ask" | "free") =>
    post<Saved>("/browser/logins/tier", { name, tier }),
  save: (form: {
    name: string;
    domains: string;
    tier: string;
    url: string;
    cookies: Record<string, unknown>[];
    username: string;
    password: string;
    credentials_from?: string;
  }) => post<Saved>("/browser/logins/import", form),
};

/** A picture of the sandbox browser, and the page it is on. */
export interface Frame {
  ok: boolean;
  idle?: boolean;
  jpeg?: string;
  url?: string;
  error?: string;
}

type Act =
  | { kind: "click"; x: number; y: number }
  | { kind: "type"; text: string }
  | { kind: "key"; key: string }
  | { kind: "scroll"; dy: number };

/** The sandbox browser's live view (ui/src/LiveView.tsx): its picture, the
 *  user's clicks and keys to it, and saving the login it holds. */
export const live = {
  open: (url: string) => post<{ ok: boolean; error?: string }>("/browser/live/open", { url }),
  frame: async (): Promise<Frame | null> => {
    try {
      return (await (await request("/browser/live/frame")).json()) as Frame;
    } catch {
      return null;
    }
  },
  act: (action: Act) => post<{ ok: boolean; error?: string }>("/browser/live/act", action),
  save: (form: { name: string; domains: string; tier: string; username: string;
                 password: string }) => post<Saved>("/browser/live/save", form),
  stop: () => post<{ ok: boolean }>("/browser/live/stop", {}),
  outbox: (name: string, b64: string) =>
    post<{ ok: boolean; error?: string; file?: string }>("/browser/live/outbox", { name, b64 }),
};

/** The host a page is on, without "www.". */
export function hostOf(url: string): string {
  try {
    return new URL(url).hostname.toLowerCase().replace(/^www\./, "");
  } catch {
    return "";
  }
}

/** Whether a login's domains reach this host: itself or a parent of it. */
export function covers(domains: string[], host: string): boolean {
  return domains.some((d) => host === d || host.endsWith(`.${d}`));
}

/**
 * Of what the user typed in the sign-in window, what to keep for this login: only
 * what the user typed on one of its own domains, the entry with a password first.
 * Typed on a look-alike page, it is not their login's, whatever it says.
 */
export function pickTyped(
  typed: { host: string; username: string; password: string }[],
  domains: string[],
): { host: string; username: string; password: string } | null {
  const own = typed.filter((t) => covers(domains, t.host) && (t.username || t.password));
  return own.find((t) => t.password && t.username) ?? own.find((t) => t.password) ?? null;
}

/** A first guess at a name: portal.contoso.example gives "contoso". */
export function suggestName(host: string): string {
  const labels = host.split(".");
  return (labels.length >= 2 ? labels[labels.length - 2] : host).replace(/[^a-z0-9-]/g, "");
}

/** Whether a page still looks like signing in rather than signed in. */
export function looksLikeSignIn(url: string): boolean {
  let path = "";
  try {
    path = new URL(url).pathname;
  } catch {
    return true;
  }
  return /(^|[./])(cas|sso|login|signin|auth|idp|duo|okta|shibboleth)([./-]|$)/i.test(
    hostOf(url) + path,
  );
}
