/**
 * Setup: one question a page, from a fresh install to a working Steve
 * (docs/plan/09-app-and-setup.md, section 2; its look is
 * docs/design/back-pages.md, Onboarding).
 *
 * A stranger had no way in. The key went into the Credential Manager from
 * a terminal, the cap was found by trial, and the app added itself to
 * Windows' start-up without asking. Now each is a page, and what each page
 * has done is the backend's to say (`/setup/state`, agent/worker/setup.py):
 * the key page is done when a key is kept, the cap's when a cap is set. So
 * setup closed halfway resumes at the first page not done, and nothing
 * here can count a page as done that isn't.
 */

import { useEffect, useState, type ChangeEvent, type ReactNode } from "react";

import { AddAccountHere } from "./Accounts";
import { api, isTauri, postJSON, request } from "./api";
import { HelloSetup, PassphraseForm } from "./Approvers";
import { control } from "./control";
import { openLink, setStartsAtLogin, startsAtLogin } from "./desktop";
import { Mark } from "./Mark";
import { Prose } from "./Prose";
import { helloAvailable } from "./session";
import { SignalPanel } from "./Signal";
import { TelegramPanel } from "./Telegram";
import "./setup.css";
import privacy from "../../PRIVACY.md?raw";

export type Page = "hello" | "openai" | "cap" | "jev" | "name" | "passphrase" | "channel"
  | "accounts" | "docker" | "preset" | "login" | "privacy";

export interface SetupState {
  pages: { page: Page; done: boolean; required: boolean }[];
  /** Where setup resumes; null once it is done. */
  next: Page | null;
  done: boolean;
  answered: Partial<Record<Page, string>>;
  /** An OpenAI key in the Credential Manager, perhaps from an earlier install. */
  key_kept: boolean;
  preset: string | null;
  /** The monthly cap kept, in dollars; null until one is. */
  cap: number | null;
}

interface Said {
  ok: boolean;
  error?: string;
  why?: string;
  says?: string;
  credit?: number;
}

/** Where setup stands. Throws when the backend can't say. */
export async function loadSetup(): Promise<SetupState> {
  const response = await request("/setup/state");
  if (!response.ok) throw new Error(`/setup/state -> HTTP ${response.status}`);
  return (await response.json()) as SetupState;
}

/** A change, and the backend's answer; never throws. A change is not sent
 *  again on its own (`request`): the user presses Continue again. */
async function send<T extends object = object>(path: string, body: unknown): Promise<Said & Partial<T>> {
  try {
    return (await (await postJSON(path, body)).json()) as Said & Partial<T>;
  } catch {
    return { ok: false, error: "Steve didn't answer. Check it is running, then try again." } as Said & Partial<T>;
  }
}

const CAP_SUGGESTED = 15;
const CAP_BOUNDS = [5, 200] as const;

