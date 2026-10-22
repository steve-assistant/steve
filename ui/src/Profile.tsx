/* Profile (back-pages.md, About you): who the user is, as the facts that
   lead every turn hold it (/control/profile), with where home was placed;
   and their day's times, which are knobs: quiet hours and the briefings.
   Each saves as it is changed. */

import { useState } from "react";
import { control, type Knob } from "./control";
import { Field, KnobField, Saved, Section } from "./Form";
import { saveKnob, usePolled, type Said } from "./pages";

const FIELDS = [
  ["name", "Your name", "What Steve calls you, and signs for you."],
  ["pronouns", "Pronouns", null],
  ["home_city", "Home city", "Where the weather and the day's times are read from."],
  ["occupation", "Work", null],
] as const;

type Key = (typeof FIELDS)[number][0];

// The day's times, in the order a day runs.
const TIMES = ["MORNING", "EVENING", "QUIET_FROM", "QUIET_UNTIL"];

function Line({ field, label, hint, kept, onKept }: {
  field: Key;
  label: string;
  hint: string | null;
  kept: string;
  onKept: (value: string, placed: string | null) => void;
}) {
  const [draft, setDraft] = useState(kept);
  const [said, setSaid] = useState<Said | null>(null);
  const [placed, setPlaced] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // A value kept elsewhere (setup, a chat) shows here at the next look,
  // unless the line is being edited. Keying the line by its value did this
  // too, but remounted it at its own save, losing "Saved" and where home
  // was placed the moment they were said.
  const [seen, setSeen] = useState(kept);
  if (kept !== seen) {
    setSeen(kept);
    if (draft.trim() === seen) setDraft(kept);
  }

  const keep = async () => {
    const value = draft.trim();
    if (value === kept) return;
    if (field === "name" && value === "") {
      setSaid({ text: "Steve needs a name to call you.", ok: false, n: (said?.n ?? 0) + 1 });
      return;
    }
    setBusy(true);
    const done = await control.setProfile({ [field]: value });
    setBusy(false);
    setSaid({ text: done.ok ? "Saved" : done.error ?? "That wasn't kept.", ok: done.ok,
              n: (said?.n ?? 0) + 1 });
    if (done.ok) {
      setPlaced(done.home?.says ?? null);
      onKept(value, done.home?.says ?? null);
    }
  };

  return (
    <div className="profile-line" data-field={field}>
      <Field label={label} hint={placed ?? hint}>
        <input className="form-input" value={draft} disabled={busy}
               onChange={(e) => setDraft(e.target.value)} onBlur={() => void keep()}
               onKeyDown={(e) => e.key === "Enter" && void keep()} />
      </Field>
      <Saved said={said} />
    </div>
  );
}

export function Profile({ onScreen, onName }: {
  onScreen: boolean;
  /** The sidebar's name, changed here. */
  onName: (name: string) => void;
}) {
  const profile = usePolled((signal) => control.profile(signal), { everyMs: 60000, onScreen });
  const autonomy = usePolled((signal) => control.autonomy(signal), { everyMs: 60000, onScreen });
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const knobs = TIMES.map((name) => autonomy.data?.knobs.find((k) => k.name === name))
    .filter((k): k is Knob => Boolean(k));

  return (
    <div className="panel form-page profile">
      <Section title="Who you are" why={
        "Steve reads this before every reply. It is the same as what you tell it in a "
        + "conversation: “I moved to Leeds” and the city typed here are one fact, and the "
        + "newer wins."}>
        {profile.data ? FIELDS.map(([field, label, hint]) => (
          <Line key={field} field={field} label={label} hint={hint}
                kept={profile.data?.profile[field] ?? ""}
                onKept={(value) => {
                  if (field === "name") onName(value);
                  void profile.again();
                }} />
        )) : <p className="form-hint">Reading…</p>}
        <div className="form-field">
          <span className="form-label">Time zone</span>
          <span>{zone}</span>
          <span className="form-hint">Windows’ own. Change it in Windows’ settings, and Steve follows.</span>
        </div>
      </Section>
      <Section title="Your day" why={
        "When the briefings come, and the quiet hours, when only what is critical reaches "
        + "you and nothing calls."}>
        {knobs.map((knob) => (
          <KnobField key={knob.name} knob={knob} onSave={async (name, value) => {
            const error = await saveKnob(name, value);
            await autonomy.again();
            return error;
          }} />
        ))}
      </Section>
    </div>
  );
}
