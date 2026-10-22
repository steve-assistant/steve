/* The day page (docs/design/journal.md): one page a day, everything that
   happened on it, the composer at its foot.

   Home was a dashboard and Chat a transcript, and neither was the record:
   a reply on the phone, a briefing, a card decided on Signal or a task done
   overnight each lived on a different tab or nowhere. The page is drawn
   from the backend's day (journal.ts), so whatever channel it came through,
   it is here, in the order it happened. */

import { type ReactNode, type RefObject, useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { hotkeyLabel, type VoiceWarm } from "./api";
import { type Day, type DayEntry, type FoldedDay, type Verdict } from "./control";
import { HandMark } from "./HandMark";
import { useLevel } from "./level";
import { choose, FORMATS, type Picked, useFileURL, useSentPhoto } from "./photos";
import { inline, Link, Prose } from "./Prose";
import {
  clock, dayName, firstWords, inLens, type Lens, localDate, type Unsent,
} from "./journal";
import "./day.css";

const LENSES: { key: Lens; name: string }[] = [
  { key: "all", name: "All" }, { key: "talk", name: "Talk" }, { key: "log", name: "Log" },
];

/** Other channels are named beside the time; the app's own is not. */
const CHANNELS: Record<string, string> = {
  signal: "Signal", telegram: "Telegram", voice: "voice", quickbar: "quick bar", desk: "desk",
};

/** Runs of this many updates or more are gathered under a bracket. */
const RUN = 3;

/** How long an entry a search went to stays marked. */
const SOUGHT_MS = 2000;

function longDate(date: string): string {
  return localDate(date).toLocaleDateString([], { weekday: "long", day: "numeric", month: "long" });
}

type SignKind = "told" | "done" | "turning" | "needs" | "failed" | "ticked" | "spoken" | "none";

// On the middle of the first line's lowercase letters, at any text size:
// the sign sits in a line set in the type of the entry's first line,
// aligned `middle`, which CSS defines as the baseline plus half the
// x-height. It was placed 0.62em down the gutter's own type, whatever the
// line beside it, so it hung low beside a reminder's smaller line, and
// further off at the larger size.
function Sign({ kind, id, face }: { kind: SignKind; id: string; face: string }) {
  if (kind === "none") return null;
  return (
    <span className={`sign-line ${face}`} aria-hidden>
      {kind === "failed" || kind === "ticked"
        ? <HandMark name={kind === "failed" ? "cross" : "check"} id={id} className={`sign sign-${kind}`} />
        : <span className={`sign sign-${kind}${kind === "turning" ? " turning" : ""}`} />}
    </span>
  );
}

/** The type an entry's first line is set in, as `Body` draws it. */
function faceOf(e: DayEntry): string {
  switch (e.kind) {
    case "reply":
    case "briefing":
      return "t-body";
    case "action":
      return e.status === "failed" ? "t-body" : "t-small";
    case "task":
      return e.summary ? "t-meta spoken-line" : "t-title";
    case "spoken":
      return "t-meta spoken-line";
    default:
      return "t-small";
  }
}

function signOf(e: DayEntry): SignKind {
  switch (e.kind) {
    case "said":
    case "reply":
      return "none";
    case "spoken":
      return "spoken";
    case "briefing":
    case "update":
    case "listening":
      // A reminder that went off is done with, ticked as on a paper list.
      if (e.kind === "update" && e.of === "reminder") return "ticked";
      return e.kind === "listening" && e.status === "open" ? "turning" : "told";
    case "action":
      if (e.status === "pending") return "needs";
      if (e.status === "failed") return "failed";
      if (e.status === "executing" || e.status === "approved") return "turning";
      return e.status === "done" ? "done" : "told";
    case "task":
      if (e.status === "failed") return "failed";
      return e.status === "done" ? "done" : "turning";
    case "asleep":
      return "none";
  }
}

/** "sent 09:14": a briefing held while the computer slept says when it went too. */
function sentLate(e: DayEntry): string | null {
  if (e.kind !== "briefing" || !e.sent || !e.at) return null;
  return Date.parse(e.sent) - Date.parse(e.at) >= 60_000 ? `sent ${clock(e.sent)}` : null;
}

function Countdown({ until, onUndo }: { until: string; onUndo: () => void }) {
  const end = Date.parse(until);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  const left = Math.ceil((end - now) / 1000);
  if (!(left > 0)) return null;
  const shown = `${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`;
  return (
    <button type="button" className="undo t-meta" onClick={onUndo}>Undo · {shown}</button>
  );
}

function Spoken({ e }: { e: Extract<DayEntry, { kind: "spoken" }> }) {
  const [open, setOpen] = useState(false);
  return (
    <div className={`spoken${open ? " open" : ""}`}>
      <button type="button" className="spoken-head" aria-expanded={open}
              onClick={() => setOpen((was) => !was)}>
        <span className="t-meta">Spoken · {e.minutes} min</span>
        <span className="t-small">{e.summary ?? e.first_words ?? ""}</span>
      </button>
      {open && (
        <ol className="transcript">
          {e.lines.map((line, i) => (
            <li key={i} className={line.role === "user" ? "by-user" : "by-steve"}>
              <time className="t-meta">{clock(line.at)}</time>
              <span className="t-small">{line.text}</span>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

/** The night's work, folded as a spoken exchange is: its summary, and the
 *  parts it did once opened. A line saying "The night's work" and "done"
 *  told nothing of what it did. */
function Night({ e }: { e: Extract<DayEntry, { kind: "task" }> }) {
  const [open, setOpen] = useState(false);
  return (
    <div className={`night${open ? " open" : ""}`}>
      <button type="button" className="spoken-head" aria-expanded={open}
              onClick={() => setOpen((was) => !was)}>
        <span className="t-meta">Overnight · {e.minutes ?? 1} min</span>
        <span className="t-small">{e.summary}</span>
      </button>
      {open && (
        <div className="night-parts">
          {(e.parts ?? []).map((part) => (
            <section key={part.title}>
              <div className="t-meta part-title">{part.title}</div>
              <ul>
                {part.items.map((item, i) => <li key={i} className="t-small">{item}</li>)}
              </ul>
            </section>
          ))}
        </div>
      )}
    </div>
  );
}

interface EntryActions {
  onReact: (messageId: number, verdict: Verdict | null) => void;
  onUndo: (actionId: number) => void;
  onQuote: (text: string) => void;
  onDeleteNote: (noteId: number) => void;
}

/** A listening note: its text opens in place, and it can be deleted with its
 *  transcript, once asked twice. The day is the only place the app shows one. */
function Listening({ e, onDelete }: {
  e: Extract<DayEntry, { kind: "listening" }>;
  onDelete: (noteId: number) => void;
}) {
  const [open, setOpen] = useState(false);
  const [sure, setSure] = useState(false);
  const taking = e.status === "open";
  return (
    <div className="t-small update listening">
      Listening notes{e.title ? `: ${e.title}` : ""}{taking ? " · still listening" : ""}
      {e.text && (
        <button type="button" className="text-button t-meta show-note"
                aria-expanded={open} onClick={() => setOpen(!open)}>
          {open ? "Hide" : "Show"}
        </button>
      )}
      {!taking && !sure && (
        <button type="button" className="text-button t-meta delete-note"
                onClick={() => setSure(true)}>
          Delete
        </button>
      )}
      {sure && (
        <span className="t-meta confirm-delete">
          {" "}Delete it, with its transcript?
          <button type="button" className="text-button" onClick={() => onDelete(e.id)}>Yes</button>
          <button type="button" className="text-button" onClick={() => setSure(false)}>No</button>
        </span>
      )}
      {open && e.text && <div className="listening-text">{e.text}</div>}
      {e.doc_url && (
        <div>
          <Link href={e.doc_url}>Its summary</Link>
        </div>
      )}
    </div>
  );
}

function Body({ e, fresh, on }: { e: DayEntry; fresh: boolean; on: EntryActions }): ReactNode {
  switch (e.kind) {
    case "said":
      return (
        <>
          {(e.photos ?? 0) > 0 && <SentPhotos message={e.id} n={e.photos ?? 0} />}
          {e.text && <div className="slip t-body-user">{e.text}</div>}
        </>
      );
    case "reply":
      return <Prose text={e.text} className="t-body" id={e.key} drawIn={fresh} />;
    case "spoken":
      return <Spoken e={e} />;
    case "briefing":
      return <Prose text={e.text} className="t-body briefing" id={e.key} drawIn={fresh} />;
    case "update":
      return (
        <div className="update t-small">
          {inline(e.text)}
          {e.text.trim().endsWith("?") && (
            <button type="button" className="text-button t-meta" onClick={() => on.onQuote(e.text)}>
              Reply
            </button>
          )}
        </div>
      );
    case "action":
      if (e.status === "failed") {
        return (
          <div className="failed t-body">
            <strong className="failed-words">{e.line}.</strong> {e.error ?? ""}
          </div>
        );
      }
      return (
        <div className={`action t-small${e.status === "pending" ? " asked" : ""}`}>
          {e.line}
          {e.undo_until && <Countdown until={e.undo_until} onUndo={() => on.onUndo(e.id)} />}
        </div>
      );
    case "task":
      if (e.summary) return <Night e={e} />;
      return (
        <div className={`task task-${e.status}`}>
          <div className={e.status === "failed" ? "t-title failed-words" : "t-title"}>{e.title}</div>
          {e.detail && <div className="t-small">{e.detail}</div>}
        </div>
      );
    case "listening":
      return <Listening e={e} onDelete={on.onDeleteNote} />;
    case "asleep":
      return null;
  }
}

function Reactions({ e, onReact }: {
  e: Extract<DayEntry, { kind: "reply" }>;
  onReact: EntryActions["onReact"];
}) {
  const marks: { verdict: Verdict; mark: "check" | "cross"; name: string }[] = [
    { verdict: "right", mark: "check", name: "Right" },
    { verdict: "not right", mark: "cross", name: "Not right" },
  ];
  return (
    <span className={`reactions${e.reaction ? " reacted" : ""}`}>
      {marks.map(({ verdict, mark, name }) => (
        <button key={verdict} type="button" title={name} aria-label={name}
                aria-pressed={e.reaction === verdict}
                className={e.reaction === verdict ? "chosen" : undefined}
                onClick={() => onReact(e.id, e.reaction === verdict ? null : verdict)}>
          <HandMark name={mark} id={`${e.key}:${verdict}`} drawIn={e.reaction === verdict} />
        </button>
      ))}
    </span>
  );
}

function Entry({ e, time, fresh, seen, on }: {
  e: DayEntry;
  time: string;
  fresh: boolean;
  seen: boolean;
  on: EntryActions;
}) {
  if (e.kind === "asleep") {
    const how = e.gap === "asleep" ? "Asleep" : "Not running";
    return (
      <li className="entry asleep" data-key={e.key}>
        <HandMark name="wave" id={e.key} className="asleep-wave" />
        <span className="t-meta">{how} {clock(e.from)} – {clock(e.to)}</span>
      </li>
    );
  }
  const channel = e.channel ? CHANNELS[e.channel] : undefined;
  const late = sentLate(e);
  return (
    <li className={`entry kind-${e.kind}${fresh ? " new" : ""}${fresh && seen ? " seen" : ""}`}
        data-key={e.key}>
      <div className="gutter">
        {time && <time className="t-meta">{time}</time>}
        {late && <span className="t-meta sent">{late}</span>}
        {time && channel && <span className="channel t-meta">{channel}</span>}
        {e.kind === "reply" && <Reactions e={e} onReact={on.onReact} />}
        <Sign kind={signOf(e)} id={e.key} face={faceOf(e)} />
      </div>
      <div className="entry-body">
        <Body e={e} fresh={fresh} on={on} />
      </div>
    </li>
  );
}

/** Updates in a row, gathered under a bracket: the first two shown, and
    all of them once one past those is sought. */
function Run({ entries, sought, children }: {
  entries: DayEntry[];
  sought: string | null;
  children: (e: DayEntry) => ReactNode;
}) {
  const [open, setOpen] = useState(false);
  // Opened in this render, so the search finds its entry drawn; and kept
  // open after it is found.
  if (!open && sought !== null && entries.slice(2).some((e) => e.key === sought)) setOpen(true);
  const shown = open ? entries : entries.slice(0, 2);
  return (
    <li className="run">
      <div className="run-head">
        <HandMark name="bracket" id={entries[0].key} className="run-bracket" />
        <span className="t-meta">While you were away · {entries.length}</span>
      </div>
      <ol className="entries">
        {shown.map(children)}
      </ol>
      {!open && (
        <button type="button" className="text-button t-meta run-more" onClick={() => setOpen(true)}>
          {entries.length - 2} more
        </button>
      )}
    </li>
  );
}

/** Entries, with runs of updates gathered. */
function grouped(entries: DayEntry[]): (DayEntry | DayEntry[])[] {
  const out: (DayEntry | DayEntry[])[] = [];
  let run: DayEntry[] = [];
  const flush = () => {
    if (run.length >= RUN) out.push(run);
    else out.push(...run);
    run = [];
  };
  for (const e of entries) {
    if (e.kind === "update") run.push(e);
    else {
      flush();
      out.push(e);
    }
  }
  flush();
  return out;
}

function Page({ day, lens, fresh, seen, on, sought, lenses, onFold, children }: {
  day: Day;
  lens: Lens;
  fresh: ReadonlySet<string>;
  seen: ReadonlySet<string>;
  on: EntryActions;
  /** The entry a search is going to. */
  sought: string | null;
  lenses?: ReactNode;
  /** An earlier day, opened: back to its band. */
  onFold?: () => void;
  children?: ReactNode;
}) {
  const entries = day.entries.filter((e) => inLens(e, lens));
  // The time once per exchange, not on every line.
  const times = new Map(entries.map((e, i) => {
    const time = clock(e.at);
    return [e.key, i > 0 && clock(entries[i - 1].at) === time ? "" : time];
  }));
  const entry = (e: DayEntry) => (
    <Entry key={e.key} e={e} time={times.get(e.key) ?? ""} fresh={fresh.has(e.key)}
           seen={seen.has(e.key)} on={on} />
  );
  const evening = day.entries.some((e) => e.kind === "briefing" && e.which === "evening");
  return (
    <section className="day-sheet" data-day={day.date} aria-label={longDate(day.date)}>
      <header className="day-head">
        <div>
          <h2 className="hand-day day-title">
            {dayName(day.date, day.today)}
            <HandMark name="underline" id={`day:${day.date}`} className="day-underline" />
          </h2>
          <div className="t-small day-date">{longDate(day.date)}</div>
        </div>
        {lenses ?? (onFold && (
          <button type="button" className="text-button t-meta day-fold" onClick={onFold}>Fold</button>
        ))}
      </header>
      <ol className="entries">
        {grouped(entries).map((item) => (Array.isArray(item)
          ? <Run key={item[0].key} entries={item} sought={sought}>{entry}</Run>
          : entry(item)))}
        {children}
      </ol>
      {evening && <Dinkus />}
    </section>
  );
}

export function Dinkus() {
  return (
    <div className="dinkus" aria-hidden>
      <span className="sign sign-red" /><span className="sign sign-yellow" /><span className="sign sign-cyan" />
    </div>
  );
}

/** A run of folded days, or an open page, in the order of their dates. */
type Shelf = { bands: (FoldedDay | FoldedDay[])[] } | { page: Day };

/**
 * The days in date order: each open page, and between them the folded
 * days, a run of empty ones as one line. A week of empty days was seven
 * bands stacked above today on a fresh install, each as tall as two lines;
 * and an opened day was put below every band, not where it was.
 */
function shelves(folded: FoldedDay[], pages: Day[]): Shelf[] {
  const open = new Set(pages.map((p) => p.date));
  const days: ({ date: string; band: FoldedDay } | { date: string; page: Day })[] = [
    ...folded.filter((d) => !open.has(d.date)).map((band) => ({ date: band.date, band })),
    ...pages.map((page) => ({ date: page.date, page })),
  ].sort((a, b) => a.date.localeCompare(b.date));
  const out: Shelf[] = [];
  for (const day of days) {
    if ("page" in day) {
      out.push({ page: day.page });
      continue;
    }
    let shelf = out[out.length - 1];
    if (!shelf || !("bands" in shelf)) out.push(shelf = { bands: [] });
    const before = shelf.bands[shelf.bands.length - 1];
    if (day.band.count > 0) shelf.bands.push(day.band);
    else if (Array.isArray(before)) before.push(day.band);
    else shelf.bands.push([day.band]);
  }
  return out;
}

function Folded({ day, onOpen }: { day: FoldedDay; onOpen: (date: string) => void }) {
  return (
    <li>
      <button type="button" className="folded" data-day={day.date} onClick={() => onOpen(day.date)}>
        <span className="folded-name">
          <span className="t-label">{localDate(day.date).toLocaleDateString([], { weekday: "long" })}</span>
          <span className="t-meta">{localDate(day.date).toLocaleDateString([], { day: "numeric", month: "long" })}</span>
        </span>
        <span className="t-small folded-line">{day.line}</span>
        <span className="t-meta folded-count">{day.count}</span>
      </button>
    </li>
  );
}

/** Empty days in a row: one line, nothing to open. */
function Quiet({ days }: { days: FoldedDay[] }) {
  const first = localDate(days[0].date);
  const when = days.length === 1
    ? first.toLocaleDateString([], { weekday: "long", day: "numeric", month: "long" })
    : new Intl.DateTimeFormat([], { day: "numeric", month: "long" })
      .formatRange(first, localDate(days[days.length - 1].date));
  return <li className="t-meta folded-quiet">{when}: nothing happened</li>;
}

/** The photos that came with the user's message, as the day shows them
 *  (agent/worker/app_photos.py); "a photo" once its day is over. */
function SentPhotos({ message, n }: { message: number; n: number }) {
  return (
    <ul className="photos" aria-label={n === 1 ? "A photo" : `${n} photos`}>
      {Array.from({ length: n }, (_, i) => <SentPhoto key={i} message={message} n={i} />)}
    </ul>
  );
}

function SentPhoto({ message, n }: { message: number; n: number }) {
  const url = useSentPhoto(message, n);
  return url === "gone"
    ? <li className="photo gone t-meta">a photo</li>
    : <li className="photo">{url && <img src={url} alt="A photo you sent" />}</li>;
}

/** A photo chosen for the next message, or one on its way. */
function Thumb({ file, onRemove }: { file: File; onRemove?: () => void }) {
  const url = useFileURL(file);
  return (
    <li className="thumb">
      {url && <img src={url} alt={file.name || "A photo"} />}
      {onRemove && (
        <button type="button" className="thumb-remove" onClick={onRemove}
                aria-label={`Take off ${file.name || "this photo"}`}>
          <HandMark name="cross" id={`off:${file.name}`} />
        </button>
      )}
    </li>
  );
}

function Composer({ draft, setDraft, picked, left, onPick, onUnpick, onSend, listening, voice,
                    talk, hotkey, onTalkStart, onTalkEnd, input }: {
  draft: string;
  setDraft: (text: string) => void;
  /** The photos chosen for the message, above the composer. */
  picked: Picked[];
  /** Which were left out, and why. */
  left: string | null;
  onPick: (files: File[]) => void;
  onUnpick: (id: number) => void;
  onSend: (text: string, photos: File[]) => void;
  listening: boolean;
  voice: VoiceWarm | null;
  /** Talk, at the sidebar's foot: off, there is no microphone button. */
  talk: boolean;
  /** The one hotkey, while one is bound. */
  hotkey: string | null;
  onTalkStart: () => void;
  onTalkEnd: () => void;
  input: RefObject<HTMLTextAreaElement | null>;
}) {
  const ready = voice?.state === "ready";
  const anywhere = hotkey ? `, or hold ${hotkeyLabel(hotkey)} anywhere` : "";
  // Held, its ring follows the voice's level (journal.md, The composer).
  const button = useRef<HTMLButtonElement>(null);
  useLevel(button, listening);
  const files = useRef<HTMLInputElement>(null);
  const submit = () => {
    const text = draft.trim();
    if (!text && picked.length === 0) return;
    onSend(text, picked.map((p) => p.file));
    setDraft("");
  };
  return (
    <>
    {picked.length > 0 && (
      <ul className="attached" aria-label="Photos to send">
        {picked.map((p) => <Thumb key={p.id} file={p.file} onRemove={() => onUnpick(p.id)} />)}
      </ul>
    )}
    {left && <p className="attached-left t-meta" role="status">{left}</p>}
    <div className={`composer${listening ? " listening" : ""}`}>
      <textarea ref={input} className="t-body" value={draft} rows={1}
                placeholder={listening ? "Listening" : "Write to Steve"}
                aria-label="Write to Steve"
                onChange={(e) => setDraft(e.target.value)}
                onPaste={(e) => {
                  // A screenshot or a copied photo: added, not pasted as
                  // text. Words on the clipboard paste as ever.
                  const pasted = [...e.clipboardData.files];
                  if (pasted.length === 0) return;
                  e.preventDefault();
                  onPick(pasted);
                }}
                onKeyDown={(e) => {
                  // Enter sends and Shift+Enter is a new line, as in every
                  // chat: the other way traps people who type fast.
                  if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                    e.preventDefault();
                    submit();
                  }
                }} />
      {(draft.trim() || picked.length > 0) && (
        <button type="button" className="send" aria-label="Send" onClick={submit}>
          <HandMark name="arrow" id="send" />
        </button>
      )}
      <button type="button" className="attach" aria-label="Add a photo"
              title="Add a photo, or paste or drop one on the page"
              onClick={() => files.current?.click()}>
        <HandMark name="clip" id="attach" className="attach-clip" />
      </button>
      <input ref={files} type="file" hidden multiple accept={FORMATS.join(",")}
             onChange={(e) => {
               onPick([...(e.target.files ?? [])]);
               // The same file again is a change.
               e.target.value = "";
             }} />
      {talk && <button type="button" ref={button} className={`voice${listening ? " on" : ""}`}
              aria-label={ready ? `Hold to talk${anywhere}` : "Speech is still loading"}
              title={ready ? `Hold to talk${anywhere}` : voice?.state === "failed"
                ? `Voice unavailable: ${voice.error ?? ""}` : "Speech loads on the first press"}
              disabled={voice?.state === "failed"}
              // Pointer events, not a click: the turn ends when the button
              // is let go, including when the pointer wanders off it.
              onPointerDown={onTalkStart}
              onPointerUp={onTalkEnd}
              onPointerLeave={() => listening && onTalkEnd()}>
        <HandMark name="mic" id="voice" className="voice-mic" />
      </button>}
    </div>
    </>
  );
}

/** A day to go to, and with a search, the entry on it; or the foot of the
 *  page, where the newest exchange is. */
export type GoTo = { date: string; key?: string; n: number } | { foot: true; n: number };

export interface DayPageProps {
  pages: Day[];
  folded: FoldedDay[];
  /** An empty week came back: earlier days wait for a click. */
  quiet: boolean;
  fresh: ReadonlySet<string>;
  onEarlier: () => void;
  onOpen: (date: string) => void;
  onFold: (date: string) => void;
  /** Go to this day (from the sidebar), whenever `n` changes. */
  goTo: GoTo | null;
  unsent: Unsent[];
  onRetry: (id: number) => void;
  /** Steve is writing a reply: what it is doing, or null for the diamond alone. */
  working: string | null | undefined;
  /** The last turn failed: why. */
  failure: string | null;
  onSend: (text: string, photos: File[]) => void;
  onReact: (messageId: number, verdict: Verdict | null) => void;
  onUndo: (actionId: number) => void;
  /** Delete a listening note, with its transcript. */
  onDeleteNote: (noteId: number) => void;
  name: string | null;
  listening: boolean;
  voice: VoiceWarm | null;
  /** The one hotkey, while one is bound. */
  hotkey: string | null;
  /** Talk, at the sidebar's foot; on unless said. */
  talk?: boolean;
  onTalkStart: () => void;
  onTalkEnd: () => void;
  onScreen: boolean;
  /** Above the pages: the backend's state while it isn't running. */
  top?: ReactNode;
  /** Notes on a call can be taken: the voice add-on is installed. */
  callNotes?: boolean;
  /** Steve can build an extension: the sandbox works here. */
  canBuild?: boolean;
  /** The day in view has changed: the sidebar marks it. */
  onDayInView?: (date: string | null) => void;
}

/** An empty day's ways in. Each fills the composer, its blank chosen, and
 *  sends nothing: nothing runs or costs until the user presses Enter. A
 *  blank page with a chat box showed none of what Steve does. */
const STARTERS: { text: string; needs?: "callNotes" | "canBuild" }[] = [
  { text: "Remind me to … at 5 pm" },
  { text: "Take notes on my next call", needs: "callNotes" },
  { text: "What's on my calendar tomorrow?" },
  { text: "Read this page for me: …" },
  { text: "Build me a tool that …", needs: "canBuild" },
];

export function DayPage(props: DayPageProps) {
  const { pages, folded, quiet, fresh, onEarlier, onOpen, onFold, goTo, unsent, onRetry, working,
          failure, onSend, onReact, onUndo, onDeleteNote, name, onScreen, top } = props;
  const [lens, setLens] = useState<Lens>("all");
  // A search's entry, sought until the page draws it, with every kind shown
  // so it is there. Set in the render `goTo` changes, not an effect after.
  const [sought, setSought] = useState<string | null>(null);
  const [wentTo, setWentTo] = useState<GoTo | null>(null);
  if (goTo !== wentTo) {
    setWentTo(goTo);
    if (goTo && "key" in goTo && goTo.key) {
      setLens("all");
      setSought(goTo.key);
    }
  }
  const [draft, setDraft] = useState("");
  // The photos for the next message: chosen, pasted or dropped on the page.
  const [picked, setPicked] = useState<Picked[]>([]);
  const [left, setLeft] = useState<string | null>(null);
  const [dropping, setDropping] = useState(false);
  const pickSeq = useRef(0);
  const pick = (files: File[]) => {
    const { add, left: out } = choose(picked.length, files);
    setLeft(out);
    setPicked([...picked, ...add.map((file) => ({ id: ++pickSeq.current, file }))]);
  };
  const sendNow = (text: string, photos: File[]) => {
    onSend(text, photos);
    setPicked([]);
    setLeft(null);
  };
  const scroller = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const stuck = useRef(true);
  const last = pages[pages.length - 1];

  // A starter's text, its "…" chosen so the first key typed replaces it.
  // The selection is set once the textarea holds the text, after the render.
  const blank = useRef<number | null>(null);
  const start = (text: string) => {
    blank.current = text.indexOf("…");
    setDraft(text);
  };
  useEffect(() => {
    const box = input.current;
    const at = blank.current;
    if (at === null || !box) return;
    blank.current = null;
    box.focus();
    if (at >= 0) box.setSelectionRange(at, at + 1);
    else box.setSelectionRange(box.value.length, box.value.length);
  }, [draft]);
  const starters = STARTERS.filter((s) => !s.needs || props[s.needs]);

  const onQuote = useCallback((text: string) => {
    setDraft(`> ${text.trim()}\n`);
    input.current?.focus();
  }, []);
  const on: EntryActions = { onReact, onUndo, onQuote, onDeleteNote };

  // The callbacks the effects below call, as they are now: the effects
  // themselves run only when what they watch changes.
  const now = useRef({ onOpen, lastDate: last?.date });
  useEffect(() => {
    now.current = { onOpen, lastDate: last?.date };
  });

  // The keyboard (journal.md, Keyboard): the lenses, the days, the composer.
  useEffect(() => {
    /** The day above or below the one at the top of the view. */
    const step = (by: -1 | 1) => {
      const box = scroller.current;
      if (!box) return;
      const marks = [...box.querySelectorAll<HTMLElement>("[data-day]")];
      const edge = box.getBoundingClientRect().top + 4;
      const current = marks.reduce((at, el, i) => (el.getBoundingClientRect().top <= edge ? i : at), 0);
      const target = marks[Math.max(0, Math.min(marks.length - 1, current + by))];
      if (!target) return;
      if (target.classList.contains("folded")) now.current.onOpen(target.dataset.day ?? "");
      target.scrollIntoView({ block: "start" });
    };
    const key = (e: KeyboardEvent) => {
      if (e.ctrlKey && !e.altKey && ["1", "2", "3"].includes(e.key)) {
        e.preventDefault();
        setLens(LENSES[Number(e.key) - 1].key);
      } else if (e.ctrlKey && (e.key === "ArrowUp" || e.key === "ArrowDown")) {
        e.preventDefault();
        step(e.key === "ArrowUp" ? -1 : 1);
      } else if (e.key === "Escape" && !e.defaultPrevented) {
        input.current?.focus();
      }
    };
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  }, []);

  // From the sidebar: that day, opened if it was folded. From a search, the
  // effect below goes on to its entry. From the quick bar's "open the app",
  // a toast or the desk card: the foot, wherever the user had scrolled, and
  // not the day the sidebar last went to, which this page, drawn again,
  // went back to.
  useEffect(() => {
    if (!goTo) return;
    if ("foot" in goTo) {
      stuck.current = true;
      const box = scroller.current;
      if (box) box.scrollTop = box.scrollHeight;
      return;
    }
    const el = scroller.current?.querySelector<HTMLElement>(`[data-day="${goTo.date}"]`);
    if (el?.classList.contains("folded")) now.current.onOpen(goTo.date);
    if (goTo.key) {
      stuck.current = false;
      return;
    }
    el?.scrollIntoView({ block: "start" });
    stuck.current = goTo.date === now.current.lastDate;
  }, [goTo]);

  // The entry sought, once the page draws it: in view, and marked a moment.
  // The mark is an attribute React doesn't own, so a render leaves it.
  useEffect(() => {
    if (!sought) return;
    const line = scroller.current?.querySelector<HTMLElement>(`[data-key="${CSS.escape(sought)}"]`);
    if (!line) return;
    setSought(null);
    line.scrollIntoView({ block: "center" });
    line.dataset.sought = "";
    window.setTimeout(() => delete line.dataset.sought, SOUGHT_MS);
  }, [sought, lens, pages]);

  // Kept at the foot while the user is there: a new entry or a reply in
  // the making scrolls into view; reading further up, nothing moves.
  const lastKey = last?.entries[last.entries.length - 1]?.key;
  useLayoutEffect(() => {
    const box = scroller.current;
    if (box && stuck.current) box.scrollTop = box.scrollHeight;
  }, [lastKey, unsent.length, working, failure, pages.length]);
  // The days before, loaded by scrolling up to near the top; after an empty
  // week, or on a page too short to scroll, only by the link. They loaded
  // whenever the top was in view: a short page always shows it, so a fresh
  // install loaded a week at once, and a day opened near the top loaded the
  // week before it too. At the top Chromium keeps no place to hold, so each
  // week pushed what was being read below the window. Only a move up the
  // user made asks: the wheel, a key, the scrollbar or a finger, within a
  // second, and with the page's height unchanged. A page folded or opened
  // moves the scroll without anyone asking, and the height alone wasn't
  // enough: under load the browser's clamp of the scroll came in a later
  // event, at the new height, and asked for a week no one wanted.
  const was = useRef({ top: 0, height: 0 });
  // Never, at first: the page's clock starts near nought when it loads.
  const byHand = useRef(-Infinity);
  const moved = () => {
    byHand.current = performance.now();
  };
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (["PageUp", "ArrowUp", "Home"].includes(event.key)) byHand.current = performance.now();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  // The day in view, told to the sidebar to mark: the newest at the foot,
  // where its last lines are, and otherwise the day across the view's
  // middle. The sidebar marked Today whatever the window showed, a page of
  // yesterday among it. Told after every render too, as a day opened,
  // folded or come with a poll moves what is in view without a scroll.
  const toldDay = useRef<string | null>(null);
  const tellDay = () => {
    const box = scroller.current;
    const marks = box ? [...box.querySelectorAll<HTMLElement>("[data-day]")] : [];
    if (!box || !marks.length || !props.onDayInView) return;
    const view = box.getBoundingClientRect();
    const middle = view.top + view.height / 2;
    const atFoot = box.scrollHeight - box.scrollTop - box.clientHeight < 4;
    const shown = atFoot ? marks[marks.length - 1]
      : marks.reduce((at, el) => (el.getBoundingClientRect().top <= middle ? el : at), marks[0]);
    const date = shown.dataset.day ?? null;
    if (date === toldDay.current) return;
    toldDay.current = date;
    props.onDayInView(date);
  };
  useEffect(tellDay);
  const onScroll = () => {
    const box = scroller.current;
    if (!box) return;
    tellDay();
    stuck.current = box.scrollHeight - box.scrollTop - box.clientHeight < 120;
    const up = box.scrollTop < was.current.top && box.scrollHeight === was.current.height
      && performance.now() - byHand.current < 1000;
    was.current = { top: box.scrollTop, height: box.scrollHeight };
    if (up && box.scrollTop < 200 && last !== undefined && !quiet) onEarlier();
  };

  // A new entry's highlight fades three seconds after it has been on
  // screen: kept in state, so a redraw of the entry doesn't bring it back.
  const [seen, setSeen] = useState<ReadonlySet<string>>(new Set());
  useEffect(() => {
    const box = scroller.current;
    if (!box || !onScreen) return;
    const watch = new IntersectionObserver((found) => {
      const keys = found.filter((f) => f.isIntersecting)
        .map((f) => (f.target as HTMLElement).dataset.key ?? "");
      if (keys.length) setSeen((all) => new Set([...all, ...keys]));
    }, { root: box });
    box.querySelectorAll(".entry.new:not(.seen)").forEach((el) => watch.observe(el));
    return () => watch.disconnect();
  });

  const empty = pages.every((p) => p.entries.length === 0) && folded.every((d) => d.count === 0);
  // The window lets a message go once a page holds it (App.tsx), so what
  // it holds is what waits.
  const waiting = unsent;
  const talking = lens !== "log";

  return (
    <div className={`day-page${dropping ? " dropping" : ""}`}
         onDragOver={(e) => {
           if (!e.dataTransfer.types.includes("Files")) return;
           e.preventDefault();
           setDropping(true);
         }}
         onDragLeave={(e) => {
           if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setDropping(false);
         }}
         onDrop={(e) => {
           if (e.dataTransfer.files.length === 0) return;
           e.preventDefault();
           setDropping(false);
           pick([...e.dataTransfer.files]);
         }}>
      <div className="day-stream" ref={scroller} onScroll={onScroll} onWheel={moved}
           onTouchMove={moved}
           onPointerDown={(e) => {
             // On the scrollbar, not on a button in the page, such as Fold.
             if (e.target === e.currentTarget) moved();
           }}>
        <div className="day-column">
          {top}
          {last && (
            <button type="button" className="text-button t-meta earlier" onClick={onEarlier}>
              Earlier days
            </button>
          )}
          {shelves(folded, pages).map((shelf) => ("bands" in shelf ? (
            <ol key={`bands:${[shelf.bands[0]].flat()[0].date}`} className="folded-days">
              {shelf.bands.map((band) => (Array.isArray(band)
                ? <Quiet key={band[0].date} days={band} />
                : <Folded key={band.date} day={band} onOpen={onOpen} />))}
            </ol>
          ) : (
            <Page key={shelf.page.date} day={shelf.page} lens={lens} fresh={fresh} seen={seen} on={on}
                  sought={sought}
                  onFold={shelf.page === last ? undefined : () => onFold(shelf.page.date)}
                  lenses={shelf.page === last ? (
                    <nav className="lenses" aria-label="Show">
                      {LENSES.map(({ key, name: shown }) => (
                        <button key={key} type="button" aria-pressed={lens === key}
                                className="t-label" onClick={() => setLens(key)}>
                          {shown}
                          {lens === key && <HandMark name="circle" id={`lens:${key}`} className="lens-circle" />}
                        </button>
                      ))}
                    </nav>
                  ) : undefined}>
              {shelf.page === last && (
                <>
                  {empty && waiting.length === 0 && working === undefined && (
                    <li className="entry kind-reply first-day">
                      <div className="gutter" />
                      <div className="entry-body"><Prose text={firstWords(name)} className="t-body" /></div>
                    </li>
                  )}
                  {/* Gone while the composer holds anything: a starter
                      would write over what the user had typed. */}
                  {last.entries.length === 0 && waiting.length === 0 && working === undefined
                    && !draft && (
                    <li className="entry starting">
                      <div className="gutter" />
                      <div className="entry-body">
                        <ul className="starters" aria-label="Ways to start">
                          {starters.map((s) => (
                            <li key={s.text}>
                              <button type="button" className="starter t-small" onClick={() => start(s.text)}>
                                {s.text}
                              </button>
                            </li>
                          ))}
                        </ul>
                      </div>
                    </li>
                  )}
                  {talking && waiting.map((u) => (
                    <li key={`unsent:${u.id}`} className={`entry kind-said unsent${u.failed ? " failed" : ""}`}>
                      <div className="gutter" />
                      <div className="entry-body">
                        {(u.photos?.length ?? 0) > 0 && (
                          <ul className="photos" aria-label="Photos sent">
                            {u.photos?.map((file, i) => <Thumb key={i} file={file} />)}
                          </ul>
                        )}
                        {u.text && <div className="slip t-body-user">{u.text}</div>}
                        {u.failed && (
                          <button type="button" className="text-button t-meta not-sent"
                                  onClick={() => onRetry(u.id)}>
                            Not sent · Retry
                          </button>
                        )}
                      </div>
                    </li>
                  ))}
                  {talking && working !== undefined && (
                    <li className="entry working" aria-live="polite">
                      <div className="gutter"><Sign kind="turning" id="working" face="t-small" /></div>
                      <div className="entry-body t-small">{working ?? ""}</div>
                    </li>
                  )}
                  {talking && failure && (
                    <li className="entry kind-action">
                      <div className="gutter"><Sign kind="failed" id={`failure:${failure}`} face="t-body" /></div>
                      <div className="entry-body failed t-body">
                        <strong className="failed-words">Couldn't answer.</strong> {failure}
                      </div>
                    </li>
                  )}
                </>
              )}
            </Page>
          )))}
        </div>
      </div>
      <div className="day-foot">
        <Composer draft={draft} setDraft={setDraft} onSend={sendNow} input={input}
                  picked={picked} left={left} onPick={pick}
                  onUnpick={(id) => setPicked(picked.filter((p) => p.id !== id))}
                  listening={props.listening} voice={props.voice} hotkey={props.hotkey}
                  talk={props.talk ?? true}
                  onTalkStart={props.onTalkStart} onTalkEnd={props.onTalkEnd} />
      </div>
    </div>
  );
}