export function Setup({ initial, connected, onStart, onDone }: {
  initial: SetupState;
  connected: boolean;
  onStart: () => void;
  onDone: () => void;
}) {
  const [state, setState] = useState(initial);
  const [at, setAt] = useState<Page>(initial.next ?? "privacy");
  const [starting, setStarting] = useState(false);
  useEffect(() => {
    if (connected) setStarting(false);
  }, [connected]);
  const pages = state.pages;
  const index = pages.findIndex((p) => p.page === at);
  const done = (page: Page) => pages.find((p) => p.page === page)?.done ?? false;

  /** This page's work is kept: on to the next page not done after it, as
   *  the backend counts them. */
  const go = async (now?: Partial<SetupState>) => {
    const fresh = now?.pages ? (now as SetupState) : await loadSetup().catch(() => null);
    if (!fresh) return;
    setState(fresh);
    if (fresh.done) {
      onDone();
      return;
    }
    const after = fresh.pages.slice(index + 1).find((p) => !p.done);
    setAt(after?.page ?? fresh.next ?? "privacy");
  };

  /** A page answered here (hello, a skip, the channel, accounts, start-up). */
  const answer = async (page: Page, given: string): Promise<string | null> => {
    const said = await send<SetupState>("/setup/answer", { page, answer: given });
    if (!said.ok) return said.error ?? "That wasn't kept.";
    await go(said);
    return null;
  };

  const page = (() => {
    switch (at) {
      case "hello": return <Hello answer={answer} />;
      case "openai": return <KeyPage service="openai" kept={done("openai")} go={go}
                                     before={state.key_kept && !done("openai")}
                                     keep={() => answer("openai", "kept")} />;
      case "cap": return <Cap kept={state.cap} go={go} />;
      case "jev": return <KeyPage service="openrouter" kept={done("jev") && !state.answered.jev}
                                  go={go} skip={() => answer("jev", "skipped")} />;
      case "name": return <Name go={go} />;
      case "passphrase": return <Passphrase kept={done("passphrase")} go={go} />;
      case "channel": return <Channel answer={answer} />;
      case "accounts": return <Accounts answer={answer} />;
      case "docker": return <Docker answer={answer} />;
      case "preset": return <Preset now={state.preset} go={go} />;
      case "login": return <Login answer={answer} />;
      case "privacy": return <Privacy answer={answer} missing={
        pages.filter((p) => p.required && !p.done).map((p) => p.page)} goTo={setAt} />;
    }
  })();

  return (
    <div className="setup" data-page={at}>
      <ol className="setup-progress" aria-label="Setup">
        {pages.map((p) => (
          <li key={p.page} aria-current={p.page === at ? "step" : undefined}
              className={p.done ? "done" : undefined} title={TITLES[p.page]}>
            <svg viewBox="0 0 10 10" aria-hidden="true"
                 className={p.page === at ? "turning" : undefined}>
              <path d="M5 1 9 5 5 9 1 5Z" />
            </svg>
          </li>
        ))}
      </ol>
      {!connected && (
        <p className="setup-down" role="status">
          {starting ? "Starting Steve: give it half a minute."
            : "Steve isn't answering, so nothing on this page can be kept yet."}
          {isTauri() && !starting && (
            <button type="button" className="setup-text" onClick={() => { setStarting(true); onStart(); }}>
              Start it
            </button>
          )}
        </p>
      )}
      <div key={at} className="setup-page arrives">{page}</div>
      {index > 0 && (
        <button type="button" className="setup-text setup-back" onClick={() => setAt(pages[index - 1].page)}>
          Back
        </button>
      )}
    </div>
  );
}

/** The window while the backend is first asked whether setup is done: the
 *  mark, starting, on paper. Without it a stranger's first seconds were the
 *  app's pages saying Steve wasn't answering, then setup over them. */
export function Waking() {
  return (
    <div className="setup">
      <div className="setup-page setup-hello">
        <Mark size={120} state="starting" label="Steve, starting" />
        <p className="setup-why">Starting Steve…</p>
      </div>
    </div>
  );
}

const TITLES: Record<Page, string> = {
  hello: "Hello",
  openai: "Your OpenAI key",
  cap: "The monthly cap",
  jev: "Jev's key",
  name: "Your name",
  passphrase: "Your passphrase",
  channel: "Your phone",
  accounts: "Mail and calendar",
  docker: "The browser and running code",
  preset: "How much Steve does unasked",
  login: "Start with Windows",
  privacy: "Before you start",
};

// ---------------------------------------------------------------------------
// The parts every page has
// ---------------------------------------------------------------------------

/** Between one word of a title and the next, and how long each takes. */
export const WORD_MS = 60;
export const WORD_FADE_MS = 240;

/**
 * A title whose words fade in one after another, and the page's text after
 * it. Each page's title arrived with the page at once, and setup read as a
 * form: one question a page is a conversation, and this paces it like one.
 */
export function Words({ text }: { text: string }) {
  const words = text.split(" ");
  return (
    <>
      {words.map((word, i) => (
        <span key={i} className="setup-word" style={{ animationDelay: `${i * WORD_MS}ms` }}>
          {word}{i < words.length - 1 ? " " : ""}
        </span>
      ))}
    </>
  );
}

/** When the text under a title of `title`'s words starts to fade in. */
export function after(title: string): string {
  return `${(title.split(" ").length - 1) * WORD_MS + WORD_FADE_MS}ms`;
}

export function Question({ title, why, children }: { title: string; why?: ReactNode; children?: ReactNode }) {
  return (
    <>
      <h1 className="t-display setup-question"><Words text={title} /></h1>
      <div className="setup-follows" style={{ animationDelay: after(title) }}>
        {why && <div className="setup-why">{why}</div>}
        {children}
      </div>
    </>
  );
}

