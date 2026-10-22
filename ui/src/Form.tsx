/* The back pages' forms (docs/design/back-pages.md, Forms): a text input
   with no box, a drawn checkbox rather than a switch, a choice of a few as
   words in a row, a number with its bounds, and "Saved" beside what was
   changed. A change saves as it is made: no Save button, except where it
   costs money or reaches outward.

   The views moved in from the tabs had a Save button for a whole group of
   knobs, and said "Saved; in force now." in a toast in the corner: the
   user changed one number and looked elsewhere to learn whether it took. */

import { useId, useState, type ReactNode } from "react";
import { type Knob } from "./control";
import { HandMark } from "./HandMark";
import { type Said, type Say, useSaid } from "./pages";
import "./forms.css";

export function Section({ title, why, actions, children }: {
  title: string;
  /** What the section is for, and what changing it does. */
  why?: ReactNode;
  /** Beside the title: a section's brake, say. */
  actions?: ReactNode;
  children?: ReactNode;
}) {
  return (
    <section className="form-section">
      <div className="form-section-head">
        <h2 className="form-title">{title}</h2>
        {actions}
      </div>
      {why && <p className="form-why">{why}</p>}
      {children}
    </section>
  );
}

/** A drawn check and "Saved", which fades; or what went wrong, which stays. */
export function Saved({ said }: { said: Said | null }) {
  return (
    <span className="form-said" role="status">
      {said && (said.ok ? (
        <span className="form-saved" key={said.n}>
          <HandMark name="check" id={`saved-${said.n}`} drawIn className="form-saved-mark" />
          {said.text}
        </span>
      ) : <span className="form-error">{said.text}</span>)}
    </span>
  );
}

export function Field({ label, hint, error, children }: {
  label: string;
  hint?: ReactNode;
  error?: string | null;
  children: ReactNode;
}) {
  return (
    <label className="form-field">
      <span className="form-label">{label}</span>
      {children}
      {hint && <span className="form-hint">{hint}</span>}
      {error && <span className="form-error" role="alert">{error}</span>}
    </label>
  );
}

/** A box, and when checked a check drawn with the box's own line inside it.
 *  Until 19 October the check was the hand's brush mark, bigger than the
 *  box and set a little differently on each: beside the box's thin, even
 *  line it looked like two drawings, and a check placed far up and to the
 *  right looked misplaced rather than handwritten. */
export function Check({ label, note, checked, disabled, onChange, inline, title }: {
  label: ReactNode;
  note?: ReactNode;
  checked: boolean;
  disabled?: boolean;
  onChange: (on: boolean) => void;
  /** In a line, at the line's size, rather than a row of its own. */
  inline?: boolean;
  title?: string;
}) {
  return (
    <label className={inline ? "form-check inline" : "form-check"} title={title}>
      <input type="checkbox" checked={checked} disabled={disabled}
             onChange={(e) => onChange(e.target.checked)} />
      <span className="form-check-box" aria-hidden="true">
        {checked && (
          <svg className="form-check-mark" viewBox="0 0 16 16" data-mark="check">
            <path d="M3.6 8.4 L6.6 11.4 L12.4 4.8" />
          </svg>
        )}
      </span>
      <span className="form-check-text">
        <span>{label}</span>
        {note && <span className="form-hint">{note}</span>}
      </span>
    </label>
  );
}

/** A choice of a few: the options as words in a row, the chosen one
 *  underlined. */
export function Choices<T extends string>({ label, value, options, disabled, onChange }: {
  label: string;
  value: T;
  options: readonly (readonly [T, string])[];
  disabled?: boolean;
  onChange: (value: T) => void;
}) {
  return (
    <div className="form-field">
      <span className="form-label">{label}</span>
      <div className="form-choices" role="radiogroup" aria-label={label}>
        {options.map(([option, says]) => (
          <button key={option} type="button" role="radio" aria-checked={option === value}
                  className={option === value ? "chosen" : undefined} disabled={disabled}
                  onClick={() => option !== value && onChange(option)}>
            {says}
          </button>
        ))}
      </div>
    </div>
  );
}

