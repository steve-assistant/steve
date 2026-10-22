import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import {
  AgentSocket,
  api,
  bindTapOrHold,
  formatDuration,
  hotkeyLabel,
  hotkeyProblem,
  DEFAULT_HOTKEY,
  HOTKEY_FALLBACKS,
  formatUSD,
  isTauri,
  type Decided,
  type YesProof,
  type Job,
  type ServerEvent,
  type DeskStatus,
  type VoiceWarm,
} from "./api";
import { control, type Need, type Places, type Verdict } from "./control";
import {
  service,
  serviceStatus,
  setStartsAtLogin,
  comeForward,
  deskCardShowing,
  notify,
  onBackendDamaged,
  onBackendFailing,
  onOpenInbox,
  onShell,
  onTapToggle,
  tellWindows,
  setTapping as setTrayTapping,
  quickbarTalk,
  showQuickbar,
  setWaiting,
  startsAtLogin,
  watchOnScreen,
  checkUpdate,
  installUpdate,
  openLink,
  quit,
  setCaption,
  type ServiceAction,
  type ServiceStatus,
  type UpdateOffer,
} from "./desktop";
import { DayPage, type GoTo } from "./Day";
import { dayName, shifted, stillUnsent, type Unsent, useJournal } from "./journal";
import { StuckNotes } from "./StuckNotes";
import { appNeeds, BACKEND_ID, SAMPLE_ID, useWide, type AppNeed, type NoteAnswer } from "./needs";
import { Power } from "./Power";
import { Accounts } from "./Accounts";
import { Activity } from "./Activity";
import { MayDo, RoutinesAndGoals } from "./Autonomy";
import { Browser } from "./Browser";
import { Channels } from "./Channels";
import { Diagnostics } from "./Diagnostics";
import { Check, Saved, Saying } from "./Form";
import { upload } from "./photos";
import { Knows } from "./Knows";
import { Modules } from "./Modules";
import { CallNotes, ComingUp } from "./Places";
import { useSaid } from "./pages";
import { Profile } from "./Profile";
import { Settings } from "./Settings";
import { Spend } from "./Spend";
import { Tour } from "./Tour";
import { loadSetup, Setup, Waking, type SetupState } from "./Setup";
import { useVoice } from "./level";
import { SignIn } from "./SignIn";
import {
  helloAvailable,
  helloSigned,
  onSession,
  sessionNow,
  signinState,
  type Ended,
  type SigninState,
} from "./session";
import { presence, Shell, type BackPageHead, type MenuGroup, type MenuPage } from "./Shell";
import {
  applyAppearance,
  loadAppSettings,
  saveAppSettings,
  watchTheme,
  type AppSettings,
} from "./appSettings";

// "home" is Today, the journal's day page; the rest are the back pages.
type View =
  | "home"
  | "knows"
  | "profile"
  | "accounts"
  | "channels"
  | "browser"
  | "maydo"
  | "routines"
  | "modules"
  | "coming"
  | "calls"
  | "spend"
  | "activity"
  | "diagnostics"
  | "settings";

// The back pages, in the menu's groups (docs/design/back-pages.md). The
// eleven tabs before them went into these, the journal and the notes: Chat
// and Home are the day page, Approvals are notes, and Tasks are Ongoing.
const MENU: { group: string; pages: { key: View; name: string }[] }[] = [
  { group: "About you", pages: [
    { key: "knows", name: "What Steve knows" },
    { key: "profile", name: "Profile" },
  ] },
  { group: "Connections", pages: [
    { key: "accounts", name: "Accounts" },
    { key: "channels", name: "Channels" },
    { key: "browser", name: "Browser logins" },
  ] },
  { group: "Steve", pages: [
    { key: "maydo", name: "What Steve may do" },
    { key: "routines", name: "Routines and goals" },
    { key: "modules", name: "Extensions" },
    { key: "spend", name: "Spend" },
    { key: "activity", name: "Activity" },
    { key: "diagnostics", name: "Diagnostics" },
    { key: "settings", name: "Settings" },
  ] },
];

const HEADS: Partial<Record<View, BackPageHead>> = {
  ...Object.fromEntries(
    MENU.flatMap(({ group, pages }) => pages.map(({ key, name }) => [key, { group, name }]))),
  // The sidebar's own places, under the days (agent/worker/places.py).
  coming: { group: "Your days", name: "Coming up" },
  calls: { group: "Your days", name: "Call notes" },
};

// A reconnect shorter than this is a worker swap, and shows nothing
// (docs/design/presence.md); longer, the mark says Starting.
const SWAP_MS = 5000;

// How often the app asks anything while it sits hidden in the tray: enough
// for the tray's dot and lines, nothing for pages nobody can see.
const HIDDEN_POLL_MS = 20000;
// OpenAI's balance used up: its note's fix opens the page that tops it up.
const TOP_UP = "https://platform.openai.com/settings/organization/billing/overview";
const HOUR_MS = 3600_000;
// A first start of the backend imports everything once: about ten seconds.
const FIRST_ANSWER_MS = 20000;
// Past that, while the app's own backend is still starting, the wait goes
// on this long, as the supervisor waits for a worker. Opened at 20 s
// regardless, a slow first start (a busy laptop, a cold disk) showed the
// stranger an empty day page, then setup over it.
const STARTING_MS = 120_000;
const STARTING_LOOK_MS = 2000;
// The inbox's kinds that toast even with the window in front: notes (a
// card, a reminder going off) and briefings.
const ALWAYS_TOASTS = new Set(["card", "reminder", "briefing"]);

type AppChange = Partial<Record<keyof AppSettings, string | null>>;

