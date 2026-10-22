/* The quick bar (presence.md, The quick bar): a slip over whatever the user
   is doing, on the one hotkey's tap, to ask or tell Steve something and go
   back to work; on its hold, to talk.

   The old bar was a chat box with the cards under it. Nothing in it said
   what was waiting, what was next or what Steve had last done, so the user
   opened the app to find out, which is what the bar is there to spare
   them. Now, before typing, it lists the notes waiting (answered in place),
   the next event or reminder, and Steve's last entry; after sending, the
   answer, with the journal's working line while it comes.

   Its own window (`quickbar` in tauri.conf.json), shown by the main window,
   which holds the hotkey (`show_quickbar`, `quickbar_talk` in lib.rs). It
   talks on its own socket, so a held key's words and answer show here,
   where the user is looking, and not in a window they aren't; the exchange
   is on today's page too. A new conversation starts after FRESH_AFTER_MS
   without a word, so "remind me at 6" this afternoon is not read as part
   of this morning's thread. */

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { AgentSocket, api, isTauri, type ServerEvent } from "./api";
import { applyAppearance, readAppSettings } from "./appSettings";
import { control, type LaterWhen, type Need, type Next } from "./control";
import { openAppFromQuickbar, tellWindows } from "./desktop";
import { clock, firstLine, stevesLine } from "./journal";
import { useVoice } from "./level";
import { Mark, type MarkState } from "./Mark";
import { Prose } from "./Prose";
import { onSession, sessionNow, signedOut } from "./session";
import "./needs.css";
import "./quickbar.css";

// The window's width (tauri.conf.json), and its height at most this share
// of the screen's, then the slip scrolls.
const WIDTH = 640;
const TALLEST = 0.6;
const FRESH_AFTER_MS = 30 * 60 * 1000;
// Closed with words unsent, they wait this long for the next opening.
const DRAFT_MS = 10 * 60 * 1000;
const SHOWN = 3; // exchanges kept on screen
// Rows under the input before typing: notes, then what's next, then
// Steve's last entry.
const ROWS = 4;
const LEAVE_MS = 320; // a note's peel (needs.css), then it is gone

const KICKER: Record<Need["kind"], string> = {
  yes: "Needs you", code: "Needs you", now: "Now", stopped: "Stopped",
};
/** Whether the bar asks a yes on this note itself. A grouped card's items
 * are ticked off in the app: a yes here, under "untick any to leave out",
 * took them all. A high-risk card's passphrase or Hello is asked there too.
 * The rest show whole beside their Yes (`quickbar-whole`): a send's first
 * line names only the account, and the bar's yes was asked without its To:,
 * Cc: or Subject:, cut to the row's one line. */
function asksYesHere(need: Need): boolean {
  return need.actions.includes("yes") && need.kind !== "code" && !need.items;
}

const LATER: [LaterWhen, string][] = [
  ["15m", "15 min"], ["1h", "An hour"], ["evening", "This evening"], ["morning", "Tomorrow"],
];

interface Exchange {
  /** What was typed or heard; none for words that never came. */
  asked?: string;
  answer?: string;
  error?: string;
}

type Say = "yes" | "no" | "done" | "not_now" | LaterWhen;

/** The last exchange, answered, if it is still waiting; else a new one. */
function answered(all: Exchange[], with_: Exchange): Exchange[] {
  const last = all[all.length - 1];
  if (!last || last.answer !== undefined || last.error) return [...all.slice(-(SHOWN - 1)), with_];
  return [...all.slice(0, -1), { ...last, ...with_ }];
}

/** "Design review, 15:00": the sooner of the next event and reminder. */
function nextLine(next: Next | null): string | null {
  const event = next?.event ?? null;
  const reminder = next?.reminder ?? null;
  const eventAt = event ? new Date(event.start).getTime() : Infinity;
  const reminderAt = reminder ? new Date(reminder.at).getTime() : Infinity;
  if (event && eventAt <= reminderAt) return `${event.summary || "An event"}, ${clock(event.start)}`;
  if (reminder) return `${reminder.text}, ${clock(reminder.at)}`;
  return null;
}

