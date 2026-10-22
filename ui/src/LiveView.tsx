import { useEffect, useRef, useState } from "react";
import { covers, hostOf, live, suggestName } from "./logins";

const KEYS = ["Enter", "Backspace", "Tab", "Escape", "ArrowUp", "ArrowDown", "ArrowLeft",
  "ArrowRight", "Delete", "Home", "End", "PageUp", "PageDown"];

type Form = { name: string; domains: string; tier: "ask" | "free"; username: string;
              password: string };

/**
 * The sandbox browser's live view (agent/browser/live.py): its screen a few
 * times a second, and the user's clicks, keys and scrolling sent to it. What
 * the user types here goes to the page, never to a model.
 *
 * The worker served this as a page of its own until 13 October 2026, and the
 * app showed it in a frame. A frame cannot carry the token every request now
 * needs (core/localapi.py), so the app draws it.
 */
export function LiveView({ onSaved, start = "" }: {
  onSaved: () => void;
  /** The address it opens on, for a sign-in the page set up (LinkedIn's). */
  start?: string;
}) {
  const [address, setAddress] = useState(start);
  const [picture, setPicture] = useState("");
  const [message, setMessage] = useState("");
  const [fileNote, setFileNote] = useState("");
  const [form, setForm] = useState<Form>({ name: "", domains: "", tier: "ask", username: "",
                                           password: "" });
  const lastUrl = useRef("");
  const editingAddress = useRef(false);
  const screen = useRef<HTMLDivElement>(null);
  const img = useRef<HTMLImageElement>(null);
  // The domains a login needs are every host its sign-in passes through: a
  // site can start on its own host, go through another's sign-in page and
  // end on a third. So every host seen is added, until the user edits the box.
  const seen = useRef<string[]>([]);
  const touched = useRef({ name: false, domains: false });

  const set = (patch: Partial<Form>) => setForm((f) => ({ ...f, ...patch }));

  useEffect(() => {
    let alive = true;
    let asking = false;
    const noteHost = (url: string) => {
      const host = hostOf(url);
      if (!host || /^[\d.]+$/.test(host) || !host.includes(".")) return;
      if (seen.current.includes(host)) return;
      seen.current = [...seen.current, host];
      setForm((f) => ({
        ...f,
        domains: touched.current.domains ? f.domains : seen.current.slice(0, 12).join(" "),
        name: touched.current.name || f.name ? f.name : suggestName(host),
      }));
    };
    const timer = window.setInterval(async () => {
      if (document.hidden || asking) return;
      asking = true;
      try {
        const f = await live.frame();
        if (!alive || !f?.ok || !f.jpeg) return;
        setPicture(`data:image/jpeg;base64,${f.jpeg}`);
        if (f.url && f.url !== lastUrl.current) {
          lastUrl.current = f.url;
          if (!editingAddress.current) setAddress(f.url);
          noteHost(f.url);
        }
      } finally {
        asking = false;
      }
    }, 350);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, []);

  // Not React's onWheel, which is passive and cannot keep the app from scrolling too.
  useEffect(() => {
    const el = screen.current;
    if (!el) return;
    const wheel = (e: WheelEvent) => {
      e.preventDefault();
      void live.act({ kind: "scroll", dy: e.deltaY });
    };
    el.addEventListener("wheel", wheel, { passive: false });
    return () => el.removeEventListener("wheel", wheel);
  }, []);

  const open = async () => {
    setMessage("");
    const r = await live.open(address);
    if (!r.ok) setMessage(r.error ?? "not opened");
  };

  const click = (e: React.MouseEvent<HTMLImageElement>) => {
    const el = img.current;
    if (!el || !el.clientWidth || !el.clientHeight) return;
    const box = el.getBoundingClientRect();
    void live.act({
      kind: "click",
      x: (e.clientX - box.left) * (el.naturalWidth / el.clientWidth),
      y: (e.clientY - box.top) * (el.naturalHeight / el.clientHeight),
    });
    screen.current?.focus();
  };

  const key = (e: React.KeyboardEvent) => {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (KEYS.includes(e.key)) {
      e.preventDefault();
      void live.act({ kind: "key", key: e.shiftKey && e.key === "Tab" ? "Shift+Tab" : e.key });
    } else if (e.key.length === 1) {
      e.preventDefault();
      void live.act({ kind: "type", text: e.key });
    }
  };

  const save = async () => {
    setMessage("");
    const domains = form.domains.toLowerCase().split(/[\s,]+/).filter(Boolean);
    const here = hostOf(lastUrl.current);
    if (here && !covers(domains, here)) {
      setMessage(`You are logged in on ${here}, which is not in these domains, so its ` +
                 "sign-in would not be saved. Add it to the domains first.");
      return;
    }
    const r = await live.save(form);
    // The form is kept when the save fails, so fixing the name does not mean
    // typing the password again.
    if (!r.ok) {
      setMessage(r.error ?? "not saved");
      return;
    }
    set({ password: "" });
    setMessage(`Saved ${r.site?.name}${r.site?.has_password ? ", with its password." : "."}` +
               " This browser closes in a minute unless you use the picture again.");
    onSaved();
  };

  const addFile = (file: File | undefined) => {
    if (!file) return;
    const reader = new FileReader();
    reader.onload = async () => {
      const b64 = String(reader.result).split(",", 2)[1] ?? "";
      const r = await live.outbox(file.name, b64);
      setFileNote(r.ok ? `Added ${r.file}` : r.error ?? "not added");
    };
    reader.readAsDataURL(file);
  };

  return (
    <div className="browser-panel">
      <p className="muted small">
        A sandboxed browser, with nothing of yours in it. Click the picture to type into it;
        what you type goes to the page, never to a model.
      </p>
      <div className="answer-row">
        <input placeholder="Address, e.g. login.northwind.example" value={address}
               onFocus={() => { editingAddress.current = true; }}
               onBlur={() => { editingAddress.current = false; }}
               onChange={(e) => setAddress(e.target.value)}
               onKeyDown={(e) => { if (e.key === "Enter") void open(); }} />
        <button className="primary" onClick={() => void open()}>Open</button>
        <button onClick={() => void live.stop()}>Close browser</button>
      </div>
      <div className="live-view" ref={screen} tabIndex={0} onKeyDown={key}
           onPaste={(e) => {
             e.preventDefault();
             void live.act({ kind: "type", text: e.clipboardData.getData("text") });
           }}>
        {picture ? <img ref={img} src={picture} alt="the sandbox browser" onClick={click} />
          : <p className="muted small">Open an address to start the browser.</p>}
      </div>
      <div className="knob">
        <span className="knob-says">Name</span>
        <input value={form.name} placeholder="northwind"
               onChange={(e) => { touched.current.name = true; set({ name: e.target.value }); }} />
      </div>
      <div className="knob">
        <span className="knob-says">Its domains (space-separated)</span>
        <input value={form.domains} placeholder="northwind.example"
               onChange={(e) => { touched.current.domains = true; set({ domains: e.target.value }); }} />
      </div>
      <div className="knob">
        <span className="knob-says">When the agent wants to use it</span>
        <select value={form.tier} onChange={(e) => set({ tier: e.target.value as "ask" | "free" })}>
          <option value="ask">Ask me each time</option>
          <option value="free">Just use it</option>
        </select>
      </div>
      <p className="muted small">
        Optional: so it can log in again without you. Stored in Windows Credential Manager;
        the agent can have them typed in, and never sees them.
      </p>
      <div className="knob">
        <span className="knob-says">Username</span>
        <input value={form.username} autoComplete="off"
               onChange={(e) => set({ username: e.target.value })} />
      </div>
      <div className="knob">
        <span className="knob-says">Password</span>
        <input type="password" value={form.password} autoComplete="new-password"
               onChange={(e) => set({ password: e.target.value })} />
      </div>
      <div className="item-actions">
        <button className="primary" onClick={() => void save()}>Save login</button>
      </div>
      {message && <div className="warn-text small">{message}</div>}
      <p className="muted small">
        The outbox: files there may be uploaded by the agent, each only after you approve a
        card naming it.
      </p>
      <input type="file" onChange={(e) => addFile(e.target.files?.[0])} />
      {fileNote && <div className="small">{fileNote}</div>}
    </div>
  );
}
