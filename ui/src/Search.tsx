/* The sidebar's search (docs/design/journal.md, Search): Ctrl+K puts the
   cursor in it, and what it finds takes the day list's place, a day at a
   time with the matching lines, the words highlighted. A line opens its
   day at that entry; Esc returns to the days.

   Nothing searched the journal: a thing said three weeks ago was found by
   scrolling back through every day between. */

import { Fragment, useEffect, useRef, useState } from "react";
import { control, type Found } from "./control";
import { dayName, localDate } from "./journal";

/** How long the typing rests before the backend is asked. */
const ASK_AFTER_MS = 250;

function named(date: string, today: string): string {
  const near = dayName(date, today);
  return near === "Today" || near === "Yesterday" ? near
    : localDate(date).toLocaleDateString([], { weekday: "long", day: "numeric", month: "long" });
}

/** A line with each of the words highlighted where it appears. */
function Marked({ line, words }: { line: string; words: string[] }) {
  const escaped = words.map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  if (!escaped.length) return <>{line}</>;
  const parts = line.split(new RegExp(`(${escaped.join("|")})`, "gi"));
  return (
    <>
      {parts.map((part, i) => (i % 2 === 1
        ? <mark key={i}>{part}</mark> : <Fragment key={i}>{part}</Fragment>))}
    </>
  );
}

export function Search({ onFound, onSearching, onOpen }: {
  /** A line chosen: its day, opened at its entry; or the day alone. */
  onFound: (date: string, key: string | null) => void;
  /** Whether what was found stands in for the day list. */
  onSearching: (searching: boolean) => void;
  /** The box wants the whole sidebar (true), or is done with it: a
      narrow window's rail opens for it. */
  onOpen: (open: boolean) => void;
}) {
  const [query, setQuery] = useState("");
  const [found, setFound] = useState<Found | null>(null);
  const input = useRef<HTMLInputElement>(null);
  const asked = query.trim();

  // Ctrl+K from anywhere in the window, its words selected to type over.
  // At once where the box is drawn: a frame later, the first key typed went
  // elsewhere. A folded sidebar opens first, and the box takes the cursor
  // once it is drawn: hidden, it cannot.
  useEffect(() => {
    const take = () => {
      input.current?.focus();
      input.current?.select();
    };
    const key = (e: KeyboardEvent) => {
      if (e.ctrlKey && !e.altKey && !e.shiftKey && e.key.toLowerCase() === "k") {
        e.preventDefault();
        onOpen(true);
        take();
        if (document.activeElement !== input.current) requestAnimationFrame(take);
      }
    };
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  }, [onOpen]);

  // Asked once the typing rests; an answer to older words is dropped.
  useEffect(() => {
    onSearching(asked !== "");
    if (!asked) return;
    let current = true;
    const timer = window.setTimeout(() => {
      control.search(asked).then((answer) => {
        if (current) setFound(answer.ok ? answer : null);
      }, () => {
        if (current) setFound(null);
      });
    }, ASK_AFTER_MS);
    return () => {
      current = false;
      window.clearTimeout(timer);
    };
  }, [asked, onSearching]);

  const shown = asked && found?.q === asked ? found : null;
  const words = asked.split(/\s+/);
  return (
    <div className="search" role="search">
      <input ref={input} type="search" className="search-box" placeholder="Search"
             aria-label="Search the journal" value={query}
             onFocus={() => onOpen(true)}
             onChange={(e) => setQuery(e.target.value)}
             onKeyDown={(e) => {
               if (e.key === "Escape") {
                 // Back to the days, not on to the composer.
                 e.preventDefault();
                 e.stopPropagation();
                 setQuery("");
                 setFound(null);
                 input.current?.blur();
                 onOpen(false);
               }
             }} />
      {shown && (shown.days.length === 0 ? (
        <p className="found-none t-small">Nothing found for “{asked}”.</p>
      ) : (
        <ol className="found" aria-label="Found">
          {shown.days.map((day) => (
            <li key={day.date}>
              <div className="found-day">{named(day.date, shown.today)}</div>
              <ul>
                {day.hits.map((hit) => (
                  <li key={hit.key}>
                    <button type="button" className="found-line"
                            onClick={() => onFound(day.date, hit.key)}>
                      <Marked line={hit.line} words={words} />
                    </button>
                  </li>
                ))}
                {day.more > 0 && (
                  <li>
                    <button type="button" className="found-more t-meta"
                            onClick={() => onFound(day.date, null)}>
                      {day.more} more that day
                    </button>
                  </li>
                )}
              </ul>
            </li>
          ))}
        </ol>
      ))}
    </div>
  );
}
