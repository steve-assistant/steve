/**
 * The control panel's client: the worker's `/control/` endpoints
 * (agent/worker/control.py), through the supervisor like everything else.
 */

import { postAnswer, postJSON, request, type ModelInfo, type YesProof } from "./api";

export interface Overview {
  worker: { ok: boolean; version: string; pid: number; duties: string; uptime_s: number };
  signal: { state?: string; [key: string]: unknown };
  sandbox: { available: boolean; reason?: string };
  waiting: number;
  spend_today: number;
  routines: Counts;
  goals: Counts;
  questions: number;
  quiet_hours: QuietHours;
  issues: { id: number; kind: string; summary: string; lately: number; last_seen: string }[];
  mailbox?: { address: string; enabled: boolean;
              today: { instruction: number; thread: number; ignored: number } };
  /** Since when the backend has been awake, and the gap before, as SQLite's
   *  UTC text (agent/awake.py). */
  awake?: { since: string | null;
            slept: { from: string; until: string; how: "asleep" | "stopped" } | null };
}

export interface Counts {
  active: number;
  paused: number;
  awaiting_approval: number;
}

export interface QuietHours {
  from: string;
  until: string;
  now: boolean;
}

export interface RoutineRow {
  id: number;
  title: string;
  status: string;
  when: string;
  next_run: string | null;
  last_run: string | null;
  last_report: string | null;
  held: string | null;
  runs: number;
  urgent: boolean;
  may_without_asking: string;
  instruction: string;
}

export interface GoalStep {
  n: number;
  text: string;
  status: string;
  note: string;
}

export interface GoalQuestion {
  n: number;
  text: string;
  asked_at: string;
  told: boolean;
  answer: string | null;
}

export interface GoalRow {
  id: number;
  title: string;
  status: string;
  progress: string;
  next_check: string | null;
  open_questions: { n: number; text: string }[];
  spent: string;
  deadline: string | null;
  may_without_asking: string;
  ask: string;
  done_when: string;
  steps: GoalStep[];
  findings: string[];
  questions: GoalQuestion[];
  user_notes: { at: string; text: string }[];
  waiting_on: string[];
  sessions: number;
  outcome: string | null;
  last_session: string | null;
  last_report: string | null;
  every_hours: number;
}

export interface Knob {
  name: string;
  says: string;
  group: "features" | "attention" | "approvals" | "spend" | "budgets" | "language" | "signin";
  kind: "int" | "usd" | "minutes" | "clock" | "choice" | "language" | "region";
  lo: number | string;
  hi: number | string;
  /** A choice's values, each with what the panel calls it. */
  choices?: [string, string][];
  value: number | string;
  default: number | string;
  /** A switch's: what it adds, and what it takes to set up (agent/features.py, `OFFERS`). */
  adds?: string;
  takes?: string;
}

/** The local readers (agent/worker/readers.py): which are here, and an
 *  install's progress. */
export interface Readers {
  piguard: boolean;
  tev1: boolean;
  installing: boolean;
  error: string | null;
  adds: string;
  takes: string;
}

export interface Autonomy {
  routines: RoutineRow[];
  goals: GoalRow[];
  quiet_hours: QuietHours;
  knobs: Knob[];
}

export interface ActivityItem {
  at: string | null;
  kind: string;
  title: string;
  detail: string;
  status: string;
  id: number;
  channel?: string | null;
  receipt?: string | null;
  error?: string | null;
  by?: string | null;
  /** The action critic's call, for an unattended session's action (core/critic.py). */
  critic?: { verdict: string | null; decision: string; mode: string } | null;
}

/** One cap or day budget against what it holds; `cap` is its knob, or null
 *  for one that is not a knob (the model trials' month). */
export interface SpendLimit {
  cap: string | null;
  name: string;
  spent: number;
  limit: number;
}

export interface Spend {
  today: number;
  week: number;
  month: number;
  by_category: { today: Record<string, number>; week: Record<string, number>; month: Record<string, number> };
  /** The month's cap first, since it stops everything; then each kind's. */
  caps: SpendLimit[];
  budgets: SpendLimit[];
  /** This period's caps reached, or nearly (agent/caps.py). */
  notices: { kind: "warned" | "stopped"; cap: string; text: string }[];
  goals: { id: number; title: string; spent: number; budget: number }[];
}

