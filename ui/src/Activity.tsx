import { useEffect, useState } from "react";
import { control, type ActivityItem, when } from "./control";
import { usePolled } from "./pages";

const KINDS: { key: string; label: string; match: (item: ActivityItem) => boolean }[] = [
  { key: "all", label: "Everything", match: () => true },
  { key: "action", label: "Actions", match: (i) => i.kind === "action" },
  { key: "goal", label: "Goals", match: (i) => i.kind === "goal" },
  { key: "routine", label: "Routines", match: (i) => i.kind === "routine" },
  { key: "mail", label: "Mailbox", match: (i) => i.kind === "mailbox" || i.kind === "mail" },
  { key: "work", label: "Tasks and the night", match: (i) => ["research", "night"].includes(i.kind) },
  { key: "issue", label: "Problems", match: (i) => i.kind === "issue" },
];

const KIND_LABEL: Record<string, string> = {
  action: "action", goal: "goal", routine: "routine", research: "task",
  night: "night", issue: "problem", mailbox: "mailbox", mail: "mail task",
};

const RECEIPT: Record<string, { text: string; tone: string }> = {
  verified_success: { text: "checked: it happened", tone: "good" },
  verification_failed: { text: "checked: it did NOT happen", tone: "bad" },
  tool_reported_success: { text: "not checked", tone: "dim" },
};

/**
 * What it did while the user was away, newest first: every action with whether
 * the read-back found it really happened (agent/receipts.py), routine runs,
 * goal sessions, tasks, changes to itself, the night, and what went wrong.
 * The audit trail Muse sells on, from the ledger rather than from memory.
 */
export function Activity({ onScreen }: { onScreen: boolean }) {
  const [hours, setHours] = useState(48);
  const { data, again } = usePolled(
    (signal) => control.activity(hours, signal), { everyMs: 15000, onScreen });
  // A new window is asked for at once, not at the next poll.
  useEffect(() => {
    void again();
  }, [hours, again]);
  const items = data?.items ?? [];
  const [kind, setKind] = useState("all");
  const filter = KINDS.find((k) => k.key === kind) ?? KINDS[0];
  const shown = items.filter(filter.match);

  return (
    <div className="panel">
      <div className="toolbar">
        <div className="chips">
          {KINDS.map((k) => (
            <button key={k.key} className={`chip ${kind === k.key ? "active" : ""}`} onClick={() => setKind(k.key)}>
              {k.label}
              <span className="muted"> {items.filter(k.match).length}</span>
            </button>
          ))}
        </div>
        <select value={hours} onChange={(e) => setHours(Number(e.target.value))}>
          <option value={24}>Last day</option>
          <option value={48}>Last two days</option>
          <option value={168}>Last week</option>
        </select>
      </div>

      {shown.length === 0 ? (
        <p className="muted">Nothing in that window.</p>
      ) : (
        <div className="feed">
          {shown.map((item) => {
            const receipt = item.receipt ? RECEIPT[item.receipt] : null;
            return (
              <div key={`${item.kind}:${item.id}`} className={`feed-row ${item.kind}`}>
                <span className="feed-when muted small">{when(item.at)}</span>
                <span className={`kind-tag ${item.kind}`}>{KIND_LABEL[item.kind] ?? item.kind}</span>
                <div className="feed-main">
                  <div className="feed-title">{item.title}</div>
                  <div className="muted small">
                    {item.detail}
                    {item.status && item.kind !== "issue" && ` · ${item.status}`}
                    {item.channel && ` · from ${item.channel}`}
                    {item.by && ` · approved by ${item.by}`}
                    {item.error && <span className="warn-text"> · {item.error}</span>}
                  </div>
                </div>
                {receipt && <span className={`receipt ${receipt.tone}`}>{receipt.text}</span>}
                {item.critic?.decision === "card" && (
                  <span className="receipt bad" title="The action critic: it judges an unattended action against what you asked">
                    critic: {item.critic.mode === "shadow" ? "would have asked you" : "asked you"}
                    {item.critic.verdict ? ` (${item.critic.verdict})` : " (could not review)"}
                  </span>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
