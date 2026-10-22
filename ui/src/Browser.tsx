import { useCallback, useEffect, useRef, useState } from "react";
import { isTauri } from "./api";
import { LiveView } from "./LiveView";
import { control } from "./control";
import { Check, Section } from "./Form";
import { type Say, usePolled } from "./pages";
import {
  closeSignin,
  onSignin,
  openSignin,
  signinCookies,
  signinTyped,
  type SigninState,
} from "./desktop";
import {
  covers,
  hostOf,
  logins,
  looksLikeSignIn,
  pickTyped,
  suggestName,
  type Login,
} from "./logins";

interface Props {
  toast: Say;
  onScreen: boolean;
}

// LinkedIn signs in through the sandbox's live view: its own window's
// cookies came back host-only and looped on redirects (agent/browser/live.py).
const LINKEDIN_LOGIN = "https://www.linkedin.com/login";

type Form = { name: string; domains: string; tier: "ask" | "free"; keepTyped: boolean;
              username: string; password: string };

/**
 * Saving a login (the maintainer, 4 October 2026: "build a better way for me
 * to save browser logins"). The site opens in the desktop app's own private
 * window, where the user signs in as in any browser -- paste, Duo, passkeys, Windows Hello.
 * The name and domains fill in from the pages the sign-in passed through,
 * the username and password are the ones the user typed there, and closing the
 * window saves it: nothing to type twice, nothing to press. Only that
 * login's cookies go to the vault (agent/browser/live.py `import_login`).
 * The sandbox's live view stays below, for what the window cannot do.
 */
