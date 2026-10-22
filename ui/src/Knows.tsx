/* What Steve knows (back-pages.md, About you): the facts it holds today and
   the memories it keeps, to read, correct and forget (agent/worker/knows.py).
   What Steve kept was readable only by asking it, and forgettable only by
   the number a morning digest gave it. */

import { useState } from "react";
import { control, type KnownFact } from "./control";
import { Forget, Saved, Section } from "./Form";
import { usePolled, useSaid } from "./pages";

const SOURCE: Record<string, string> = {
  user_words: "you said",
  confirmed: "you confirmed",
  mail: "from your mail",
  web: "from the web",
  judge: "Steve's reading",
};

function sourceOf(source: string): string {
  return SOURCE[source] ?? (source.startsWith("inferred") ? "Steve's guess" : source);
}

/** An attribute as words: "home_city" is "home city". */
function said(attribute: string): string {
  return attribute.replace(/_/g, " ");
}

function FactRow({ fact, onCorrect, onForget }: {
  fact: KnownFact;
  onCorrect: (fact: KnownFact, value: string) => Promise<boolean>;
  onForget: (fact: KnownFact) => void;
}) {
  const [editing, setEditing] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const keep = async () => {
    if (editing === null || editing.trim() === "" || editing.trim() === fact.value) {
      setEditing(null);
      return;
    }
    setBusy(true);
    if (await onCorrect(fact, editing.trim())) setEditing(null);
    setBusy(false);
  };

  return (
    <li data-fact={fact.id}>
      <div className="knows-fact">
        <span className="knows-attribute">{said(fact.attribute)}</span>
        {editing === null ? (
          <span className="knows-value">{fact.value}</span>
        ) : (
          <input className="form-input knows-value" value={editing} disabled={busy} autoFocus
                 aria-label={`${fact.about}: ${fact.attribute}`}
                 onChange={(e) => setEditing(e.target.value)}
                 onKeyDown={(e) => {
                   if (e.key === "Enter") void keep();
                   if (e.key === "Escape") { e.stopPropagation(); setEditing(null); }
                 }} />
        )}
      </div>
      <div className="form-list-meta">
        {sourceOf(fact.source)}{fact.until ? ` · until ${fact.until}` : ""}
        {" · "}
        {editing === null ? (
          <button type="button" className="form-quiet" onClick={() => setEditing(fact.value)}>
            Correct
          </button>
        ) : (
          <>
            <button type="button" className="form-quiet" disabled={busy}
                    onClick={() => void keep()}>Keep</button>
            <button type="button" className="form-quiet" onClick={() => setEditing(null)}>
              Cancel
            </button>
          </>
        )}
        <Forget asks={`Forget that ${fact.about === "You" ? "your" : `${fact.about}’s`} ${said(fact.attribute)} is ${fact.value}?`}
                busy={busy} onConfirm={() => onForget(fact)} />
      </div>
    </li>
  );
}

export function Knows({ onScreen }: { onScreen: boolean }) {
  const { data, failed, again } = usePolled((signal) => control.knows(signal),
                                            { everyMs: 30000, onScreen });
  const [said, say] = useSaid();

  const correct = async (fact: KnownFact, value: string) => {
    const done = await control.correctFact(fact.id, value);
    say(done.ok ? "Corrected" : done.error ?? "That wasn't kept.", done.ok);
    if (done.ok) await again();
    return done.ok;
  };
  const forgetFact = async (fact: KnownFact) => {
    const done = await control.forgetFact(fact.id);
    say(done.ok ? "Forgotten" : done.error ?? "That wasn't forgotten.", done.ok);
    await again();
  };
  const forgetMemory = async (path: string) => {
    const done = await control.forgetMemory(path);
    say(done.ok ? "Forgotten" : done.error ?? "That wasn't forgotten.", done.ok);
    await again();
  };

  if (!data) {
    return <div className="form-page"><p className="form-why">
      {failed ? "Steve isn't answering, so what it knows can't be read now." : "Reading…"}
    </p></div>;
  }

  // Grouped by whom they are about, the user first, as the backend orders them.
  const groups = new Map<string, KnownFact[]>();
  for (const fact of data.facts) groups.set(fact.about, [...(groups.get(fact.about) ?? []), fact]);

  return (
    <div className="panel form-page knows">
      <p className="page-said"><Saved said={said} /></p>
      <Section title="Facts" why={
        "What Steve holds as true today, and where each came from. A correction is your word "
        + "on it, so nothing Steve reads later replaces it. Forgotten, a fact is no longer used."}>
        {data.facts.length === 0 && <p className="form-hint">None yet. Steve keeps a fact when you tell it one, or confirm one it read.</p>}
        {[...groups].map(([about, facts]) => (
          <div key={about} className="knows-group">
            <h3 className="knows-about">{about}</h3>
            <ul className="form-list">
              {facts.map((fact) => (
                <FactRow key={fact.id} fact={fact} onCorrect={correct}
                         onForget={(f) => void forgetFact(f)} />
              ))}
            </ul>
          </div>
        ))}
      </Section>
      <Section title="Memories" why={
        "Notes Steve kept for itself: what you asked of it, what it learned about your work. "
        + "Forgotten, a memory is deleted; Steve's own history keeps a copy, as it always has."}>
        {data.memories.length === 0 && <p className="form-hint">None yet.</p>}
        <ul className="form-list">
          {data.memories.map((m) => (
            <li key={m.path} data-memory={m.path}>
              <div>{m.summary}</div>
              <div className="form-list-meta">
                {m.kind}{m.until ? ` · until ${m.until}` : ""}{" · "}
                <Forget asks={`Forget “${m.summary.slice(0, 60)}${m.summary.length > 60 ? "…" : ""}”?`}
                        onConfirm={() => void forgetMemory(m.path)} />
              </div>
            </li>
          ))}
        </ul>
      </Section>
    </div>
  );
}