export interface ModelsView {
  lineup: Record<string, ModelInfo>;
  switches: {
    in_force: Record<string, string | null>;
    watching: Record<string, unknown> | null;
    history: {
      role: string;
      from_slug: string;
      to_slug: string;
      kind: string;
      by: string;
      why: string;
      outcome: string | null;
      at: string;
    }[];
  };
  trials: {
    id: number;
    slug: string;
    role: string;
    incumbent: string;
    decision: { switch?: boolean; why?: string; [key: string]: unknown } | null;
    spent_usd: number;
    report: string | null;
    error: string | null;
    started_at: string | null;
    finished_at: string | null;
  }[];
}

export interface Done {
  ok: boolean;
  error?: string;
  [key: string]: unknown;
}

async function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await request(path, { signal });
  if (!response.ok) throw new Error(`${path} -> HTTP ${response.status}`);
  return (await response.json()) as T;
}

async function post<T extends Done>(path: string, body: unknown = {}): Promise<T> {
  try {
    const response = await postJSON(path, body);
    return (await response.json()) as T;
  } catch (err) {
    return { ok: false, error: String(err) } as T;
  }
}

/** An answer to a module's, a server's or a build's card, with the sign-in's session. */
async function answer<T extends Done>(body: Record<string, unknown>): Promise<T> {
  try {
    return await postAnswer<T>("/control/modules", body);
  } catch (err) {
    return { ok: false, error: String(err) } as T;
  }
}