export function Actions({ onContinue, busy, ready = true, label = "Continue", onSkip, skipLabel }: {
  onContinue: () => void;
  busy?: boolean;
  ready?: boolean;
  label?: string;
  onSkip?: () => void;
  skipLabel?: string;
}) {
  return (
    <div className="setup-actions">
      <button type="button" className="setup-continue" disabled={busy || !ready} onClick={onContinue}>
        {busy ? "One moment…" : label}
      </button>
      {onSkip && (
        <button type="button" className="setup-text" disabled={busy} onClick={onSkip}>
          {skipLabel ?? "Skip for now"}
        </button>
      )}
    </div>
  );
}

export function ErrorLine({ text }: { text: string | null }) {
  return text ? <p className="setup-error" role="alert">{text}</p> : null;
}

export function Field({ label, children, hint }: { label: string; children: ReactNode; hint?: ReactNode }) {
  return (
    <label className="setup-field">
      <span className="t-label">{label}</span>
      {children}
      {hint && <span className="setup-hint">{hint}</span>}
    </label>
  );
}

// ---------------------------------------------------------------------------
// The pages
// ---------------------------------------------------------------------------

type Answer = (page: Page, given: string) => Promise<string | null>;
type Go = (now?: Partial<SetupState>) => Promise<void>;

const HELLO = "I'm Steve. Let's get me working.";

function Hello({ answer }: { answer: Answer }) {
  const [error, setError] = useState<string | null>(null);
  return (
    <div className="setup-hello">
      <div className="setup-mark draws"><Mark size={120} label="Steve" /></div>
      <p className="hand-day setup-hand"><Words text="Hello" /></p>
      <h1 className="t-display setup-question"><Words text={HELLO} /></h1>
      <div className="setup-follows" style={{ animationDelay: after(HELLO) }}>
        <p className="setup-why">
          A few questions, one a page. Only some are needed; the rest can wait, and Steve asks
          again where each is used.
        </p>
        <ErrorLine text={error} />
        <Actions label="Begin" onContinue={async () => setError(await answer("hello", "seen"))} />
      </div>
    </div>
  );
}

const KEY_PAGES = {
  openai: {
    title: "Your OpenAI key",
    why: (
      <>
        <p>
          Steve thinks with OpenAI's models, on your account, and pays for them with this key.
          Make one in OpenAI's dashboard, under API keys.
        </p>
        <p>
          Make it a project key, in a project with its own monthly budget: a second limit,
          beside the one Steve keeps.
        </p>
      </>
    ),
    label: "OpenAI key",
  },
  openrouter: {
    title: "Jev's key",
    why: (
      <>
        <p>
          Jev reads what reaches Steve from outside, mail and web pages, for instructions hidden
          in it, before Steve acts on any of it. It runs on OpenRouter, which is asked to keep
          nothing.
        </p>
        <p>
          Without it, Steve checks on this laptop where it can, and otherwise reads outside text
          only in extracts.
        </p>
      </>
    ),
    label: "OpenRouter key",
  },
} as const;

/** A `.env` near Steve that names keys it takes: its path and the names,
 *  never a value (agent/worker/envfile.py). */
interface EnvFile {
  file: string;
  keys: string[];
  empty: string[];
}

const ENV_LABELS: Record<string, string> = {
  OPENAI_API_KEY: "OpenAI", OPENROUTER_API_KEY: "OpenRouter", BRAVE_API_KEY: "Brave Search",  // pragma: allowlist secret
};

/** The keys a `.env` holds, offered by name: use them, or enter them by
 *  hand. Each one used is checked as a pasted key is, and kept only if its
 *  service accepts it. */
