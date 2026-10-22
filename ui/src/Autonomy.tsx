import { useState } from "react";
import { Loading } from "./Loading";
import { formatUSD } from "./api";
import { control, type GoalRow, type RoutineRow, when } from "./control";
import { KnobField, Saved, Section } from "./Form";
import { saveKnob, usePolled, useSaid } from "./pages";

/* Routines and goals, and What Steve may do (back-pages.md, Steve): one
   page before, Autonomy, which also held the passphrase (now in Settings),
   the caps (now on Spend) and the day's times (now on Profile). */

interface Props {
  onRoutine: (id: number, op: "pause" | "resume" | "cancel") => Promise<void>;
  onGoal: (
    id: number,
    op: "pause" | "resume" | "drop" | "answer",
    extra?: { text?: string; question?: number; add_budget_usd?: number },
  ) => Promise<void>;
}

const STEP_MARK: Record<string, string> = {
  done: "✓", doing: "…", todo: "○", waiting_on_user: "?", waiting_on_someone: "⧗", dropped: "×",
};

function Routine({ routine, busy, onRoutine }: { routine: RoutineRow; busy: string | null; onRoutine: Props["onRoutine"] }) {
  const key = `routine:${routine.id}`;
  return (
    <div className="item">
      <div className="item-head">
        <span className={`status-pill ${routine.status}`}>{routine.status.replace("_", " ")}</span>
        <span className="item-title">{routine.title}</span>
        <span className="muted small">{routine.when}</span>
      </div>
      <div className="item-body">
        <div className="muted small">
          {routine.runs} run{routine.runs === 1 ? "" : "s"} · next {when(routine.next_run)}
          {routine.urgent ? " · may reach you in quiet hours" : ""}
        </div>
        <div className="small">Does: {routine.instruction}</div>
        <div className="small">Without asking: {routine.may_without_asking}</div>
        {routine.last_report && <div className="quote">Last told you: {routine.last_report}</div>}
        {routine.held && <div className="quote warn-text">Held for the morning: {routine.held}</div>}
        <div className="item-actions">
          {routine.status === "active" && (
            <button disabled={busy === key} onClick={() => void onRoutine(routine.id, "pause")}>Pause</button>
          )}
          {routine.status === "paused" && (
            <button disabled={busy === key} onClick={() => void onRoutine(routine.id, "resume")}>Resume</button>
          )}
          <button disabled={busy === key} onClick={() => void onRoutine(routine.id, "cancel")}>Cancel</button>
        </div>
      </div>
    </div>
  );
}

