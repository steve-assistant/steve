import { Fragment, useEffect, useState, type ReactNode } from "react";
import { fromSqlite } from "./api";
import {
  control,
  when,
  type Account,
  type AccountSettings,
  type AddAccount as AddRequest,
  type Choice,
  type Mailbox,
  type Module,
  type SetupField,
  type Tested,
} from "./control";
import { openLink } from "./desktop";
import { Check } from "./Form";
import { Loading } from "./Loading";
import { type Say } from "./pages";

interface Props {
  toast: Say;
  /** Whether anyone can see the window: hidden in the tray, nothing is asked. */
  onScreen: boolean;
}

// An account's health changes in the background (a password revoked, a
// server down), so the list is read again while it is on screen.
const POLL_MS = 15000;

/**
 * Settings: Accounts. The backend's sentences send the user here ("in
 * Steve's app, under Settings: Accounts"), so every account it reads is
 * here with its health, said by the backend, and the one thing that fixes
 * it. Nothing names a provider: every label, guide step and link comes from
 * the backend's listing (agent/accounts.py `listing`), so a new module needs
 * no new app.
 */
export function Accounts({ toast, onScreen }: Props) {
  const [modules, setModules] = useState<Module[]>([]);
  const [accounts, setAccounts] = useState<Account[] | null>(null);
  const [mailbox, setMailbox] = useState<Mailbox | null>(null);
  const [adding, setAdding] = useState(false);
  // Bumped by each change the user makes, to read the list again at once.
  const [changes, setChanges] = useState(0);

  useEffect(() => {
    if (!onScreen) return;
    const controller = new AbortController();
    const read = async () => {
      // Each on its own: an own address that cannot be read is no reason
      // to hide the accounts.
      const [listed, own] = await Promise.allSettled([
        control.modules(controller.signal),
        control.mailbox(controller.signal),
      ]);
      if (controller.signal.aborted) return;
      if (listed.status === "fulfilled") {
        setModules(listed.value.modules);
        setAccounts(listed.value.accounts);
      }
      if (own.status === "fulfilled") setMailbox(own.value);
    };
    void read();
    const timer = window.setInterval(() => void read(), POLL_MS);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, [onScreen, changes]);

  const changed = () => setChanges((n) => n + 1);

  return (
    <div className="panel">
      <section>
        <div className="section-head">
          <h3>Your accounts</h3>
          {!adding && (
            <button className="primary" onClick={() => setAdding(true)}>Add account</button>
          )}
        </div>
        {adding && (
          <AddAccount modules={modules} toast={toast} onAdded={changed}
                      onClose={() => { setAdding(false); changed(); }} />
        )}
        {accounts === null ? (
          <Loading />
        ) : accounts.length === 0 ? (
          !adding && (
            <p className="muted small">
              None yet. Add one, and Steve can read its mail, calendar, files or tasks for you.
            </p>
          )
        ) : (
          accounts.map((account) => (
            <AccountCard key={account.key} account={account}
                         module={modules.find((m) => m.id === account.module)}
                         toast={toast} onChanged={changed} />
          ))
        )}
      </section>

      {mailbox && <OwnAddress mailbox={mailbox} toast={toast} onChanged={changed} />}
    </div>
  );
}

/* ------------------------------------------------------------- helpers */

/** "Mail and calendar": what an account connects, in the user's words. */
function inWords(classes: string[]): string {
  const words = classes.map((c) => c.replace(/_/g, " "));
  if (words.length === 0) return "";
  const list = words.length === 1 ? words[0]
    : `${words.slice(0, -1).join(", ")} and ${words[words.length - 1]}`;
  return list.charAt(0).toUpperCase() + list.slice(1);
}

/** What turning an account off, or removing it, does to the cards waiting on it. */
function waitingSentence(count: number): string {
  if (count <= 0) return "No waiting requests.";
  return count === 1 ? "1 waiting request will be cancelled."
    : `${count} waiting requests will be cancelled.`;
}

/** Where an account's secret is revoked: its preset's page, else its
 *  module's, or "" where the manifest names none. */
function revokeAt(module: Module | undefined, preset: string | null): string {
  if (!module) return "";
  return module.setup.presets.find((p) => p.id === preset)?.revoke || module.setup.revoke;
}

/** Beside a secret's field: where to take it back, should it leak. */
function RevokeLink({ url, toast }: { url: string; toast: Say }) {
  return url ? <LinkButton url={url} toast={toast}>Where to revoke it</LinkButton> : null;
}

/** A page opened in the user's own browser, never in the app's window
 *  (`openLink`): a button, so nothing can follow it inside the app. */
function LinkButton({ url, toast, children }: { url: string; toast: Say;
                                                children: ReactNode }) {
  return (
    <button className="link-button" title={url}
            onClick={() => void openLink(url).then((r) => { if (!r.ok) toast(r.error ?? "Could not open it.", false); })}>
      {children}
    </button>
  );
}

interface Problem {
  text: string;
  fix?: string;
}

/** A refusal as the backend said it, with the page that fixes it if there is one. */
function ProblemLine({ problem, toast }: { problem: Problem | null; toast: Say }) {
  if (!problem) return null;
  return (
    <div className="warn-text small">
      {problem.text}
      {problem.fix && (
        <> <LinkButton url={problem.fix} toast={toast}>Open the page that fixes it</LinkButton></>
      )}
    </div>
  );
}

function problemOf(result: { error?: string; fix?: unknown }, fallback: string): Problem {
  return { text: result.error ?? fallback,
           fix: typeof result.fix === "string" && result.fix ? result.fix : undefined };
}

/** An account's test: one sentence per part it read, then its health. A
 *  `pin` step keeps what the test found, so the list is what it will use.
 *  On an account's card the list has just been read again, so its health line
 *  already says the result (`said`); saying it twice read as two problems. */
function TestResult({ tested, pinned, toast, said }: { tested: Tested; pinned: boolean;
                                                       toast: Say;
                                                       said?: string | null }) {
  const repeated = said !== undefined && said === tested.health && !tested.fix;
  return (
    <div>
      {tested.found.length > 0 && (
        <>
          <div className="small">{pinned ? "It found these, and will use only these:" : "It found:"}</div>
          <ul className="findings small">
            {tested.found.map((line, i) => <li key={i}>{line}</li>)}
          </ul>
        </>
      )}
      {!repeated && (
        <div className={tested.ok ? "small" : "warn-text small"}>
          {tested.health}
          {tested.fix && (
            <> <LinkButton url={tested.fix} toast={toast}>Open the page that fixes it</LinkButton></>
          )}
        </div>
      )}
      {!tested.ok && tested.detail && <div className="muted small">{tested.detail}</div>}
    </div>
  );
}

/* ------------------------------------------------------------- the list */

type Open = null | "edit" | "password" | "off" | "remove";

function AccountCard({ account, module, toast, onChanged }: {
  account: Account;
  module: Module | undefined;
  toast: Say;
  onChanged: () => void;
}) {
  const [open, setOpen] = useState<Open>(null);
  const [busy, setBusy] = useState<null | "test" | "sign_in" | "save">(null);
  const [tested, setTested] = useState<Tested | null>(null);
  const [problem, setProblem] = useState<Problem | null>(null);
  const [secrets, setSecrets] = useState<Record<string, string>>({});
  // The code a server Steve runs showed, to enter at its sign-in page.
  const [shown, setShown] = useState<ShownCode | null>(null);

  const secretFields = module?.setup.fields.filter((f) => f.type === "secret") ?? [];
  const pinned = module?.setup.steps.includes("pin") ?? false;

  /** One change at a time: what it said goes under the card, then the list is read again. */
  const run = async (what: "test" | "sign_in" | "save", act: () => Promise<boolean>) => {
    setBusy(what);
    setProblem(null);
    setTested(null);
    try {
      if (await act()) setOpen(null);
    } finally {
      setBusy(null);
      onChanged();
    }
  };

  const test = () => run("test", async () => {
    const result = await control.testAccount(account.key);
    if (!result.ok || !result.test) {
      setProblem(problemOf(result, "Could not check it."));
      return false;
    }
    setTested(result.test);
    return true;
  });

  const signIn = () => run("sign_in", async () => {
    const result = await control.signIn(account.key);
    if (!result.ok || !result.test) {
      setProblem(problemOf(result, "The sign-in did not finish."));
      return false;
    }
    setTested(result.test);
    return true;
  });

  // A server Steve runs signs itself in: it shows a code, which the user
  // enters in their own browser, then checks it. The browser's sign-in
  // above refuses such an account, which once left it no way back in.
  const serverSignIn = () => run("sign_in", async () => {
    setShown(null);
    const result = await control.serverSignIn(account.key);
    if (!result.ok || !result.page || !result.code) {
      setProblem(problemOf(result, "The server showed no sign-in code."));
      return false;
    }
    setShown({ page: result.page, code: result.code,
               until: result.expires_s ? Date.now() + 1000 * result.expires_s : 0 });
    return false;
  });

  const checkSignedIn = () => run("test", async () => {
    const result = await control.testAccount(account.key);
    if (!result.ok || !result.test) {
      setProblem(problemOf(result, "Could not check it."));
      return false;
    }
    setShown(null);
    setTested(result.test);
    return true;
  });

  // A new password is kept, then tested at once: the card says whether it works.
  const savePassword = () => run("save", async () => {
    const typed = Object.fromEntries(Object.entries(secrets).filter(([, value]) => value !== ""));
    const saved = await control.editAccount(account.key, { fields: typed });
    if (!saved.ok) {
      setProblem(problemOf(saved, "Could not keep the new password."));
      return false;
    }
    setSecrets({});
    const result = await control.testAccount(account.key);
    if (!result.ok || !result.test) {
      setProblem(problemOf(result, "Kept, but could not check it."));
      return true;
    }
    setTested(result.test);
    return true;
  });

  const switchTo = (enabled: boolean) => run("save", async () => {
    const result = await control.editAccount(account.key, { enabled });
    if (!result.ok) {
      setProblem(problemOf(result, enabled ? "Could not turn it on." : "Could not turn it off."));
      return false;
    }
    return true;
  });

  const remove = async (forget: boolean) => {
    setBusy("save");
    const result = await control.removeAccount(account.key, forget);
    setBusy(null);
    if (!result.ok) {
      setProblem(problemOf(result, "Could not remove it."));
      return;
    }
    toast(`Removed ${account.name}.`);
    onChanged();
  };

  // Never tested, it has no action of its own yet: a test is the first one.
  const action = account.action || (account.state === null ? "test" : "");
  const healthClass = account.state === "working" ? "small"
    : account.state === null || account.state === "off" ? "muted small" : "warn-text small";

  return (
    <div className="item">
      <div className="item-head">
        <span className="item-title">{account.name}</span>
        <span className="muted small">{inWords(account.classes)}</span>
        <Check inline label={account.enabled ? "On" : "Off"}
               title={account.enabled ? "Turn it off" : "Turn it on"}
               checked={account.enabled} disabled={busy !== null}
               onChange={(on) => {
                 if (on) void switchTo(true);
                 else setOpen("off");
               }} />
      </div>
      <div className="item-body">
        {account.addresses.length > 0 && (
          <div className="muted small">{account.addresses.join(", ")}</div>
        )}
        <div className={healthClass}>{account.health ?? "Not checked yet."}</div>

        {busy === "sign_in" && (
          <div className="small">
            {account.sign_in === "server_sign_in"
              ? "Starting its server, which shows a code…"
              : "Waiting for you to sign in in your browser…"}
          </div>
        )}
        {busy === "test" && <div className="muted small">Checking…</div>}
        {shown && <ServerCode shown={shown} toast={toast} />}
        <ProblemLine problem={problem} toast={toast} />
        {tested && <TestResult tested={tested} pinned={pinned} toast={toast} said={account.health} />}

        <div className="item-actions">
          {action === "sign_in" && account.sign_in === "server_sign_in" && (
            <>
              {shown && (
                <button className="primary" disabled={busy !== null} onClick={() => void checkSignedIn()}>
                  I’ve entered it: check it
                </button>
              )}
              <button className={shown ? undefined : "primary"} disabled={busy !== null}
                      onClick={() => void serverSignIn()}>
                {shown ? "Get a new code" : "Sign in again"}
              </button>
            </>
          )}
          {action === "sign_in" && account.sign_in !== "server_sign_in" && (
            <button className="primary" disabled={busy !== null} onClick={() => void signIn()}>
              Sign in again
            </button>
          )}
          {action === "password" && secretFields.length > 0 && (
            <button className="primary" disabled={busy !== null}
                    onClick={() => setOpen(open === "password" ? null : "password")}>
              Enter the new password
            </button>
          )}
          {(action === "test" || (action === "password" && secretFields.length === 0)) && (
            <button className="primary" disabled={busy !== null} onClick={() => void test()}>
              {account.state === null ? "Test" : "Check again"}
            </button>
          )}
          {action === "link" && account.fix && (
            <button className="primary" disabled={busy !== null}
                    onClick={() => void openLink(account.fix).then((r) => {
                      if (!r.ok) toast(r.error ?? "Could not open it.", false);
                    })}>
              Open the page that fixes it
            </button>
          )}
          {action === "turn_on" && (
            <button className="primary" disabled={busy !== null} onClick={() => void switchTo(true)}>
              Turn it on
            </button>
          )}
          <button disabled={busy !== null} onClick={() => setOpen(open === "edit" ? null : "edit")}>
            Edit
          </button>
          <button disabled={busy !== null} onClick={() => setOpen(open === "remove" ? null : "remove")}>
            Remove
          </button>
        </div>

        {open === "password" && (
          <div className="account-form">
            {secretFields.map((field) => (
              <div className="field" key={field.name}>
                <span className="small">{field.label || "Password"}</span>
                <input type="password" autoComplete="new-password" value={secrets[field.name] ?? ""}
                       onChange={(e) => setSecrets((s) => ({ ...s, [field.name]: e.target.value }))} />
                <RevokeLink url={revokeAt(module, account.preset)} toast={toast} />
              </div>
            ))}
            <div className="item-actions">
              <button className="primary"
                      disabled={busy !== null || !secretFields.some((f) => secrets[f.name])}
                      onClick={() => void savePassword()}>
                {busy === "save" ? "Checking…" : "Keep it and check"}
              </button>
              <button onClick={() => { setSecrets({}); setOpen(null); }}>Cancel</button>
            </div>
          </div>
        )}

        {open === "edit" && (
          <EditAccount account={account} busy={busy !== null}
                       onSave={(change) => run("save", async () => {
                         const result = await control.editAccount(account.key, change);
                         if (!result.ok) setProblem(problemOf(result, "Could not save that."));
                         return result.ok;
                       })}
                       onCancel={() => setOpen(null)} />
        )}

        {open === "off" && (
          <div className="question">
            <div className="small">{waitingSentence(account.waiting)} Nothing is deleted.</div>
            <div className="item-actions">
              <button className="danger" disabled={busy !== null} onClick={() => void switchTo(false)}>
                Turn off
              </button>
              <button onClick={() => setOpen(null)}>Cancel</button>
            </div>
          </div>
        )}

        {open === "remove" && (
          <div className="question">
            <div className="small">
              {waitingSentence(account.waiting)} Its settings and password are deleted. What
              Steve learned from its mail is kept.
            </div>
            <div className="item-actions">
              <button className="danger" disabled={busy !== null} onClick={() => void remove(false)}>
                Remove
              </button>
              <button className="danger" disabled={busy !== null} onClick={() => void remove(true)}>
                Remove, and forget what Steve learned
              </button>
              <button onClick={() => setOpen(null)}>Cancel</button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function EditAccount({ account, busy, onSave, onCancel }: {
  account: Account;
  busy: boolean;
  onSave: (change: { name?: string; settings: AccountSettings }) => void;
  onCancel: () => void;
}) {
  const [name, setName] = useState(account.name);
  // Watched unless the user said not: the backend's default too.
  const [watched, setWatched] = useState(account.settings.watched !== false);
  const [every, setEvery] = useState(account.settings.every_min ? String(account.settings.every_min) : "");
  const [defaultCalendar, setDefaultCalendar] = useState(account.settings.default_calendar === true);
  const hasCalendar = account.classes.includes("calendar");

  const minutes = Number(every);
  const everyOk = every.trim() === "" || (Number.isInteger(minutes) && minutes >= 1 && minutes <= 30);

  const save = () => {
    const settings: AccountSettings = { watched };
    if (every.trim() !== "") settings.every_min = minutes;
    if (hasCalendar) settings.default_calendar = defaultCalendar;
    const trimmed = name.trim();
    onSave({ name: trimmed && trimmed !== account.name ? trimmed : undefined, settings });
  };

  return (
    <div className="account-form">
      <div className="field">
        <span className="small">Name</span>
        <input value={name} onChange={(e) => setName(e.target.value)} />
      </div>
      <Check label="Watch it for what’s new" checked={watched} onChange={setWatched} />
      <div className="field">
        <span className="small">Look every … minutes (1 to 30)</span>
        <input type="number" min={1} max={30} step={1} value={every} placeholder="as usual"
               disabled={!watched} onChange={(e) => setEvery(e.target.value)} />
      </div>
      {!everyOk && <div className="warn-text small">How often is a whole number of minutes, 1 to 30.</div>}
      {hasCalendar && (
        <Check label="New events go on this calendar" checked={defaultCalendar}
               onChange={setDefaultCalendar} />
      )}
      <div className="item-actions">
        <button className="primary" disabled={busy || !everyOk || !name.trim()} onClick={save}>Save</button>
        <button onClick={onCancel}>Cancel</button>
      </div>
    </div>
  );
}

/* ------------------------------------------------------- add an account */

const EMAIL = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

type Stage =
  | { at: "address" }
  | { at: "setup"; module: Module; preset: string | null; fromAddress: boolean }
  | { at: "sign_in"; module: Module; key: string }
  // The server Steve runs for it signs in itself, with a code the user
  // enters at its page; `shown` is that code, once the server has said it.
  | { at: "server_sign_in"; module: Module; key: string; shown: ShownCode | null }
  | { at: "done"; module: Module; key: string; tested: Tested | null };

/** A server's sign-in code, and when it runs out (a time from `Date.now()`). */
interface ShownCode {
  page: string;
  code: string;
  until: number;
}

/**
 * A server's sign-in code, large, with Copy, its page and how long it has
 * left. The page is opened only if it is HTTPS, and only in the user's
 * own browser: the backend already holds it to the page the module's
 * profile declares, so a server cannot send the user anywhere else, and
 * the app keeps to the same rule for every link it opens.
 */
function ServerCode({ shown, toast }: { shown: ShownCode; toast: Say }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  const left = Math.max(0, Math.round((shown.until - now) / 1000));
  const https = shown.page.startsWith("https://");
  // In the back pages' terms: the code in the data face, large, the page
  // and the time left below it in ink-faint, running out in red ink.
  return (
    <div className="server-code">
      <span className="form-label">Enter this code at the sign-in page, in your browser</span>
      <code className="server-code-text">{shown.code}</code>
      <span className="form-list-meta">{shown.page}</span>
      {shown.until > 0 && (
        <span className={left > 0 ? "form-hint" : "form-error"} role="timer">
          {left > 0
            ? `The code works for ${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")} more.`
            : "The code has run out: get a new one."}
        </span>
      )}
      <div className="form-actions">
        <button type="button" onClick={() => void navigator.clipboard.writeText(shown.code)
          .then(() => toast("Copied."), () => toast("Could not copy it.", false))}>
          Copy
        </button>
        {https ? (
          <LinkButton url={shown.page} toast={toast}>Open the page</LinkButton>
        ) : (
          <span className="form-error">Its page is not HTTPS, so Steve does not open it.</span>
        )}
      </div>
    </div>
  );
}

/** The classes on offer: the preset's, else the module's. */
function offered(module: Module, preset: string | null): string[] {
  const chosen = module.setup.presets.find((p) => p.id === preset);
  return chosen && chosen.classes.length > 0 ? chosen.classes : module.classes;
}

/**
 * Add account on a page of its own (setup's, ui/src/Setup.tsx): the kinds
 * of account read once, what it says shown under it, and closing it starts
 * it again for the next account rather than hiding it.
 */
export function AddAccountHere({ onAdded }: { onAdded: () => void }) {
  const [modules, setModules] = useState<Module[]>([]);
  const [said, setSaid] = useState<string | null>(null);
  const [round, setRound] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    control.modules(controller.signal).then((listed) => setModules(listed.modules))
      .catch(() => undefined);
    return () => controller.abort();
  }, []);

  return (
    <>
      <AddAccount key={round} modules={modules} toast={setSaid} onAdded={onAdded}
                  onClose={() => setRound((n) => n + 1)} />
      {said && <p className="muted small">{said}</p>}
    </>
  );
}

/**
 * Add account starts from the address: its domain picks the kind of
 * account (`pick`), or the backend asks its one question. Then the kind's
 * own steps, in its manifest's order. Advanced, closed until asked for,
 * lists every kind Steve can add, to pick one no address leads to (a
 * calendar's private link has none).
 */
function AddAccount({ modules, toast, onAdded, onClose }: {
  modules: Module[];
  toast: Say;
  onAdded: () => void;
  onClose: () => void;
}) {
  const [stage, setStage] = useState<Stage>({ at: "address" });
  const [address, setAddress] = useState("");
  const [question, setQuestion] = useState<{ text: string; yes: Choice; no: Choice } | null>(null);
  const [advanced, setAdvanced] = useState(false);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<Problem | null>(null);
  // A class another account holds for this address: the add again, moving it here.
  const [held, setHeld] = useState<{ klass: string; request: AddRequest } | null>(null);

  // The setup's answers, kept apart so choosing another kind starts clean.
  const [preset, setPreset] = useState<string>("");
  const [values, setValues] = useState<Record<string, string | boolean>>({});
  const [files, setFiles] = useState<Record<string, string>>({});
  const [ticked, setTicked] = useState<boolean[]>([]);
  const [classes, setClasses] = useState<string[]>([]);

  const choose = (choice: Choice, fromAddress: boolean) => {
    const module = modules.find((m) => m.id === choice.module);
    if (!module) {
      setProblem({ text: "Steve has nothing installed that adds that account." });
      return;
    }
    setQuestion(null);
    setProblem(null);
    setHeld(null);
    setPreset(choice.preset ?? "");
    setValues({});
    setFiles({});
    setTicked(module.setup.guide.map(() => false));
    setClasses(offered(module, choice.preset));
    setStage({ at: "setup", module, preset: choice.preset, fromAddress });
  };

  const pick = async () => {
    setBusy(true);
    setProblem(null);
    setQuestion(null);
    const result = await control.pick(address.trim());
    setBusy(false);
    if (!result.ok) {
      setProblem(problemOf(result, "Could not tell what kind of account that is."));
    } else if (result.question && result.yes && result.no) {
      setQuestion({ text: result.question, yes: result.yes, no: result.no });
    } else if (result.module) {
      choose({ module: result.module, preset: result.preset ?? null }, true);
    } else {
      setProblem({ text: "Could not tell what kind of account that is." });
    }
  };

  const send = async (request: AddRequest, module: Module) => {
    setBusy(true);
    setProblem(null);
    setHeld(null);
    const result = await control.addAccount(request);
    setBusy(false);
    if (!result.ok || !result.key) {
      setProblem(problemOf(result, "Could not add it."));
      if (result.held) setHeld({ klass: result.held.class, request });
      return;
    }
    onAdded();
    if (result.next === "browser_sign_in") setStage({ at: "sign_in", module, key: result.key });
    else if (result.next === "server_sign_in") {
      setStage({ at: "server_sign_in", module, key: result.key, shown: null });
    } else setStage({ at: "done", module, key: result.key, tested: result.test ?? null });
  };

  // Installing and starting the server can take a minute the first time;
  // the code it shows then works for the minutes it says.
  const serverSignIn = async (module: Module, key: string) => {
    setBusy(true);
    setProblem(null);
    const result = await control.serverSignIn(key);
    setBusy(false);
    if (!result.ok || !result.page || !result.code) {
      setProblem(problemOf(result, "The server showed no sign-in code."));
      return;
    }
    // No lifetime said: no countdown, rather than one that has run out.
    setStage({ at: "server_sign_in", module, key, shown: {
      page: result.page, code: result.code,
      until: result.expires_s ? Date.now() + 1000 * result.expires_s : 0 } });
  };

  const signIn = async (module: Module, key: string) => {
    setBusy(true);
    setProblem(null);
    const result = await control.signIn(key);
    setBusy(false);
    onAdded();
    if (!result.ok || !result.test) {
      setProblem(problemOf(result, "The sign-in did not finish."));
      return;
    }
    setStage({ at: "done", module, key, tested: result.test });
  };

  const testAgain = async (module: Module, key: string) => {
    setBusy(true);
    setProblem(null);
    const result = await control.testAccount(key);
    setBusy(false);
    onAdded();
    if (!result.ok || !result.test) setProblem(problemOf(result, "Could not check it."));
    else setStage({ at: "done", module, key, tested: result.test });
  };

  return (
    <div className="item">
      <div className="item-body">
        {stage.at === "address" && (
          <>
            <div className="answer-row">
              <input type="email" placeholder="The account’s address, e.g. you@example.com" value={address}
                     onChange={(e) => { setAddress(e.target.value); setQuestion(null); }}
                     onKeyDown={(e) => { if (e.key === "Enter" && EMAIL.test(address.trim())) void pick(); }} />
              <button className="primary" disabled={busy || !EMAIL.test(address.trim())}
                      onClick={() => void pick()}>
                Next
              </button>
            </div>
            {question && (
              <div className="question">
                <div className="small">{question.text}</div>
                <div className="item-actions">
                  <button onClick={() => choose(question.yes, true)}>Yes</button>
                  <button onClick={() => choose(question.no, true)}>No</button>
                </div>
              </div>
            )}
            <ProblemLine problem={problem} toast={toast} />
            <div>
              <button className="link-button" onClick={() => setAdvanced((a) => !a)}>
                {advanced ? "Hide Advanced" : "Advanced"}
              </button>
            </div>
            {advanced && (
              <div>
                {/* The listing holds every module, and one that provides no
                    class of account (a tool, Skills, a server) adds none. */}
                {modules.filter((m) => m.available && m.classes.length > 0).map((m) => (
                  <div className="row" key={m.id}>
                    <span className="row-title" title={m.for}>
                      <strong>{m.label}</strong> <span className="muted small">{m.for}</span>
                    </span>
                    <span className="muted small">{m.effort}{m.enabled ? "" : " · switched off"}</span>
                    <button disabled={!m.enabled} onClick={() => choose({ module: m.id, preset: null }, false)}>
                      Use this
                    </button>
                  </div>
                ))}
              </div>
            )}
            <div className="item-actions">
              <button onClick={onClose}>Cancel</button>
            </div>
          </>
        )}

        {stage.at === "setup" && (
          <Setup module={stage.module} presetFromAddress={stage.preset}
                 address={stage.fromAddress ? address.trim() : null}
                 preset={preset} onPreset={(id) => {
                   setPreset(id);
                   setClasses(offered(stage.module, id || null));
                 }}
                 values={values} onValue={(name, value) => setValues((v) => ({ ...v, [name]: value }))}
                 files={files} onFile={(name, file) => setFiles((f) => ({ ...f, [name]: file }))}
                 ticked={ticked} onTick={(i, on) => setTicked((t) => t.map((x, j) => (j === i ? on : x)))}
                 classes={classes} onClasses={setClasses}
                 onAddress={setAddress} typedAddress={address}
                 busy={busy} toast={toast}
                 onAdd={(request) => void send(request, stage.module)}
                 onBack={() => { setStage({ at: "address" }); setProblem(null); setHeld(null); }} />
        )}

        {stage.at === "sign_in" && (
          <>
            <div><strong>{stage.module.label}</strong></div>
            {busy ? (
              <div className="small">Waiting for you to sign in in your browser…</div>
            ) : (
              <div className="small">
                Added. Now sign in to it in your browser: Steve opens the page, and waits for you.
              </div>
            )}
            <div className="item-actions">
              <button className="primary" disabled={busy} onClick={() => void signIn(stage.module, stage.key)}>
                Sign in in your browser
              </button>
              <button disabled={busy} onClick={onClose}>Later</button>
            </div>
          </>
        )}

        {stage.at === "server_sign_in" && (
          <>
            <div><strong>{stage.module.label}</strong></div>
            {stage.shown ? (
              <ServerCode shown={stage.shown} toast={toast} />
            ) : busy ? (
              <div className="small">Starting its server, which shows a code…</div>
            ) : (
              <div className="small">
                Added. Now sign it in: Steve starts its server, which shows a code you enter at
                its sign-in page, in your browser.
              </div>
            )}
            <ProblemLine problem={problem} toast={toast} />
            <div className="item-actions">
              {stage.shown ? (
                <button className="primary" disabled={busy}
                        onClick={() => void testAgain(stage.module, stage.key)}>
                  {busy ? "Checking…" : "I’ve entered it: check it"}
                </button>
              ) : null}
              <button className={stage.shown ? undefined : "primary"} disabled={busy}
                      onClick={() => void serverSignIn(stage.module, stage.key)}>
                {stage.shown ? "Get a new code" : "Get the code"}
              </button>
              <button disabled={busy} onClick={onClose}>Later</button>
            </div>
          </>
        )}

        {stage.at === "done" && (
          <>
            <div><strong>{stage.module.label}</strong>: added.</div>
            {stage.tested ? (
              <TestResult tested={stage.tested} pinned={stage.module.setup.steps.includes("pin")}
                          toast={toast} />
            ) : (
              <div className="muted small">Not checked yet.</div>
            )}
            <div className="item-actions">
              <button className="primary" onClick={onClose}>Done</button>
              {(!stage.tested || !stage.tested.ok) && (
                <button disabled={busy} onClick={() => void testAgain(stage.module, stage.key)}>
                  {busy ? "Checking…" : "Check again"}
                </button>
              )}
            </div>
          </>
        )}

        {stage.at !== "address" && <ProblemLine problem={problem} toast={toast} />}
        {held && stage.at === "setup" && (
          <div className="item-actions">
            <button disabled={busy}
                    onClick={() => void send({ ...held.request, take: true }, stage.module)}>
              Move the {held.klass} for this address here
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

/** One kind of account's setup, its steps in its manifest's order. */
function Setup(props: {
  module: Module;
  presetFromAddress: string | null;
  /** The address it was picked by; null when picked from Advanced. */
  address: string | null;
  typedAddress: string;
  onAddress: (address: string) => void;
  preset: string;
  onPreset: (id: string) => void;
  values: Record<string, string | boolean>;
  onValue: (name: string, value: string | boolean) => void;
  files: Record<string, string>;
  onFile: (name: string, file: string) => void;
  ticked: boolean[];
  onTick: (index: number, on: boolean) => void;
  classes: string[];
  onClasses: (classes: string[]) => void;
  busy: boolean;
  toast: Say;
  onAdd: (request: AddRequest) => void;
  onBack: () => void;
}) {
  const { module, values, classes } = props;
  const steps = module.setup.steps;
  const asksAddress = steps.includes("address");
  const address = props.address ?? (asksAddress ? props.typedAddress.trim() : "");
  // The preset is asked only when the address did not pick one.
  const asksPreset = steps.includes("preset") && props.presetFromAddress === null
    && module.setup.presets.length > 0;
  const onOffer = offered(module, props.preset || null);
  // Servers and ports, closed until asked for: a preset fills them.
  const [servers, setServers] = useState(false);
  const plainFields = module.setup.fields.filter((f) => !f.advanced);
  const serverFields = module.setup.fields.filter((f) => f.advanced);

  const fieldsMissing = steps.includes("fields") && module.setup.fields.some((f) => {
    if (f.optional || f.type === "flag") return false;
    const value = values[f.name];
    return typeof value !== "string" || (f.type === "secret" ? value === "" : value.trim() === "");
  });
  const ready = !props.busy
    && (!asksAddress || EMAIL.test(address))
    && (!steps.includes("guide") || props.ticked.every(Boolean))
    && (!asksPreset || props.preset !== "")
    && !fieldsMissing
    && classes.length > 0;

  const add = () => {
    const fields: Record<string, string | boolean> = {};
    if (steps.includes("fields")) {
      for (const f of module.setup.fields) {
        const value = values[f.name];
        if (f.type === "flag") {
          // An optional flag left alone is not sent: an explicit "off"
          // would stand over what the account's preset says.
          if (value === true || !f.optional) fields[f.name] = value === true;
        } else if (typeof value === "string" && value.trim() !== "") {
          // A password, or a file's contents, as given; anything else without its spaces.
          fields[f.name] = f.type === "secret" || f.type === "file" ? value : value.trim();
        }
      }
    }
    const request: AddRequest = { module: module.id, address, fields };
    const preset = props.presetFromAddress ?? (props.preset || null);
    if (preset) request.preset = preset;
    // Sent only when the user took one away: left alone, the backend adds
    // what a field joins (a calendar's address) as well.
    if (onOffer.length > 1 && classes.length < onOffer.length) request.classes = classes;
    props.onAdd(request);
  };

  return (
    <>
      <div>
        <strong>{module.label}</strong>
        {module.effort && <span className="muted small"> · {module.effort}</span>}
      </div>
      {module.for && <div className="muted small">{module.for}</div>}
      {props.address && <div className="small">For <strong>{props.address}</strong></div>}

      {steps.map((step) => {
        // Picked by its address, the account has it already (above).
        if (step === "address") {
          return props.address !== null ? null : (
            <div key={step} className="field">
              <span className="small">The account’s address</span>
              <input type="email" placeholder="you@example.com" value={props.typedAddress}
                     onChange={(e) => props.onAddress(e.target.value)} />
            </div>
          );
        }
        if (step === "guide" && module.setup.guide.length > 0) {
          return (
            <div key={step}>
              {module.setup.guide.map((g, i) => (
                <Check key={i} checked={props.ticked[i] ?? false}
                       onChange={(on) => props.onTick(i, on)}
                       label={<>
                         {g.text}
                         {g.link && (
                           <> <LinkButton url={g.link} toast={props.toast}>Open it</LinkButton></>
                         )}
                       </>} />
              ))}
            </div>
          );
        }
        if (step === "preset" && asksPreset) {
          return (
            <div key={step} className="field">
              <span className="small">Which is it?</span>
              <select value={props.preset} onChange={(e) => props.onPreset(e.target.value)}>
                <option value="">Choose…</option>
                {module.setup.presets.map((p) => <option key={p.id} value={p.id}>{p.label}</option>)}
              </select>
            </div>
          );
        }
        if (step === "fields") {
          const input = (f: SetupField) => (
            <FieldInput key={f.name} field={f} value={values[f.name]}
                        fileName={props.files[f.name]}
                        revoke={revokeAt(module, props.preset || null)}
                        onValue={(value) => props.onValue(f.name, value)}
                        onFile={(name) => props.onFile(f.name, name)}
                        toast={props.toast} />
          );
          return (
            <Fragment key={step}>
              {plainFields.map(input)}
              {serverFields.length > 0 && (
                <div>
                  <button className="link-button" onClick={() => setServers((s) => !s)}>
                    {servers ? "Hide Advanced" : "Advanced: its servers"}
                  </button>
                </div>
              )}
              {servers && serverFields.map(input)}
            </Fragment>
          );
        }
        if (step === "classes" && onOffer.length > 1) {
          return (
            <div key={step} className="field">
              <span className="small">What Steve may use</span>
              <span className="chips">
                {onOffer.map((c) => (
                  <Check inline key={c} label={inWords([c])}
                         checked={classes.includes(c)}
                         onChange={(on) => props.onClasses(on
                           ? onOffer.filter((x) => x === c || classes.includes(x))
                           : classes.filter((x) => x !== c))} />
                ))}
              </span>
            </div>
          );
        }
        if (step === "browser_sign_in") {
          return (
            <div key={step} className="muted small">
              Once it is added, you sign in to it in your browser.
            </div>
          );
        }
        return null;
      })}

      <div className="item-actions">
        <button className="primary" disabled={!ready} onClick={add}>
          {props.busy ? "Adding…" : "Add"}
        </button>
        <button disabled={props.busy} onClick={props.onBack}>Back</button>
      </div>
    </>
  );
}

/** One setup field, by its type. A file is read here, as text: its
 *  contents are the value, never its path. */
function FieldInput({ field, value, fileName, revoke, onValue, onFile, toast }: {
  field: SetupField;
  value: string | boolean | undefined;
  fileName: string | undefined;
  /** Where a secret is revoked, shown beside a secret's field: "" for none. */
  revoke: string;
  onValue: (value: string | boolean) => void;
  onFile: (name: string) => void;
  toast: Say;
}) {
  const label = `${field.label || field.name}${field.optional ? " (optional)" : ""}`;
  const text = typeof value === "string" ? value : "";

  if (field.type === "flag") {
    return (
      <Check label={label} checked={value === true} onChange={onValue} />
    );
  }

  let input: ReactNode;
  if (field.type === "secret") {
    input = <input type="password" autoComplete="new-password" value={text}
                   onChange={(e) => onValue(e.target.value)} />;
  } else if (field.type === "file") {
    input = (
      <span>
        <input type="file" onChange={(e) => {
          const file = e.target.files?.[0];
          if (!file) return;
          file.text().then((contents) => { onValue(contents); onFile(file.name); },
                           () => toast(`Could not read ${file.name}.`, false));
        }} />
        {fileName && <span className="muted small"> {fileName}</span>}
      </span>
    );
  } else if (field.type === "choice") {
    input = (
      <select value={text} onChange={(e) => onValue(e.target.value)}>
        <option value="">Choose…</option>
        {field.choices.map((c) => <option key={c} value={c}>{c}</option>)}
      </select>
    );
  } else {
    const placeholder = field.type === "address" ? "you@example.com"
      : field.type === "link" ? "https://…" : "";
    input = <input type={field.type === "address" ? "email" : "text"} value={text}
                   placeholder={placeholder} onChange={(e) => onValue(e.target.value)} />;
  }
  return (
    <div className="field">
      <span className="small">{label}</span>
      {input}
      {field.type === "secret" && <RevokeLink url={revoke} toast={toast} />}
    </div>
  );
}

/* ----------------------------------------------------- Steve's own address */

const KIND: Record<Mailbox["recent"][number]["kind"], string> = {
  instruction: "Your instruction",
  thread: "A reply in a conversation",
  ignored: "Left alone",
};

/** The address a tag makes at an account: its own, with `+tag` before the @
 *  (core/mailbox.py `alias`). */
function plusAddress(address: string, tag: string): string {
  const [local, domain] = address.toLowerCase().split("@");
  if (!local || !domain) return "";
  return `${local.split("+")[0]}+${tag.trim().toLowerCase()}@${domain}`;
}

/**
 * Steve's own address: a plus-address at one of the user's mail accounts,
 * so a mail forwarded or copied to it reaches Steve. Only mail from the
 * addresses that count as the user's is taken as an instruction; the rest is
 * shown here with why.
 */
function OwnAddress({ mailbox, toast, onChanged }: {
  mailbox: Mailbox;
  toast: Say;
  onChanged: () => void;
}) {
  const able = mailbox.accounts.filter((a) => a.plus_addresses);
  const unable = mailbox.accounts.filter((a) => !a.plus_addresses);
  const [key, setKey] = useState("");
  const [tag, setTag] = useState(mailbox.tag || "agent");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<Problem | null>(null);
  const [looked, setLooked] = useState<string[] | null>(null);
  const [looking, setLooking] = useState(false);

  const chosen = able.find((a) => a.key === key) ?? able[0];
  const preview = chosen ? plusAddress(chosen.address, tag) : "";

  const act = async (what: () => Promise<{ ok: boolean; error?: string; fix?: unknown }>,
                     fallback: string): Promise<boolean> => {
    setBusy(true);
    setProblem(null);
    const result = await what();
    setBusy(false);
    if (!result.ok) setProblem(problemOf(result, fallback));
    onChanged();
    return result.ok;
  };

  if (!mailbox.address) {
    return (
      <section>
        <h3>Steve’s own address</h3>
        <p className="muted small setting-note">
          An address of Steve’s own, at one of your mail accounts. Forward it a mail, or copy it
          into a conversation, and Steve acts on what you write there. Only mail from you counts
          as an instruction.
        </p>
        {mailbox.accounts.length === 0 ? (
          <p className="muted small">Add a mail account first.</p>
        ) : (
          <>
            {able.length > 0 && (
              <>
                <div className="field">
                  <span className="small">At</span>
                  <select value={chosen?.key ?? ""} onChange={(e) => setKey(e.target.value)}>
                    {able.map((a) => <option key={a.key} value={a.key}>{a.name} ({a.address})</option>)}
                  </select>
                </div>
                <div className="field">
                  <span className="small">The word after the +</span>
                  <input value={tag} onChange={(e) => setTag(e.target.value)} />
                </div>
                {preview && <div className="small">Steve’s address would be <strong>{preview}</strong></div>}
                <div className="item-actions">
                  <button className="primary" disabled={busy || !chosen || !tag.trim()}
                          onClick={() => void act(async () => {
                            const result = await control.chooseMailbox(chosen.key, tag.trim());
                            if (result.ok && result.address) toast(`Steve’s address is ${result.address}.`);
                            return result;
                          }, "Could not give Steve that address.")}>
                    Turn it on
                  </button>
                </div>
              </>
            )}
            {unable.map((a) => (
              <div key={a.key} className="muted small">
                {a.name} ({a.address}) can’t hold it: its mail does not deliver an address with a + in it.
              </div>
            ))}
          </>
        )}
        <ProblemLine problem={problem} toast={toast} />
      </section>
    );
  }

  const holder = mailbox.accounts.find((a) => a.key === mailbox.account);
  return (
    <section>
      <h3>Steve’s own address</h3>
      <div className="row">
        <span className="row-title">
          <strong>{mailbox.address}</strong>
          {holder && <span className="muted small"> at {holder.name}</span>}
        </span>
        <button disabled={busy} onClick={() => void act(control.stopMailbox, "Could not stop it.")}>
          Stop
        </button>
      </div>

      <div className="small own-heading">Mail from these addresses counts as yours:</div>
      {mailbox.addresses.length === 0 && <p className="muted small">None yet.</p>}
      {mailbox.addresses.map((a) => (
        <div key={a.address} className="row">
          <span className="row-title">
            <Check inline label={a.address} checked={a.opted_in}
                   disabled={busy}
                   onChange={(on) => void act(() => control.userAddress(a.address, on, false),
                                              "Could not change that.")} />
          </span>
          <Check inline label="Only I send as this domain"
                 checked={a.own_domain} disabled={busy || !a.opted_in}
                 onChange={(on) => void act(() => control.userAddress(a.address, true, on),
                                            "Could not change that.")} />
        </div>
      ))}

      <div className="small own-heading">
        Try it: send any mail to <strong>{mailbox.address}</strong> from your phone.
      </div>
      <div className="item-actions">
        <button disabled={busy} onClick={() => void (async () => {
          setLooked(null);
          setLooking(true);
          let done: string[] = [];
          const ok = await act(async () => {
            const result = await control.lookNow();
            done = result.done ?? [];
            return result;
          }, "Could not look just now.");
          setLooking(false);
          if (ok) setLooked(done);
        })()}>
          {looking ? "Looking…" : "Look now"}
        </button>
        {looked !== null && (
          <span className="muted small">{looked.length === 0 ? "Nothing new." : looked.join(" ")}</span>
        )}
      </div>
      <ProblemLine problem={problem} toast={toast} />

      {mailbox.recent.length > 0 && (
        <>
          <div className="small own-heading">What came lately, and what it counted as:</div>
          {mailbox.recent.map((r) => (
            <div key={r.id} className="row">
              <span className="muted small">{when(fromSqlite(r.created_at))}</span>
              <div className="small own-mail">
                <strong>{r.sender}</strong> {r.subject}
                {r.why && <div className="muted">{r.why}</div>}
              </div>
              <span className={r.kind === "ignored" ? "muted small" : "small"}>{KIND[r.kind] ?? r.kind}</span>
            </div>
          ))}
        </>
      )}
    </section>
  );
}
