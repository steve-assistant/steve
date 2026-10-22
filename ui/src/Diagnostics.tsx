/* Diagnostics (back-pages.md, Steve): whether the backend runs, Start,
   Restart and Stop, the end of its log, what went wrong today, and the
   figures under the hood. Spread before over Home (the agent, today's
   errors) and Settings' General tab (spend, routing, the sandbox). */

import { useEffect } from "react";

import { api, formatUSD, isTauri } from "./api";
import { control } from "./control";
import { type ServiceAction, type ServiceStatus } from "./desktop";
import { Saved, Section } from "./Form";
import { type Said, usePolled } from "./pages";
import { Power } from "./Power";

const ROLES = ["converse", "reason", "escalate", "bulk", "triage"];

export function Diagnostics({
  connected, service, onService, serviceBusy, onRepair, repairing, said, onScreen, onAsk,
}: {
  connected: boolean;
  service: ServiceStatus | null;
  onService: (action: ServiceAction) => Promise<void>;
  serviceBusy: ServiceAction | null;
  /** Fetch this version's files again, for a backend whose files are damaged. */
  onRepair: () => Promise<void>;
  repairing: boolean;
  /** What the last Start, Stop, Restart or Repair did. */
  said: Said | null;
  onScreen: boolean;
  /** Ask the app's shell how the backend is, now. */
  onAsk: () => Promise<void>;
}) {
  // The app asks the shell every 30 s, for the sidebar; here the log's end
  // is the point, so it is asked at once and every 5 s while shown.
  useEffect(() => {
    if (!onScreen) return;
    void onAsk();
    const timer = window.setInterval(() => void onAsk(), 5000);
    return () => window.clearInterval(timer);
  }, [onScreen, onAsk]);
  const overview = usePolled((signal) => control.overview(signal), { everyMs: 10000, onScreen });
  const status = usePolled((signal) => api.status(signal), { everyMs: 10000, onScreen });
  const models = status.data
    ? ROLES.map((role) => status.data?.models[role]).filter((m) => m !== undefined)
    : [];
  const issues = overview.data?.issues ?? [];

  return (
    <div className="panel diagnostics">
      <p className="page-said"><Saved said={said} /></p>
      <Section title="The agent">
        <Power overview={connected ? overview.data : null} connected={connected} service={service}
               onService={onService} serviceBusy={serviceBusy}
               onUpdate={onRepair} updateBusy={repairing} />
      </Section>
      <Section title="The end of its log" why={
        "What the backend wrote last, newest at the bottom. Its whole log is in Steve’s data "
        + "folder, under logs."}>
        {!isTauri() ? <p className="form-hint">Only the desktop app reads the log.</p>
          : service && service.log_tail.length > 0
            ? <pre className="form-log">{service.log_tail.join("\n")}</pre>
            : <p className="form-hint">Nothing written yet.</p>}
      </Section>

      <Section title="What went wrong today" why={
        "Each kind of failure once, with how often it happened lately."}>
        {issues.length === 0 ? <p className="form-hint">Nothing.</p> : (
          <table className="form-table">
            <tbody>
              {issues.map((issue) => (
                <tr key={issue.id}>
                  <td>{issue.kind.replace(/_/g, " ")}</td>
                  <td>{issue.summary}</td>
                  <td className="form-figure">{issue.lately}×</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Section>

      {status.data && (
        <>
          <Section title="Under the hood">
            <table className="form-table">
              <tbody>
                <tr><td>Spent today</td><td className="form-figure">{formatUSD(status.data.cost.today_usd)}</td></tr>
                <tr><td>Spent in all</td><td className="form-figure">{formatUSD(status.data.cost.total_usd)}</td></tr>
                <tr><td>Model calls</td><td className="form-figure">{status.data.cost.calls}</td></tr>
                <tr><td>Memories</td><td className="form-figure">{status.data.memories}</td></tr>
                <tr>
                  <td>Tasks</td>
                  <td className="form-figure">
                    {status.data.queue.queued} queued · {status.data.queue.running} running ·{" "}
                    {status.data.queue.done} done · {status.data.queue.failed} failed
                  </td>
                </tr>
                <tr>
                  <td>Code sandbox</td>
                  <td className="form-figure">
                    {status.data.sandbox.available
                      ? `Docker ${status.data.sandbox.server_version ?? ""}`
                      : status.data.sandbox.reason ?? "not available"}
                  </td>
                </tr>
              </tbody>
            </table>
          </Section>
          <Section title="Models" why="Which model does what, and what each costs per million tokens.">
            <table className="form-table">
              <thead>
                <tr><th>for</th><th>model</th><th>effort</th><th className="form-figure">in / out</th></tr>
              </thead>
              <tbody>
                {models.map((m) => (
                  <tr key={m.role}>
                    <td>{m.role}</td>
                    <td title={m.note}><code>{m.slug}</code></td>
                    <td>{m.effort ?? "—"}{m.service_tier !== "default" && ` · ${m.service_tier}`}</td>
                    <td className="form-figure">${m.usd_per_mtok.input} / ${m.usd_per_mtok.output}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Section>
        </>
      )}
    </div>
  );
}
