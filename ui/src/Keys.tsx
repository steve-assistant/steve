/**
 * Settings: Accounts, the models' keys. Setup was the only place to give
 * one, so a key skipped there, or one to change, took a PowerShell command,
 * and the first install found nowhere in the app to see or change a key
 * (22 October 2026). A new key is checked with its service before it is
 * kept, by the route setup uses (`/setup/key`, agent/worker/setup.py), and
 * the next request to the models carries it (agent/provider.py).
 */
import { useEffect, useState } from "react";
import { postJSON, request } from "./api";

type Service = "openai" | "openrouter";

const KEYS: { service: Service; label: string; without: string }[] = [
  { service: "openai", label: "OpenAI key", without: "Steve can’t answer without it." },
  { service: "openrouter", label: "OpenRouter key, for Jev",
    without: "Steve runs without it, but much of it then works only in part." },
];

interface Said {
  ok: boolean;
  error?: string;
  credit?: number;
}

export function Keys({ onScreen }: { onScreen: boolean }) {
  const [kept, setKept] = useState<Record<Service, boolean> | null>(null);
  // Bumped by each key kept, to read which are again.
  const [changes, setChanges] = useState(0);

  useEffect(() => {
    if (!onScreen) return;
    let gone = false;
    void (async () => {
      try {
        const said = (await (await request("/setup/keys")).json()) as {
          ok: boolean; keys?: Record<Service, boolean> };
        if (!gone && said.ok && said.keys) setKept(said.keys);
      } catch {
        // Unread, the section waits; the accounts above are shown regardless.
      }
    })();
    return () => {
      gone = true;
    };
  }, [onScreen, changes]);

  if (!kept) return null;
  return (
    <section className="keys">
      <h3>Your keys</h3>
      <p className="muted small setting-note">
        Steve thinks with OpenAI’s models, on your OpenAI key. Jev checks what Steve reads and
        what it does on its own, on your OpenRouter key. A new key is checked with its service,
        then kept in Windows’ Credential Manager in place of the old one.
      </p>
      {KEYS.map((k) => (
        <KeyRow key={k.service} {...k} kept={kept[k.service]}
                onKept={() => setChanges((n) => n + 1)} />
      ))}
    </section>
  );
}

function KeyRow({ service, label, without, kept, onKept }: {
  service: Service;
  label: string;
  without: string;
  kept: boolean;
  onKept: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [works, setWorks] = useState<string | null>(null);

  const check = async () => {
    setBusy(true);
    setError(null);
    let said: Said;
    try {
      said = (await (await postJSON("/setup/key", { service, key })).json()) as Said;
    } catch {
      said = { ok: false, error: "Steve didn’t answer. Check it is running, then try again." };
    }
    setBusy(false);
    if (!said.ok) {
      setError(said.error ?? "The key wasn’t kept.");
      return;
    }
    setKey("");
    setOpen(false);
    setWorks(typeof said.credit === "number"
      ? `Kept, and it works. $${said.credit.toFixed(2)} of credit left.`
      : "Kept, and it works.");
    onKept();
  };

  const close = () => {
    setOpen(false);
    setKey("");
    setError(null);
  };

  return (
    <div className="key-row" data-key={service}>
      <div className="row">
        <span className="row-title">
          <strong>{label}</strong>
          <span className="muted small">{kept ? " · kept" : " · not given"}</span>
        </span>
        {!open && (
          <button type="button" onClick={() => { setOpen(true); setWorks(null); }}>
            {kept ? "Replace" : "Add"}
          </button>
        )}
      </div>
      {!kept && !open && <p className="muted small">{without}</p>}
      {open && (
        <div className="account-form">
          <div className="field">
            <span className="small">The new key</span>
            <input type="password" autoComplete="off" spellCheck={false} value={key} autoFocus
                   aria-label={`The new ${label}`}
                   onChange={(e) => setKey(e.target.value)}
                   onKeyDown={(e) => e.key === "Enter" && key.trim() && !busy && void check()} />
          </div>
          <div className="item-actions">
            <button type="button" className="primary" disabled={busy || !key.trim()}
                    onClick={() => void check()}>
              {busy ? "Checking…" : "Check and keep"}
            </button>
            <button type="button" disabled={busy} onClick={close}>Cancel</button>
          </div>
        </div>
      )}
      {works && <div className="small" role="status">{works}</div>}
      {error && <div className="warn-text small" role="alert">{error}</div>}
    </div>
  );
}
