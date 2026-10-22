import { useState } from "react";
import { formatUSD } from "./api";
import { control, type Knob, type Spend as SpendView, when } from "./control";
import { KnobField, Saved, Section } from "./Form";
import { saveKnob, usePolled, useSaid } from "./pages";

// The caps first, the month's at the top: the one setup asked.
const CAP_GROUPS = [
  ["spend", "Caps", "Steve stops spending on a kind of work when its cap is reached, and says so. Zero, where it is allowed, turns that spending off."],
  ["budgets", "Daily budgets", "What Steve may spend on its own each day: noticing, preparing, routines, goals and talking at the desk."],
] as const;

const ROLE_ORDER = ["converse", "reason", "escalate", "bulk", "triage"];

/** A bar against a cap turns amber near it and red at it; one that only
 *  compares kinds of work with each other (`relative`) never warns. */
function Bar({ spent, limit, relative = false }: { spent: number; limit: number; relative?: boolean }) {
  const share = limit > 0 ? Math.min(spent / limit, 1) : 0;
  const tone = relative ? "" : share >= 1 ? "full" : share > 0.8 ? "high" : "";
  return (
    <div className="bar" title={`${formatUSD(spent)} of ${formatUSD(limit)}`}>
      <div className={`bar-fill ${tone}`}
           style={{ width: `${Math.max(share * 100, spent > 0 ? 2 : 0)}%` }} />
    </div>
  );
}

function Limits({ limits }: { limits: SpendView["caps"] }) {
  return (
    <>
      {limits.map((b) => (
        <div key={b.name} className="budget">
          <span className="budget-name">{b.name}</span>
          <Bar spent={b.spent} limit={b.limit} />
          <span className="budget-num">{formatUSD(b.spent)} / {formatUSD(b.limit)}</span>
        </div>
      ))}
    </>
  );
}

/**
 * What it costs, on which kind of work, against which cap -- and which models
 * it runs on, what the scout changed, and the one button that puts them back
 * as written. A cap reached, or nearly, is a banner at the top: the user who
 * opens this page is asking why something stopped.
 */
