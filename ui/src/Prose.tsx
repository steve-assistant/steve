/* Steve's words on the page: paragraphs, lists behind outline diamonds,
   code on a slip, and links (docs/design/journal.md, "Steve's formatting").

   The replies are the model's Markdown. Shown raw, a list was a column of
   hyphens and a bold word wore four asterisks; parsed by a Markdown library
   into HTML, a reply could put any element into the window that holds the
   backend's token. So this reads the few forms the model uses into React
   elements, and anything else stays text. A link is a button that opens the
   user's browser (desktop.ts `openLink`), never an `<a href>`.

   Bold and quotes were drawn as plain bold and as a paragraph, so what
   Steve marked as not to be missed looked like the rest. Now the hand marks
   them, as a person reading the reply with a pen would: a line under a
   bold, and a line down a quote's left side, in the reply's own ink. Three
   at most, the first in reading order: a page of marks marks nothing.

   Until 19 October a short bold (a time, a price) was circled in red. Seen
   in the app, the circles crowded the words beside them and read as
   corrections, so every marked bold is underlined now. */

import { type ReactNode, useState } from "react";
import { openLink } from "./desktop";
import { HandMark } from "./HandMark";
import type { MarkName } from "./marks";
import "./prose.css";

type Block =
  | { kind: "p"; lines: string[] }
  | { kind: "quote"; lines: string[] }
  | { kind: "ul" | "ol"; items: string[] }
  | { kind: "code"; text: string };

