/* A hand-drawn mark: an underline, a circle, a check, a cross, a strike, an
   arrow, a bracket or a wave (docs/design/foundations.md, "Marks").

   A mark that looks the same every time reads as a font's glyph, not a
   pen's. So each has a few variants, and the thing it marks picks one by
   its id: the same entry always gets the same mark, and two neighbours
   rarely match. Chosen at random, a mark would change at every redraw. */

import { MARKS, type MarkName } from "./marks";
import { variantOf } from "./variant";
import "./mark.css";

// Drawn along or round a run of text, to its size: stretched, not scaled.
// A circle scaled whole round one digit came out as a flat oval through it,
// and a bracket scaled whole beside a tall quote stopped short of its ends.
const ALONG_TEXT: ReadonlySet<MarkName> = new Set(["underline", "strike", "wave", "circle", "bracket"]);

export function HandMark({ name, id, drawIn = false, className }: {
  name: MarkName;
  /** What it marks: an entry's or a note's id. */
  id: string;
  /** True at the moment its event happens: it draws itself in, once. */
  drawIn?: boolean;
  className?: string;
}) {
  const mark = MARKS[name];
  const variant = variantOf(id, mark.variants.length);
  return (
    <svg
      className={["hand-mark", drawIn && "draws", className].filter(Boolean).join(" ")}
      viewBox={mark.viewBox}
      preserveAspectRatio={ALONG_TEXT.has(name) ? "none" : "xMidYMid meet"}
      data-mark={name}
      data-variant={variant}
      aria-hidden
    >
      {mark.variants[variant].map((d) => <path key={d} d={d} />)}
    </svg>
  );
}