function EnvOffer({ files, onUsed, onByHand }: {
  files: EnvFile[];
  /** What each key's service said, one line each, and whether OpenAI's was kept. */
  onUsed: (lines: string[], openai: Said | undefined) => void;
  onByHand: () => void;
}) {
  const [chosen, setChosen] = useState(files[0].file);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const names = (file: EnvFile) => file.keys.join(", ") || "no key filled in";

  const use = async () => {
    setBusy(true);
    setError(null);
    const said = await send<{ keys: Record<string, Said> }>("/setup/envfile", { file: chosen });
    setBusy(false);
    if (!said.ok || !said.keys) {
      setError(said.error ?? "The keys weren't kept.");
      return;
    }
    onUsed(Object.entries(said.keys).map(([name, kept]) =>
      `${ENV_LABELS[name] ?? name}: ${kept.ok ? kept.says : kept.error}`), said.keys.OPENAI_API_KEY);
  };

  return (
    <div className="setup-envfile">
      {files.length === 1 ? (
        <p>
          A <code>.env</code> file at <code>{files[0].file}</code> holds {names(files[0])}.
          {files[0].empty.length > 0 && ` ${files[0].empty.join(", ")} empty, skipped.`} Use
          these keys, or enter them by hand.
        </p>
      ) : (
        <fieldset>
          <legend>Steve found keys in more than one <code>.env</code> file. Which should it use?</legend>
          {files.map((file) => (
            <label key={file.file} className="setup-envfile-choice">
              <input type="radio" name="envfile" value={file.file} checked={chosen === file.file}
                     onChange={() => setChosen(file.file)} />
              <code>{file.file}</code>: {names(file)}
              {file.empty.length > 0 && ` (${file.empty.join(", ")} empty, skipped)`}
            </label>
          ))}
        </fieldset>
      )}
      <ErrorLine text={error} />
      <Actions label="Use these keys" busy={busy} ready={files.some((f) => f.file === chosen && f.keys.length > 0)}
               onContinue={() => void use()} onSkip={onByHand} skipLabel="Enter keys by hand" />
    </div>
  );
}

function KeyPage({ service, kept, go, skip, before = false, keep }: {
  service: keyof typeof KEY_PAGES;
  kept: boolean;
  go: Go;
  skip?: () => Promise<string | null>;
  /** A key the Credential Manager holds from before this page: kept as it
   *  is, or a new one pasted over it. */
  before?: boolean;
  keep?: () => Promise<string | null>;
}) {
  const words = KEY_PAGES[service];
  const [key, setKey] = useState("");
  const [replacing, setReplacing] = useState(!kept);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [works, setWorks] = useState<string | null>(null);
  // A `.env` near Steve, offered on the first key page while its key
  // isn't kept; null until asked, or once set aside.
  const [files, setFiles] = useState<EnvFile[] | null>(null);
  const [fromFile, setFromFile] = useState<string[]>([]);

  useEffect(() => {
    if (service !== "openai" || kept) return;
    let live = true;
    request("/setup/envfile").then((r) => r.json() as Promise<{ ok: boolean; files?: EnvFile[] }>)
      .then((said) => { if (live && said.ok && said.files?.length) setFiles(said.files); })
      .catch(() => undefined);
    return () => { live = false; };
  }, [service, kept]);

  const used = (lines: string[], openai: Said | undefined) => {
    setFiles(null);
    setFromFile(lines);
    if (openai?.ok) {
      setReplacing(false);
      setWorks(null);
    } else {
      setError(openai?.error ?? null);
    }
  };

  const check = async () => {
    setBusy(true);
    setError(null);
    const said = await send("/setup/key", { service, key });
    setBusy(false);
    if (!said.ok) {
      setError(said.error ?? "The key wasn't kept.");
      return;
    }
    setKey("");
    setReplacing(false);
    // OpenAI says nothing of credit until an answer is asked for, which
    // would spend the user's money; with none, the first answer stops and a
    // note says to top up. "Works." alone promised more than was checked.
    setWorks(typeof said.credit === "number"
      ? `Works. $${said.credit.toFixed(2)} of credit left.`
      : "Works. If the account has no credit, a note says so at the first answer.");
  };

  const keepIt = async () => {
    if (!keep) return;
    setBusy(true);
    setError(await keep());
    setBusy(false);
  };

  if (files) {
    return (
      <Question title={words.title} why={words.why}>
        <EnvOffer files={files} onUsed={used} onByHand={() => setFiles(null)} />
      </Question>
    );
  }

  return (
    <Question title={words.title} why={words.why}>
      {fromFile.length > 0 && (
        <ul className="setup-envfile-kept" role="status">
          {fromFile.map((line) => <li key={line}>{line}</li>)}
        </ul>
      )}
      {before && !works && fromFile.length === 0 && (
        <p className="setup-kept-before">
          A key is already kept for Steve in Windows' Credential Manager. Keep it, or paste a
          new one.
        </p>
      )}
      {replacing ? (
        <Field label={words.label}>
          <input type="password" autoComplete="off" spellCheck={false} value={key} autoFocus
                 onChange={(e) => setKey(e.target.value)}
                 onKeyDown={(e) => e.key === "Enter" && key.trim() && !busy && void check()} />
        </Field>
      ) : (
        <div className="setup-field setup-kept">
          <span className="t-label">{words.label}</span>
          <span className="setup-dots" aria-label="kept">••••••••••••••••</span>
          <button type="button" className="setup-text" onClick={() => { setReplacing(true); setWorks(null); }}>
            Replace
          </button>
        </div>
      )}
      {works && <p className="setup-works" role="status">{works}</p>}
      <ErrorLine text={error} />
      {replacing && before && !key.trim()
        ? <Actions label="Keep this key" busy={busy} onContinue={() => void keepIt()} />
        : replacing
          ? <Actions label="Check the key" busy={busy} ready={key.trim() !== ""} onContinue={check}
                     onSkip={skip && (async () => setError(await skip()))} />
          : <Actions onContinue={() => void go()} />}
    </Question>
  );
}

