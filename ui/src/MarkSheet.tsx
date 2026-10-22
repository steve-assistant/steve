/* The specimen sheet's page (specimen.tsx): the mark in every state and
   size, and every hand-drawn mark's variants, on the design's paper.

   `window.__mark(props)` sets the one live mark, `#live`, so a test can
   change its state after it appears, as the app will. */

import { useEffect, useState } from "react";
import { HandMark } from "./HandMark";
import { Mark, type MarkProps, type MarkState } from "./Mark";
import { MARKS, type MarkName } from "./marks";

declare global {
  interface Window {
    /** Sets the live mark's props; `fresh` shows it anew, as on a load. */
    __mark?: (props: MarkProps, fresh?: boolean) => void;
  }
}

const STATES: MarkState[] = ["idle", "working", "listening", "speaking", "asleep", "catching-up",
  "starting", "stopped"];
const SIZES = [16, 24, 40, 56, 120];
const IDS = Array.from({ length: 12 }, (_, n) => `entry-${n + 1}`);

function Live() {
  const [live, setLive] = useState<{ props: MarkProps; key: number }>(
    { props: { size: 120 }, key: 0 });
  useEffect(() => {
    window.__mark = (props, fresh = false) =>
      setLive((was) => ({ props, key: fresh ? was.key + 1 : was.key }));
  }, []);
  return <div id="live"><Mark key={live.key} {...live.props} /></div>;
}

export function MarkSheet() {
  const rows: [string, Omit<MarkProps, "size">][] = [
    ...STATES.map((state): [string, Omit<MarkProps, "size">] => [state, { state }]),
    ["needs you", { needsYou: true }],
    ["quiet", { quiet: true }],
  ];
  return (
    <main className="grain" style={{ padding: 24, background: "var(--paper)", color: "var(--ink)" }}>
      <Live />
      <table id="states" className="t-small">
        <tbody>
          {rows.map(([name, props]) => (
            <tr key={name} data-row={name}>
              <th className="t-label" style={{ textAlign: "left", paddingRight: 16 }}>{name}</th>
              {SIZES.map((size) => (
                <td key={size} style={{ padding: 8 }}><Mark size={size} {...props} /></td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      <table id="marks">
        <tbody>
          {(Object.keys(MARKS) as MarkName[]).map((name) => (
            <tr key={name}>
              <th className="t-label" style={{ textAlign: "left", paddingRight: 16 }}>{name}</th>
              {MARKS[name].variants.map((paths, n) => (
                <td key={n} style={{ padding: 8 }}>
                  <svg className="hand-mark" viewBox={MARKS[name].viewBox} height={28}
                    data-mark={name} data-variant={n}>
                    {paths.map((d) => <path key={d} d={d} />)}
                  </svg>
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      <p id="hand" style={{ display: "flex", gap: 8 }}>
        {IDS.map((id) => <span key={id} data-id={id} style={{ width: 20, height: 20 }}>
          <HandMark name="check" id={id} /></span>)}
      </p>
    </main>
  );
}
