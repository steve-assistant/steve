/* Steve's mark: the logo without its rounded square, showing what Steve is
   doing (docs/design/presence.md, "The mark").

   Each state changes one thing, so a glance reads it: the yellow diamond
   turns while Steve works, the cyan one grows while it listens or speaks, a
   red circle is drawn around the red one when something needs you. The
   centre diamond is Steve itself, and only changes while the backend starts
   or has stopped.

   The shapes come from the logo's own file (`devtools/marks.py` writes
   marks.ts), not from a redrawing: a mark drawn again by hand drifts from
   the logo a little at each redrawing. */

import { useRef, useState, type CSSProperties } from "react";
import { useLevel } from "./level";
import { LOGO, MARKS, type Diamond } from "./marks";
import "./mark.css";

export type MarkState =
  | "idle"
  | "working"
  | "listening"
  | "speaking"
  | "asleep"
  | "catching-up"
  | "starting"
  | "stopped";

export type MarkProps = {
  /** Its side in pixels: 16 to 24 draws the small mark, 40 and up the full. */
  size: number;
  state?: MarkState;
  needsYou?: boolean;
  quiet?: boolean;
  /** Counts entries as they arrive: each new one nods the body once. */
  arrivals?: number;
  /** What a screen reader says; without one the mark is decoration. */
  label?: string;
};

const SIDE = 568; // the view box's side, in the logo's units
const SMALL_UP_TO = 24;

/** A diamond as a path, `r` from its centre to a corner. `inset` moves its
    edges in, so an outline's stroke stays inside the filled diamond's edge. */
function diamond({ x, y, r }: Diamond, scale = 1, inset = 0) {
  const s = r * scale - inset;
  const at = (n: number) => +n.toFixed(2);
  return `M${at(x)} ${at(y - s)}L${at(x + s)} ${at(y)}L${at(x)} ${at(y + s)}L${at(x - s)} ${at(y)}Z`;
}

type Look = "filled" | "outline" | "catching";

function Gem({ shape, colour, scale, line, look, extra, order }: {
  shape: Diamond;
  colour: string;
  scale: number;
  line: number;
  look: Look;
  extra?: string;
  order?: number;
}) {
  return (
    <g
      className={["diamond", look, extra].filter(Boolean).join(" ")}
      style={{ "--gem": colour, "--n": order ?? 0 } as CSSProperties}
    >
      <path className="fill" d={diamond(shape, scale)} />
      <path className="line" d={diamond(shape, scale, line * Math.SQRT1_2)} strokeWidth={line} />
    </g>
  );
}

export function Mark({ size, state = "idle", needsYou = false, quiet = false, arrivals = 0, label }:
  MarkProps) {
  // A mark draws in when its event happens, never on load (foundations.md,
  // Marks): a circle already wanted when the mark appears is just there.
  const [needed, setNeeded] = useState(needsYou);
  const [drawIn, setDrawIn] = useState(false);
  if (needsYou !== needed) {
    setNeeded(needsYou);
    setDrawIn(needsYou);
  }
  // The same for the nod: an entry that arrives, not the count on load.
  const [counted, setCounted] = useState(arrivals);
  const [nods, setNods] = useState(0);
  if (arrivals !== counted) {
    setCounted(arrivals);
    setNods((n) => n + 1);
  }

  const small = size <= SMALL_UP_TO;
  const scale = small ? LOGO.small : 1;
  // An outline 1.25 px wide on the screen, and never thinner than the
  // logo's own outline diamond.
  const px = SIDE / size;
  const line = Math.max(14, 1.25 * px);
  const ends = state === "starting" || state === "stopped";
  const look: Look = state === "asleep" || state === "stopped" ? "outline"
    : state === "catching-up" ? "catching" : "filled";
  const voice = state === "listening" || state === "speaking";
  // The cyan diamond follows the voice's level by a CSS variable, without
  // a render at each one.
  const svg = useRef<SVGSVGElement>(null);
  useLevel(svg, voice);

  return (
    <svg
      ref={svg}
      className={["mark", quiet && "quiet"].filter(Boolean).join(" ")}
      viewBox={LOGO.viewBox}
      width={size}
      height={size}
      style={{ "--px": px } as CSSProperties}
      data-state={state}
      data-needs-you={needsYou || undefined}
      {...(label ? { role: "img", "aria-label": label } : { "aria-hidden": true })}
    >
      <g key={nods} className={nods ? "body nods" : "body"}>
        {/* The full body has a diamond cut around the centre, which makes
            the logo's outline diamond. While the backend starts or has
            stopped the centre is drawn alone, on a body without the cut. */}
        {(small || ends ? LOGO.bodySmall : LOGO.body).map((d) => (
          <path key={d} className={ends ? "faint" : undefined} d={d} />
        ))}
        {!small && (
          <Gem shape={LOGO.centre} colour="var(--ink)" scale={1} line={line}
            look={state === "stopped" ? "outline" : "filled"}
            extra={state === "starting" ? "turning working" : undefined} />
        )}
      </g>
      <g className="gems">
        <Gem shape={LOGO.red} colour="var(--red)" scale={scale} line={line} look={look} order={0} />
        <Gem shape={LOGO.yellow} colour="var(--yellow)" scale={scale} line={line} look={look} order={1}
          extra={state === "working" ? "turning working" : undefined} />
        <Gem shape={LOGO.cyan} colour="var(--cyan)" scale={scale} line={line} look={look} order={2}
          extra={voice ? "voice" : undefined} />
      </g>
      {needsYou && (
        <g className={drawIn ? "circle draws" : "circle"}>
          <svg x={-8} y={56} width={246} height={226} viewBox={MARKS.circle.viewBox}
            preserveAspectRatio="none" overflow="visible">
            {MARKS.circle.variants[0].map((d) => <path key={d} d={d} />)}
          </svg>
        </g>
      )}
    </svg>
  );
}
