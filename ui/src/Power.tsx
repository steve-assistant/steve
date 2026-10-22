import { useState, type ReactNode } from "react";
import { fromSqlite, isTauri } from "./api";
import { type Overview, when } from "./control";
import { type ServiceAction, type ServiceStatus } from "./desktop";
import "./forms.css";

export interface PowerProps {
  overview: Overview | null;
  connected: boolean;
  service: ServiceStatus | null;
  onService: (action: ServiceAction) => Promise<void>;
  serviceBusy: ServiceAction | null;
  /** Fetch this version's files again, as an update would. */
  onUpdate: (repair: boolean) => Promise<void>;
  updateBusy: boolean;
}

function uptime(seconds: number): string {
  const count = (n: number, one: string) => `${n} ${one}${n === 1 ? "" : "s"}`;
  if (seconds < 90) return count(Math.round(seconds), "second");
  if (seconds < 5400) return count(Math.round(seconds / 60), "minute");
  return `${(seconds / 3600).toFixed(1)} hours`;
}

/** ", awake since 9:45 pm", and the sentence after it saying whether the
 *  computer slept or the backend wasn't running before (agent/awake.py). */
function awake(stretch: Overview["awake"] | null): [string, string] {
  if (!stretch?.since) return ["", ""];
  const since = `, awake since ${when(fromSqlite(stretch.since))}`;
  if (!stretch.slept) return [since, ""];
  const gap = stretch.slept.how === "asleep" ? "The computer slept" : "Steve wasn’t running";
  return [since, ` ${gap} from ${when(fromSqlite(stretch.slept.from))} to `
    + `${when(fromSqlite(stretch.slept.until))}.`];
}

/**
 * Whether the backend runs, and the words that start, stop and repair it.
 * Here because it is the one thing Steve cannot do for itself: a worker
 * asked to stop cannot then be asked to start.
 *
 * It was a dashboard's card, with a green dot, a bold "Running" and "duties
 * active", in a window that is otherwise prose: the one part that looked
 * like another program. Now it says the same in a sentence.
 */
export function Power({
  overview, connected, service, onService, serviceBusy, onUpdate, updateBusy,
}: PowerProps) {
  const [confirming, setConfirming] = useState(false);
  const stopped = service?.state === "stopped";
  const failing = service?.state === "failing";
  const damaged = service?.state === "damaged";
  const busy = serviceBusy !== null;
  const button = (label: string, act: () => void, { disabled = busy, danger = false } = {}) => (
    <button type="button" key={label} className={danger ? "form-danger" : "form-quiet"}
            disabled={disabled} onClick={act}>
      {label}
    </button>
  );

  let said: string;
  if (connected) {
    const [since, slept] = awake(overview?.awake ?? null);
    said = overview
      ? `Steve is running: version ${overview.worker.version}, up ${uptime(overview.worker.uptime_s)}${since}.${slept}`
      : "Steve is running.";
  } else if (damaged) {
    said = `Steve’s files are damaged: ${service?.damaged ?? "a file is not as shipped"}. So the app `
      + "will not start its backend. Repair fetches this version’s files again, as an update "
      + "would; running Steve’s installer again does the same. Your data is kept.";
  } else if (failing) {
    said = `Steve’s backend keeps stopping: it stopped ${service?.restarts ?? 0} times in ten `
      + "minutes, so the app has stopped starting it. The end of its log is below.";
  } else if (stopped) {
    said = "Steve is stopped. It stays off until you press Start, or the app starts again.";
  } else {
    said = "Steve isn’t answering. The app starts it again if it stops.";
  }

  let choices: ReactNode[];
  if (!isTauri()) {
    choices = [<span key="app" className="form-hint">Start and stop from the desktop app.</span>];
  } else if (damaged) {
    choices = [button(updateBusy ? "Repairing…" : "Repair", () => void onUpdate(true),
                      { disabled: updateBusy })];
  } else if (!connected) {
    choices = [button(serviceBusy === "start" ? "Starting…" : "Start", () => void onService("start"))];
  } else if (confirming) {
    choices = [
      button(serviceBusy === "stop" ? "Stopping…" : "Stop it until I press Start", () => {
        setConfirming(false);
        void onService("stop");
      }, { danger: true }),
      button("Cancel", () => setConfirming(false), { disabled: false }),
    ];
  } else {
    choices = [
      button(serviceBusy === "restart" ? "Restarting…" : "Restart", () => void onService("restart")),
      button("Stop", () => setConfirming(true)),
    ];
  }

  return (
    <>
      <p className="power">{said} <span className="power-choices">{choices}</span></p>
      {failing && service && service.log_tail.length > 0 && (
        <pre className="log-tail">{service.log_tail.join("\n")}</pre>
      )}
    </>
  );
}