/** Languages and regions offered, with the one held whatever it is. Named
 *  in English by the browser, which knows every tag. */
const LANGUAGES = ["en-US", "en-GB", "en-CA", "en-AU", "en-IE", "en-IN", "en-NZ", "fr-FR",
  "fr-CA", "de-DE", "es-ES", "es-MX", "it-IT", "pt-BR", "pt-PT", "nl-NL", "sv-SE", "da-DK",
  "nb-NO", "fi-FI", "pl-PL", "cs-CZ", "tr-TR", "ja-JP", "ko-KR", "zh-CN", "zh-TW"];
const REGIONS = ["US", "GB", "CA", "AU", "IE", "IN", "NZ", "FR", "DE", "ES", "MX", "IT", "BR",
  "PT", "NL", "BE", "CH", "AT", "SE", "DK", "NO", "FI", "PL", "CZ", "TR", "JP", "KR", "CN",
  "TW", "SG", "ZA"];

function named(tag: string, type: "language" | "region"): string {
  try {
    return new Intl.DisplayNames(["en"], { type }).of(tag) ?? tag;
  } catch {
    return tag;
  }
}

function step(knob: Knob): number {
  if (knob.kind === "minutes") return 5;
  if (knob.kind !== "usd") return 1;
  const hi = Number(knob.hi);
  return hi > 20 ? 1 : hi > 2 ? 0.5 : 0.05;
}

function shown(knob: Knob, value: number | string): string {
  return knob.kind === "usd" ? Number(value).toFixed(2) : String(value);
}

const SWITCH = ["off", "on"];

/** One knob, saved as it is changed: `onSave` answers an error, or null.
 *  null as a value puts the knob back to its default. */