const BULLET = /^\s*(?:[-*•]|\d+[.)])\s+/;
const NUMBERED = /^\s*\d+[.)]\s+/;
const FENCE = /^\s*```/;
const QUOTE = /^\s{0,3}>\s?/;

/** The most marks one reply carries; the rest of its bold stays bold. */
export const MOST_MARKS = 3;
// A bold up to this long is underlined. Longer bold stays bold: the line
// is drawn under its words' one box (an inline block), and a long run
// would wrap into a box two lines tall.
const UNDERLINED = 48;

/** The text cut into paragraphs, lists and fenced code. */
export function blocks(text: string): Block[] {
  const out: Block[] = [];
  const lines = text.replace(/\r\n?/g, "\n").split("\n");
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (FENCE.test(line)) {
      const code: string[] = [];
      while (++i < lines.length && !FENCE.test(lines[i])) code.push(lines[i]);
      out.push({ kind: "code", text: code.join("\n") });
      continue;
    }
    if (!line.trim()) continue;
    const last = out[out.length - 1];
    if (QUOTE.test(line)) {
      // A quote runs on over its lines, a bare ">" between them included.
      const goesOn = last?.kind === "quote" && QUOTE.test(lines[i - 1] ?? "");
      const quote = goesOn ? last : { kind: "quote" as const, lines: [] };
      if (!goesOn) out.push(quote);
      const said = line.replace(QUOTE, "");
      if (said.trim()) quote.lines.push(said);
      continue;
    }
    if (BULLET.test(line)) {
      const kind = NUMBERED.test(line) ? "ol" : "ul";
      const item = line.replace(BULLET, "");
      // Items with a blank line between them are still one list.
      if (last?.kind === kind) last.items.push(item);
      else out.push({ kind, items: [item] });
      continue;
    }
    // A line under a list item, indented, carries on that item.
    if ((last?.kind === "ul" || last?.kind === "ol") && /^\s+\S/.test(line) && lines[i - 1]?.trim()) {
      last.items[last.items.length - 1] += ` ${line.trim()}`;
      continue;
    }
    if (last?.kind === "p" && lines[i - 1]?.trim()) last.lines.push(line);
    else out.push({ kind: "p", lines: [line] });
  }
  return out;
}

// **bold**, `code`, [words](https://...), and a bare https address.
const INLINE = /\*\*([^*]+)\*\*|`([^`]+)`|\[([^\]]+)\]\((https:\/\/[^\s)]+)\)|(https:\/\/[^\s<>()]+[^\s<>().,;:!?'"])/g;

/** A link in Steve's words. One that would not open said nothing, so the
 *  click seemed lost (step 11's audit): why it did not is said beside it. */
export function Link({ href, children }: { href: string; children: ReactNode }) {
  const [failed, setFailed] = useState("");
  const open = () => void openLink(href).then((r) =>
    setFailed(r.ok ? "" : r.error ?? "It would not open."));
  return (
    <>
      <button type="button" className="prose-link" title={href} onClick={open}>
        {children}
      </button>
      {failed && <span className="prose-link-failed" role="status"> ({failed})</span>}
    </>
  );
}

/** Hands out one reply's marks: the named mark while any are left, else
    nothing. Each takes its shape from the reply's id and its place, so a
    reply looks the same at every redraw. */
type Pen = (name: MarkName, className: string) => ReactNode;

function penFor(id: string | undefined, drawIn: boolean): Pen {
  let used = 0;
  return (name, className) => {
    if (id === undefined || used >= MOST_MARKS) return null;
    used += 1;
    return <HandMark name={name} id={`${id}:${used}`} drawIn={drawIn} className={className} />;
  };
}

// A function, not a component: the pen is spent here, in Prose's own
// render and in reading order. A child component would spend it whenever
// React rendered that child, twice over in strict mode.
function bold(key: string, words: string, pen?: Pen): ReactNode {
  const mark = words.trim().length <= UNDERLINED && pen?.("underline", "prose-underline");
  if (!mark) return <strong key={key}>{words}</strong>;
  return <strong key={key} className="marked">{words}{mark}</strong>;
}

export function inline(text: string, pen?: Pen): ReactNode[] {
  const out: ReactNode[] = [];
  let from = 0;
  for (const m of text.matchAll(INLINE)) {
    const at = m.index ?? 0;
    if (at > from) out.push(text.slice(from, at));
    const key = `${at}`;
    if (m[1] !== undefined) out.push(bold(key, m[1], pen));
    else if (m[2] !== undefined) out.push(<code key={key} className="t-code">{m[2]}</code>);
    else if (m[3] !== undefined) out.push(<Link key={key} href={m[4]}>{m[3]}</Link>);
    else out.push(<Link key={key} href={m[5]}>{m[5]}</Link>);
    from = at + m[0].length;
  }
  if (from < text.length) out.push(text.slice(from));
  return out;
}

export function Prose({ text, className, id, drawIn = false }: {
  text: string;
  className?: string;
  /** The reply's id. Given, the hand marks its bold and its quotes. */
  id?: string;
  /** True when the reply arrives as the user looks: its marks draw in. */
  drawIn?: boolean;
}) {
  const pen = penFor(id, drawIn);
  return (
    <div className={["prose", className].filter(Boolean).join(" ")}>
      {blocks(text).map((block, i) => {
        if (block.kind === "code") return <pre key={i} className="t-code">{block.text}</pre>;
        if (block.kind === "quote") {
          if (!block.lines.length) return null;
          // Taken before the words inside it: the bracket comes first in reading.
          const mark = pen("bracket", "prose-bracket");
          return (
            <blockquote key={i} className={mark ? "marked" : undefined}>
              {mark}
              {block.lines.map((line, j) => (
                <span key={j}>{j > 0 && <br />}{inline(line, pen)}</span>
              ))}
            </blockquote>
          );
        }
        if (block.kind === "p") {
          return (
            <p key={i}>
              {block.lines.map((line, j) => (
                <span key={j}>{j > 0 && <br />}{inline(line, pen)}</span>
              ))}
            </p>
          );
        }
        const List = block.kind;
        return (
          <List key={i}>
            {block.items.map((item, j) => <li key={j}>{inline(item, pen)}</li>)}
          </List>
        );
      })}
    </div>
  );
}
