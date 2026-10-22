import { useEffect, useState } from "react";
import { api, type SignalState } from "./api";

function megabytes(bytes: number): string {
  return `${Math.round(bytes / 1_000_000)} MB`;
}

/**
 * Signal on this computer: set up, linked by a code the phone scans, and
 * unlinked (agent/worker/pairing.py). Until step 4 linking was a command
 * line that printed the code in a terminal, and signal-cli and Java had to
 * be installed by hand first. Step 9 designs the screen; this is the least
 * that makes it usable.
 */
export function SignalPanel() {
  const [state, setState] = useState<SignalState | null>(null);
  const [qr, setQr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [said, setSaid] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const now = state?.state;

  // Every 2 s while something is under way, so progress moves and a scan
  // shows at once; every 10 s otherwise.
  useEffect(() => {
    const controller = new AbortController();
    const ask = async () => {
      try {
        setState((await api.signalStatus(controller.signal)).bridge);
      } catch {
        // The worker restarting: the next round asks again.
      }
    };
    void ask();
    const timer = window.setInterval(() => void ask(),
                                     now === "installing" || now === "linking" ? 2000 : 10000);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, [now]);

  useEffect(() => {
    if (now !== "linking") setQr(null);
  }, [now]);

  const act = async (what: () => Promise<void>) => {
    setBusy(true);
    setSaid(null);
    try {
      await what();
    } catch (err) {
      setSaid(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const install = () => act(async () => {
    const answer = await api.signalInstall();
    if (answer.error) setSaid(answer.error);
    if (answer.bridge) setState(answer.bridge);
  });

  const link = () => act(async () => {
    const answer = await api.signalLink();
    if (answer.qr) {
      setQr(answer.qr);
      setState({ state: "linking", wait_s: answer.wait_s });
    } else {
      setSaid(answer.error ?? "Signal gave no code to scan.");
    }
  });

  const unlink = () => act(async () => {
    setConfirming(false);
    const answer = await api.signalUnlink();
    setState(answer.bridge);
    setSaid(answer.bridge.state === "unlinked" ? answer.goodbye : (answer.bridge.error ?? null));
  });

  const why = state?.error ?? state?.hint;
  const linked = now === "ready" || now === "starting" || now === "failed";

  return (
    <section>
      <h3>Signal</h3>
      {!linked && (
        <p className="muted small">
          Linked, this computer becomes one of your Signal devices, as Signal Desktop does: from
          then on it receives your messages in every chat. Steve reads only Note to Self and acts
          only on what you write there. It ignores the rest, and deletes their attachments as they
          arrive. Type "unlink" in Note to Self, or press Unlink here, to undo it.
        </p>
      )}

      {state === null && <p className="muted small">Asking…</p>}

      {(now === "cold" || now === "starting") && <p>Starting Signal…</p>}

      {now === "unavailable" && (
        <div className="brakes">
          <button className="primary" disabled={busy} onClick={() => void install()}>
            Set up Signal
          </button>
          <span className="muted small">
            Downloads signal-cli and the Java it runs on, about 184 MB, checked against their
            published fingerprints.
          </span>
        </div>
      )}

      {now === "installing" && (
        <div>
          <progress value={state?.done ?? 0} max={state?.of ?? 1} />
          <span className="muted small">
            {" "}Downloading: {megabytes(state?.done ?? 0)} of {megabytes(state?.of ?? 0)}
          </span>
        </div>
      )}

      {now === "unlinked" && (
        <div className="brakes">
          <button className="primary" disabled={busy} onClick={() => void link()}>
            Show a code to scan
          </button>
        </div>
      )}

      {now === "linking" && (
        <div>
          {qr ? (
            <img className="signal-qr" alt="A code to scan with Signal on your phone"
                 src={`data:image/svg+xml;charset=utf-8,${encodeURIComponent(qr)}`} />
          ) : (
            <p className="muted small">Press "Show a new code" to see the code here.</p>
          )}
          <p className="small">
            On your phone: Signal, Settings, Linked devices, Link new device. Then scan this. It
            works for {Math.round((state?.wait_s ?? 300) / 60)} minutes.
          </p>
          <button disabled={busy} onClick={() => void link()}>Show a new code</button>
        </div>
      )}

      {now === "ready" && <p>Linked. Message Steve in Note to Self.</p>}

      {linked && (
        <div className="brakes">
          {confirming ? (
            <>
              <button className="danger" disabled={busy} onClick={() => void unlink()}>
                Unlink, and delete Signal's keys here
              </button>
              <button onClick={() => setConfirming(false)}>Cancel</button>
            </>
          ) : (
            <button disabled={busy} onClick={() => setConfirming(true)}>Unlink</button>
          )}
        </div>
      )}

      {why && !said && <p className="warn-text small">{why}</p>}
      {said && <p className="small">{said}</p>}
    </section>
  );
}