/** A reply that reached a closed bar: the desk card opens to show it
 *  (presence.md, The quick bar). The bar never opens again on its own. */
async function toCardIfClosed(text: string | null | undefined): Promise<void> {
  if (!isTauri() || !text?.trim()) return;
  const { getCurrentWindow } = await import("@tauri-apps/api/window");
  if (await getCurrentWindow().isVisible().catch(() => true)) return;
  await tellWindows("deskcard-reply", firstLine(text));
}

async function hideSelf(): Promise<void> {
  if (!isTauri()) return;
  const { getCurrentWindow } = await import("@tauri-apps/api/window");
  await getCurrentWindow().hide();
}

export function QuickBar() {
  const [text, setText] = useState("");
  const [exchanges, setExchanges] = useState<Exchange[]>([]);
  const [thinking, setThinking] = useState(false);
  const [working, setWorking] = useState<string | null>(null);
  const [listening, setListening] = useState(false);
  const voiceNow = useVoice();
  const [connected, setConnected] = useState(false);
  const [needs, setNeeds] = useState<Need[]>([]);
  const [next, setNext] = useState<Next | null>(null);
  const [last, setLast] = useState<string | null>(null);
  const [answering, setAnswering] = useState<string | null>(null);
  const [leaving, setLeaving] = useState<string[]>([]);
  const [later, setLater] = useState<string | null>(null);
  const [failed, setFailed] = useState<{ id: string; error: string } | null>(null);
  // Signed out (security model, section 4.2): the bar shows nothing of
  // Steve's and hears nothing, and sends the user to the app to sign in.
  // It never signs in itself: a slip over another window is no place to
  // type a passphrase.
  const [out, setOut] = useState(false);
  const outRef = useRef(false);
  const signedInRef = useRef<string | null>(null);
  const socketRef = useRef<AgentSocket | null>(null);
  const sessionRef = useRef<number | null>(null);
  const lastUsedRef = useRef(0);
  const closedRef = useRef(0);
  const listeningRef = useRef(false);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const boxRef = useRef<HTMLDivElement>(null);
  const heightRef = useRef(0);

  // What the rows show, asked each time the bar opens and after an answer,
  // and the theme and Reading the user chose: without them the bar
  // followed Windows' light or dark, whatever the app was in.
  const refresh = useCallback(async () => {
    const away = await signedOut();
    outRef.current = away;
    setOut(away);
    if (away) return;
    const [needsNow, nextNow, day] = await Promise.all([
      control.needs().catch(() => null),
      control.next().catch(() => null),
      control.day().catch(() => null),
      readAppSettings().then(async (settings) => {
        if (!settings) return;
        const quiet = settings.theme === "ink-in-quiet-hours"
          && ((await control.autonomy().catch(() => null))?.quiet_hours.now ?? false);
        applyAppearance(settings, quiet);
      }),
    ]);
    if (needsNow) setNeeds(needsNow.needs);
    setNext(nextNow);
    const lines = (day?.entries ?? []).map(stevesLine).filter((l): l is string => !!l?.trim());
    setLast(lines.length ? firstLine(lines[lines.length - 1]) : null);
  }, []);

  const connect = useCallback(() => {
    socketRef.current?.dispose();
    const socket = new AgentSocket({
      onOpen: () => setConnected(true),
      onClose: () => setConnected(false),
      onEvent: (event: ServerEvent) => {
        switch (event.type) {
          case "reply":
            sessionRef.current = event.session_id;
            setExchanges((all) => answered(all, { answer: event.text }));
            setThinking(false);
            setWorking(null);
            void refresh();
            void toCardIfClosed(event.text);
            break;
          case "doing":
            if (event.of === "turn") setWorking(event.text);
            break;
          case "voice_heard":
            setExchanges((all) => [...all.slice(-(SHOWN - 1)), { asked: event.heard }]);
            break;
          case "voice_turn":
            setExchanges((all) => answered(all, { answer: event.reply }));
            setThinking(false);
            setWorking(null);
            void refresh();
            void toCardIfClosed(event.reply);
            break;
          case "voice_empty":
            setExchanges((all) => answered(all, {
              error: event.reason === "nothing heard" ? "Didn't catch that." : event.reason,
            }));
            setThinking(false);
            break;
          case "voice_unavailable":
            listeningRef.current = false;
            setListening(false);
            setThinking(false);
            setExchanges((all) => answered(all, {
              error: event.warm.state === "warming" ? "The speech models are still loading."
                : `Voice unavailable: ${event.warm.error ?? event.warm.state}`,
            }));
            break;
          case "error":
            setExchanges((all) => answered(all, { error: event.error }));
            setThinking(false);
            setWorking(null);
            break;
        }
      },
    });
    socket.connect();
    socketRef.current = socket;
  }, [refresh]);

  useEffect(() => {
    connect();
    return () => socketRef.current?.dispose();
  }, [connect]);

  // Signed in or out from the start, and at every change, so a hold of the
  // key before the bar was first shown is refused too.
  useEffect(() => {
    let off = () => {};
    let gone = false;
    const check = () => {
      void signedOut().then((away) => {
        outRef.current = away;
        setOut(away);
      });
      // Carried by a press: the backend opens no microphone without it.
      void sessionNow().then((session) => {
        signedInRef.current = session;
      });
    };
    check();
    void onSession(check).then((stop) => {
      if (gone) stop();
      else off = stop;
    });
    return () => {
      gone = true;
      off();
    };
  }, []);

  // Push-to-talk on this bar's own socket. The hold's release, Enter or
  // Esc ends it; there is no cancel (routes.py), so each sends what was
  // heard, and an answer to a closed bar lands on the page.
  const talk = useCallback((on: boolean) => {
    if (on) {
      if (listeningRef.current || outRef.current) return;
      const session = signedInRef.current;
      if (!socketRef.current?.send({ type: "voice_start", ...(session ? { session } : {}) })) {
        setExchanges((all) => answered(all, { error: "Steve isn't answering right now." }));
        return;
      }
      lastUsedRef.current = Date.now();
      listeningRef.current = true;
      setListening(true);
    } else if (listeningRef.current) {
      socketRef.current?.send({ type: "voice_stop" });
      listeningRef.current = false;
      setListening(false);
      setThinking(true);
    }
  }, []);

  // Closed, the bar stops listening, as Esc does. The tray's and the desk
  // card's Talk hold no key to let go of: hidden by a click elsewhere, the
  // bar kept the microphone open until the backend's two-minute cap.
  const close = useCallback(() => {
    talk(false);
    closedRef.current = Date.now();
    void hideSelf();
  }, [talk]);

  // Shown: a fade and a rise, the box focused, a draft dropped after
  // DRAFT_MS, and a fresh conversation after FRESH_AFTER_MS.
  useEffect(() => {
    if (!isTauri()) return;
    const offs: (() => void)[] = [];
    let gone = false;
    void (async () => {
      const { listen } = await import("@tauri-apps/api/event");
      const shown = await listen("quickbar-shown", () => {
        if (closedRef.current && Date.now() - closedRef.current > DRAFT_MS) setText("");
        if (lastUsedRef.current && Date.now() - lastUsedRef.current > FRESH_AFTER_MS) {
          sessionRef.current = null;
          setExchanges([]);
          connect(); // a socket holds its conversation: a new one is a new thread
        }
        setFailed(null);
        setLater(null);
        void refresh();
        rise(boxRef.current);
        inputRef.current?.focus();
      });
      const talked = await listen<boolean>("quickbar-talk", ({ payload }) => talk(payload));
      if (gone) {
        shown();
        talked();
      } else offs.push(shown, talked);
    })();
    return () => {
      gone = true;
      offs.forEach((off) => off());
    };
  }, [connect, refresh, talk]);

  // Clicked away: gone, as a launcher goes. An answer still coming carries
  // on, and lands on the page.
  useEffect(() => {
    if (!isTauri()) return;
    let off = () => {};
    void (async () => {
      const { getCurrentWindow } = await import("@tauri-apps/api/window");
      off = await getCurrentWindow().onFocusChanged(({ payload: focused }) => {
        if (!focused) close();
      });
    })();
    return () => off();
  }, [close]);

  // The window is as tall as what it shows, up to TALLEST of the screen.
  useLayoutEffect(() => {
    if (!isTauri() || !boxRef.current) return;
    const tallest = Math.round(window.screen.availHeight * TALLEST);
    boxRef.current.style.maxHeight = `${tallest}px`;
    const height = Math.min(Math.ceil(boxRef.current.scrollHeight) + 2, tallest);
    if (height === heightRef.current) return;
    heightRef.current = height;
    void (async () => {
      const { getCurrentWindow, LogicalSize } = await import("@tauri-apps/api/window");
      await getCurrentWindow().setSize(new LogicalSize(WIDTH, height));
    })();
  });

  const send = useCallback((): boolean => {
    const asked = text.trim();
    if (!asked || thinking) return false;
    const sent = socketRef.current?.send({
      type: "chat",
      text: asked,
      session_id: sessionRef.current,
    });
    if (!sent) {
      setExchanges((all) => answered(all, { asked, error: "Steve isn't answering right now." }));
      return false;
    }
    lastUsedRef.current = Date.now();
    setExchanges((all) => [...all.slice(-(SHOWN - 1)), { asked }]);
    setText("");
    setThinking(true);
    return true;
  }, [text, thinking]);

  // A note answered here peels, as on the page, and the list is asked again.
  const answer = useCallback(async (need: Need, say: Say) => {
    setAnswering(need.id);
    setFailed(null);
    let done: { ok: boolean; error?: string };
    try {
      if (say === "yes" || say === "no") {
        done = need.action_id === undefined ? { ok: false, error: "That isn't a card." }
          : await api.decideAction(need.action_id, say === "yes", {}, undefined, need.seen);
      } else if (say === "done") {
        done = await control.needDone(need.id);
      } else if (say === "not_now") {
        done = await control.needNotNow(need.id);
      } else {
        done = await control.needLater(need.id, say);
      }
    } catch (err) {
      done = { ok: false, error: String(err) };
    }
    setAnswering(null);
    setLater(null);
    if (!done.ok) {
      setFailed({ id: need.id, error: done.error ?? "That didn't go through." });
      return;
    }
    setLeaving((all) => [...all, need.id]);
    window.setTimeout(() => {
      setNeeds((all) => all.filter((n) => n.id !== need.id));
      setLeaving((all) => all.filter((id) => id !== need.id));
      void refresh();
    }, LEAVE_MS);
  }, [refresh]);

  const openApp = useCallback(() => {
    closedRef.current = Date.now();
    void openAppFromQuickbar();
  }, []);

  const state: MarkState = !connected ? "starting" : listening ? "listening"
    : voiceNow === "speaking" ? "speaking" : thinking ? "working" : "idle";

  // Before typing: the notes first, then what's next and Steve's last
  // entry, those two only before anything is asked.
  const quiet = text === "" && !listening;
  const extras = [
    ...(exchanges.length === 0 && nextLine(next) ? [{ key: "next", line: `Next · ${nextLine(next)}` }] : []),
    ...(exchanges.length === 0 && last ? [{ key: "last", line: last }] : []),
  ];
  const room = Math.max(1, ROWS - extras.length);
  const notes = needs.length > room ? needs.slice(0, room - 1) : needs;
  const more = needs.length - notes.length;

  if (out) {
    return (
      <div className="quickbar" ref={boxRef}
           onKeyDown={(e) => { if (e.key === "Escape") { e.preventDefault(); close(); } }}>
        <div className="quickbar-input quickbar-out">
          <Mark size={24} state="idle" />
          <p className="quickbar-line t-small">Sign in to Steve first.</p>
          <button type="button" className="note-main" autoFocus onClick={openApp}>Open Steve</button>
        </div>
      </div>
    );
  }

  return (
    <div className="quickbar" ref={boxRef}>
      <div className="quickbar-input">
        <Mark size={24} state={state} needsYou={needs.length > 0}
              label={connected ? undefined : "Steve isn't answering right now"} />
        <textarea
          ref={inputRef}
          rows={1}
          autoFocus
          value={text}
          aria-label="Ask or tell Steve"
          placeholder={listening ? "Listening…" : "Ask or tell Steve"}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              if (listening) talk(false);
              else if (send() && e.ctrlKey) openApp();
            } else if (e.key === "Escape") {
              e.preventDefault();
              close();
            }
          }}
        />
      </div>

      {listening && (
        <p className="quickbar-listening t-small" aria-live="polite">
          Let go of the key, or press Enter, to send.
        </p>
      )}

      {exchanges.map((x, i) => (
        <div key={i} className="quickbar-exchange">
          {x.asked && <p className="quickbar-asked t-small">{x.asked}</p>}
          {/* Marked by what was asked, not its place, which moves as
              older exchanges leave; an answer always arrives in view. */}
          {x.answer !== undefined && (
            <Prose className="quickbar-answer t-small" text={x.answer}
                   id={`quick:${x.asked ?? ""}`} drawIn />
          )}
          {x.error && <p className="quickbar-answer quickbar-error t-small">{x.error}</p>}
        </div>
      ))}
      {thinking && (
        <p className="quickbar-working t-small" aria-live="polite">
          <span className="quickbar-sign turning" aria-hidden />
          {working ?? ""}
        </p>
      )}

      {quiet && (notes.length > 0 || extras.length > 0) && (
        <ul className="quickbar-rows">
          {notes.map((need) => {
            const asks = asksYesHere(need);
            return (
            <li key={need.id}
                className={`quickbar-row quickbar-note${asks ? " quickbar-whole" : ""}${
                  leaving.includes(need.id) ? " note-answered" : ""}`}>
              <span className="quickbar-kicker hand-kicker">{KICKER[need.kind]}</span>
              <span className="quickbar-line" title={need.text}>
                {failed?.id === need.id ? <span className="quickbar-error">{failed.error}</span>
                  : asks ? <>
                    {firstLine(need.text)}
                    {need.detail && <span className="quickbar-detail">{need.detail}</span>}
                  </> : firstLine(need.text)}
              </span>
              <span className="quickbar-actions">
                {later === need.id ? LATER.map(([when, label]) => (
                  <button key={when} type="button" className="note-other"
                          disabled={answering === need.id}
                          onClick={() => void answer(need, when)}>{label}</button>
                )) : need.actions.map((act) => {
                  const still = answering === need.id || leaving.includes(need.id);
                  if (act === "yes" && asks) {
                    return <button key={act} type="button" className="note-main" disabled={still}
                                   onClick={() => void answer(need, "yes")}>Yes</button>;
                  }
                  if (act === "done") {
                    return <button key={act} type="button" className="note-main" disabled={still}
                                   onClick={() => void answer(need, "done")}>Done</button>;
                  }
                  if (act === "no" || act === "not_now") {
                    return <button key={act} type="button" className="note-other" disabled={still}
                                   onClick={() => void answer(need, act)}>
                      {act === "no" ? "No" : "Not now"}
                    </button>;
                  }
                  if (act === "later") {
                    return <button key={act} type="button" className="note-other" disabled={still}
                                   onClick={() => setLater(need.id)}>Later</button>;
                  }
                  // A passphrase's yes, or a fix: done in the app.
                  return <button key={act} type="button" className="note-other" disabled={still}
                                 onClick={openApp}>Open Steve</button>;
                })}
              </span>
            </li>
            );
          })}
          {more > 0 && (
            <li className="quickbar-row quickbar-note">
              <button type="button" className="note-other" onClick={openApp}>
                {more} more {more === 1 ? "note" : "notes"} in Steve
              </button>
            </li>
          )}
          {extras.map(({ key, line }) => (
            <li key={key} className={`quickbar-row quickbar-${key} t-small`} title={line}>{line}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** The opening: a fade and an 8px rise, as the tokens say (none when
    Windows' animation effects are off, --rise-window 0). */
function rise(box: HTMLElement | null): void {
  if (!box?.animate) return;
  const tokens = getComputedStyle(document.documentElement);
  const by = tokens.getPropertyValue("--rise-window").trim() || "0px";
  const ms = parseFloat(tokens.getPropertyValue("--t-window")) || 0;
  box.animate([{ opacity: 0, transform: `translateY(${by})` }, { opacity: 1, transform: "none" }],
              { duration: ms, easing: "ease-out" });
}