function Cap({ kept, go }: { kept: number | null; go: Go }) {
  const [dollars, setDollars] = useState(kept ?? CAP_SUGGESTED);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lo, hi] = CAP_BOUNDS;
  const step = (by: number) => setDollars((d) => Math.min(hi, Math.max(lo, d + by)));

  const keep = async () => {
    setBusy(true);
    const said = await control.knobs({ MONTH_USD: dollars });
    setBusy(false);
    if (!said.ok) setError(said.error ?? "The cap wasn't kept.");
    else await go();
  };

  return (
    <Question title="How much may Steve spend a month?" why={
      <p>
        Everything Steve asks of the models counts: your conversations, what it notices on its
        own, and the work it does overnight. At the cap it stops spending until the month turns.
        Your project key's budget is a second limit, at OpenAI.
      </p>}>
      <div className="setup-field setup-number">
        <span className="t-label">Each month, in dollars</span>
        <div>
          <button type="button" aria-label="Less" onClick={() => step(-5)} disabled={dollars <= lo}>−</button>
          <input type="number" min={lo} max={hi} value={dollars} aria-label="Dollars a month"
                 onChange={(e) => setDollars(Number(e.target.value))} />
          <button type="button" aria-label="More" onClick={() => step(5)} disabled={dollars >= hi}>+</button>
        </div>
        <span className="setup-hint">between {lo} and {hi}; {CAP_SUGGESTED} suits most</span>
      </div>
      <ErrorLine text={error} />
      <Actions busy={busy} ready={dollars >= lo && dollars <= hi} onContinue={keep} />
    </Question>
  );
}

const NO_PROFILE = { name: "", pronouns: "", home_city: "", occupation: "" };

function Name({ go }: { go: Go }) {
  const [kept, setKept] = useState(NO_PROFILE);
  const [form, setForm] = useState(NO_PROFILE);
  const [placed, setPlaced] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    void control.profile().then((now) => {
      const known = { ...NO_PROFILE, ...Object.fromEntries(
        Object.entries(now?.profile ?? {}).filter(([k]) => k in NO_PROFILE)) };
      setKept(known);
      // Into the fields still empty: put over the form whole, it wiped a
      // name typed before the profile came (CI's slow backend, 22 October
      // 2026), and Continue then waited on a name the user had given.
      setForm((typed) => Object.fromEntries(Object.entries(typed).map(
        ([k, v]) => [k, v || known[k as keyof typeof known]])) as typeof NO_PROFILE);
    }).catch(() => undefined);
  }, []);

  const set = (key: keyof typeof NO_PROFILE) => (e: ChangeEvent<HTMLInputElement>) =>
    setForm((f) => ({ ...f, [key]: e.target.value }));

  const keep = async () => {
    // Only what changed: a home city sent again is placed again, a
    // request to a map's service for nothing new.
    const changes = Object.fromEntries(
      Object.entries(form).filter(([k, v]) => v.trim() !== kept[k as keyof typeof kept]));
    if (Object.keys(changes).length > 0) {
      setBusy(true);
      setError(null);
      const said = await control.setProfile(changes);
      setBusy(false);
      if (!said.ok) {
        setError(said.error ?? "That wasn't kept.");
        return;
      }
      setKept({ ...kept, ...changes });
      // Where home was placed, said before going on: the weather and the
      // day's times are read from it.
      if (said.home?.says) {
        setPlaced(said.home.says);
        return;
      }
    }
    await go();
  };

  return (
    <Question title="What should Steve call you?" why={
      <p>The rest is optional. It helps Steve write as you would, and read the day where you are.</p>}>
      <Field label="Your name"><input value={form.name} onChange={set("name")} autoFocus /></Field>
      <Field label="Pronouns (optional)"><input value={form.pronouns} onChange={set("pronouns")} /></Field>
      <Field label="Home city (optional)" hint={placed}>
        <input value={form.home_city} onChange={(e) => { set("home_city")(e); setPlaced(null); }} />
      </Field>
      <Field label="Work (optional)"><input value={form.occupation} onChange={set("occupation")} /></Field>
      <ErrorLine text={error} />
      <Actions busy={busy} ready={form.name.trim() !== ""} onContinue={keep} />
    </Question>
  );
}

