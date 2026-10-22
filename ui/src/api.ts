/**
 * Client for Steve's backend.
 *
 * Everything goes through the supervisor's port, never directly to a worker:
 * the worker's port changes on every swap, and the whole point of the
 * supervisor is that the UI does not have to care.
 *
 * The supervisor's port is chosen at each start, and every request must carry
 * the token it made then (core/localapi.py). The desktop shell reads both from
 * the run file and hands them over only once the backend has proved it holds
 * the token (`backend` in ui/src-tauri/src/lib.rs). Every request here goes
 * through `request`, which adds the token; a socket carries it in a
 * subprotocol, since a browser's socket sends no headers.
 */

import { heardLevel, type Voice } from "./level";

export interface Backend {
  url: string;
  token: string;
}

let current: Promise<Backend> | null = null;

/** The backend's address and token, once it has proved itself. `fresh`
 *  has the shell prove it again now, rather than trust the last proof. */
export function backend(fresh = false): Promise<Backend> {
  if (fresh) current = null;
  if (!current) {
    const asked = (async () => {
      if (!isTauri()) {
        // Vite's "preview" mode (`python -m devtools.preview`) passes the
        // backend's paths on to a backend on the test fakes and adds its token
        // itself. The app is built in another mode, so this is not in it.
        if (import.meta.env.MODE === "preview") return { url: "", token: "preview" };
        throw new Error("Only the desktop app can reach Steve's backend: it holds the token.");
      }
      const { invoke } = await import("@tauri-apps/api/core");
      return await invoke<Backend>("backend");
    })();
    // A failure is not kept: the backend may still be starting.
    asked.catch(() => {
      if (current === asked) current = null;
    });
    current = asked;
  }
  return current;
}

/** Ask the shell again next time: the backend restarted, on a new port with a new token. */
export function forgetBackend(): void {
  current = null;
}

/** A request to the backend, with its token. Tried once more, with the
 *  backend asked for again, after a 401: it may have restarted on a new
 *  port with a new token, and a 401 is refused before anything is done.
 *
 *  A failed connection is tried again only for a read. A fetch that fails
 *  cannot say whether the request arrived: the backend may have approved,
 *  sent or written before the connection dropped, and a change sent again
 *  blind would be made twice. It is the user's to send again; the backend
 *  is asked for afresh, so their next try finds it if it moved.
 *
 *  Whatever the app sends goes to a backend proven just before. The last
 *  proof was trusted until a request failed, and a program that took the
 *  port after a restart and answered every request fails none: it was
 *  sent the passphrase, a key or a bot token (step 11's audit). A read
 *  may use the last proof: the token it carries dies with the backend
 *  that made it, and the socket's close drops the proof (`AgentSocket`). */
export async function request(path: string, init: RequestInit = {}): Promise<Response> {
  const reads = ["GET", "HEAD"].includes((init.method ?? "GET").toUpperCase());
  for (let attempt = 0; ; attempt += 1) {
    const { url, token } = await backend(!reads);
    try {
      const response = await fetch(`${url}${path}`, {
        ...init,
        headers: { ...(init.headers as Record<string, string> | undefined),
                   Authorization: `Bearer ${token}` },
      });
      if (response.status !== 401 || attempt > 0) return response;
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") throw err;
      if (attempt > 0 || !reads) {
        forgetBackend();
        throw err;
      }
    }
    forgetBackend();
  }
}

