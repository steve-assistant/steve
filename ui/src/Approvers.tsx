import { useCallback, useEffect, useState } from "react";
import { api, type ApprovalsSetup } from "./api";
import { Loading } from "./Loading";
import {
  forgetHello, helloAvailable, resetPassphrase, resetPassphraseByWindows, setUpHello,
  signInWithPassphrase, signinState,
} from "./session";

const RETRY_MS = 5000;

/**
 * Who may approve: the passphrase the app's yes to spending takes, and the
 * authenticator whose code a yes to spending takes everywhere else.
 *
 * The backend checks both (core/monitor.py); this only collects them. Until
 * step 4 the authenticator was set up at a terminal, which an app's user
 * does not have, and the app's yes needed nothing.
 */
export function PassphraseForm({ isSet, hello = false, forgotten = false, onDone }: {
  isSet: boolean;
  /** Windows Hello is set up for Steve here: a forgotten passphrase can be
   *  reset with a glance in place of the old one; otherwise with Windows'
   *  own sign-in. */
  hello?: boolean;
  /** Opened at the reset: the sign-in page's "Forgotten?". */
  forgotten?: boolean;
  onDone: (words: string) => void;
}) {
  const [old, setOld] = useState("");
  const [words, setWords] = useState("");
  const [again, setAgain] = useState("");
  const [forgot, setForgot] = useState(forgotten);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const reset = async () => {
    if (!hello) return await resetPassphraseByWindows(words);
    const state = await signinState();
    return state ? await resetPassphrase(state.hello_name, words)
      : { ok: false, error: "Steve didn't answer. Check it is running, then try again." };
  };
  const by = hello ? "Windows Hello" : "your Windows sign-in";

  const save = async () => {
    if (words !== again) {
      setError("the two don't match");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const result = forgot ? await reset() : await api.choosePassphrase(words, isSet ? old : undefined);
      // The first passphrase is also the app's first sign-in: from now on
      // the app shows nothing without one (ui/src/SignIn.tsx).
      if (result.ok && !isSet) await signInWithPassphrase(words);
      if (result.ok) onDone(words);
      else setError(result.error === "cancelled" ? "Cancelled." : result.error ?? "it was not kept");
    } catch (err) {
      setError(String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="knob-group">
      {isSet && !forgot && (
        <input type="password" placeholder="The passphrase now" value={old}
               onChange={(e) => setOld(e.target.value)} />
      )}
      <input type="password" placeholder="A new passphrase, 8 characters or more" value={words}
             onChange={(e) => setWords(e.target.value)} />
      <input type="password" placeholder="The same again" value={again}
             onChange={(e) => setAgain(e.target.value)} />
      <div className="item-actions">
        <button className="primary" disabled={busy || words.length === 0} onClick={() => void save()}>
          {forgot ? (busy ? `Waiting for ${by}…` : `Reset it with ${by}`)
            : isSet ? "Change it" : "Choose it"}
        </button>
        {isSet && !forgot && (
          <button className="link-button" disabled={busy} onClick={() => { setForgot(true); setError(null); }}>
            Forgotten it?
          </button>
        )}
        {error && <span className="warn-text small" role="alert">{error}</span>}
      </div>
      <p className="muted small">
        {`Steve keeps only a hash of it. Forgotten, it can be reset here with ${by}.`}
      </p>
    </div>
  );
}

function AuthenticatorSetup({ onDone }: { onDone: () => void }) {
  const [words, setWords] = useState("");
  const [shown, setShown] = useState<{ qr: string; key: string } | null>(null);
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const start = async () => {
    setBusy(true);
    setError(null);
    try {
      const result = await api.startAuthenticator(words);
      if (result.ok && result.qr && result.key) setShown({ qr: result.qr, key: result.key });
      else setError(result.error ?? "it could not start");
    } catch (err) {
      setError(String(err));
    } finally {
      setWords("");
      setBusy(false);
    }
  };

  const confirm = async () => {
    setBusy(true);
    setError(null);
    try {
      const result = await api.confirmAuthenticator(code);
      if (result.ok) onDone();
      else setError(result.error ?? "it was not kept");
    } catch (err) {
      setError(String(err));
    } finally {
      setBusy(false);
    }
  };

  if (!shown) {
    return (
      <div className="knob-group">
        <input type="password" placeholder="Your passphrase" value={words}
               onChange={(e) => setWords(e.target.value)} />
        <div className="item-actions">
          <button className="primary" disabled={busy || words.length === 0} onClick={() => void start()}>
            Show a code to scan
          </button>
          {error && <span className="warn-text small">{error}</span>}
        </div>
      </div>
    );
  }
  return (
    <div className="knob-group">
      <img className="signal-qr" alt="A code to scan with your authenticator app"
           src={`data:image/svg+xml;charset=utf-8,${encodeURIComponent(shown.qr)}`} />
      <p className="muted small">Or type this key into it: <code>{shown.key}</code></p>
      <input inputMode="numeric" placeholder="The 6 digits it shows for Steve" value={code}
             onChange={(e) => setCode(e.target.value)} />
      <div className="item-actions">
        <button className="primary" disabled={busy || code.length === 0} onClick={() => void confirm()}>
          Keep it
        </button>
        {error && <span className="warn-text small">{error}</span>}
      </div>
    </div>
  );
}

/**
 * Windows Hello for Steve: a key Windows makes, registered with the
 * backend under the passphrase (core/hello.py). Given the passphrase just
 * chosen, in setup, it doesn't ask for it again.
 */
export function HelloSetup({ passphrase, onDone, onSkip }: {
  passphrase?: string;
  onDone: () => void;
  onSkip?: () => void;
}) {
  const [words, setWords] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const start = async () => {
    setBusy(true);
    setError(null);
    const state = await signinState();
    const said = state ? await setUpHello(state.hello_name, passphrase ?? words)
      : { ok: false, error: "Steve didn't answer. Check it is running, then try again." };
    setBusy(false);
    setWords("");
    if (said.ok) onDone();
    else setError(said.error === "cancelled" ? "Cancelled." : said.error ?? "It wasn't set up.");
  };

  return (
    <div className="knob-group">
      {passphrase === undefined && (
        <input type="password" placeholder="Your passphrase" value={words}
               onChange={(e) => setWords(e.target.value)} />
      )}
      <div className="item-actions">
        <button className="primary" disabled={busy || (passphrase === undefined && !words)}
                onClick={() => void start()}>
          {busy ? "Waiting for Windows Hello…" : "Set up Windows Hello"}
        </button>
        {onSkip && <button className="link-button" disabled={busy} onClick={onSkip}>Not now</button>}
        {error && <span className="warn-text small" role="alert">{error}</span>}
      </div>
    </div>
  );
}

function HelloForget({ onDone }: { onDone: () => void }) {
  const [words, setWords] = useState("");
  const [error, setError] = useState<string | null>(null);
  const forget = async () => {
    const said = await forgetHello(words);
    setWords("");
    if (said.ok) onDone();
    else setError(said.error ?? "It wasn't removed.");
  };
  return (
    <div className="knob-group">
      <input type="password" placeholder="Your passphrase" value={words}
             onChange={(e) => setWords(e.target.value)} />
      <div className="item-actions">
        <button className="primary" disabled={!words} onClick={() => void forget()}>Stop using Hello</button>
        {error && <span className="warn-text small" role="alert">{error}</span>}
      </div>
    </div>
  );
}

export function Approvers() {
  const [setup, setSetup] = useState<ApprovalsSetup | null>(null);
  const [open, setOpen] = useState<"passphrase" | "authenticator" | "hello" | null>(null);
  const [helloHere, setHelloHere] = useState(false);
  useEffect(() => void helloAvailable().then(setHelloHere), []);

  const refresh = useCallback(async () => {
    try {
      setSetup(await api.approvalsSetup());
    } catch {
      setSetup(null);
    }
  }, []);

  // Asked again while it has no answer: asked once, a backend that was
  // restarting left this saying "Loading…" until the page was opened again.
  useEffect(() => {
    if (setup) return;
    void refresh();
    const timer = window.setInterval(() => void refresh(), RETRY_MS);
    return () => window.clearInterval(timer);
  }, [setup, refresh]);

  const done = () => {
    setOpen(null);
    void refresh();
  };

  if (!setup) return <Loading />;
  return (
    <>
      <div className="knob">
        <span className="knob-says">
          Passphrase, for a yes to spending in this app: {setup.passphrase ? "set" : "not set"}
        </span>
        <button className="link-button" onClick={() => setOpen(open === "passphrase" ? null : "passphrase")}>
          {setup.passphrase ? "change" : "choose one"}
        </button>
      </div>
      {open === "passphrase" && (
        <PassphraseForm isSet={setup.passphrase} hello={setup.hello && helloHere} onDone={done} />
      )}

      <div className="knob">
        <span className="knob-says">
          Authenticator, for a yes to spending by message or voice:{" "}
          {setup.authenticator ? "set up" : "not set up"}
        </span>
        {setup.passphrase ? (
          <button className="link-button"
                  onClick={() => setOpen(open === "authenticator" ? null : "authenticator")}>
            {setup.authenticator ? "replace" : "set up"}
          </button>
        ) : (
          <span className="muted small">choose a passphrase first</span>
        )}
      </div>
      {!setup.authenticator && (
        <p className="muted small">
          It takes an authenticator app on your phone, one that shows six-digit codes, and a
          QR code scanned from here.
        </p>
      )}
      {setup.codes_paused_until && (
        <p className="warn-text small" role="status">
          Wrong codes were sent, or could not be counted, so a yes to spending by message or
          voice is not taken until {new Date(setup.codes_paused_until * 1000).toLocaleString([], {
            weekday: "short", hour: "numeric", minute: "2-digit" })}. You can still answer in
          this app.
        </p>
      )}
      {open === "authenticator" && <AuthenticatorSetup onDone={done} />}

      {helloHere && (
        <div className="knob">
          <span className="knob-says">
            Windows Hello, to sign in and say yes to spending with a glance:{" "}
            {setup.hello ? "set up" : "not set up"}
          </span>
          {setup.passphrase ? (
            <button className="link-button" onClick={() => setOpen(open === "hello" ? null : "hello")}>
              {setup.hello ? "stop using it" : "set up"}
            </button>
          ) : (
            <span className="muted small">choose a passphrase first</span>
          )}
        </div>
      )}
      {open === "hello" && (setup.hello ? <HelloForget onDone={done} /> : <HelloSetup onDone={done} />)}
    </>
  );
}