export function Browser({ toast, onScreen }: Props) {
  const [saved, setSaved] = useState<Login[]>([]);
  // LinkedIn's step shows only with the sandboxed browser switched on, and
  // asks first that the user accept the risk to their account.
  const switches = usePolled((signal) => control.autonomy(signal), { everyMs: 60000, onScreen });
  const browserOn = switches.data?.knobs.find((k) => k.name === "BROWSER")?.value === "on";
  const [risked, setRisked] = useState(false);
  const [liveStart, setLiveStart] = useState("");
  const [address, setAddress] = useState("");
  const [open, setOpen] = useState(false);
  const [hosts, setHosts] = useState<string[]>([]);
  const [current, setCurrent] = useState("");
  const [form, setForm] = useState<Form>({ name: "", domains: "", tier: "ask", keepTyped: true,
                                           username: "", password: "" });
  const [typedSoFar, setTypedSoFar] = useState<{ host: string; username: string; has_password: boolean }[]>([]);
  const [held, setHeld] = useState<SigninState | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [sandbox, setSandbox] = useState(!isTauri());
  // Their edits win: once the user types in a box, the window stops filling it.
  const touched = useRef({ name: false, domains: false });
  const seen = useRef<string[]>([]);
  // The form as it is now, for the window's "finished" event, which is
  // listened to once and would otherwise see the form as it first was.
  const formRef = useRef(form);
  useEffect(() => {
    formRef.current = form;
  }, [form]);

  const set = (patch: Partial<Form>) => setForm((f) => ({ ...f, ...patch }));

  const refresh = useCallback(async () => {
    try {
      setSaved(await logins.list());
    } catch {
      /* supervisor may be mid-swap */
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  /** Save from what the window held: its page, cookies and what the user typed. */
  const saveFrom = useCallback(
    async (state: SigninState): Promise<boolean> => {
      const f = formRef.current;
      setError("");
      if (state.error) {
        setError(`Could not read the window: ${state.error}`);
        return false;
      }
      const list = f.domains.toLowerCase().split(/[\s,]+/).filter(Boolean);
      const here = hostOf(state.url);
      if (here && !covers(list, here)) {
        setError(`The window was on ${here}, which is not in these domains. Add it, then save.`);
        return false;
      }
      // Typed into the form here, they win; otherwise what the user typed in the
      // window, if the user wants it kept.
      const fromWindow = f.keepTyped && !f.password ? pickTyped(state.typed ?? [], list) : null;
      const r = await logins.save({
        name: f.name, domains: f.domains, tier: f.tier, url: state.url, cookies: state.cookies,
        username: f.password ? f.username : fromWindow?.username ?? "",
        password: f.password ? f.password : fromWindow?.password ?? "",
        credentials_from: fromWindow?.host,
      });
      if (!r.ok) {
        setError(r.error ?? "not saved");
        return false;
      }
      setHeld(null);
      setOpen(false);
      set({ password: "" });
      toast(`Saved ${r.site?.name}${r.site?.has_password ? ", with its password" : ""}.`);
      await refresh();
      return true;
    },
    [refresh, toast],
  );

  useEffect(() => {
    let off = () => {};
    void onSignin({
      // Every host the sign-in passes through fills the domains, until the
      // user edits them: a school's portal goes portal.example.edu, its
      // single sign-on, then the course site, and the session lives on the last.
      onNavigated: (url) => {
        const host = hostOf(url);
        if (!host || !host.includes(".") || /^[\d.]+$/.test(host)) return;
        if (seen.current.includes(host)) return;
        seen.current = [...seen.current, host];
        setHosts(seen.current);
        setForm((f) => ({
          ...f,
          domains: touched.current.domains ? f.domains : seen.current.slice(0, 12).join(" "),
          name: touched.current.name || f.name ? f.name : suggestName(seen.current[0]),
        }));
      },
      onPage: (url) => setCurrent(url),
      // The user closed the window: that is "done". Saved at once if it ended up
      // signed in; held, with a button, if it was closed on a sign-in page.
      onFinished: (state) => {
        setOpen(false);
        if (state.error || looksLikeSignIn(state.url)) {
          setHeld(state);
          setError(state.error ? `Could not read the window: ${state.error}`
            : "The window was closed on a sign-in page, so nothing was saved.");
          return;
        }
        void saveFrom(state).then((ok) => { if (!ok) setHeld(state); });
      },
      onClosed: () => setOpen(false),
    }).then((dispose) => {
      off = dispose;
    });
    return () => off();
  }, [saveFrom]);

  // While the window is open, what it has seen them type -- the username and
  // whether a password, never the password.
  useEffect(() => {
    if (!open) return;
    const timer = window.setInterval(() => void signinTyped().then(setTypedSoFar), 1000);
    return () => window.clearInterval(timer);
  }, [open]);

  const start = async (url: string, login?: Login) => {
    setError("");
    setHeld(null);
    const target = /^https?:\/\//i.test(url.trim()) ? url.trim() : `https://${url.trim()}`;
    touched.current = { name: Boolean(login), domains: Boolean(login) };
    seen.current = [];
    setHosts([]);
    setCurrent("");
    setTypedSoFar([]);
    setForm({ name: login?.name ?? "", domains: login ? login.domains.join(" ") : "",
              tier: login?.tier ?? "ask", keepTyped: true, username: "", password: "" });
    const r = await openSignin(target);
    if (!r.ok) {
      setError(r.error ?? "could not open the sign-in window");
      return;
    }
    setOpen(true);
  };

  const saveNow = async () => {
    setBusy(true);
    try {
      const state = held ?? (await signinCookies());
      if (await saveFrom(state)) await closeSignin();
    } catch (err) {
      setError(String(err));
    } finally {
      setBusy(false);
    }
  };

  const changeTier = async (login: Login, tier: "ask" | "free") => {
    const r = await logins.setTier(login.name, tier);
    if (!r.ok) toast(r.error ?? "Not changed.", false);
    else toast(`${login.name}: ${tier === "free" ? "the agent just uses it now" : "it asks you first now"}.`);
    await refresh();
  };

  const forget = async (login: Login) => {
    await logins.forget(login.name);
    toast(`Forgot ${login.name}.`);
    await refresh();
  };

  const domainList = form.domains.toLowerCase().split(/[\s,]+/).filter(Boolean);
  const willKeep = typedSoFar.find((t) => t.has_password && covers(domainList, t.host))
    ?? typedSoFar.find((t) => covers(domainList, t.host));
  const editing = open || held !== null;

  return (
    <div className="panel">
      {isTauri() && (
        <section>
          <h3>Add a login</h3>
          {!editing ? (
            <>
              <div className="answer-row">
                <input placeholder="The site to sign in to, e.g. portal.example.com" value={address}
                       onChange={(e) => setAddress(e.target.value)}
                       onKeyDown={(e) => { if (e.key === "Enter" && address.trim()) void start(address); }} />
                <button className="primary" disabled={!address.trim()} onClick={() => void start(address)}>
                  Open sign-in window
                </button>
              </div>
              <p className="muted small">
                It opens in a private window of its own. Sign in there as you would anywhere —
                paste, Duo, passkeys and Windows Hello all work — then just close the window: the
                login saves itself, with the username and password you typed. Only that site's
                cookies go to the vault; no model ever sees them or your password.
              </p>
              {error && <div className="warn-text small">{error}</div>}
            </>
          ) : (
            <div className="item">
              <div className="item-body">
                <div className="small">
                  {held ? "The window is closed. " : signedIn(current) ? "Looks signed in — close the window to save. "
                    : "Sign in in the window that opened; close it when you're done. "}
                  {current && <>Now on <strong>{hostOf(current)}</strong></>}
                  {hosts.length > 1 && <span className="muted"> (by way of {hosts.slice(0, -1).join(", ")})</span>}
                </div>
                <div className="knob">
                  <span className="knob-says">Name</span>
                  <input value={form.name} placeholder="e.g. work-portal"
                         onChange={(e) => { touched.current.name = true; set({ name: e.target.value }); }} />
                </div>
                <div className="knob">
                  <span className="knob-says">Its domains (filled in from the pages it went through)</span>
                  <input value={form.domains}
                         onChange={(e) => { touched.current.domains = true; set({ domains: e.target.value }); }} />
                </div>
                <div className="knob">
                  <span className="knob-says">When the agent wants to use it</span>
                  <select value={form.tier} onChange={(e) => set({ tier: e.target.value as "ask" | "free" })}>
                    <option value="ask">Ask me each time</option>
                    <option value="free">Just use it</option>
                  </select>
                </div>
                <Check checked={form.keepTyped}
                       onChange={(on) => set({ keepTyped: on })}
                       label={<>
                         Keep the username and password I type in the window, so it can sign in
                         again without me
                         {willKeep ? ` — ${willKeep.username || "a username"}${willKeep.has_password ? " and a password" : ""}, typed on ${willKeep.host}` : ""}
                       </>} />
                <details>
                  <summary className="small">Or type them here instead</summary>
                  <div className="knob">
                    <span className="knob-says">Username</span>
                    <input value={form.username} autoComplete="off" onChange={(e) => set({ username: e.target.value })} />
                  </div>
                  <div className="knob">
                    <span className="knob-says">Password</span>
                    <input type="password" value={form.password} autoComplete="new-password"
                           onChange={(e) => set({ password: e.target.value })} />
                  </div>
                </details>
                {error && <div className="warn-text small">{error}</div>}
                <div className="item-actions">
                  <button className="primary" disabled={busy} onClick={() => void saveNow()}>
                    {busy ? "Saving…" : held ? "Save anyway" : "Save now"}
                  </button>
                  <button disabled={busy}
                          onClick={() => { void closeSignin(); setOpen(false); setHeld(null); setError(""); }}>
                    {held ? "Discard" : "Cancel"}
                  </button>
                </div>
              </div>
            </div>
          )}
        </section>
      )}

      <section>
        <h3>Saved logins</h3>
        {saved.length === 0 ? (
          <p className="muted small">None yet.</p>
        ) : (
          saved.map((login) => (
            <div key={login.name} className="row">
              <span className="row-title">
                <strong>{login.name}</strong> <span className="muted">{login.domains.join(", ")}</span>
              </span>
              <span className="muted small">
                {login.has_password ? "password kept" : "no password"}
                {login.has_session ? "" : " · signed out"}
              </span>
              <select value={login.tier} title="When the agent wants to use this login"
                      onChange={(e) => void changeTier(login, e.target.value as "ask" | "free")}>
                <option value="ask">Ask me first</option>
                <option value="free">Just use it</option>
              </select>
              {isTauri() && (
                <button onClick={() => void start(`https://${login.domains[0]}`, login)}>Sign in again</button>
              )}
              <button onClick={() => void forget(login)}>Forget</button>
            </div>
          ))
        )}
      </section>

      <section>
        {isTauri() && (
          <button className="link-button" onClick={() => setSandbox((s) => !s)}>
            {sandbox ? "Hide the sandbox view" : "Use the sandbox view instead"}
          </button>
        )}
        {sandbox && <LiveView key={liveStart} start={liveStart} onSaved={() => void refresh()} />}
      </section>

      {browserOn && (
        <Section title="LinkedIn, if you want it" why={
          "Optional. With a LinkedIn login saved, Steve can read a profile before a meeting, a "
          + "company or some jobs, as you, a few pages at a time. It never posts, messages, "
          + "connects or applies."}>
          {saved.some((login) => covers(login.domains, "www.linkedin.com")) ? (
            <p className="form-hint" data-linkedin="saved">
              Signed in. To sign out, forget its login above.
            </p>
          ) : (
            <div data-linkedin="step">
              <p className="form-warning">
                Automated reading may break LinkedIn’s terms, and LinkedIn may restrict or
                close an account it suspects of it. That risk is to your account, so sign in
                only if you accept it.
              </p>
              <Check label="I accept the risk to my LinkedIn account" checked={risked}
                     onChange={setRisked} />
              <div className="form-actions">
                <button type="button" className="form-main" disabled={!risked}
                        onClick={() => {
                          setSandbox(true);
                          setLiveStart(LINKEDIN_LOGIN);
                        }}>
                  Sign in through the live view
                </button>
              </div>
              {liveStart === LINKEDIN_LOGIN && (
                <p className="form-hint">
                  Press Open in the view above, sign in there, then save the login.
                </p>
              )}
            </div>
          )}
        </Section>
      )}
    </div>
  );
}

function signedIn(url: string): boolean {
  return Boolean(url) && !looksLikeSignIn(url);
}