/** POST a JSON body. */
export function postJSON(path: string, body: unknown): Promise<Response> {
  return request(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export type Role = "triage" | "converse" | "reason" | "escalate" | "bulk";

export interface Reply {
  session_id: number;
  text: string;
  role: Role;
  escalated_from: Role | null;
  escalation_reason: string | null;
  deferred_jobs: number[];
  tools_used: string[];
  cost_usd: number;
  latency_ms: number;
  cached_tokens: number;
}

export interface Job {
  id: number;
  kind: string;
  prompt: string;
  why: string | null;
  status: "queued" | "running" | "done" | "failed" | "cancelled";
  attempts: number;
  result: string | null;
  error: string | null;
}

export interface ModelInfo {
  role: string;
  slug: string;
  label: string;
  effort: string | null;
  service_tier: string;
  context_window: number;
  usd_per_mtok: { input: number; cached_input: number; output: number };
  note: string;
}

export interface Status {
  version: string;
  pid: number;
  models: Record<string, ModelInfo>;
  sandbox: { available: boolean; reason?: string; server_version?: string };
  queue: { queued: number; running: number; done: number; failed: number };
  memories: number;
  cost: { total_usd: number; today_usd: number; calls: number };
  active_sessions: number;
}

/** Why a yes was refused, when the app can do something about it. */
export type DecideNeeds = "passphrase" | "new_passphrase" | "signin";

/** What a high-risk yes carries: the passphrase, or Hello's signature over
 *  the backend's challenge for that card. */
export type YesProof = { passphrase?: string; hello?: { nonce: string; signature: string } };

export interface Decided {
  ok: boolean;
  error?: string;
  undone?: boolean;
  needs?: DecideNeeds;
}

export interface ApprovalsSetup {
  passphrase: boolean;
  authenticator: boolean;
  hello: boolean;
  codes_paused_until?: number | null;
}

async function getJSON<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await request(path, { signal });
  if (!response.ok) {
    throw new Error(`${path} -> HTTP ${response.status}`);
  }
  return (await response.json()) as T;
}

export const api = {
  status: (signal?: AbortSignal) => getJSON<Status>("/status", signal),
  jobs: (signal?: AbortSignal) => getJSON<{ jobs: Job[] }>("/jobs", signal),
  /** Say yes or no to something the agent wants to do, with the sign-in's
   *  session. A yes to spending carries the passphrase or Windows Hello's
   *  signature; the backend checks them, never the app. A session the
   *  backend no longer holds is ended in every window, which then asks for
   *  a sign-in. `only` is a yes to some items of a grouped card, by their
   *  numbers from 1; `seen` is the card's hash as its note read it, and a
   *  yes to a card that has changed since is refused. */
  decideAction: (action_id: number, approve: boolean, proof: YesProof = {},
                 only?: number[], seen?: string) =>
    postAnswer<Decided>("/actions/decide", {
      action_id, approve, ...proof, ...(only ? { only } : {}), ...(seen ? { seen } : {}),
    }),

  /** Which second factors are set, and, after five wrong codes, until when
   *  (seconds since the epoch) a code by message or voice is not taken. */
  approvalsSetup: (signal?: AbortSignal) =>
    getJSON<ApprovalsSetup>("/approvals/setup", signal),

  /** Choose the passphrase, or change it with the old one. */
  choosePassphrase: async (newWords: string, old?: string) => {
    const response = await postJSON("/approvals/passphrase", { new: newWords, old });
    return (await response.json()) as { ok: boolean; error?: string };
  },

  /** A new authenticator secret, as a QR code and as text, kept only once
   *  a code from it is confirmed. Both steps carry the sign-in's session,
   *  as linking a phone does (`postAnswer`). */
  startAuthenticator: (passphrase: string) =>
    postAnswer<{ ok: boolean; error?: string; qr?: string; key?: string }>(
      "/approvals/authenticator", { passphrase }),

  confirmAuthenticator: (code: string) =>
    postAnswer<{ ok: boolean; error?: string }>("/approvals/authenticator/confirm", { code }),

  signalStatus: (signal?: AbortSignal) =>
    getJSON<{ bridge: SignalState }>("/signal/status", signal),

  /** Download and check signal-cli and its Java, in the background. */
  signalInstall: async () => {
    const response = await postJSON("/signal/install", {});
    return (await response.json()) as { bridge?: SignalState; error?: string };
  },

  /** A code for the phone to scan, as SVG; asked again, a new one. */
  signalLink: () =>
    postAnswer<{ qr?: string; wait_s?: number; error?: string }>("/signal/link", {}),

  telegramStatus: (signal?: AbortSignal) =>
    getJSON<{ bridge: TelegramState }>("/telegram/status", signal),

  /** The user's bot, by BotFather's token: checked with Telegram, then kept. */
  telegramToken: (token: string) =>
    postAnswer<{ bridge?: TelegramState; bot?: string; error?: string }>(
      "/telegram/token", { token }),

  /** Six digits to send the bot; the chat that sends them is paired. */
  telegramPair: () =>
    postAnswer<{ code?: string; bot?: string; wait_s?: number; error?: string }>(
      "/telegram/pair", {}),

  telegramUnpair: async () => {
    const response = await postJSON("/telegram/unpair", {});
    return (await response.json()) as { bridge: TelegramState; goodbye: string };
  },

  /** Stop Signal here and delete its keys; `goodbye` says what is left to do. */
  signalUnlink: async () => {
    const response = await postJSON("/signal/unlink", {});
    return (await response.json()) as { bridge: SignalState; goodbye: string };
  },

  voice: (signal?: AbortSignal) =>
    getJSON<{ warm: VoiceWarm; listening: boolean; speaking: boolean; hands_free?: DeskStatus }>(
      "/voice/status",
      signal,
    ),

  /** Take notes on what the laptop plays -- a call, a video -- or stop
   *  and write them up. Needs no microphone. */
  setTap: async (on: boolean) => {
    const response = await postJSON("/voice/tap", { on });
    return (await response.json()) as { ok: boolean; error?: string; tapping?: boolean };
  },

};

/** What the desk is doing besides push-to-talk (agent/worker/desk.status):
 *  notes being taken of what the laptop plays, and the note open. The
 *  worker reports hands-free's switch and counts there too, and the app
 *  shows none of them: hands-free is not offered in v0.1
 *  (docs/plan/07-voice.md, item 15). */
export interface DeskStatus {
  /** Notes being taken of what the laptop plays (voice/listen.Tap). */
  tap?: {
    on: boolean;
    ok: boolean;
    error: string | null;
    /** The voice add-on the notes need is installed (desk.add_on). */
    add_on?: boolean;
  };
  note?: { id: number; kind: string; since: string } | null;
}

/**
 * The one hotkey (docs/design/presence.md, The hotkey): tapped, it opens the
 * quick bar; held for HOLD_MS, it is push-to-talk until it is let go. It
 * replaced two keys, Alt+End to talk and Alt+` for the quick bar, which were
 * also the defaults of the edition this code was copied from, which may run
 * beside it as the same Windows user: whichever started first held both,
 * and the other's keys did nothing (tests/gate/test_identity.py keeps them
 * apart now).
 *
 * Fn cannot be used, on any binding. Fn is resolved inside the keyboard's own
 * controller and produces no scancode, so Windows never sees it and no
 * application can bind it -- a combination containing Fn registers
 * successfully and then never fires.
 */
export const HOLD_MS = 250;

/** Tried in order while the user has chosen none: the first that Windows
 *  lets this app have is the hotkey (docs/plan/spikes/s9-app.md, 2). */
export const HOTKEY_FALLBACKS = ["Super+Alt+S", "Super+Alt+J", "CommandOrControl+Alt+S"] as const;
export const DEFAULT_HOTKEY = HOTKEY_FALLBACKS[0];

export const HOTKEY_CHOICES = [
  ...HOTKEY_FALLBACKS,
  "Super+Alt+Q",
  "Alt+Insert",
  "CommandOrControl+Shift+Space",
] as const;

export function isTauri(): boolean {
  return "__TAURI_INTERNALS__" in window;
}

/** The sign-in's session the shell holds (src-tauri/src/signin.rs), or null
 *  while signed out: every answer to a card carries it (ui/src/session.ts). */
export async function sessionNow(): Promise<string | null> {
  if (!isTauri()) return null;
  const { invoke } = await import("@tauri-apps/api/core");
  return (await invoke<string | null>("session_get").catch(() => null)) ?? null;
}

/** Posts an answer to a card (an action's, a module's or a server's, a
 *  build's), or a step that makes a new approver (linking a phone, giving
 *  Telegram a bot, setting up the authenticator), with the sign-in's
 *  session, which the backend asks of each (agent/worker/signin.py
 *  `refused`). A session the backend no longer holds is ended in every
 *  window, which then asks for a sign-in. */
export async function postAnswer<T>(path: string, body: Record<string, unknown>): Promise<T> {
  const session = await sessionNow();
  const response = await postJSON(path, { ...body, ...(session ? { session } : {}) });
  const answered = (await response.json()) as T;
  if ((answered as { needs?: unknown }).needs === "signin" && isTauri()) {
    const { invoke } = await import("@tauri-apps/api/core");
    await invoke("session_end").catch(() => {});
  }
  return answered;
}

/**
 * Register a hotkey's press and release, returning a disposer.
 *
 * In a plain browser there is no such thing as a global shortcut, so this is
 * a no-op and the on-screen mic button remains the way in -- which keeps
 * `npm run dev` usable without the desktop shell.
 */
export async function bindPushToTalk(
  accelerator: string,
  onDown: () => void,
  onUp: () => void,
): Promise<() => void> {
  if (!isTauri()) return () => {};
  try {
    const { register, unregister } = await import(
      "@tauri-apps/plugin-global-shortcut"
    );
    // Re-registering the same accelerator throws, so clear it first; this
    // happens every time the binding is changed at runtime.
    try {
      await unregister(accelerator);
    } catch {
      /* was not registered */
    }
    await register(accelerator, (event) => {
      // Press and release are distinct, which is what makes this
      // push-to-talk rather than a toggle you can leave on by accident.
      if (event.state === "Pressed") onDown();
      else onUp();
    });
    noteHotkey(accelerator, null);
    return () => {
      void unregister(accelerator).catch(() => {});
    };
  } catch (err) {
    // Said, not just logged: until 5 October 2026 the app's permissions let
    // it register no hotkey at all, and push-to-talk failed here, silently,
    // from the day the desktop app was installed.
    noteHotkey(accelerator, String(err));
    return () => {};
  }
}

/**
 * The one hotkey bound: `onTap` for a press let go within HOLD_MS; `onHold`
 * once it has been held that long, then `onLetGo` when it is let go. `ok`
 * is whether Windows let this app have it.
 */
export async function bindTapOrHold(
  accelerator: string,
  { onTap, onHold, onLetGo }: { onTap: () => void; onHold: () => void; onLetGo: () => void },
): Promise<{ off: () => void; ok: boolean }> {
  let timer: number | undefined;
  let down = false;
  let held = false;
  const off = await bindPushToTalk(accelerator, () => {
    down = true;
    held = false;
    window.clearTimeout(timer);
    timer = window.setTimeout(() => {
      held = true;
      onHold();
    }, HOLD_MS);
  }, () => {
    // A release with no press before it (the key held while it was bound)
    // is no tap.
    if (!down) return;
    down = false;
    window.clearTimeout(timer);
    if (held) onLetGo();
    else onTap();
  });
  return { off: () => { window.clearTimeout(timer); off(); }, ok: hotkeyProblem(accelerator) === null };
}

const hotkeyProblems = new Map<string, string>();

function noteHotkey(accelerator: string, problem: string | null): void {
  if (problem) hotkeyProblems.set(accelerator, problem);
  else hotkeyProblems.delete(accelerator);
  window.dispatchEvent(new CustomEvent("hotkeys-changed"));
}

/** Why a hotkey could not be registered -- usually another app holds it --
 *  or null if it is working. */
export function hotkeyProblem(accelerator: string): string | null {
  return hotkeyProblems.get(accelerator) ?? null;
}

/** A hotkey as the user would write it. */
export function hotkeyLabel(accelerator: string): string {
  return accelerator.replace("CommandOrControl", "Ctrl").replace("Super", "Win")
    .replace("Backquote", "`");
}

export interface VoiceTurn {
  heard: string;
  reply: string;
  timing: {
    captured_s: number;
    live_ms: number | null;
    stt_ms: number;
    agent_ms: number;
    first_token_ms: number;
    first_audio_ms: number;
    total_ms: number;
    spoken_s: number;
  };
  interrupted: boolean;
  error: string | null;
  role?: string;
  cost_usd?: number;
  tools_used?: string[];
}

export interface VoiceWarm {
  state: "cold" | "warming" | "ready" | "failed";
  stt?: { device?: string; model?: string };
  tts?: { device?: string; available?: boolean };
  error?: string;
  warm_s?: number;
}

/** A message Steve sent the user unprompted: a row of the outbox
 *  (agent/outbox.py). Its moments are SQLite's UTC text (`fromSqlite`). */
/** Signal on this computer (agent/worker/pairing.py): `cold` before it is
 *  tried, `unavailable` (not set up, or its files changed), `installing`,
 *  `unlinked`, `linking`, `starting`, `ready`, or `failed`. */
export interface SignalState {
  state: string;
  hint?: string;
  error?: string;
  /** While installing: bytes downloaded, of how many. */
  done?: number;
  of?: number;
  /** While linking: how long the code works, in seconds. */
  wait_s?: number;
}

export interface TelegramState {
  state: string;
  error?: string;
  /** The bot's username, once Steve has one. */
  bot?: string | null;
  /** While a pairing code waits: how long it works, in seconds. */
  wait_s?: number;
}

export interface InboxMessage {
  id: number;
  kind: "reminder" | "briefing" | "card" | "notice" | "job" | "reply";
  text: string;
  action_id: number | null;
  /** The remote channel it goes to as well, or null for the app alone. */
  remote: string | null;
  created_at: string;
  seen_at: string | null;
  sent_at: string | null;
  attempts: number;
  next_try_at: string | null;
  /** Why it stopped trying the remote channel without sending it. */
  stopped: string | null;
}

/** SQLite's UTC text ("2026-10-13 18:04:00") as an ISO moment a `Date`
 *  reads as UTC: read as it is, it would be taken for local time. */
export function fromSqlite(stamp: string | null): string | null {
  return stamp ? `${stamp.replace(" ", "T")}Z` : null;
}

export type ServerEvent =
  | { type: "hello"; version: string; pid: number }
  | { type: "inbox"; message: InboxMessage }
  | { type: "pong"; pid: number }
  | { type: "thinking"; session_id: number }
  | ({ type: "reply" } & Reply)
  | { type: "job_finished"; job: Job }
  | { type: "listening" }
  | { type: "voice_heard"; heard: string }
  | ({ type: "voice_turn" } & VoiceTurn)
  | ({ type: "voice_empty"; reason: string } & Partial<VoiceTurn>)
  | { type: "voice_unavailable"; warm: VoiceWarm }
  // What Steve is doing (agent/worker/journal.py): `of` says for what, a
  // turn or "job:<id>"; a null `text` takes the line down.
  | { type: "doing"; text: string | null; of: string }
  // The voice's level while it listens or speaks, from 0 to 1 (level.ts).
  | { type: "level"; of: Voice; level: number }
  | { type: "error"; error: string };

type Handlers = {
  onEvent: (event: ServerEvent) => void;
  onOpen?: () => void;
  onClose?: () => void;
};

/**
 * A websocket that reconnects.
 *
 * Reconnection is not optional here: every swap retires the worker
 * this socket is pinned to, so the connection dropping is a routine event
 * rather than an error. Backoff is capped low because the gap is normally
 * under a second.
 */
export class AgentSocket {
  private socket: WebSocket | null = null;
  private closed = false;
  private attempt = 0;
  private timer: number | null = null;
  private handlers: Handlers;

  constructor(handlers: Handlers) {
    this.handlers = handlers;
  }

  connect(): void {
    if (this.closed) return;
    backend().then(
      (found) => this.open(found),
      () => this.retry(),
    );
  }

  private open({ url, token }: Backend): void {
    if (this.closed) return;
    // The token rides in a subprotocol; the server answers with "steve".
    const socket = new WebSocket(`${url.replace(/^http/, "ws")}/ws`, ["steve", `token.${token}`]);
    this.socket = socket;

    socket.onopen = () => {
      this.attempt = 0;
      this.handlers.onOpen?.();
    };

    socket.onmessage = (event) => {
      try {
        const told = JSON.parse(event.data) as ServerEvent;
        // Eight a second: to the level's own store, not a window's state.
        if (told.type === "level") heardLevel(told);
        else this.handlers.onEvent(told);
      } catch {
        // A frame we cannot parse is not worth tearing the connection down.
      }
    };

    socket.onclose = () => {
      // A worker gone mid-sentence tells no quiet after it.
      heardLevel();
      // The backend may have restarted, on a new port and token, and
      // another program may take the old port: a read must not go on to it
      // on the last proof. A socket that did open closes when its backend
      // stops, so this is the first word of a restart.
      forgetBackend();
      this.handlers.onClose?.();
      this.retry();
    };

    socket.onerror = () => socket.close();
  }

  private retry(): void {
    if (this.closed) return;
    const delay = Math.min(250 * 2 ** this.attempt, 4000);
    this.attempt += 1;
    this.timer = window.setTimeout(() => this.connect(), delay);
  }

  send(payload: unknown): boolean {
    if (this.socket?.readyState !== WebSocket.OPEN) return false;
    this.socket.send(JSON.stringify(payload));
    return true;
  }

  dispose(): void {
    this.closed = true;
    if (this.timer) window.clearTimeout(this.timer);
    this.socket?.close();
  }
}

export function formatUSD(value: number): string {
  if (value === 0) return "$0";
  if (value < 0.01) return `$${value.toFixed(5)}`;
  return `$${value.toFixed(2)}`;
}

export function formatDuration(ms: number): string {
  return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`;
}
