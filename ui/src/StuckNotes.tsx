/* Notes: what needs the user, stuck to the side of the page
   (docs/design/notes.md).

   A yes waited in the Approvals tab, a reminder going off was a toast gone
   in seconds, and a cap reached sat in Overview: three places to look, and
   nothing on the page said any of them held something. Now one list
   (/control/needs, with the app's own two from needs.ts) is drawn where
   the eye passes: in the margin of a wide window, and folded into one
   strip at the top of the page in a narrower one.

   A note stays until it is answered, here or on the phone, or expires.
   Answered here, its button ticks and it peels; answered elsewhere, it
   peels too; expired, it fades. One that comes while the window is hidden
   arrives when the window is shown, not while nobody sees it. */

import { useEffect, useRef, useState, type CSSProperties, type KeyboardEvent } from "react";
import type { DecideNeeds } from "./api";
import type { LaterWhen } from "./control";
import { Check } from "./Form";
import { HandMark } from "./HandMark";
import { clock } from "./journal";
import { tiltOf, type AppNeed, type NoteAnswer } from "./needs";
import "./needs.css";

type Answered = { ok: boolean; error?: string; needs?: DecideNeeds };
type Fate = "tick" | "peel" | "fade";

const KICKER: Record<AppNeed["kind"], string> = {
  yes: "Needs you", code: "Your passphrase", now: "Now", stopped: "Stopped",
};
// What a screen reader hears as a note arrives (notes.md, Screen readers).
const SAID: Record<AppNeed["kind"], string> = {
  yes: "needs you", code: "needs you", now: "now", stopped: "stopped",
};
const FIX: Record<string, string> = {
  top_up: "Top up", raise_cap: "Raise the cap", link: "Link Signal", sign_in: "Sign in",
  password: "Enter the password", test: "Test it", turn_on: "Turn it on", start: "Start",
};
const LATER: [LaterWhen, string][] = [
  ["15m", "in 15 minutes"], ["1h", "in an hour"], ["evening", "this evening"],
  ["morning", "tomorrow morning"],
];
// Up to three show in a pile (notes.md, Wide window); the rest are "+N".
const PILE = 3;
// The tick on the button, then the peel (tokens.css `--t-note-answered`).
const TICK_MS = 300;
const LEAVE_MS = 320;

function Main({ label, ticked, disabled, onClick, submit = false }: {
  label: string;
  ticked: boolean;
  disabled: boolean;
  onClick?: () => void;
  submit?: boolean;
}) {
  return (
    <button type={submit ? "submit" : "button"} className="note-main" disabled={disabled}
            data-ticked={ticked || undefined} onClick={onClick}>
      <span className="note-main-label">{label}</span>
      {ticked && <HandMark name="check" id={label} drawIn className="note-tick" />}
    </button>
  );
}