async function put<T extends Done>(path: string, body: unknown): Promise<T> {
  try {
    const response = await request(path, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    return (await response.json()) as T;
  } catch (err) {
    return { ok: false, error: String(err) } as T;
  }
}

// One page of the journal (agent/worker/journal.py). Every moment is ISO UTC.
interface EntryBase {
  /** Unique on the page, and the same in every answer: `since` replaces by it. */
  key: string;
  at: string | null;
  changed: string | null;
  /** Where it happened, when not here: "signal", "voice", "telegram". */
  channel: string | null;
  /** Changed after the user last looked. */
  new: boolean;
}

export type Verdict = "right" | "not right";

export type DayEntry = EntryBase & (
  | { kind: "said"; id: number; session_id: number; text: string;
      /** How many photos came with it (agent/worker/app_photos.py). */
      photos?: number }
  | { kind: "reply"; id: number; session_id: number; text: string; reaction: Verdict | null }
  | { kind: "spoken"; session_id: number; first_words: string | null; summary: string | null;
      minutes: number; ended: string;
      lines: { role: "user" | "assistant"; text: string; at: string }[] }
  | { kind: "briefing" | "update"; id: number; of: string; text: string;
      which: "morning" | "evening" | null; sent: string | null; stopped: string | null }
  | { kind: "action"; id: number; status: string; asked: boolean; by: string | null;
      what: string; line: string; error: string | null; decided: string | null;
      executed: string | null; undo_until: string | null }
  | { kind: "task"; id: number; of: string; title: string; detail: string | null;
      status: string; finished: string | null;
      /** A night's, folded: its summary, its minutes and the parts it opens to. */
      summary?: string; minutes?: number; parts?: { title: string; items: string[] }[] }
  | { kind: "listening"; id: number; of: string; title: string | null; text: string | null;
      doc_url: string | null; status: string; ended: string | null }
  | { kind: "asleep"; id: number; gap: string; from: string | null; to: string | null }
);

export interface Day {
  ok: boolean;
  /** YYYY-MM-DD: the page, which turns at `turns_at` in the user's zone. */
  date: string;
  today: string;
  turns_at: string;
  /** Ask again with `since` this for only what changed. */
  as_of: string;
  looked: string | null;
  entries: DayEntry[];
}

/** What a search found: the entries holding every word, by page, newest
    page first, each with the line it was found in. */
export interface Found {
  ok: boolean;
  /** The words as they were asked, to drop an answer to older ones. */
  q: string;
  today: string;
  days: { date: string; hits: { key: string; kind: string; at: string; line: string }[];
          more: number }[];
}

/** A page before the open ones, folded to a line. */
export interface FoldedDay {
  date: string;
  line: string;
  count: number;
}

/** A note: something that needs the user (agent/worker/needs.py). Not a
 *  listening note, which is an entry on the day (`DayEntry`). */
export interface Need {
  kind: "yes" | "code" | "now" | "stopped";
  /** Its kind and what it is about: "card:12", "reminder:40", "cap:spend:day". */
  id: string;
  text: string;
  /** The detail that matters, never a body. */
  detail: string | null;
  /** A card's word, for answering from the phone. */
  word: string | null;
  at: string | null;
  /** "yes", "no", "done", "later", "not_now", or the one fix of a stopped note. */
  actions: string[];
  action_id?: number;
  expires?: string | null;
  /** A grouped card's items, each one's first line, numbered from 1 for a
   *  yes to some of them (`only`). Absent on a card of one. */
  items?: string[];
  /** A card's hash as read: a yes sends it, and is refused if the card
   *  has changed since (`approvals.decide`). */
  seen?: string;
}

/** What's next (journal.py `next_up`): the next timed event and the next
 *  reminder within three hours. A reminder's `at` is local wall time. */
export interface Next {
  ok: boolean;
  event: { summary: string | null; start: string; end: string; location: string | null } | null;
  reminder: { id: number; text: string; at: string } | null;
}

/** The sidebar's places and what each holds (agent/worker/places.py). */
export interface Places {
  ok: boolean;
  coming: number;
  extensions: { installed: number; building: number; can_build: boolean };
  calls: number;
}

/** Coming up: the reminders still to go off, soonest first, and the week's
 *  events. Each `at` is a moment, UTC with a Z. */
export interface Coming {
  ok: boolean;
  reminders: { id: number; text: string; at: string }[];
  /** Done on today's page, by when they went off. */
  done: { id: number; text: string; at: string }[];
  events: { summary: string | null; start: string; end: string; all_day: boolean;
            location: string | null }[];
}

/** A note taken on a call or in a room, with the page that opens at it. */
export interface CallNote {
  id: number;
  kind: "call" | "lecture";
  title: string | null;
  status: string;
  at: string;
  date: string;
  key: string;
}

/** "Later"'s choices (needs.py `LATER`). */
export type LaterWhen = "15m" | "1h" | "evening" | "morning";

export interface KnownFact {
  id: number;
  /** "You", or whom it is about by name. */
  about: string;
  attribute: string;
  value: string;
  source: string;
  until: string | null;
}

export interface KnownMemory {
  path: string;
  kind: string;
  summary: string;
  at: string | null;
  until: string | null;
}

export interface Known {
  ok: boolean;
  facts: KnownFact[];
  memories: KnownMemory[];
}

/** Where a home city was placed (agent/home.py `placed`). */
export interface Placed {
  status: "placed" | "none" | "unknown" | "unreachable";
  place?: string;
  says: string;
}

export const control = {
  overview: (signal?: AbortSignal) => get<Overview>("/control/overview", signal),
  autonomy: (signal?: AbortSignal) => get<Autonomy>("/control/autonomy", signal),
  readers: (signal?: AbortSignal) => get<Readers>("/control/readers", signal),
  installReaders: () => post<Done>("/control/readers/install"),
  activity: (hours: number, signal?: AbortSignal) =>
    get<{ hours: number; items: ActivityItem[] }>(`/control/activity?hours=${hours}`, signal),
  spend: (signal?: AbortSignal) => get<Spend>("/control/spend", signal),
  models: (signal?: AbortSignal) => get<ModelsView>("/control/models", signal),

  routine: (id: number, op: "pause" | "resume" | "cancel") =>
    post<Done>("/control/routine", { id, op }),
  goal: (
    id: number,
    op: "pause" | "resume" | "drop" | "answer",
    extra: { text?: string; question?: number; add_budget_usd?: number } = {},
  ) => post<Done>("/control/goal", { id, op, ...extra }),
  stop: (what: "routines" | "goals") => post<Done & { paused?: number }>("/control/stop", { what }),
  knobs: (changes: Record<string, number | string | null>) =>
    post<Done & { knobs?: Knob[] }>("/control/knobs", { changes }),
  revertModels: () =>
    post<Done & { reverted?: string[] }>("/control/models/revert"),
  /** A page of the journal, today's by default; with `since`, only what changed. */
  day: (date?: string | null, since?: string | null, signal?: AbortSignal) => {
    const query = new URLSearchParams();
    if (date) query.set("date", date);
    if (since) query.set("since", since);
    const asked = query.toString();
    return get<Day>(`/control/day${asked ? `?${asked}` : ""}`, signal);
  },
  /** The `n` pages before `before`, folded. */
  days: (before: string, n: number, signal?: AbortSignal) =>
    get<{ ok: boolean; days: FoldedDay[] }>(`/control/days?before=${before}&n=${n}`, signal),
  /** The entries holding every word of `q`. */
  search: (q: string, signal?: AbortSignal) =>
    get<Found>(`/control/search?q=${encodeURIComponent(q)}`, signal),
  /** The user has seen the page up to `until` (now by default). */
  looked: (until?: string) => post<Done & { looked?: string }>("/control/looked",
                                                              until ? { until } : {}),
  /** A check or a cross on a reply, or null to take it off. */
  react: (messageId: number, verdict: Verdict | null) =>
    post<Done>("/control/react", { message_id: messageId, verdict }),
  needs: (signal?: AbortSignal) => get<{ needs: Need[] }>("/control/needs", signal),
  needDone: (id: string) => post<Done>("/control/needs/done", { id }),
  needLater: (id: string, when: LaterWhen) =>
    post<Done & { at?: string }>("/control/needs/later", { id, when }),
  needNotNow: (id: string) => post<Done>("/control/needs/not-now", { id }),
  next: (signal?: AbortSignal) => get<Next>("/control/next", signal),
  places: (signal?: AbortSignal) => get<Places>("/control/places", signal),
  coming: (signal?: AbortSignal) => get<Coming>("/control/coming", signal),
  calls: (signal?: AbortSignal) => get<{ ok: boolean; calls: CallNote[] }>("/control/calls", signal),
  deleteNote: (id: number) => post<Done>("/control/note", { id, op: "delete" }),
  /** Who the user is: the facts that lead every turn (agent/facts.py `IDENTITY`). */
  profile: (signal?: AbortSignal) =>
    get<{ profile: Record<string, string> }>("/control/profile", signal),
  /** Change some of them, "" to clear one. A home city changed is placed at once. */
  setProfile: (changes: Record<string, string>) =>
    put<Done & { profile?: Record<string, string>; home?: Placed }>("/control/profile",
                                                                    { profile: changes }),
  /** What Steve knows (agent/worker/knows.py): facts held today, and memories. */
  knows: (signal?: AbortSignal) => get<Known>("/control/knows", signal),
  correctFact: (id: number, value: string) =>
    post<Done & { id?: number; value?: string }>("/control/knows/correct", { id, value }),
  forgetFact: (id: number) => post<Done>("/control/knows/forget", { id }),
  forgetMemory: (path: string) => post<Done>("/control/knows/forget", { path }),

  // Modules and accounts (agent/worker/accounts.py): one listing for both
  // pages, and one POST for each, its `op` naming the change.
  modules: (signal?: AbortSignal) => get<ModulesListing>("/control/modules", signal),

  // The Modules page's changes. Approving, allowing a build and importing
  // are the app's alone: the routes answer only the token the app holds,
  // and an answer to a card only with the sign-in's session too.
  switchModule: (id: string, on: boolean) =>
    post<Done>("/control/modules", { op: "switch", id, on }),
  /** `server` only for an MCP server's card: the secret typed, and the tools freed. */
  /** The proof is the passphrase or Hello's signature, where the card needs one.
   *  `on` turns the module on once approved; `on: false` and `why` back say
   *  the yes was taken but it would not start. */
  approveModule: (admission: number, proof: YesProof,
                  server?: { secrets: Record<string, string>; free: string[] }, on = false) =>
    answer<Done & { id?: string; on?: boolean; why?: string }>({
      op: "approve", admission, ...proof, ...server, ...(on ? { on } : {}) }),
  declineModule: (admission: number) => answer<Done>({ op: "decline", admission }),
  /** Lists the server's tools through the broker, up to its card: seconds, or minutes for a package. */
  addServer: (server: AddServer) =>
    post<Done & { admission?: number }>("/control/modules", { op: "add_server", ...server }),
  /** `zip` is the file's bytes in base64; `replace` deletes the installed module's secrets and data first. */
  importModule: (zip: string, replace: boolean) =>
    post<Done & { admission?: number }>("/control/modules", { op: "import", zip, replace }),
  /** The running version's folder as a zip, in base64, and the file's name. */
  exportModule: (id: string) =>
    post<Done & { name?: string; zip?: string }>("/control/modules", { op: "export", id }),
  /** `data_from`: when the copy of its data that came back was taken. */
  rollback: (id: string, version: string) =>
    post<Done & { data_from?: string }>("/control/modules", { op: "rollback", id, version }),
  removeModule: (id: string, forget: boolean) =>
    post<Done>("/control/modules", { op: "remove", id, forget }),
  /** "Allow this build": past the day's cap, by at most what one build may spend. */
  allowBuild: (build: number) => answer<Done>({ op: "allow", build }),
  dismissBuild: (build: number) => answer<Done>({ op: "dismiss", build }),
  dismissReport: (report: number) => answer<Done>({ op: "dismiss", report }),

  pick: (address: string) =>
    post<Done & Partial<Choice> & { question?: string; yes?: Choice; no?: Choice }>(
      "/control/accounts", { op: "pick", address }),
  addAccount: (add: AddAccount) =>
    post<Done & { key?: string; next?: "browser_sign_in" | "server_sign_in"; test?: Tested;
                  fix?: string; held?: { class: string; address: string; name: string } }>(
      "/control/accounts", { op: "add", ...add }),
  /** Opens the user's own browser and waits for the sign-in there: minutes, perhaps. */
  signIn: (key: string) =>
    post<Done & { test?: Tested; fix?: string }>("/control/accounts", { op: "sign_in", key }),
  /**
   * The server Steve runs for the account, installed if it is not and
   * started, asked to sign in: the page and the code it shows, which the
   * user enters there in their own browser. Only the profile's page, and a
   * code of its pattern (agent/accounts.py `server_sign_in`).
   */
  serverSignIn: (key: string) =>
    post<Done & { page?: string; code?: string; expires_s?: number }>(
      "/control/accounts", { op: "server_sign_in", key }),
  testAccount: (key: string) =>
    post<Done & { test?: Tested; fix?: string }>("/control/accounts", { op: "test", key }),
  /** `fields` holds only new secrets (a new password); a `test` follows it. */
  editAccount: (key: string, change: { name?: string; settings?: AccountSettings;
                                       enabled?: boolean; fields?: Record<string, string> }) =>
    post<Done & { fix?: string }>("/control/accounts", { op: "edit", key, ...change }),
  removeAccount: (key: string, forget: boolean) =>
    post<Done>("/control/accounts", { op: "remove", key, forget }),

  // Steve's own address: a plus-address of one of the user's mail accounts.
  mailbox: (signal?: AbortSignal) => get<Mailbox>("/control/mailbox", signal),
  chooseMailbox: (key: string, tag: string) =>
    post<Done & { address?: string }>("/control/mailbox", { op: "choose", key, tag }),
  stopMailbox: () => post<Done>("/control/mailbox", { op: "stop" }),
  userAddress: (address: string, on: boolean, ownDomain: boolean) =>
    post<Done>("/control/mailbox", { op: "address", address, on, own_domain: ownDomain }),
  lookNow: () => post<Done & { done?: string[] }>("/control/mailbox", { op: "look" }),
};

/** What a module asks for to add an account: its manifest's `[setup]`. */
export interface SetupField {
  name: string;
  type: "text" | "address" | "host" | "secret" | "link" | "file" | "choice" | "flag";
  label: string;
  optional: boolean;
  choices: string[];
  /** Servers and their words (IMAP, CalDAV): shown only under Advanced. */
  advanced: boolean;
}

export interface Preset {
  id: string;
  label: string;
  domains: string[];
  classes: string[];
  /** Where its secrets are revoked: an https page, or "" for the module's. */
  revoke: string;
}

/** Where a module came from: shipped with Steve, built by Steve on request, or a zip or server the user added. */
export type ModuleOrigin = "builtin" | "built" | "imported";

/** A module from outside's kept version (core/lifecycle.py `versions`), newest first. */
export interface ModuleVersion {
  version: string;
  approved_at: string | null;
  /** When the copy of its data that rolling back to it restores was taken; null for the one it runs. */
  data_from: string | null;
  current: boolean;
}

/** A module's watch since it was turned on or changed (core/canary.py `Counts`). */
export interface Watch {
  turns: number;
  bad: number;
  by_kind: Record<string, number>;
}

/** An MCP server module's server and its pinned tools (agent/mcptools.py `view`). */
export interface ModuleServer {
  /** "" for a server Steve runs, which `package` names instead. */
  url: string;
  loopback?: boolean;
  package?: string;
  tools: { name: string; read: boolean; offered: boolean; free: boolean }[];
  error?: string;
}

/** A module: a kind of account Steve can add, with its setup in order, and its place on the Modules page. */
export interface Module {
  id: string;
  label: string;
  for: string;
  adds: string;
  effort: string;
  tier: string;
  origin: ModuleOrigin;
  version: string;
  /** Empty for a built-in, which its release fixes. */
  versions: ModuleVersion[];
  /** Null for a built-in, and for one not watched yet. */
  watch: Watch | null;
  enabled: boolean;
  classes: string[];
  domains: string[];
  /** Whether this version of Steve can build every part of it. */
  available: boolean;
  server: ModuleServer | null;
  /** What it may reach and do (agent/accounts.py `_reach`): the slots it fills, */
  slots: string[];
  /** the hosts it may reach, and whether also one the user gives it, */
  hosts: string[];
  typed: boolean;
  /** and core's reading of its grants, in its card's words. */
  grants: string[];
  setup: {
    steps: string[];
    fields: SetupField[];
    presets: Preset[];
    /** `link` is an https page, or "". */
    guide: { text: string; link: string }[];
    /** Where a secret given here is revoked: an https page, or "". */
    revoke: string;
  };
}

/** A tool an MCP server listed, as its card shows it, in the server's own words (screened). */
export interface CardTool {
  name: string;
  title: string;
  description: string;
  /** Labelled by the server as a read. */
  read: boolean;
  /** Whether Steve can offer it under a model's tool name. */
  offered: boolean;
  /** A next version's: freed now, as it was when freed (agent/accounts.py `_shown_card`). */
  free?: boolean;
}

/** An MCP server module's server, on its card (core/lifecycle.py `_server_card`). */
export interface CardServer {
  url: string;
  loopback: boolean;
  /** The name of the secret it takes, or "" for none. */
  secret: string;
  /** A next version's secret goes on as kept, to where it went before: not asked again. */
  secret_kept?: boolean;
  // A package Steve runs: its name@version, its installed tree's hash, how
  // many packages it brought, and whether it may reach any public host.
  package?: string;
  tree?: string;
  packages?: number;
  anywhere?: boolean;
  holds_its_sign_in?: boolean;
  tools: CardTool[];
}

/** The second model's reading of the code: advice, never trust. */
export interface Review {
  approved: boolean;
  reason: string;
  model: string;
  /** False: the code was longer than the reviewer reads. */
  whole: boolean;
}

/**
 * What a new version changes (core/lifecycle.py `more`), with each of its
 * words for the model that are new or changed, as they were and are
 * (`reworded`). A card made before that has no `words`.
 */
export interface VersionChanges {
  more: string[];
  fewer: string[];
  words?: { where: string; was: string; now: string }[];
}

/** A card waiting for the user's yes (core/lifecycle.py `record`), its keys in the card's order. */
export interface Admission {
  admission: number;
  made_at: string;
  id: string;
  version: string;
  origin: "built" | "imported";
  /** "version": Steve's next version of its own; "replace": the installed one goes first. */
  kind: "new" | "version" | "replace";
  /** Core's reading of its grants, never the module's own. */
  summary: string[];
  passphrase: boolean;
  /** A module Steve built: what it was asked, in the words it was asked, and where. */
  request: string | null;
  came_from: string | null;
  /** The user's own last message where they asked: `request` is the model's words.
   * Absent on a card kept before it was recorded. */
  user_said?: string | null;
  /** Each grant the request's words do not name. */
  marks: { what: "host" | "class" | "reads"; value: string }[] | null;
  own_words: {
    label: string;
    for: string;
    adds: string;
    effort: string;
    prompt: string;
    skills: { title: string; when: string; steps: string[] }[];
    attention: { looks_at: string; judge: string; every_min: number } | null;
    routines: { title: string; instruction: string; at: string; days: string[] }[];
  };
  /** A new version: what it grants now that it did not, and no longer. */
  changes: VersionChanges | null;
  server: CardServer | null;
  details: {
    manifest: string;
    hash: string;
    hosts: string[];
    /** It also reaches a host or a link the user gives it. */
    typed: boolean;
    files: string[];
    review: Review | null;
    tests: string | null;
  };
}

export type BuildState =
  | "needs_allow" | "running" | "stopped" | "failed" | "cannot" | "carded" | "dismissed";

/** A module Steve builds on request (agent/modbuild.py `builds`), newest first. */
export interface Build {
  id: number;
  request: string;
  origin: "user" | "night" | "agent";
  came_from: string;
  /** A new version's module; "" for a new one. */
  module: string;
  state: BuildState;
  estimate_usd: number;
  estimate_s: number;
  /** Spent so far, over every run of it. */
  cost_usd: number;
  seconds: number;
  /** Why it ended or stopped as it did. */
  said: string;
  admission: number | null;
  created_at: string;
  finished_at: string | null;
}

/** A request that could not be a module: a slot Steve lacks, or a problem in Steve itself. Redacted. */
export interface Report {
  id: number;
  build_id: number;
  kind: "slot" | "core";
  text: string;
  created_at: string;
  request: string;
}

export interface ModulesListing {
  modules: Module[];
  accounts: Account[];
  admissions: Admission[];
  builds: Build[];
  reports: Report[];
}

/** An MCP server the user adds: one on the web (or on this computer) by its address, or a package Steve runs. */
export type AddServer =
  | { name: string; url: string; secret: string; label: string }
  | { name: string; package: string; entry: string; hosts: string[]; anywhere: boolean;
      label: string };

export type AccountState = "working" | "needs_you" | "cant_reach" | "cant_work_here" | "off";

export interface AccountSettings {
  watched?: boolean;
  every_min?: number;
  default_calendar?: boolean;
}

export interface Account {
  key: string;
  name: string;
  module: string;
  preset: string | null;
  enabled: boolean;
  classes: string[];
  addresses: string[];
  settings: AccountSettings;
  /** Null: never tested. */
  state: AccountState | null;
  /** The one sentence, made by the backend: shown as it is. */
  health: string | null;
  /** An https page that fixes it, or "". */
  fix: string;
  /** The state's one action. */
  action: "sign_in" | "password" | "test" | "link" | "turn_on" | "";
  /** Approval cards waiting that act on this account: cancelled if it goes off. */
  waiting: number;
  /** The sign-in "sign in again" means: the browser's, the server's Steve runs, or none. */
  sign_in: "browser_sign_in" | "server_sign_in" | null;
}

/** An account's test: one sentence per part it read, and its health. */
export interface Tested {
  ok: boolean;
  found: string[];
  health: string;
  state: AccountState | null;
  detail: string;
  fix: string;
}

export interface Choice {
  module: string;
  preset: string | null;
}

export interface AddAccount {
  module: string;
  address: string;
  classes?: string[];
  fields: Record<string, string | boolean>;
  preset?: string;
  name?: string;
  /** Move the class from the account that holds it for this address. */
  take?: boolean;
}

export interface Mailbox {
  /** "" while off. */
  address: string;
  account: string;
  tag: string;
  accounts: { key: string; name: string; address: string; plus_addresses: boolean }[];
  /** The addresses that count as the user's. */
  addresses: { address: string; opted_in: boolean; own_domain: boolean; account: boolean }[];
  recent: {
    id: number;
    sender: string;
    subject: string;
    kind: "instruction" | "thread" | "ignored";
    why: string;
    note: string;
    created_at: string;
  }[];
}

/** A moment as the user would say it: "14:05", "yesterday 21:30", "Mon 3 Oct 09:00". */
export function when(iso: string | null | undefined): string {
  if (!iso) return "—";
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return iso;
  const now = new Date();
  const time = at.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const days = Math.round(
    (new Date(now.toDateString()).getTime() - new Date(at.toDateString()).getTime()) / 86400000,
  );
  if (days === 0) return time;
  if (days === 1) return `yesterday ${time}`;
  if (days === -1) return `tomorrow ${time}`;
  return `${at.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" })} ${time}`;
}