function Goal({ goal, busy, onGoal }: { goal: GoalRow; busy: string | null; onGoal: Props["onGoal"] }) {
  const key = `goal:${goal.id}`;
  const [open, setOpen] = useState(goal.open_questions.length > 0);
  const [answers, setAnswers] = useState<Record<number, string>>({});
  const [note, setNote] = useState("");
  const [more, setMore] = useState("1");
  const spentOut = /\$([\d.]+) of \$([\d.]+)/.exec(goal.spent);
  const budgetSpent = spentOut ? Number(spentOut[1]) >= Number(spentOut[2]) : false;

  return (
    <div className={`item ${goal.open_questions.length ? "asking" : ""}`}>
      <button className="item-head" onClick={() => setOpen((o) => !o)}>
        <span className={`status-pill ${goal.status}`}>{goal.status.replace("_", " ")}</span>
        <span className="item-title">{goal.title}</span>
        {goal.open_questions.length > 0 && (
          <span className="badge">{goal.open_questions.length} question{goal.open_questions.length === 1 ? "" : "s"}</span>
        )}
        <span className="muted small">{goal.progress} · {goal.spent}</span>
      </button>
      {open && (
        <div className="item-body">
          <div className="small">You asked: {goal.ask}</div>
          <div className="small">Done when: {goal.done_when}</div>
          <div className="muted small">
            {goal.sessions} session{goal.sessions === 1 ? "" : "s"} · looks again {goal.next_check ? when(goal.next_check) : "when you answer"}
            {" "}· every {goal.every_hours}h{goal.deadline ? ` · deadline ${when(goal.deadline)}` : ""}
            {" "}· without asking: {goal.may_without_asking}
          </div>

          <ol className="steps">
            {goal.steps.map((step) => (
              <li key={step.n} className={`step ${step.status}`}>
                <span className="step-mark" title={step.status.replace(/_/g, " ")}>{STEP_MARK[step.status] ?? "○"}</span>
                <span>{step.text}{step.note && <span className="muted"> — {step.note}</span>}</span>
              </li>
            ))}
          </ol>

          {goal.questions.map((q) => (
            <div key={q.n} className="question">
              <div className="small"><strong>Q{q.n}.</strong> {q.text}</div>
              {q.answer !== null ? (
                <div className="muted small">You said: {q.answer}</div>
              ) : (
                <div className="answer-row">
                  <input placeholder="Your answer" value={answers[q.n] ?? ""}
                         onChange={(e) => setAnswers({ ...answers, [q.n]: e.target.value })} />
                  <button className="primary" disabled={busy === key || !(answers[q.n] ?? "").trim()}
                          onClick={() => void onGoal(goal.id, "answer", { text: answers[q.n], question: q.n })
                            .then(() => setAnswers({ ...answers, [q.n]: "" }))}>
                    Answer
                  </button>
                </div>
              )}
            </div>
          ))}

          {goal.findings.length > 0 && (
            <details>
              <summary className="small">What it found ({goal.findings.length})</summary>
              <ul className="findings">{goal.findings.map((f, i) => <li key={i} className="small">{f}</li>)}</ul>
            </details>
          )}
          {goal.waiting_on.length > 0 && <div className="muted small">Waiting on mail from {goal.waiting_on.join(", ")}</div>}
          {goal.last_report && <div className="quote">Last told you: {goal.last_report}</div>}

          <div className="answer-row">
            <input placeholder="Tell it something: a change, a preference, what next" value={note}
                   onChange={(e) => setNote(e.target.value)} />
            <button disabled={busy === key || !note.trim()}
                    onClick={() => void onGoal(goal.id, "answer", { text: note }).then(() => setNote(""))}>
              Send
            </button>
          </div>

          <div className="item-actions">
            {goal.status === "active" && (
              <button disabled={busy === key} onClick={() => void onGoal(goal.id, "pause")}>Pause</button>
            )}
            {goal.status === "paused" && (
              <>
                {budgetSpent && (
                  <select value={more} onChange={(e) => setMore(e.target.value)}>
                    {["0.5", "1", "2", "5"].map((v) => <option key={v} value={v}>+{formatUSD(Number(v))}</option>)}
                  </select>
                )}
                <button disabled={busy === key}
                        onClick={() => void onGoal(goal.id, "resume", budgetSpent ? { add_budget_usd: Number(more) } : {})}>
                  Resume
                </button>
              </>
            )}
            <button disabled={busy === key} onClick={() => void onGoal(goal.id, "drop")}>Drop</button>
          </div>
        </div>
      )}
    </div>
  );
}


// The groups of knobs What Steve may do shows, each under its heading. The
// switches' group is named in agent/features.py (`SWITCHES_GROUP`), which a
// tool that is switched off quotes to the user; test_features.py ties them.
const GROUP_NAMES = {
  features: "What Steve may use on this computer",
  approvals: "Where you may say yes",
  attention: "Noticing and messaging",
};

// Knobs of those groups that live on other pages: the day's times on
// Profile, the weather's units in Settings.
const ELSEWHERE = new Set(["QUIET_FROM", "QUIET_UNTIL", "MORNING", "EVENING", "UNITS"]);

/** A routine's or a goal's change, from either page: the error is said on
 *  the page, and the page asks again. */
function useChanges(again: () => Promise<void>, say: (text: string, ok?: boolean) => void) {
  const [busy, setBusy] = useState<string | null>(null);
  const onRoutine: Props["onRoutine"] = async (id, op) => {
    setBusy(`routine:${id}`);
    const result = await control.routine(id, op);
    if (!result.ok) say(result.error ?? `Couldn’t ${op} it.`, false);
    await again();
    setBusy(null);
  };
  const onGoal: Props["onGoal"] = async (id, op, extra = {}) => {
    setBusy(`goal:${id}`);
    const result = await control.goal(id, op, extra);
    if (!result.ok) say(result.error ?? `Couldn’t ${op} it.`, false);
    else if (op === "answer") say("Passed on. It works on it at its next session, soon.");
    await again();
    setBusy(null);
  };
  const onStop = async (what: "routines" | "goals") => {
    const result = await control.stop(what);
    if (!result.ok) say(result.error ?? "Couldn’t stop them.", false);
    else say(`Paused ${result.paused ?? 0} ${what}.`);
    await again();
  };
  return { busy, onRoutine, onGoal, onStop };
}

/** Routines and goals (back-pages.md, Steve): each one's state, with Pause,
 *  Resume and the rest. */