function NoteCard({ need, at, tilt, fate, arriving, behind, onAnswer, onTicked, onArrived, hello }: {
  need: AppNeed;
  /** Its place in a pile, top first. */
  at: number;
  tilt: number;
  fate: Fate | undefined;
  arriving: boolean;
  /** Under the top note of a pile: drawn, but not reachable until spread. */
  behind: boolean;
  onAnswer: (need: AppNeed, answer: NoteAnswer) => Promise<Answered>;
  onTicked: (id: string) => void;
  onArrived: (id: string) => void;
  hello: boolean;
}) {
  const [mode, setMode] = useState<"ask" | "later" | "passphrase">("ask");
  const [words, setWords] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [details, setDetails] = useState(false);
  // A grouped card's items left ticked: all at first. A yes with some
  // unticked is a yes to the rest (`only`); a code card's items go
  // together, its code bound to all of them, and have no boxes.
  const [kept, setKept] = useState<boolean[]>(() => need.items?.map(() => true) ?? []);
  // Ticked afresh when the card changes: seeded once, a note drawn with one
  // item kept no box for the items joined later, and its yes took them all.
  const [keptFor, setKeptFor] = useState(need.seen);
  if (keptFor !== need.seen) {
    setKeptFor(need.seen);
    setKept(need.items?.map(() => true) ?? []);
  }
  const choosing = need.kind === "yes" && !!need.items;
  const only = choosing && kept.includes(false)
    ? kept.flatMap((on, i) => (on ? [i + 1] : [])) : undefined;
  const ticked = fate === "tick";
  const still = busy || fate !== undefined;

  const answer = async (said: NoteAnswer) => {
    setBusy(true);
    setError(null);
    const done = await onAnswer(need, said);
    setBusy(false);
    if (done.ok) {
      // A fix opens where the fixing is done; the note goes when it's fixed.
      if (said.say !== "fix") onTicked(need.id);
      return;
    }
    setWords("");
    setError(done.needs === "new_passphrase" ? "Choose a passphrase first, in Settings."
      : done.needs === "passphrase" ? "That isn’t your passphrase."
      : done.error ?? "That didn’t go through. Try again.");
  };

  const back = () => {
    setMode("ask");
    setWords("");
    setError(null);
  };

  const fix = need.actions.find((a) => a !== "not_now");
  let actions;
  if (mode === "passphrase") {
    actions = (
      <form className="note-code" onSubmit={(e) => {
        e.preventDefault();
        void answer({ say: "yes", passphrase: words });
      }}>
        <input type="password" className="note-passphrase t-small" aria-label="Your passphrase"
               placeholder="Your passphrase" autoFocus value={words} disabled={still}
               onChange={(e) => setWords(e.target.value)} />
        <div className="note-actions">
          <Main label="Yes" submit ticked={ticked} disabled={still || !words} />
          {hello && (
            <button type="button" className="note-other" disabled={still}
                    onClick={() => void answer({ say: "yes", hello: true })}>Use Windows Hello</button>
          )}
          <button type="button" className="note-other" disabled={still} onClick={back}>Cancel</button>
        </div>
      </form>
    );
  } else if (mode === "later") {
    actions = (
      <ul className="note-later" aria-label="Later">
        {LATER.map(([when, words]) => (
          <li key={when}>
            <button type="button" className="note-other" disabled={still}
                    onClick={() => void answer({ say: "later", when })}>
              {words}
            </button>
          </li>
        ))}
      </ul>
    );
  } else if (need.kind === "yes" || need.kind === "code") {
    actions = (
      <div className="note-actions">
        <Main label={only ? `Yes to ${only.length}` : "Yes"} ticked={ticked}
              disabled={still || only?.length === 0}
              onClick={() => (need.kind === "code" ? setMode("passphrase")
                : void answer({ say: "yes", ...(only ? { only } : {}) }))} />
        <button type="button" className="note-other" disabled={still}
                onClick={() => void answer({ say: "no" })}>No</button>
      </div>
    );
  } else if (need.kind === "now") {
    actions = (
      <div className="note-actions">
        <Main label="Done" ticked={ticked} disabled={still} onClick={() => void answer({ say: "done" })} />
        {need.actions.includes("later") && (
          <button type="button" className="note-other" disabled={still}
                  onClick={() => setMode("later")}>Later</button>
        )}
      </div>
    );
  } else {
    actions = (
      <div className="note-actions">
        {fix && (
          <Main label={FIX[fix] ?? fix} ticked={false} disabled={still}
                onClick={() => void answer({ say: "fix", action: fix })} />
        )}
        {need.actions.includes("not_now") && (
          <button type="button" className="note-other" disabled={still}
                  onClick={() => void answer({ say: "not_now" })}>Not now</button>
        )}
        {need.log && need.log.length > 0 && (
          <button type="button" className="note-other" aria-expanded={details}
                  onClick={() => setDetails((d) => !d)}>Details</button>
        )}
      </div>
    );
  }

  const when = clock(need.at);
  const classes = ["note", "grain", "lifted-note", arriving && "note-arrives",
    fate === "peel" && "note-answered", fate === "fade" && "note-fades"];
  return (
    <article className={classes.filter(Boolean).join(" ")} data-need={need.id} data-kind={need.kind}
             tabIndex={-1} inert={behind} style={{ "--tilt-of": tilt, "--at": at } as CSSProperties}
             onAnimationEnd={(e) => {
               if (e.animationName === "steve-note-arrives") onArrived(need.id);
             }}
             onKeyDown={(e) => {
               // Esc leaves the passphrase or Later's list first, then the note.
               if (e.key === "Escape" && mode !== "ask") {
                 e.preventDefault();
                 back();
               }
             }}>
      <span className="note-pin" aria-hidden />
      <p className="note-kicker hand-kicker">{need.kicker ?? KICKER[need.kind]}</p>
      <p className="note-text t-small">{need.text}</p>
      {need.detail && <p className="note-detail t-small">{need.detail}</p>}
      {need.items && (
        <ol className="note-items t-small">
          {need.items.map((item, i) => (
            <li key={i}>
              {choosing ? (
                <Check label={item} checked={kept[i]} disabled={still}
                       onChange={(on) => setKept((was) => was.map((k, j) => (j === i ? on : k)))} />
              ) : item}
            </li>
          ))}
        </ol>
      )}
      {actions}
      {error && <p className="note-error t-small" role="alert">{error}</p>}
      {details && need.log && <pre className="note-log t-code">{need.log.join("\n")}</pre>}
      {(when || need.word) && (
        <p className="note-meta t-meta">
          {when}
          {when && need.word && " · "}
          {need.word && (
            <span className="note-word" title={`Or reply YES ${need.word} from your phone.`}>
              {need.word}
            </span>
          )}
        </p>
      )}
    </article>
  );
}

