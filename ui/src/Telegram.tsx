import { useEffect, useState } from "react";
import { api, type TelegramState } from "./api";

/**
 * Telegram, for a user who does not use Signal: their own bot, given by its
 * token, then paired to their chat by a code sent to it
 * (agent/worker/pairing.py). Step 9 designs the screen; this is the least
 * that makes it usable, and says plainly what Telegram is not.
 */
export function TelegramPanel() {
  const [state, setState] = useState<TelegramState | null>(null);
  const [token, setToken] = useState("");
  const [code, setCode] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [said, setSaid] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const now = state?.state;

  // Every 2 s while a code waits, so pairing shows at once; every 10 s otherwise.
  useEffect(() => {
    const controller = new AbortController();
    const ask = async () => {
      try {
        setState((await api.telegramStatus(controller.signal)).bridge);
      } catch {
        // The worker restarting: the next round asks again.
      }
    };
    void ask();
    const timer = window.setInterval(() => void ask(), now === "pairing" ? 2000 : 10000);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, [now]);

  useEffect(() => {
    if (now !== "pairing") setCode(null);
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

  const give = () => act(async () => {
    const answer = await api.telegramToken(token.trim());
    setToken("");
    if (answer.error) setSaid(answer.error);
    if (answer.bridge) setState(answer.bridge);
  });

  const pair = () => act(async () => {
    const answer = await api.telegramPair();
    if (answer.code) {
      setCode(answer.code);
      setState({ state: "pairing", bot: answer.bot, wait_s: answer.wait_s });
    } else {
      setSaid(answer.error ?? "Telegram gave no code.");
    }
  });

  const unpair = () => act(async () => {
    setConfirming(false);
    const answer = await api.telegramUnpair();
    setState(answer.bridge);
    setSaid(answer.goodbye);
  });

  const bot = state?.bot ? `@${state.bot}` : "your bot";

  return (
    <section>
      <h3>Telegram</h3>
      {now !== "ready" && (
        <p className="muted small">
          Instead of Signal, if you prefer. Telegram is not end-to-end encrypted: its servers read
          every message, and every card Steve sends you. And it keeps a message for a computer
          that is asleep for only a day. Steve answers one chat with a bot of your own, and ignores
          everyone else who writes to it.
        </p>
      )}

      {state === null && <p className="muted small">Asking…</p>}
      {(now === "cold" || now === "starting") && <p>Starting Telegram…</p>}

      {(now === "unavailable" || now === "failed") && (
        <div className="knob-group">
          <p className="small">
            In Telegram, message @BotFather, send /newbot, and follow its steps. Paste the token
            it gives you here.
          </p>
          <input type="password" placeholder="The bot's token" value={token}
                 onChange={(e) => setToken(e.target.value)} />
          <div className="brakes">
            <button className="primary" disabled={busy || token.trim().length === 0}
                    onClick={() => void give()}>
              Use this bot
            </button>
          </div>
        </div>
      )}

      {now === "unpaired" && (
        <div className="brakes">
          <button className="primary" disabled={busy} onClick={() => void pair()}>
            Pair your chat
          </button>
          <span className="muted small">Steve's bot: {bot}</span>
        </div>
      )}

      {now === "pairing" && (
        <div>
          {code ? (
            <p>
              Send <code>{code}</code> to {bot} in Telegram, within{" "}
              {Math.round((state?.wait_s ?? 600) / 60)} minutes.
            </p>
          ) : (
            <p className="muted small">Press "Show a new code" to see the code here.</p>
          )}
          <button disabled={busy} onClick={() => void pair()}>Show a new code</button>
        </div>
      )}

      {now === "ready" && (
        <>
          <p>Paired with {bot}. Cards come with Yes and No buttons; send /unpair to stop.</p>
          <div className="brakes">
            {confirming ? (
              <>
                <button className="danger" disabled={busy} onClick={() => void unpair()}>
                  Unpair, and forget this chat
                </button>
                <button onClick={() => setConfirming(false)}>Cancel</button>
              </>
            ) : (
              <button disabled={busy} onClick={() => setConfirming(true)}>Unpair</button>
            )}
          </div>
        </>
      )}

      {state?.error && !said && <p className="warn-text small">{state.error}</p>}
      {said && <p className="small">{said}</p>}
    </section>
  );
}