function Passphrase({ kept, go }: { kept: boolean; go: Go }) {
  // The passphrase just chosen, for Hello's setup, which takes it.
  const [chosen, setChosen] = useState<string | null>(null);
  const [hello, setHello] = useState(false);
  useEffect(() => void helloAvailable().then(setHello), []);

  if (chosen !== null) {
    return (
      <Question title="Use Windows Hello too" why={
        <p>
          Then a glance or your PIN signs you in to Steve and says yes to spending, in place
          of typing your passphrase each time. The passphrase still works.
        </p>}>
        <div className="setup-panel">
          <HelloSetup passphrase={chosen} onDone={() => void go()} onSkip={() => void go()} />
        </div>
      </Question>
    );
  }
  return (
    <Question title="Choose a passphrase" why={
      <>
        <p>
          A few words only you know, to sign in to Steve and asked before it does anything that
          matters from the app: sending as you, deleting, paying.
        </p>
        <p>It can't be recovered, only reset, and resetting it is asked for here too.</p>
      </>}>
      {kept && <p className="setup-works" role="status">A passphrase is kept.</p>}
      <div className="setup-panel">
        <PassphraseForm isSet={kept} onDone={(words) => (hello ? setChosen(words) : void go())} />
      </div>
      {kept && <Actions onContinue={() => void go()} />}
    </Question>
  );
}

function Channel({ answer }: { answer: Answer }) {
  const [which, setWhich] = useState<"signal" | "telegram">("signal");
  const [linked, setLinked] = useState<{ signal: boolean; telegram: boolean }>({ signal: false, telegram: false });
  const [error, setError] = useState<string | null>(null);

  // Whether either is linked, asked as the panels change it.
  useEffect(() => {
    let live = true;
    const look = async () => {
      const [signal, telegram] = await Promise.all([
        api.signalStatus().catch(() => null), api.telegramStatus().catch(() => null)]);
      if (live) setLinked({ signal: signal?.bridge.state === "ready", telegram: telegram?.bridge.state === "ready" });
    };
    void look();
    const every = window.setInterval(look, 3000);
    return () => { live = false; window.clearInterval(every); };
  }, []);

  return (
    <Question title="Answer Steve from your phone" why={
      <p>
        Steve messages you when something needs you, and you answer from wherever you are.
        Signal is recommended: your messages are end-to-end encrypted.
      </p>}>
      <div className="setup-choices" role="radiogroup" aria-label="Channel">
        {(["signal", "telegram"] as const).map((c) => (
          <button key={c} type="button" role="radio" aria-checked={which === c}
                  className={which === c ? "chosen" : undefined} onClick={() => setWhich(c)}>
            {c === "signal" ? "Signal" : "Telegram"}
          </button>
        ))}
      </div>
      <div className="setup-panel">{which === "signal" ? <SignalPanel /> : <TelegramPanel />}</div>
      <ErrorLine text={error} />
      <Actions ready={linked[which]} onContinue={async () => setError(await answer("channel", which))}
               onSkip={async () => setError(await answer("channel", "skipped"))}
               skipLabel="Skip: answer only at this laptop" />
    </Question>
  );
}