function expired(need: AppNeed): boolean {
  return !!need.expires && Date.parse(need.expires) <= Date.now();
}

function say(need: AppNeed): string {
  return `New note, ${SAID[need.kind]}: ${need.text}`;
}

export function StuckNotes({ needs, wide, onScreen, onAnswer, hello = false }: {
  /** Null until the first answer, so the notes already there aren't
   *  announced as new. */
  needs: AppNeed[] | null;
  /** In the margin, or folded at the top of a narrower page. */
  wide: boolean;
  onScreen: boolean;
  onAnswer: (need: AppNeed, answer: NoteAnswer) => Promise<Answered>;
  /** Windows Hello is set up for Steve here: a yes to spending can use it. */
  hello?: boolean;
}) {
  const [prev, setPrev] = useState<AppNeed[] | null>(needs);
  // Notes gone from the list, still drawn while they leave, where they were.
  const [kept, setKept] = useState<{ need: AppNeed; at: number }[]>([]);
  const [fate, setFate] = useState<Record<string, Fate>>({});
  const [arrived, setArrived] = useState<ReadonlySet<string>>(() => new Set());
  const [said, setSaid] = useState({ polite: "", urgent: "" });
  const [spread, setSpread] = useState(false);
  const [open, setOpen] = useState(false);
  const [focusing, setFocusing] = useState(0);
  const focusAt = useRef<number | null>(null);
  const box = useRef<HTMLElement>(null);
  const count = useRef(0);

  // The list changed: what went leaves where it was, what came is said.
  if (prev !== needs) {
    setPrev(needs);
    if (prev && needs) {
      const listed = new Set(needs.map((n) => n.id));
      const before = new Set(prev.map((n) => n.id));
      const gone = prev.flatMap((need, at) => (listed.has(need.id) ? [] : [{ need, at }]));
      if (gone.length && onScreen) {
        setKept((k) => [...k.filter((g) => !gone.some((n) => n.need.id === g.need.id)), ...gone]);
        setFate((f) => {
          const out = { ...f };
          for (const g of gone) out[g.need.id] ??= expired(g.need) ? "fade" : "peel";
          return out;
        });
      }
      const fresh = needs.filter((n) => !before.has(n.id));
      if (fresh.length) {
        const urgent = (n: AppNeed) => n.kind === "now" || n.kind === "stopped";
        setSaid({
          polite: fresh.filter((n) => !urgent(n)).map(say).join(" "),
          urgent: fresh.filter(urgent).map(say).join(" "),
        });
      }
    }
  }

  const drawn = [...(needs ?? [])];
  for (const g of kept) {
    if (!drawn.some((n) => n.id === g.need.id)) drawn.splice(Math.min(g.at, drawn.length), 0, g.need);
  }
  const stacked = wide && !spread && drawn.length > 1;
  if (spread && drawn.length <= 1) setSpread(false);

  useEffect(() => {
    count.current = drawn.length;
  });

  // Gone from the list and done leaving: no longer drawn.
  useEffect(() => {
    const leaving = Object.keys(fate).filter((id) => fate[id] !== "tick");
    if (leaving.length === 0) return;
    const timer = window.setTimeout(() => {
      const listed = new Set((needs ?? []).map((n) => n.id));
      const done = leaving.filter((id) => !listed.has(id));
      if (done.length === 0) return;
      setKept((k) => k.filter((g) => !done.includes(g.need.id)));
      setFate((f) => {
        const out = { ...f };
        for (const id of done) delete out[id];
        return out;
      });
    }, LEAVE_MS);
    return () => window.clearTimeout(timer);
  }, [fate, needs]);

  // Ctrl+J: to the first note, unfolding the strip if it is folded.
  useEffect(() => {
    const key = (e: globalThis.KeyboardEvent) => {
      if (!e.ctrlKey || e.altKey || e.shiftKey || e.key.toLowerCase() !== "j") return;
      if (count.current === 0) return;
      e.preventDefault();
      setOpen(true);
      focusAt.current = 0;
      setFocusing((n) => n + 1);
    };
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  }, []);

  useEffect(() => {
    if (focusAt.current === null) return;
    const notes = box.current?.querySelectorAll<HTMLElement>("article.note");
    notes?.[Math.min(focusAt.current, notes.length - 1)]?.focus();
    focusAt.current = null;
  }, [focusing]);

  const ticked = (id: string) => {
    setFate((f) => ({ ...f, [id]: "tick" }));
    window.setTimeout(() => setFate((f) => (f[id] === "tick" ? { ...f, [id]: "peel" } : f)), TICK_MS);
  };
  const landed = (id: string) => setArrived((s) => new Set(s).add(id));

  // Up and down move between notes; Tab moves within one.
  const onKey = (e: KeyboardEvent<HTMLElement>) => {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    const target = e.target as HTMLElement;
    if (target.closest("input, textarea")) return;
    const notes = [...(box.current?.querySelectorAll<HTMLElement>("article.note") ?? [])];
    const here = notes.findIndex((n) => n.contains(target));
    if (here < 0) return;
    e.preventDefault();
    const next = Math.max(0, Math.min(drawn.length - 1, here + (e.key === "ArrowDown" ? 1 : -1)));
    if (stacked && next > 0) setSpread(true);
    focusAt.current = next;
    setFocusing((n) => n + 1);
  };

  const live = (
    <>
      <div className="notes-said" aria-live="polite">{said.polite}</div>
      <div className="notes-said" aria-live="assertive">{said.urgent}</div>
    </>
  );
  if (drawn.length === 0) return live;

  const side = Math.sign(tiltOf(drawn[0].id));
  const shown = stacked ? drawn.slice(0, PILE) : drawn;
  const more = stacked ? drawn.length - shown.length : 0;
  const cards = shown.map((need, i) => {
    // A pile fans out: each lower note leans the other way.
    const tilt = stacked && i > 0 ? (i % 2 ? -side : side) * Math.abs(tiltOf(need.id)) : tiltOf(need.id);
    return (
      <NoteCard key={need.id} need={need} at={i} tilt={tilt} fate={fate[need.id]}
                arriving={onScreen && !arrived.has(need.id) && fate[need.id] === undefined}
                behind={stacked && i > 0} onAnswer={onAnswer} onTicked={ticked} onArrived={landed}
                hello={hello} />
    );
  });

  const label = drawn.length === 1 ? "1 note" : `${drawn.length} notes`;
  return (
    <>
      <section ref={box} className="notes" data-wide={wide || undefined} onKeyDown={onKey}
               aria-label={`Needs you, ${drawn.length}`}>
        {!wide && (
          <button type="button" className="notes-strip t-small" aria-expanded={open}
                  onClick={() => setOpen((o) => !o)}>
            <span className="note-pin" aria-hidden />
            <span className="notes-strip-count">{label}</span>
            <span className="notes-strip-first">{drawn[0].text}</span>
          </button>
        )}
        {(wide || open) && (
          <div className={stacked ? "notes-pile" : "notes-column"}
               style={{ "--pile": shown.length } as CSSProperties}>
            {cards}
            {stacked && (
              <button type="button" className="notes-spread" onClick={() => setSpread(true)}
                      aria-label={`Spread out all ${drawn.length} notes`}>
                {more > 0 && <span className="notes-more t-meta">+{more}</span>}
              </button>
            )}
          </div>
        )}
      </section>
      {live}
    </>
  );
}