export function Spend({ onScreen }: { onScreen: boolean }) {
  const [window, setWindow] = useState<"today" | "week" | "month">("week");
  const figures = usePolled((signal) => Promise.all([
    control.spend(signal), control.models(signal), control.autonomy(signal)]), { onScreen });
  const [spend, models, autonomy] = figures.data ?? [null, null, null];
  const [reverting, setReverting] = useState(false);
  const [said, say] = useSaid();
  const switched = models ? Object.entries(models.switches.in_force).filter(([, slug]) => slug) : [];

  const onRevert = async () => {
    setReverting(true);
    const result = await control.revertModels();
    if (!result.ok) say(result.error ?? "Couldn’t put them back.", false);
    else say(result.reverted?.length ? `Put back: ${result.reverted.join(", ")}` : "Nothing was switched.");
    await figures.again();
    setReverting(false);
  };
  const caps = (group: string) => (autonomy?.knobs ?? []).filter((k: Knob) => k.group === group);

  return (
    <div className="panel">
      <p className="page-said"><Saved said={said} /></p>
      {spend?.notices.map((n) => (
        <p key={`${n.kind}-${n.cap}`} className={`cap-notice ${n.kind}`}>{n.text}</p>
      ))}
      {autonomy && CAP_GROUPS.map(([group, title, why]) => (
        <Section key={group} title={title} why={why}>
          {caps(group).map((knob) => (
            <KnobField key={knob.name} knob={knob} onSave={async (name, value) => {
              const error = await saveKnob(name, value);
              await figures.again();
              return error;
            }} />
          ))}
        </Section>
      ))}
      {spend && (
        <>
          <section>
            <h3>Spend</h3>
            <div className="cards">
              <div className="card"><div className="value">{formatUSD(spend.today)}</div><div className="label">today</div></div>
              <div className="card"><div className="value">{formatUSD(spend.week)}</div><div className="label">last 7 days</div></div>
              <div className="card"><div className="value">{formatUSD(spend.month)}</div><div className="label">this month</div></div>
            </div>
          </section>

          <section>
            <div className="section-head">
              <h3>By kind</h3>
              <select value={window} onChange={(e) => setWindow(e.target.value as typeof window)}>
                <option value="today">Today</option>
                <option value="week">Last 7 days</option>
                <option value="month">This month</option>
              </select>
            </div>
            {Object.entries(spend.by_category[window]).map(([kind, usd]) => {
              const top = Math.max(...Object.values(spend.by_category[window]), 0.000001);
              return (
                <div key={kind} className="budget">
                  <span className="budget-name">{kind}</span>
                  <Bar spent={usd} limit={top} relative />
                  <span className="budget-num">{formatUSD(usd)}</span>
                </div>
              );
            })}
          </section>

          <section>
            <h3>Against the caps</h3>
            <Limits limits={spend.caps} />
          </section>

          <section>
            <h3>Against the budgets</h3>
            <Limits limits={spend.budgets} />
            {spend.goals.map((g) => (
              <div key={g.id} className="budget">
                <span className="budget-name">Goal: {g.title}</span>
                <Bar spent={g.spent} limit={g.budget} />
                <span className="budget-num">{formatUSD(g.spent)} / {formatUSD(g.budget)}</span>
              </div>
            ))}
          </section>
        </>
      )}

      {models && (
        <>
          <section>
            <div className="section-head">
              <h3>Models in use</h3>
              <button disabled={reverting || switched.length === 0} onClick={() => void onRevert()}
                      title="The same as texting REVERT MODELS">
                {reverting ? "Putting back…" : "Revert to the lineup as written"}
              </button>
            </div>
            <table className="models">
              <thead><tr><th>Role</th><th>Model</th><th>$/M in · out</th><th>Note</th></tr></thead>
              <tbody>
                {ROLE_ORDER.filter((r) => models.lineup[r]).map((role) => {
                  const m = models.lineup[role];
                  const scout = models.switches.in_force[role];
                  return (
                    <tr key={role}>
                      <td className="role-cell">{role}</td>
                      <td>
                        {m.label}
                        {scout && <span className="badge">switched by the scout</span>}
                      </td>
                      <td className="muted">{m.usd_per_mtok.input} · {m.usd_per_mtok.output}</td>
                      <td className="muted small">{m.note}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {models.switches.watching && (
              <p className="small warn-text">A switch is being watched for a day; it is put back if calls get slower or dearer.</p>
            )}
          </section>

          <section>
            <h3>The model scout's trials</h3>
            {models.trials.length === 0 ? (
              <p className="muted small">None yet.</p>
            ) : (
              models.trials.map((t) => (
                <div key={t.id} className="row">
                  <span className={`status-pill ${t.decision?.switch ? "done" : t.error ? "failed" : "queued"}`}>
                    {t.decision?.switch ? "better" : t.error ? "failed" : "kept"}
                  </span>
                  <span className="row-title">{t.slug} for {t.role} <span className="muted">vs {t.incumbent}</span></span>
                  <span className="muted small">{formatUSD(t.spent_usd)} · {when(t.finished_at ?? t.started_at)}</span>
                </div>
              ))
            )}
            {models.switches.history.length > 0 && (
              <details>
                <summary className="small">Switches and reverts ({models.switches.history.length})</summary>
                {models.switches.history.map((h, i) => (
                  <div key={i} className="muted small">
                    {h.at} · {h.kind} {h.role}: {h.from_slug} → {h.to_slug} by {h.by} ({h.why}){h.outcome ? ` · ${h.outcome}` : ""}
                  </div>
                ))}
              </details>
            )}
          </section>
        </>
      )}
    </div>
  );
}