export default function App() {
  const [view, setView] = useState<View>("home");
  // Whether anyone can see the window (`watchOnScreen`): hidden in the tray,
  // the app asks only for what the tray shows.
  const [onScreen, setOnScreen] = useState(true);
  useEffect(() => watchOnScreen(setOnScreen), []);
  // For the socket's handler, which must not be remade (and its socket
  // reopened) each time the window is shown or hidden.
  const onScreenRef = useRef(onScreen);
  onScreenRef.current = onScreen;
  // Whether a toast may show its text: not while signed out, which is set
  // below with the sign-in, nor before the sign-in is known.
  const signedOutRef = useRef(true);
  const [connected, setConnected] = useState(false);
  // Setup until it is done (ui/src/Setup.tsx), asked of the backend each
  // time it connects, so a fresh data folder, or setup closed halfway,
  // opens on it. "unknown" where the backend can't say, and the app opens
  // as before: a setup that can't be read must not lock anyone out.
  const [setup, setSetup] = useState<SetupState | "asking" | "unknown">("asking");
  // The first answer is waited for this long, and longer while the shell
  // says its backend is starting; then the app opens anyway, where Start
  // is, for a backend that doesn't come up.
  const [gaveUpWaiting, setGaveUpWaiting] = useState(false);
  useEffect(() => {
    let gone = false;
    let timer = 0;
    const began = Date.now();
    const look = async () => {
      const state = isTauri() ? (await serviceStatus())?.state : undefined;
      if (gone) return;
      if (state === "starting" && Date.now() - began < STARTING_MS) {
        timer = window.setTimeout(() => void look(), STARTING_LOOK_MS);
      } else {
        setGaveUpWaiting(true);
      }
    };
    timer = window.setTimeout(() => void look(), FIRST_ANSWER_MS);
    return () => {
      gone = true;
      window.clearTimeout(timer);
    };
  }, []);
  useEffect(() => {
    if (!connected) return;
    loadSetup().then(setSetup)
      .catch(() => setSetup((now) => (now === "asking" ? "unknown" : now)));
  }, [connected]);
  // Over five seconds without the backend.
  const [longGone, setLongGone] = useState(false);
  useEffect(() => {
    if (connected) {
      setLongGone(false);
      return;
    }
    const timer = window.setTimeout(() => setLongGone(true), SWAP_MS);
    return () => window.clearTimeout(timer);
  }, [connected]);
  // What Steve is doing, by what it is for: a turn's line, a task's
  // (agent/worker/journal.py). The newest is the sidebar's.
  const [doing, setDoing] = useState<Record<string, string>>({});
  // The user's name at the sidebar's foot, from their profile.
  const [name, setName] = useState("");
  // The title bar in the theme's `desk` colour, at each change of theme.
  useEffect(() => watchTheme((theme) => void setCaption(theme)), []);
  // Sent from here and not on the page yet; the last turn's failure, or
  // anything else done from the page that didn't take; a count the socket
  // bumps so the page asks for what changed at once.
  const [unsent, setUnsent] = useState<Unsent[]>([]);
  const unsentSeq = useRef(0);
  const [failure, setFailure] = useState<string | null>(null);
  const [poke, setPoke] = useState(0);
  const [goTo, setGoTo] = useState<GoTo | null>(null);
  const [thinking, setThinking] = useState(false);
  // The tasks queued or running, for the sidebar's Ongoing.
  const [jobs, setJobs] = useState<Job[]>([]);
  // What the sidebar's places hold, asked with the tasks; null until known.
  const [places, setPlaces] = useState<Places | null>(null);
  const [listening, setListening] = useState(false);
  const voiceNow = useVoice();
  // Whether this turn's words were shown on release (`voice_heard`), so
  // `voice_turn` does not show them twice.
  const heardShown = useRef(false);
  const [voice, setVoice] = useState<VoiceWarm | null>(null);
  // The app's own choices (ui/src/appSettings.ts), kept by the backend: null
  // until it has answered once, and the hotkeys are not bound before then, so
  // a chosen one is never shadowed by the default for a moment.
  const [app, setApp] = useState<AppSettings | null>(null);
  // The hotkey the user chose, if any, and the one bound.
  const chosenHotkey = app?.hotkey ?? null;
  const [hotkey, setHotkey] = useState<string | null>(null);
  const updateChecks = app?.update_checks === "on";
  const [desk, setDesk] = useState<DeskStatus | null>(null);
  const deskRef = useRef<DeskStatus | null>(null);
  // Bumped when a hotkey's registration succeeds or fails, to re-read
  // `hotkeyProblem` (api.ts).
  const [, setHotkeyTick] = useState(0);
  // What needs the user (agent/worker/needs.py): null until first answered.
  const [needs, setNeeds] = useState<Need[] | null>(null);
  // The stopped backend's note, put away until then by "Not now".
  const [backendAway, setBackendAway] = useState(0);
  const wide = useWide();

  // The backend as the app's shell sees it (ui/src-tauri/src/backend.rs).
  const [svc, setSvc] = useState<ServiceStatus | null>(null);
  const [svcBusy, setSvcBusy] = useState<ServiceAction | null>(null);
  // What the last Start, Stop, Restart or Repair did: said on Diagnostics,
  // and on the day while the backend is down.
  const [serviceSaid, sayService] = useSaid(8000);
  const [atLogin, setAtLogin] = useState<boolean | null>(null);
  const [update, setUpdate] = useState<UpdateOffer | null>(null);
  const [updateBusy, setUpdateBusy] = useState(false);
  const updateRef = useRef<UpdateOffer | null>(null);
  useEffect(() => {
    updateRef.current = update;
  }, [update]);

  const socketRef = useRef<AgentSocket | null>(null);

  const handleEvent = useCallback(
    (event: ServerEvent) => {
      switch (event.type) {
        case "thinking":
          setThinking(true);
          break;

        case "reply":
          // The reply is on the page's next answer, with its id for reactions.
          setThinking(false);
          setPoke((n) => n + 1);
          break;

        case "inbox": {
          // On the day page at its next answer, asked for at once. A note
          // or a briefing makes a toast even in front of them, so it is in
          // Windows' notification centre too (presence.md, Windows toasts);
          // the rest only away from the window: a finished task's result
          // comes this way, to a user who stopped waiting for it. While the
          // desk card shows, the rest make none: the card is the quiet way
          // to see them.
          const { message } = event;
          if (ALWAYS_TOASTS.has(message.kind)) {
            void notify(message.kind, message.text, signedOutRef.current);
          } else if (!(onScreenRef.current && document.hasFocus())) {
            void deskCardShowing().then((showing) => {
              if (!showing) void notify(message.kind, message.text, signedOutRef.current);
            });
          }
          setPoke((n) => n + 1);
          break;
        }

        case "job_finished": {
          const { job } = event;
          setPoke((n) => n + 1);
          setJobs((all) => [job, ...all.filter((j) => j.id !== job.id)]);
          break;
        }

        case "listening":
          setListening(true);
          break;

        case "doing": {
          const { of, text } = event;
          setDoing((all) => {
            const rest = Object.fromEntries(Object.entries(all).filter(([k]) => k !== of));
            return text ? { ...rest, [of]: text } : rest;
          });
          break;
        }

        case "voice_heard":
          // The words on release, while the reply is still being written:
          // shown only with the reply, a slow answer looked like one unheard.
          heardShown.current = true;
          setListening(false);
          setThinking(true);
          setPoke((n) => n + 1);
          break;

        case "voice_turn":
          setListening(false);
          setThinking(false);
          heardShown.current = false;
          setPoke((n) => n + 1);
          break;

        case "voice_empty":
          setListening(false);
          setThinking(false);
          setFailure(event.reason === "nothing heard" ? "Didn't catch that." : event.reason);
          break;

        case "voice_unavailable":
          setListening(false);
          setFailure(event.warm.state === "warming"
            ? "The speech models are still loading."
            : `Voice unavailable: ${event.warm.error ?? event.warm.state}`);
          break;

        case "error":
          setThinking(false);
          setListening(false);
          setFailure(event.error);
          setPoke((n) => n + 1);
          break;
      }
    },
    [],
  );

  useEffect(() => {
    const socket = new AgentSocket({
      onEvent: handleEvent,
      onOpen: () => setConnected(true),
      onClose: () => {
        setConnected(false);
        setThinking(false);
        setDoing({});
      },
    });
    socket.connect();
    socketRef.current = socket;
    return () => socket.dispose();
  }, [handleEvent]);

  useEffect(() => {
    if (!connected || app) return;
    let stopped = false;
    void loadAppSettings().then((settings) => {
      if (!stopped && settings) setApp(settings);
    });
    return () => {
      stopped = true;
    };
  }, [connected, app]);

  // Whether the user's quiet hours are now, for Ink in quiet hours: the
  // backend's word, which knows their zone and the hours they set, asked
  // every five minutes while that theme is chosen and not otherwise.
  const [quiet, setQuiet] = useState(false);
  const byQuietHours = connected && app?.theme === "ink-in-quiet-hours";
  useEffect(() => {
    if (!byQuietHours) return;
    const controller = new AbortController();
    const ask = () => void control.autonomy(controller.signal)
      .then((a) => setQuiet(a.quiet_hours.now), () => {});
    ask();
    const timer = window.setInterval(ask, 5 * 60_000);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, [byQuietHours]);
  // Before the paint, so a chosen theme never flashes the default first.
  useLayoutEffect(() => {
    if (app) applyAppearance(app, quiet);
  }, [app, quiet]);

  /** A change to the app's own settings: what went wrong, or null. */
  const changeApp = useCallback(async (changes: AppChange) => {
    const answer = await saveAppSettings(changes);
    if (!answer.ok || !answer.settings) return answer.error ?? "That setting wasn't kept.";
    setApp(answer.settings);
    // The desk card reads them too: turned off here, it goes at once,
    // not at its next look. Signed "main", so this window doesn't reload
    // what it just saved.
    void tellWindows("app-settings", "main");
    return null;
  }, []);

  /** A card's yes or no, from its note. What went wrong the note says. */
  const decideAction = useCallback(
    async (id: number, approve: boolean, proof: YesProof = {}, only?: number[],
           seen?: string): Promise<Decided> => {
      try {
        return await api.decideAction(id, approve, proof, only, seen);
      } catch (err) {
        return { ok: false, error: String(err) };
      }
    },
    [],
  );

  // While the window is on screen, the tasks (for Ongoing) and the notes;
  // hidden, only the notes, for the tray's dot. Each back page asks for its
  // own (ui/src/pages.ts), and the day page for the day (journal.ts).
  useEffect(() => {
    const controller = new AbortController();
    let cancelled = false;

    // Each answer counts on its own: one route failing (an old worker
    // without it, say) left the sidebar's Ongoing stale too. One not
    // answered keeps its last value; the supervisor may be mid-swap.
    const refresh = async () => {
      const [n, j, p] = await Promise.allSettled([
        control.needs(controller.signal),
        onScreen ? api.jobs(controller.signal) : null,
        onScreen ? control.places(controller.signal) : null,
      ]);
      if (cancelled) return;
      if (n.status === "fulfilled") setNeeds(n.value.needs);
      if (j.status === "fulfilled" && j.value) setJobs(j.value.jobs);
      if (p.status === "fulfilled" && p.value?.ok) setPlaces(p.value);
    };

    void refresh();
    const timer = window.setInterval(() => void refresh(), onScreen ? 5000 : HIDDEN_POLL_MS);
    return () => {
      cancelled = true;
      controller.abort();
      window.clearInterval(timer);
    };
  }, [onScreen]);

  // Read again once setup is done, where the name was given.
  const setupDone = setup !== "asking" && setup !== "unknown" && setup.done;
  useEffect(() => {
    if (!connected) return;
    void control.profile().then((p) => setName(p.profile?.name ?? ""), () => {});
  }, [connected, setupDone]);

  // The sign-in (ui/src/SignIn.tsx): once setup is done and a passphrase is
  // set, nothing shows until the shell holds a session. Asked again at each
  // connection, and told by the shell when the session starts or ends.
  // Past setup includes a setup that can't be read: the app opens then, and
  // it opened unsigned too, until a screen test caught it.
  const pastSetup = setup === "unknown" || setupDone;
  const [signin, setSignin] = useState<SigninState | null | "asking">("asking");
  const [signedIn, setSignedIn] = useState(false);
  useEffect(() => {
    signedOutRef.current = signin === "asking" || (!!signin?.required && !signedIn);
  }, [signin, signedIn]);
  const [ended, setEnded] = useState<Ended | null>(null);
  // The session itself, which a press of push-to-talk carries: the backend
  // opens no microphone for one without it (agent/worker/routes.py).
  const sessionRef = useRef<string | null>(null);
  useEffect(() => {
    if (!connected || !pastSetup) return;
    let gone = false;
    void Promise.all([signinState(), sessionNow()]).then(([state, session]) => {
      if (gone) return;
      sessionRef.current = session;
      setSignin(state);
      setSignedIn(session !== null);
    });
    return () => {
      gone = true;
    };
  }, [connected, pastSetup]);
  useEffect(() => {
    const off = onSession((live, why) => {
      setSignedIn(live);
      void sessionNow().then((session) => {
        sessionRef.current = session;
      });
      if (!live) {
        setEnded(why ?? null);
        // Only a real answer replaces what is known. Taken as it came, the
        // null of a backend that can't say (restarting, as at a lapse) read
        // as "no sign-in needed", and the app opened on the day it held.
        void signinState().then((state) => setSignin((known) => state ?? known));
      }
    });
    return () => void off.then((dispose) => dispose());
  }, []);
  // Windows Hello on this laptop, and set up for Steve: a yes to spending
  // can use it (StuckNotes.tsx).
  const [helloHere, setHelloHere] = useState(false);
  useEffect(() => void helloAvailable().then(setHelloHere), []);
  const helloReady = helloHere && signin !== "asking" && signin?.hello === true;

  // A toast or the desk card was clicked, or the card's menu asked for its
  // settings: the shell has brought the window forward.
  useEffect(() => {
    const offs: (() => void)[] = [];
    let cancelled = false;
    const keep = (off: () => void) => (cancelled ? off() : offs.push(off));
    void onOpenInbox(() => {
      setView("home");
      setGoTo((was) => ({ foot: true, n: (was?.n ?? 0) + 1 }));
    }).then(keep);
    void onShell<string>("open-view", (to) => {
      if (to === "settings") setView("settings");
    }).then(keep);
    // The tray's "Show the desk card" saves a setting in the card's window:
    // Settings here said the card was off until the app was opened again.
    // Not this window's own notices, which every window hears: reloaded,
    // one change's answer could land after the next change and show the
    // setting back as it was.
    void onShell<string | null>("app-settings", (from) => {
      if (from === "main") return;
      void loadAppSettings().then((settings) => {
        if (settings) setApp(settings);
      });
    }).then(keep);
    return () => {
      cancelled = true;
      offs.forEach((off) => off());
    };
  }, []);

  // The backend as the app's shell sees it (backend.rs): stopped, starting,
  // or stopping too often to start again -- the one thing the backend cannot
  // answer when it is down. Asked only while someone can see the answer.
  const refreshService = useCallback(async () => {
    if (isTauri()) setSvc(await serviceStatus());
  }, []);

  useEffect(() => {
    if (!onScreen) return;
    void refreshService();
    const timer = window.setInterval(() => void refreshService(), 30000);
    return () => window.clearInterval(timer);
  }, [refreshService, connected, onScreen]);

  // Told at once, rather than at the next poll: the stopped backend's note
  // says why, with the end of its log, and Power on the day says a damaged
  // file and offers Repair.
  useEffect(() => {
    const disposers: (() => void)[] = [];
    let cancelled = false;
    const keep = (off: () => void) => {
      if (cancelled) off();
      else disposers.push(off);
    };
    void onBackendFailing(() => void refreshService()).then(keep);
    void onBackendDamaged(() => void refreshService()).then(keep);
    return () => {
      cancelled = true;
      disposers.forEach((off) => off());
    };
  }, [refreshService]);

  useEffect(() => {
    void startsAtLogin().then(setAtLogin);
  }, []);

  // The look for a newer Steve: a while after start, so it never slows the
  // start itself, then once a day. A failed look is not worth saying at
  // every start (a build with no release server says so each time); Look now
  // in Settings says why. A release already found is kept until Install,
  // since a new look replaces the one the app holds.
  useEffect(() => {
    if (!updateChecks) return;
    let stopped = false;
    const look = async () => {
      if (updateRef.current) return;
      const found = await checkUpdate(false);
      if (stopped) return;
      if (found.error) console.warn("update check:", found.error);
      else setUpdate(found.offer);
    };
    const first = window.setTimeout(() => void look(), 60_000);
    const daily = window.setInterval(() => void look(), 24 * 3600_000);
    return () => {
      stopped = true;
      window.clearTimeout(first);
      window.clearInterval(daily);
    };
  }, [updateChecks]);

  // Look now, in Settings: what was found, in a line.
  const onCheckUpdate = useCallback(async () => {
    setUpdateBusy(true);
    try {
      const found = await checkUpdate(false);
      if (found.error) return found.error;
      if (!found.offer) return "This is the newest Steve.";
      setUpdate(found.offer);
      return `Steve ${found.offer.version} is ready to install, below.`;
    } finally {
      setUpdateBusy(false);
    }
  }, []);

  // Install the release found, or for a repair, look for this version's
  // own installer first. On success the app exits as the installer starts;
  // otherwise, what went wrong.
  const onUpdate = useCallback(async (repair: boolean): Promise<string | null> => {
    setUpdateBusy(true);
    try {
      let offer = repair ? null : updateRef.current;
      if (!offer) {
        const found = await checkUpdate(repair);
        if (found.error) return found.error;
        if (!found.offer) return "No release of Steve was found to install.";
        offer = found.offer;
      }
      const done = await installUpdate();
      if (done.ok) return null;
      setUpdate(null);
      return `Couldn’t install it: ${done.error ?? "no reason given"}`;
    } finally {
      setUpdateBusy(false);
    }
  }, []);

  const onRepair = useCallback(async () => {
    sayService("Fetching this version’s files again. The app starts again when it is done.");
    const error = await onUpdate(true);
    if (error) sayService(error, false);
  }, [onUpdate, sayService]);

  const onService = useCallback(
    async (action: ServiceAction) => {
      setSvcBusy(action);
      const result = await service(action);
      if (!result.ok) sayService(`Couldn’t ${action} it: ${result.text.slice(0, 200)}`, false);
      else sayService(action === "stop" ? "Stopped. It stays off until you press Start."
                      : action === "start" ? "Starting: give it half a minute."
                      : "Restarting: give it half a minute.");
      if (result.status) setSvc(result.status);
      setSvcBusy(null);
    },
    [sayService],
  );

  const onStartsAtLogin = useCallback(async (on: boolean) => {
    try {
      await setStartsAtLogin(on);
      setAtLogin(await startsAtLogin());
      return null;
    } catch (err) {
      return String(err);
    }
  }, []);

  // Push-to-talk. Down starts capture, up ends the turn -- the socket is the
  // only state, so a dropped connection cannot leave the mic stuck open.
  const talkDown = useCallback(() => {
    const session = sessionRef.current;
    if (socketRef.current?.send({ type: "voice_start", ...(session ? { session } : {}) })) {
      setListening(true);
    }
  }, []);

  const talkUp = useCallback(() => {
    setListening(false);
    setThinking(true);
    socketRef.current?.send({ type: "voice_stop" });
  }, []);

  // The one hotkey (api.ts, bindTapOrHold): a tap opens the quick bar, a
  // hold talks in it, where the words heard and the answer show
  // (QuickBar.tsx). Unchosen, it is the first of HOTKEY_FALLBACKS Windows
  // lets this app have. Bound again whenever the choice changes.
  const loaded = app !== null;
  // Talk, at the sidebar's foot: off, a hold is a slow tap. Read at each
  // hold rather than bound in, which would bind the hotkey again.
  const talkOn = app?.talk !== "off";
  const talkRef = useRef(talkOn);
  useEffect(() => {
    talkRef.current = talkOn;
  }, [talkOn]);
  useEffect(() => {
    if (!loaded) return;
    let dispose = () => {};
    let cancelled = false;
    void (async () => {
      for (const key of chosenHotkey ? [chosenHotkey] : HOTKEY_FALLBACKS) {
        // Whether this hold talks: Talk as it was when the hold began, so a
        // switch turned meanwhile can't leave the quick bar listening.
        let talking = false;
        const { off, ok } = await bindTapOrHold(key, {
          onTap: () => void showQuickbar(),
          onHold: () => {
            talking = talkRef.current;
            if (!talking) return;
            setListening(true);
            void quickbarTalk(true);
          },
          onLetGo: () => {
            if (!talking) {
              void showQuickbar();
              return;
            }
            talking = false;
            setListening(false);
            void quickbarTalk(false);
          },
        });
        if (cancelled) {
          off();
          return;
        }
        dispose = off;
        setHotkey(key);
        if (ok) return;
      }
    })();
    return () => {
      cancelled = true;
      dispose();
    };
  }, [loaded, chosenHotkey]);

  // Hands-free has no switch, hotkey or tray item here: it is not offered
  // in v0.1, and the worker refuses it (agent/worker/desk.offered).
  useEffect(() => {
    deskRef.current = desk;
  }, [desk]);

  // Notes of what the laptop plays (a call, a video): Settings' Voice and
  // the tray menu, shown on the tray.
  const tapping = Boolean(desk?.tap?.on);
  useEffect(() => {
    void setTrayTapping(tapping);
  }, [tapping]);

  /** Turns the notes on or off: what went wrong, or null. */
  const toggleTap = useCallback(async (on?: boolean): Promise<string | null> => {
    const next = on ?? !deskRef.current?.tap?.on;
    try {
      const result = await api.setTap(next);
      if (!result.ok) return result.error ?? "Couldn’t take notes on what’s playing.";
      setDesk((prev) => (prev ? { ...prev, tap: { on: next, ok: next, error: null } } : prev));
      return null;
    } catch (err) {
      return `Notes: ${String(err)}`;
    }
  }, []);

  // From the tray, where the window is likely hidden: Windows says a failure.
  useEffect(() => {
    let dispose = () => {};
    let cancelled = false;
    void onTapToggle(() => {
      // The tray's menu is there for whoever is at the desk. Signed out, it
      // started notes for them, the user's own switch; it brings the
      // sign-in instead, and its tick goes back to what is true.
      if (signedOutRef.current) {
        void setTrayTapping(!!deskRef.current?.tap?.on);
        void comeForward();
        return;
      }
      void toggleTap().then((error) => {
        if (error) void notify("notice", error, signedOutRef.current);
      });
    }).then((off) => {
      if (cancelled) off();
      else dispose = off;
    });
    return () => {
      cancelled = true;
      dispose();
    };
  }, [toggleTap]);

  useEffect(() => {
    const changed = () => setHotkeyTick((n) => n + 1);
    window.addEventListener("hotkeys-changed", changed);
    return () => window.removeEventListener("hotkeys-changed", changed);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    const tick = async () => {
      try {
        const said = await api.voice(controller.signal);
        setVoice(said.warm);
        setDesk(said.hands_free ?? null);
      } catch {
        /* supervisor may be mid-swap */
      }
    };
    void tick();
    // Hidden, slower: it only keeps the tray's notes line true.
    const timer = window.setInterval(tick, onScreen ? 5000 : HIDDEN_POLL_MS);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, [onScreen]);

  // Today: the newest exchange, at the foot of today's page, whatever the
  // journal showed. It only switched to the journal, so on the journal it
  // did nothing, and from a back page the journal came back at the day the
  // sidebar had last gone to.
  const goToday = useCallback(() => {
    setView("home");
    setGoTo((was) => ({ foot: true, n: (was?.n ?? 0) + 1 }));
  }, []);
  // The day the journal shows, for the sidebar to mark (Day.tsx).
  const [dayInView, setDayInView] = useState<string | null>(null);
  // The tour: due once setup ends, or asked for from the name's menu. Ended
  // by Done, "Skip the tour" or Esc, and then not due again.
  // Its stop is kept here, for every page (Tour.tsx).
  const [touring, setTouring] = useState(false);
  const [tourAt, setTourAt] = useState(0);
  const tourDue = app?.tour === "due";
  const endTour = useCallback(() => {
    setTouring(false);
    setTourAt(0);
    if (tourDue) void changeApp({ tour: "off" });
  }, [tourDue, changeApp]);

  // A message waits on the page as sent until the day holds it; one the
  // socket couldn't take stays, marked, for Retry.
  // A message's photos go up first, each on its own request
  // (agent/worker/app_photos.py), and the message names them.
  const deliver = useCallback(async (id: number, text: string, photos: File[]) => {
    const names: string[] = [];
    for (const photo of photos) {
      const kept = await upload(photo);
      if (!kept.name) {
        setFailure(kept.error ?? "That photo wasn't taken.");
        setUnsent((all) => all.map((u) => (u.id === id ? { ...u, failed: true } : u)));
        return;
      }
      names.push(kept.name);
    }
    const ok = socketRef.current?.send(
      names.length ? { type: "chat", text, photos: names } : { type: "chat", text }) ?? false;
    if (ok) setFailure(null);
    setUnsent((all) => all.map((u) => (u.id === id ? { ...u, at: Date.now(), failed: !ok } : u)));
  }, []);
  const send = useCallback((text: string, photos: File[] = []) => {
    const id = ++unsentSeq.current;
    setFailure(null);
    setUnsent((all) => [...all, { id, text, photos, at: Date.now(), failed: false }]);
    void deliver(id, text, photos);
  }, [deliver]);
  const retry = useCallback((id: number) => {
    const message = unsent.find((u) => u.id === id);
    if (message) void deliver(id, message.text, message.photos ?? []);
  }, [unsent, deliver]);
  const undo = useCallback(async (id: number) => {
    const result = await api.decideAction(id, false);
    if (!result.ok) setFailure(result.error ?? "Too late: it already went.");
    setPoke((n) => n + 1);
  }, []);
  const journal = useJournal({ connected, onScreen, poke });
  const { react, drop, pages } = journal;
  // A message leaves the list once a page holds it. Matched only where the
  // page was drawn, against the newest day, every message sent stayed in
  // this list; in a window left open overnight, the page's turn at 4 a.m.
  // left nothing that held the evening's messages, and they came back at
  // the foot of the new day, greyed as if still on their way. Before the
  // paint, so no frame shows a message twice, held and waiting.
  useLayoutEffect(() => {
    const entries = pages.flatMap((p) => p.entries);
    setUnsent((all) => {
      const left = stillUnsent(all, entries);
      return left.length === all.length ? all : left;
    });
  }, [pages]);
  const deleteNote = useCallback(async (id: number) => {
    const result = await control.deleteNote(id);
    if (result.ok) drop(`listening:${id}`);
    else setFailure(result.error ?? "Couldn't delete that note.");
    setPoke((n) => n + 1);
  }, [drop]);
  const onReact = useCallback((id: number, verdict: Verdict | null) => {
    void react(id, verdict).then((ok) => {
      if (!ok) setFailure("Couldn't keep that mark.");
    });
  }, [react]);

  // The notes: the app's own (needs.ts), then the backend's. The sample
  // shows on the first day only, until it is dismissed.
  const firstDay = journal.pages.length > 0
    && journal.pages.every((p) => p.entries.length === 0)
    && journal.folded.every((d) => d.count === 0);
  const allNeeds: AppNeed[] | null = needs === null ? null : [
    ...appNeeds({ service: svc, putAway: backendAway,
                  sample: firstDay && app?.sample_note === "show" }),
    ...needs,
  ];
  const needCount = allNeeds?.length ?? 0;

  // The tray's dot: a note waiting for them, even with the window hidden.
  useEffect(() => {
    void setWaiting(needCount);
  }, [needCount]);

  // Answering a note (StuckNotes.tsx): a card through /actions/decide; the
  // rest through the needs routes; a fix opens where the fixing is done,
  // and the note goes once it is fixed.
  const answerNeed = useCallback(async (need: AppNeed, answer: NoteAnswer) => {
    let done: Decided;
    if (need.action_id === undefined && (answer.say === "yes" || answer.say === "no")) {
      done = { ok: false, error: "That isn't a card." };
    } else if (answer.say === "yes" && answer.hello && need.action_id !== undefined) {
      // Hello signs the backend's challenge for this card as it is now.
      const signed = signin !== "asking" && signin?.hello
        ? await helloSigned(signin.hello_name, { for: "yes", action_id: need.action_id })
        : { error: "Windows Hello isn't set up for Steve." };
      done = !("error" in signed)
        ? await decideAction(need.action_id, true, { hello: signed }, undefined, need.seen)
        : { ok: false, error: signed.error === "cancelled" ? "Cancelled." : signed.error };
    } else if (answer.say === "yes" || answer.say === "no") {
      done = await decideAction(need.action_id!, answer.say === "yes",
                                answer.say === "yes" && answer.passphrase
                                  ? { passphrase: answer.passphrase } : {},
                                answer.say === "yes" ? answer.only : undefined, need.seen);
    } else if (answer.say === "fix") {
      done = { ok: true };
      if (answer.action === "start") void onService("start");
      // The note stays, saying why, if the page would not open.
      else if (answer.action === "top_up") done = await openLink(TOP_UP);
      else if (answer.action === "raise_cap") setView("spend");
      else if (answer.action === "link") setView("channels");
      else setView("accounts");
    } else if (need.id === SAMPLE_ID) {
      const kept = await saveAppSettings({ sample_note: "dismissed" });
      if (kept.ok && kept.settings) setApp(kept.settings);
      done = { ok: kept.ok, error: kept.error };
    } else if (need.id === BACKEND_ID) {
      setBackendAway(Date.now() + HOUR_MS);
      done = { ok: true };
    } else if (answer.say === "done") {
      done = await control.needDone(need.id);
    } else if (answer.say === "later") {
      done = await control.needLater(need.id, answer.when);
    } else {
      done = await control.needNotNow(need.id);
    }
    if (done.ok) {
      const fresh = await control.needs().catch(() => null);
      if (fresh) setNeeds(fresh.needs);
      // The day says what was answered ("You said yes"), at once. Left to
      // the page's poll, it came up to five seconds later, and never while
      // Windows took the window for hidden: under load, with the screen
      // locked, the exit walk waited 30 s for it.
      setPoke((n) => n + 1);
    }
    return done;
  }, [decideAction, onService, signin]);

  const menu: MenuGroup[] = MENU.map(({ group, pages }) => ({
    group, pages: pages.map(({ key, name: page }) => ({ key, name: page })),
  }));
  // Under the days. Call notes only where notes can be taken, or were.
  const sidebarPlaces: MenuPage[] = [
    { key: "coming", name: "Coming up", count: places?.coming },
    { key: "modules", name: "Extensions", count: places?.extensions.installed,
      note: (places?.extensions.building ?? 0) > 0 ? "building" : undefined },
    ...(desk?.tap?.add_on === true || (places?.calls ?? 0) > 0
      ? [{ key: "calls", name: "Call notes", count: places?.calls }] : []),
  ];
  // A day's page, at an entry if one is given: a search's line, a call note.
  const openAt = (date: string, key: string | null) => {
    setView("home");
    // Its page is read first, however far back: only this week's are in
    // the list to scroll to. Unless the user has gone elsewhere meanwhile,
    // as to Today: a slow page took them back to the search's day.
    const asked = goTo?.n ?? 0;
    void journal.open(date).then(() => setGoTo((was) => ((was?.n ?? 0) !== asked ? was : {
      date, key: key ?? undefined, n: asked + 1 })));
  };
  const lines = Object.values(doing);
  const shown = presence({
    service: svc?.state ?? null, connected, longGone, listening, speaking: voiceNow === "speaking",
    doing: lines.length ? lines[lines.length - 1] : null, thinking, needs: needCount,
  });
  const week = [
    ...journal.folded.slice(-6).filter((d) => d.count > 0).map((d) => d.date),
    ...journal.pages.map((p) => p.date),
  ];
  const today = journal.pages[journal.pages.length - 1]?.today ?? null;
  const days = [...new Set(week)].sort().reverse()
    .filter((date) => today === null || date >= shifted(today, -6))
    .map((date) => ({ date, label: today ? dayName(date, today) : date }));
  const unread = journal.pages[journal.pages.length - 1]?.entries.filter((e) => e.new).length ?? 0;
  // Ongoing's tasks: each one's entry is on today's page, with what it is doing.
  const ongoing = jobs
    .filter((j) => j.status === "queued" || j.status === "running")
    .map((j) => ({ key: String(j.id), text: j.why?.split(":").pop()?.trim() || j.prompt }));

  if (setup === "asking" && !gaveUpWaiting) return <Waking />;
  if (setup !== "asking" && setup !== "unknown" && !setup.done) {
    return <Setup initial={setup} connected={connected} onStart={() => void onService("start")}
                  onDone={() => {
                    setSetup({ ...setup, done: true });
                    void changeApp({ tour: "due" });
                  }} />;
  }
  if (pastSetup && signin === "asking" && !gaveUpWaiting) return <Waking />;
  if (pastSetup && signin !== "asking" && signin?.required && !signedIn) {
    return <SignIn state={signin} ended={ended} onSignedIn={() => {
      setEnded(null);
      setSignedIn(true);
    }} />;
  }

  return (
    <Shell presence={shown} name={name} ongoing={ongoing} onOngoing={goToday}
           days={days} unread={unread} needs={needCount} inView={dayInView}
           onDay={(date) => {
             setView("home");
             setGoTo((was) => ({ date, n: (was?.n ?? 0) + 1 }));
           }}
           places={sidebarPlaces} onFound={openAt}
           menu={menu} page={view === "home" ? null : HEADS[view] ?? null}
           onPage={(key) => setView(key as View)} onToday={goToday}
           onQuit={() => void quit()}
           onTour={() => {
             setView("home");
             setTourAt(0);
             setTouring(true);
           }}
           switches={(
             <>
               <Check label="Talk" checked={talkOn}
                      onChange={(on) => void changeApp({ talk: on ? "on" : "off" })} />
               {desk?.tap?.add_on && (
                 <Check label="Call notes" checked={tapping}
                        onChange={(on) => void toggleTap(on).then((error) => {
                          if (error) void notify("notice", error, signedOutRef.current);
                        })} />
               )}
             </>
           )}
           margin={wide ? (
             <StuckNotes needs={allNeeds} wide onScreen={onScreen} onAnswer={answerNeed}
                         hello={helloReady} />
           ) : undefined}>
      {view === "home" && (
        <DayPage
          pages={journal.pages}
          folded={journal.folded}
          quiet={journal.quiet}
          fresh={journal.fresh}
          onEarlier={() => void journal.earlier()}
          onOpen={(date) => void journal.open(date)}
          onFold={(date) => void journal.fold(date)}
          goTo={goTo}
          unsent={unsent}
          onRetry={retry}
          working={thinking || doing.turn !== undefined ? doing.turn ?? null : undefined}
          failure={failure}
          onSend={send}
          onReact={onReact}
          onUndo={(id) => void undo(id)}
          onDeleteNote={(id) => void deleteNote(id)}
          name={name}
          listening={listening}
          voice={voice}
          hotkey={hotkey}
          talk={talkOn}
          onTalkStart={talkDown}
          onTalkEnd={talkUp}
          onScreen={onScreen}
          callNotes={desk?.tap?.add_on === true}
          canBuild={places?.extensions.can_build === true}
          onDayInView={setDayInView}
          top={(
            <>
              {/* Kept for what the stopped backend's note doesn't do:
                  stopped by hand, starting, damaged. */}
              {!connected && svc?.state !== "failing" && (
                <>
                  <Power overview={null} connected={connected} service={svc}
                         onService={onService} serviceBusy={svcBusy}
                         onUpdate={onRepair} updateBusy={updateBusy} />
                  <p className="page-said"><Saved said={serviceSaid} /></p>
                </>
              )}
              {update && !update.repair && (
                <p className="day-update" data-update={update.version}>
                  Steve {update.version} is ready to install.{" "}
                  <button type="button" onClick={() => setView("settings")}>In Settings</button>
                </p>
              )}
              {!wide && (
                <StuckNotes needs={allNeeds} wide={false} onScreen={onScreen}
                            onAnswer={answerNeed} hello={helloReady} />
              )}
            </>
          )}
        />
      )}
      {view === "knows" && <Knows onScreen={onScreen} />}
      {view === "profile" && <Profile onScreen={onScreen} onName={setName} />}
      {view === "accounts" && (
        <Saying>{(say) => <Accounts toast={say} onScreen={onScreen} />}</Saying>
      )}
      {view === "channels" && <Channels onScreen={onScreen} />}
      {view === "browser" && <Saying>{(say) => <Browser toast={say} onScreen={onScreen} />}</Saying>}
      {view === "maydo" && <MayDo onScreen={onScreen} />}
      {view === "routines" && <RoutinesAndGoals onScreen={onScreen} />}
      {view === "modules" && (
        <Saying>{(say) => (
          <Modules toast={say} onScreen={onScreen}
                   hello={helloReady && signin ? signin.hello_name : null}
                   canBuild={places?.extensions.can_build === true}
                   onAsk={(text) => { send(text); setView("home"); }} />
        )}</Saying>
      )}
      {view === "coming" && <ComingUp onScreen={onScreen} />}
      {view === "calls" && <CallNotes onScreen={onScreen} onOpen={openAt} />}
      {view === "spend" && <Spend onScreen={onScreen} />}
      {view === "activity" && <Activity onScreen={onScreen} />}
      {view === "diagnostics" && (
        <Diagnostics connected={connected} service={svc} onService={onService}
                     serviceBusy={svcBusy} onRepair={onRepair} repairing={updateBusy}
                     said={serviceSaid} onScreen={onScreen} onAsk={refreshService} />
      )}
      {view === "settings" && (
        <Settings
          app={app}
          onApp={changeApp}
          onScreen={onScreen}
          voice={voice}
          hotkey={{
            bound: hotkey,
            problem: hotkey ? hotkeyProblem(hotkey) : null,
            // Not chosen, and the default was taken: which one is in use, and why.
            fellBack: !chosenHotkey && hotkey !== null && hotkey !== DEFAULT_HOTKEY
              ? `${hotkeyLabel(DEFAULT_HOTKEY)} is held by another app, so Steve uses `
                + `${hotkeyLabel(hotkey)}.`
              : null,
          }}
          startsAtLogin={atLogin}
          onStartsAtLogin={onStartsAtLogin}
          update={update}
          updateBusy={updateBusy}
          onCheckUpdate={onCheckUpdate}
          onUpdate={() => onUpdate(false)}
          tapping={tapping}
          onTap={toggleTap}
          addOn={desk?.tap?.add_on !== false}
        />
      )}
      {(touring || tourDue) && (
        <Tour hotkey={hotkey} at={tourAt} onAt={setTourAt} onEnd={endTour} />
      )}
    </Shell>
  );
}

export { formatDuration, formatUSD };
