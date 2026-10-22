/**
 * Coming up and Call notes: two of the sidebar's places, beside Extensions
 * (agent/worker/places.py).
 *
 * Reminders and the notes Steve takes on a call were there only for those
 * who knew to ask: nothing on screen listed them. Coming up lists what is
 * set, by when it goes off, and the week's events; Call notes lists the
 * notes, each opening its day at the note, since the day is where a note is
 * read and deleted.
 */

import { control, when, type CallNote } from "./control";
import { Section } from "./Form";
import { HandMark } from "./HandMark";
import { Loading } from "./Loading";
import { usePolled } from "./pages";
import "./places.css";

export function ComingUp({ onScreen }: { onScreen: boolean }) {
  // The calendars are asked each time, so not often: a minute apart.
  const { data, failed } = usePolled((signal) => control.coming(signal), { everyMs: 60_000, onScreen });
  if (data === null) return <Waiting failed={failed} />;
  return (
    <div className="panel form-page places-page">
      <Section title="Reminders" why={
        "What Steve will remind you of, soonest first. To set, move or cancel one, ask Steve: "
        + "“Remind me to call the vet at 5 pm.”"}>
        {data.reminders.length === 0 && data.done.length === 0
          ? <p className="form-hint">None set.</p>
          : (
            <ul className="form-list">
              {data.reminders.map((r) => (
                <li key={r.id} className="place-row">
                  <span className="form-list-meta place-when">{when(r.at)}</span>
                  <span>{r.text}</span>
                </li>
              ))}
              {/* Today's done, struck through by hand, as on a paper list:
                  what went off stays in sight for the day. */}
              {data.done.map((r) => (
                <li key={`done:${r.id}`} className="place-row place-done">
                  <span className="form-list-meta place-when">{when(r.at)}</span>
                  <s className="place-struck">
                    {r.text}
                    <HandMark name="strike" id={`reminder:${r.id}`} className="place-strike" />
                  </s>
                </li>
              ))}
            </ul>
          )}
      </Section>
      <Section title="This week" why="Your calendars' events for the next seven days.">
        {data.events.length === 0
          ? <p className="form-hint">Nothing on your calendars, or none connected.</p>
          : (
            <ul className="form-list">
              {data.events.map((e, i) => (
                <li key={`${e.start}:${i}`} className="place-row">
                  <span className="form-list-meta place-when">
                    {e.all_day ? new Date(e.start).toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" }) : when(e.start)}
                  </span>
                  <span>{e.summary ?? "An event"}{e.location && <span className="form-list-meta"> · {e.location}</span>}</span>
                </li>
              ))}
            </ul>
          )}
      </Section>
    </div>
  );
}

function Waiting({ failed }: { failed: boolean }) {
  return (
    <div className="panel form-page">
      {failed ? <p className="form-hint">Steve isn't answering; asking again.</p> : <Loading />}
    </div>
  );
}

export function CallNotes({ onScreen, onOpen }: {
  onScreen: boolean;
  /** Its day, at the note. */
  onOpen: (date: string, key: string) => void;
}) {
  const { data, failed } = usePolled((signal) => control.calls(signal), { everyMs: 15_000, onScreen });
  if (data === null) return <Waiting failed={failed} />;
  const calls: CallNote[] = data.calls;
  return (
    <div className="panel form-page places-page">
      <Section title="Notes on your calls" why={
        "Turn on Call notes at the sidebar's foot, and Steve takes notes on your next call or "
        + "meeting: who said what, and what was agreed. Each opens on its day."}>
        {calls.length === 0
          ? <p className="form-hint">None yet.</p>
          : (
            <ul className="form-list">
              {calls.map((c) => (
                <li key={c.id}>
                  <button type="button" className="place-row" onClick={() => onOpen(c.date, c.key)}>
                    <span className="form-list-meta place-when">{when(c.at)}</span>
                    <span>{c.title || (c.kind === "call" ? "A call" : "A meeting")}
                      {c.status === "open" && <span className="form-list-meta"> · still listening</span>}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
      </Section>
    </div>
  );
}