function Accounts({ answer }: { answer: Answer }) {
  const [added, setAdded] = useState(0);
  const [error, setError] = useState<string | null>(null);
  return (
    <Question title="Your mail and calendar" why={
      <p>
        Steve reads them to know your day, and drafts in them for you to send. Start from an
        address; more can be added later, from Accounts.
      </p>}>
      <div className="setup-panel">
        <AddAccountHere onAdded={() => setAdded((n) => n + 1)} />
      </div>
      {added > 0 && <p className="setup-works" role="status">{added === 1 ? "One account added." : `${added} accounts added.`}</p>}
      <ErrorLine text={error} />
      <Actions ready={added > 0} onContinue={async () => setError(await answer("accounts", "added"))}
               onSkip={async () => setError(await answer("accounts", "skipped"))} />
    </Question>
  );
}

type DockerNow = { installed: boolean; running: boolean };
const DOCKER_DOWNLOAD = "https://www.docker.com/products/docker-desktop/";

/** The browser and running code, offered only with Docker Desktop running.
 *  Turned on without it, the switches said nothing until Steve first tried
 *  one and failed. Looked at only while the page is open, never by asking
 *  Docker (agent/worker/setup.py `docker`), and Steve installs nothing. */
function Docker({ answer }: { answer: Answer }) {
  const [now, setNow] = useState<DockerNow | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Looked at again every few seconds, so starting Docker Desktop with the
  // page open is seen without a press.
  useEffect(() => {
    let live = true;
    const look = async () => {
      const said = await request("/setup/docker").then((r) => r.json() as Promise<{ ok: boolean } & DockerNow>)
        .catch(() => null);
      if (live && said?.ok) setNow({ installed: said.installed, running: said.running });
    };
    void look();
    const every = window.setInterval(look, 3000);
    return () => { live = false; window.clearInterval(every); };
  }, []);

  const turnOn = async () => {
    setBusy(true);
    const said = await control.knobs({ BROWSER: "on", RUN_CODE: "on" });
    if (!said.ok) {
      setBusy(false);
      setError(said.error ?? "The switches weren't turned on.");
      return;
    }
    const failed = await answer("docker", "on");
    setBusy(false);
    setError(failed);
  };

  return (
    <Question title="Let Steve browse and run code?" why={
      <p>
        With these on, Steve reads web pages in a browser of its own, and runs the code it writes
        to work through your files and data. Each runs shut away from the rest of the laptop, in
        Docker Desktop. Steve never installs Docker or changes its settings.
      </p>}>
      {now && (now.running
        ? <p className="setup-works" role="status">Docker Desktop is running.</p>
        : now.installed
          ? <p className="setup-hint" role="status">
              Docker Desktop is installed, and not running. Start it, and this page sees it in a few
              seconds.
            </p>
          : <p className="setup-hint" role="status">
              Docker Desktop isn't installed. It's free for personal use; installing it needs an
              administrator, and often a restart.{" "}
              <button type="button" className="setup-text" onClick={() => void openLink(DOCKER_DOWNLOAD)
                .then((r) => { if (!r.ok) setError(r.error ?? "Could not open it."); })}>
                Get Docker Desktop
              </button>
            </p>)}
      <ErrorLine text={error} />
      <Actions busy={busy} ready={now?.running === true} label="Turn them on" onContinue={turnOn}
               onSkip={async () => setError(await answer("docker", "skipped"))} />
      <p className="setup-hint">Both switches are also on What Steve may do, to turn on later.</p>
    </Question>
  );
}

const PRESETS = [
  { name: "quiet", title: "Quiet", says: "Up to four messages a day, one at night if it's urgent, and no asking how a meeting went." },
  { name: "balanced", title: "Balanced", says: "What most people want: messages when something needs you, a few a day." },
  { name: "more", title: "More", says: "Up to twelve messages a day, and a word after every meeting." },
] as const;

function Preset({ now, go }: { now: string | null; go: Go }) {
  const [chosen, setChosen] = useState(now ?? "balanced");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const keep = async () => {
    setBusy(true);
    const said = await send<SetupState>("/setup/preset", { preset: chosen });
    setBusy(false);
    if (!said.ok) setError(said.error ?? "That wasn't kept.");
    else await go(said);
  };

  return (
    <Question title="How much should Steve do unasked?" why={
      <p>How often it speaks up on its own. It never sends, spends or deletes without your yes, whichever you choose, and each can be changed later.</p>}>
      <div className="setup-presets" role="radiogroup" aria-label="How much">
        {PRESETS.map((p) => (
          <button key={p.name} type="button" role="radio" aria-checked={chosen === p.name}
                  className={chosen === p.name ? "chosen" : undefined} onClick={() => setChosen(p.name)}>
            <span className="setup-preset-name">{p.title}</span>
            <span className="setup-hint">{p.says}</span>
          </button>
        ))}
      </div>
      <ErrorLine text={error} />
      <Actions busy={busy} onContinue={keep} />
    </Question>
  );
}