export function KnobField({ knob, onSave }: {
  knob: Knob;
  onSave: (name: string, value: number | string | null) => Promise<string | null>;
}) {
  const [draft, setDraft] = useState<string | null>(null);
  const [said, setSaid] = useState<Said | null>(null);
  const [busy, setBusy] = useState(false);
  const id = useId();

  const save = async (value: number | string | null) => {
    setBusy(true);
    const error = await onSave(knob.name, value);
    setBusy(false);
    setDraft(null);
    setSaid({ text: error ?? "Saved", ok: error === null, n: (said?.n ?? 0) + 1 });
  };

  const moved = String(knob.value) !== String(knob.default);
  const back = moved && (
    <button type="button" className="form-quiet" disabled={busy} onClick={() => void save(null)}>
      Back to {knob.kind === "choice"
        ? knob.choices?.find(([v]) => v === knob.default)?.[1] ?? String(knob.default)
        : shown(knob, knob.default)}
    </button>
  );

  if (knob.kind === "choice" && knob.choices?.map(([v]) => v).join() === SWITCH.join()) {
    return (
      <div className="form-knob" data-knob={knob.name}>
        <Check label={knob.says} checked={knob.value === "on"} disabled={busy}
               note={knob.adds && `${knob.adds} ${knob.takes ?? ""}`}
               onChange={(on) => void save(on ? "on" : "off")} />
        <Saved said={said} />
      </div>
    );
  }

  if (knob.kind === "choice" && (knob.choices?.length ?? 0) <= 3) {
    return (
      <div className="form-knob" data-knob={knob.name}>
        <Choices label={knob.says} value={String(knob.value)} options={knob.choices ?? []}
                 disabled={busy} onChange={(v) => void save(v)} />
        <div className="form-knob-foot">{back}<Saved said={said} /></div>
      </div>
    );
  }

  if (knob.kind === "choice" || knob.kind === "language" || knob.kind === "region") {
    const options: [string, string][] = knob.kind === "choice" ? knob.choices ?? []
      : [...new Set([String(knob.value), ...(knob.kind === "language" ? LANGUAGES : REGIONS)])]
        .map((tag) => [tag, named(tag, knob.kind as "language" | "region")]);
    return (
      <div className="form-knob" data-knob={knob.name}>
        <Field label={knob.says}>
          <select className="form-select" value={String(knob.value)} disabled={busy}
                  onChange={(e) => void save(e.target.value)}>
            {options.map(([v, says]) => <option key={v} value={v}>{says}</option>)}
          </select>
        </Field>
        <div className="form-knob-foot">{back}<Saved said={said} /></div>
      </div>
    );
  }

  if (knob.kind === "clock") {
    const value = draft ?? String(knob.value);
    return (
      <div className="form-knob" data-knob={knob.name}>
        <Field label={knob.says}>
          <input type="time" className="form-input form-time" value={value} disabled={busy}
                 onChange={(e) => setDraft(e.target.value)}
                 onBlur={() => draft !== null && draft !== String(knob.value) && void save(draft)} />
        </Field>
        <div className="form-knob-foot">{back}<Saved said={said} /></div>
      </div>
    );
  }

  // A number with bounds: − and + save at once; typed, it saves on Enter
  // or on leaving the box.
  const lo = Number(knob.lo);
  const hi = Number(knob.hi);
  const now = Number(draft ?? knob.value);
  const by = (sign: 1 | -1) => {
    const next = Math.min(hi, Math.max(lo, Math.round((now + sign * step(knob)) * 100) / 100));
    if (next !== Number(knob.value)) void save(next);
  };
  const commit = () => {
    if (draft !== null && draft.trim() !== "" && Number(draft) !== Number(knob.value)) {
      void save(Number(draft));
    } else setDraft(null);
  };
  return (
    <div className="form-knob" data-knob={knob.name}>
      <div className="form-field form-number">
        <label className="form-label" htmlFor={id}>{knob.says}</label>
        <div>
          <button type="button" aria-label={`Less: ${knob.says}`} disabled={busy || now <= lo}
                  onClick={() => by(-1)}>−</button>
          <input id={id} className="form-input" inputMode="decimal"
                 value={draft ?? shown(knob, knob.value)} disabled={busy}
                 onChange={(e) => setDraft(e.target.value)} onBlur={commit}
                 onKeyDown={(e) => e.key === "Enter" && commit()} />
          <button type="button" aria-label={`More: ${knob.says}`} disabled={busy || now >= hi}
                  onClick={() => by(1)}>+</button>
          <Saved said={said} />
        </div>
        <span className="form-hint">
          between {shown(knob, lo)} and {shown(knob, hi)}{back && " · "}{back}
        </span>
      </div>
    </div>
  );
}

/** Destructive: text in red ink, then a second step that names what will
 *  be lost. */
export function Forget({ label = "Forget", asks, busy, onConfirm }: {
  label?: string;
  /** "Forget everything Steve knows about Theo?" */
  asks: string;
  busy?: boolean;
  onConfirm: () => void;
}) {
  const [asking, setAsking] = useState(false);
  if (!asking) {
    return (
      <button type="button" className="form-danger" disabled={busy}
              onClick={() => setAsking(true)}>{label}</button>
    );
  }
  return (
    <span className="form-asking">
      <span>{asks}</span>
      <button type="button" className="form-danger" disabled={busy}
              onClick={() => { setAsking(false); onConfirm(); }}>{label}</button>
      <button type="button" className="form-quiet" onClick={() => setAsking(false)}>Keep</button>
    </span>
  );
}

/** A page that says what its changes did in a line at its top: Accounts,
 *  Extensions and Browser logins, which said it in the window's toasts. */
export function Saying({ children }: { children: (say: Say) => ReactNode }) {
  const [said, say] = useSaid();
  return (
    <>
      <p className="page-said"><Saved said={said} /></p>
      {children(say)}
    </>
  );
}
