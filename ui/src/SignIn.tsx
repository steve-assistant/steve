import { useCallback, useEffect, useRef, useState } from "react";
import { PassphraseForm } from "./Approvers";
import { Mark } from "./Mark";
import { Actions, ErrorLine, Field, Question } from "./Setup";
import {
  ENDED,
  helloAvailable,
  signInWithHello,
  signInWithPassphrase,
  type Ended,
  type SigninState,
} from "./session";

/**
 * The sign-in, in place of everything else, once a passphrase is set and
 * the shell holds no session (security model, section 4.2). Before it, a
 * laptop left unlocked showed whoever sat down everything Steve holds.
 *
 * With Windows Hello set up, Hello is asked for at once while the window is
 * in front, and again by its button; the passphrase is always there
 * instead. A cancelled prompt says nothing: the user chose to.
 */
export function SignIn({ state, ended, onSignedIn }: {
  state: SigninState;
  ended: Ended | null;
  onSignedIn: () => void;
}) {
  const [hello, setHello] = useState(false);
  const [typing, setTyping] = useState(false);
  const [words, setWords] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [resetting, setResetting] = useState(false);
  const asked = useRef(false);

  const glance = useCallback(async () => {
    asked.current = true;
    setBusy(true);
    setError(null);
    const said = await signInWithHello(state.hello_name);
    setBusy(false);
    if (said.ok) onSignedIn();
    else if (said.error !== "cancelled") setError(said.error ?? "Windows Hello didn't answer.");
  }, [state.hello_name, onSignedIn]);

  useEffect(() => {
    if (!state.hello) return;
    let gone = false;
    void helloAvailable().then((ready) => {
      if (gone || !ready) return;
      setHello(true);
      // Asked by itself once, while someone can see the window.
      if (document.hasFocus() && !asked.current) void glance();
    });
    const focused = () => {
      if (!asked.current) void glance();
    };
    window.addEventListener("focus", focused);
    return () => {
      gone = true;
      window.removeEventListener("focus", focused);
    };
  }, [state.hello, glance]);

  const typed = async () => {
    setBusy(true);
    setError(null);
    const said = await signInWithPassphrase(words);
    setBusy(false);
    setWords("");
    if (said.ok) onSignedIn();
    else setError(said.error ?? "That didn't go through. Try again.");
  };

  return (
    <div className="setup">
      <div className="setup-page setup-hello signin">
        <Mark size={96} label="Steve" />
        <Question title="Welcome back" why={ended ? <p>{ENDED[ended]}</p> : undefined} />
        {state.gone && (
          <p className="setup-why" role="alert">
            Steve's passphrase file was removed or changed, so the passphrase is locked. Choose a new
            one with {hello ? "Windows Hello" : "your Windows sign-in"}.
          </p>
        )}
        <div className={hello && !typing ? "signin-body" : "signin-body typing"}>
          {resetting ? (
            // Without Hello, nothing signs in before the reset: Windows'
            // own sign-in stands in for the old passphrase (core/winsignin.py).
            <PassphraseForm isSet forgotten onDone={(chosen) => void signInWithPassphrase(chosen)
              .then((said) => (said.ok ? onSignedIn() : setError(said.error ?? "Try again.")))} />
          ) : hello && !typing ? (
            // Not `busy`, which would say only "One moment…" and hold the
            // passphrase back while Windows' prompt waits.
            <Actions label={busy ? "Waiting for Windows Hello…" : "Sign in with Windows Hello"}
                     ready={!busy} onContinue={() => void glance()}
                     onSkip={() => { setTyping(true); setError(null); }} skipLabel="Use my passphrase" />
          ) : (
            <form onSubmit={(e) => { e.preventDefault(); if (words) void typed(); }}>
              <Field label="Your passphrase"
                     hint={hello ? "Forgotten? Sign in with Windows Hello, then reset it in Settings."
                       : "Forgotten? Reset it with your Windows sign-in, below."}>
                <input type="password" autoFocus value={words} disabled={busy}
                       onChange={(e) => setWords(e.target.value)} />
              </Field>
              <Actions label="Sign in" busy={busy} ready={words !== ""} onContinue={() => void typed()}
                       onSkip={hello ? () => { setTyping(false); setError(null); }
                         : () => { setResetting(true); setError(null); }}
                       skipLabel={hello ? "Use Windows Hello" : "Forgotten it?"} />
            </form>
          )}
          <ErrorLine text={error} />
        </div>
      </div>
    </div>
  );
}