function Login({ answer }: { answer: Answer }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Null: this app can't start with Windows, being one given a data folder
  // of its own (lib.rs `run`), whose login entry would be the user's Steve's.
  const [can, setCan] = useState<boolean | undefined>(undefined);

  useEffect(() => {
    void startsAtLogin().then((on) => setCan(on !== null));
  }, []);

  const choose = async (on: boolean) => {
    setBusy(true);
    setError(null);
    try {
      if (can) await setStartsAtLogin(on);
    } catch (err) {
      setBusy(false);
      setError(`Windows didn't take that: ${String(err)}`);
      return;
    }
    const why = await answer("login", on ? "on" : "off");
    setBusy(false);
    setError(why);
  };

  if (can === false) {
    return (
      <Question title="Start Steve with Windows?" why={
        <p>
          This Steve keeps its things in a folder of its own, so it can't start with Windows:
          that is the installed Steve's. Start this one yourself.
        </p>}>
        <ErrorLine text={error} />
        <Actions busy={busy} onContinue={() => void choose(false)} />
      </Question>
    );
  }
  return (
    <Question title="Start Steve with Windows?" why={
      <p>
        Steve can only notice things, and answer your phone, while it runs. Started with Windows,
        it waits in the tray, out of the way.
      </p>}>
      <ErrorLine text={error} />
      <Actions busy={busy || can === undefined} label="Yes, start with Windows"
               onContinue={() => void choose(true)}
               onSkip={() => void choose(false)} skipLabel="No, I'll start it myself" />
    </Question>
  );
}

/* The privacy statement, PRIVACY.md, built into the app: shown whole, so
   what Setup says can't drift from it, and without a link that would work
   only online. Prose reads no headings, so each section's title is set
   here and its words go to Prose; the file's own title is the button's. */
function Statement() {
  const [intro, ...sections] = privacy.split(/^## /m);
  return (
    <div className="setup-privacy">
      <Prose text={intro.replace(/^# .*$/m, "")} className="t-small" />
      {sections.map((section) => {
        const [title, ...words] = section.split("\n");
        return (
          <section key={title}>
            <h3>{title}</h3>
            <Prose text={words.join("\n")} className="t-small" />
          </section>
        );
      })}
    </div>
  );
}

function Privacy({ answer, missing, goTo }: { answer: Answer; missing: Page[]; goTo: (page: Page) => void }) {
  const [drive, setDrive] = useState<string | null>(null);
  const [full, setFull] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    void request("/setup/drive").then((r) => r.json()).then((d: { says?: string }) => setDrive(d.says || null))
      .catch(() => setDrive(null));
  }, []);

  return (
    <Question title="Before you start">
      <p className="setup-why">
        What you say to Steve, and what it reads to answer you, goes to OpenAI on your key, which
        is asked not to keep it. With an OpenRouter key, its checks of what it reads go there.
        Your mail and calendar stay with their own providers, read from here. Messages to your
        phone go through the channel you linked. Pages and searches you ask for go to the web,
        as any visit would. Anything else leaves only for a service you set up, and everything
        Steve keeps stays on this computer.
        <button type="button" className="setup-text" aria-expanded={full} onClick={() => setFull(!full)}>
          {full ? "Hide it" : "What leaves this computer, in full"}
        </button>
      </p>
      {full && <Statement />}
      {drive && <p className="setup-why setup-drive">{drive}</p>}
      {missing.length > 0 && (
        <p className="setup-error">
          Still to do first:{" "}
          {missing.map((page, i) => (
            <span key={page}>
              {i > 0 && ", "}
              <button type="button" className="setup-text" onClick={() => goTo(page)}>{TITLES[page]}</button>
            </span>
          ))}
          .
        </p>
      )}
      <ErrorLine text={error} />
      <Actions label="Start" busy={busy} ready={missing.length === 0} onContinue={async () => {
        setBusy(true);
        setError(await answer("privacy", ""));
        setBusy(false);
      }} />
    </Question>
  );
}