export function RoutinesAndGoals({ onScreen }: { onScreen: boolean }) {
  const { data, again } = usePolled((signal) => control.autonomy(signal), { onScreen });
  const [said, say] = useSaid();
  const { busy, onRoutine, onGoal } = useChanges(again, say);
  if (!data) return <div className="panel"><Loading className="muted" /></div>;
  const goalsLive = data.goals.filter((g) => g.status !== "done");

  return (
    <div className="panel">
      <p className="page-said"><Saved said={said} /></p>
      <Section title="Goals" why={
        "What you asked Steve to see through over days: it works on each in sessions, and "
        + "asks you when it needs to."}>
        {goalsLive.length === 0 ? (
          <p className="form-hint">
            None. Ask for one: “find a time next week for a call with Alex”.
          </p>
        ) : (
          goalsLive.map((goal) => <Goal key={goal.id} goal={goal} busy={busy} onGoal={onGoal} />)
        )}
      </Section>
      <Section title="Routines" why="What Steve does on a schedule you set.">
        {data.routines.length === 0 ? (
          <p className="form-hint">None. Ask for one: “every Sunday at 7pm, recap my week”.</p>
        ) : (
          data.routines.map((routine) => (
            <Routine key={routine.id} routine={routine} busy={busy} onRoutine={onRoutine} />
          ))
        )}
      </Section>
    </div>
  );
}

/** The local readers (agent/worker/readers.py): what they add and take,
 *  and an install with a click, where it took a command at a terminal. */
function LocalReaders({ onScreen }: { onScreen: boolean }) {
  const [asked, setAsked] = useState(false);
  // Every few seconds while an install runs, to say when it is done.
  const { data, again } = usePolled((signal) => control.readers(signal),
                                    { everyMs: asked ? 3000 : 30000, onScreen });
  if (!data) return null;
  const missing = [!data.piguard && "PIGuard", !data.tev1 && "Tev1"].filter(Boolean);
  const install = async () => {
    setAsked(true);
    await control.installReaders();
    await again();
  };

  return (
    <Section title="Local readers" why={data.adds}>
      <p className="form-hint">
        {missing.length === 0 ? "Both are installed: PIGuard and Tev1."
          : data.installing ? "Installing. It can take a while, and you can leave this page."
          : missing.length === 1 ? `${missing[0]} isn’t installed yet. ${data.takes}`
          : data.takes}
      </p>
      {missing.length > 0 && (
        <div className="form-actions">
          <button type="button" disabled={data.installing} onClick={() => void install()}>
            {data.installing ? "Installing…" : "Install them"}
          </button>
        </div>
      )}
      {data.error && !data.installing && (
        <p className="form-error" role="alert">The last install stopped: {data.error}</p>
      )}
    </Section>
  );
}

/** What Steve may do (back-pages.md, Steve): what it may use on this
 *  computer, where a yes may be given, how much it notices and messages,
 *  and the brakes. */
export function MayDo({ onScreen }: { onScreen: boolean }) {
  const { data, again } = usePolled((signal) => control.autonomy(signal), { onScreen });
  const [said, say] = useSaid();
  const { onStop } = useChanges(again, say);
  if (!data) return <div className="panel"><Loading className="muted" /></div>;
  const save = async (name: string, value: number | string | null) => {
    const error = await saveKnob(name, value);
    await again();
    return error;
  };
  const active = (rows: { status: string }[]) => rows.some((r) => r.status === "active");

  return (
    <div className="panel form-page may-do">
      <p className="page-said"><Saved said={said} /></p>
      {(["features", "approvals", "attention"] as const).map((group) => (
        <Section key={group} title={GROUP_NAMES[group]} why={
          group === "features"
            ? "Each is off until you turn it on. A tool that needs one says which, and that it is here."
            : group === "approvals"
              ? "Where a yes may come from, by how much is at stake. Anything that spends money takes your passphrase too."
              : "How often Steve may message you unasked, and what it does around your meetings."}>
          {data.knobs.filter((k) => k.group === group && !ELSEWHERE.has(k.name)).map((knob) => (
            <KnobField key={knob.name} knob={knob} onSave={save} />
          ))}
        </Section>
      ))}
      <LocalReaders onScreen={onScreen} />
      <Section title="Brakes" why={
        "Pauses every routine, or every goal, at once: the same as texting STOP ROUTINES or "
        + "STOP GOALS. Resume them one at a time in Routines and goals."}>
        <div className="form-actions">
          <button type="button" disabled={!active(data.routines)}
                  onClick={() => void onStop("routines")}>Stop all routines</button>
          <button type="button" disabled={!active(data.goals)}
                  onClick={() => void onStop("goals")}>Stop all goals</button>
        </div>
      </Section>
    </div>
  );
}
