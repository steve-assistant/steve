import { useEffect, useRef, useState } from "react";
import { PassphraseForm } from "./Approvers";
import { formatUSD, fromSqlite, type YesProof } from "./api";
import {
  control,
  when,
  type AddServer,
  type Admission,
  type Build,
  type CardServer,
  type Module,
  type ModulesListing,
  type Report,
  type VersionChanges,
} from "./control";
import { Check, Choices, Field, Section } from "./Form";
import { Loading } from "./Loading";
import { type Say } from "./pages";
import { helloSigned } from "./session";
import "./extensions.css";

interface Props {
  toast: Say;
  /** Whether anyone can see the window: hidden in the tray, nothing is asked. */
  onScreen: boolean;
  /** This install's Windows Hello key, where a yes can be a glance; else null. */
  hello: string | null;
  /** Steve can build one here (the sandbox works): the page asks for one. */
  canBuild?: boolean;
  /** Ask for a tool in the conversation, where the build is followed. */
  onAsk?: (text: string) => void;
}

// A build moves on while the page is open (running, then a card), and a
// module's watch counts its calls, so the list is read again while it is
// on screen.
const POLL_MS = 10000;

/**
 * The Extensions page (06-modules.md, section 9): every module, built in,
 * built by Steve or imported, with its switch, its watch and its versions;
 * the cards waiting for the user's yes; the builds under way and the
 * reports of requests that could not be modules.
 *
 * A module from outside is on only after its card, and the card is the
 * one place the user decides, so it keeps section 5's order: core's reading
 * of what the grants allow comes first, since that is computed from the
 * manifest and cannot lie; the module's own description comes after, in a
 * block that says whose words they are, because a module describes itself
 * however it likes. An import is unsigned, so its hash stands at the top.
 * Every label and sentence here is the backend's (agent/accounts.py
 * `listing`, core/lifecycle.py `record`), so a new module needs no new app.
 *
 * In the back pages' terms (docs/design/back-pages.md): sections with a
 * sentence on what they are for, rows between hairlines rather than boxed
 * cards, a drawn checkbox for a module's switch, words in a row for a
 * choice of two, and a second step that names what a removal loses. It was
 * step 6's tabs restyled by the back pages' stylesheet: boxed cards, a
 * switch, and chips, in a page whose every other form was paper's.
 */
export function Modules({ toast, onScreen, hello, canBuild = false, onAsk }: Props) {
  const [listing, setListing] = useState<ModulesListing | null>(null);
  const [adding, setAdding] = useState(false);
  // Bumped by each change the user makes, to read the list again at once.
  const [changes, setChanges] = useState(0);

  useEffect(() => {
    if (!onScreen) return;
    const controller = new AbortController();
    const read = async () => {
      try {
        const listed = await control.modules(controller.signal);
        if (!controller.signal.aborted) setListing(listed);
      } catch {
        /* the supervisor may be mid-swap; the next tick reads it again */
      }
    };
    void read();
    const timer = window.setInterval(() => void read(), POLL_MS);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, [onScreen, changes]);

  const changed = () => setChanges((n) => n + 1);
  const importing = useImportZip(toast, changed);

  if (listing === null) {
    return <div className="panel form-page"><Loading /></div>;
  }
  const builds = listing.builds.filter((b) => b.state !== "dismissed");
  const underWay = builds.filter((b) => ["needs_allow", "running", "stopped"].includes(b.state));
  const ended = builds.filter((b) => !underWay.includes(b));

  return (
    <div className="panel form-page extensions">
      {canBuild && onAsk && <AskForOne onAsk={onAsk} />}
      {listing.admissions.length > 0 && (
        <Section title="Waiting for your yes" why={
          "Nothing from outside is on until you say yes here. What Steve reads in its manifest "
          + "comes first; what it says of itself comes after, marked as its own words."}>
          <ul className="form-list">
            {listing.admissions.map((card) => (
              <AdmissionCard key={card.admission} card={card} toast={toast} onChanged={changed}
                             hello={hello} />
            ))}
          </ul>
        </Section>
      )}

      <Section title="Installed" why={
        "Each extension, built in, built by Steve or imported. Turned off, its tools, sections "
        + "and routines leave from the next turn, and nothing is deleted."}
               actions={
                 <span className="ext-head-actions">
                   {importing.button}
                   {!adding && (
                     <button type="button" className="form-quiet" onClick={() => setAdding(true)}>
                       Add a server
                     </button>
                   )}
                 </span>
               }>
        {importing.said}
        {adding && (
          <AddServerForm toast={toast} onAdded={() => { setAdding(false); changed(); }}
                         onClose={() => setAdding(false)} />
        )}
        <ul className="form-list">
          {listing.modules.map((module) => (
            <ModuleCard key={module.id} module={module} toast={toast} onChanged={changed}
                        waiting={listing.accounts.filter((a) => a.module === module.id)
                          .reduce((sum, a) => sum + a.waiting, 0)}
                        onImprove={canBuild && onAsk && module.origin === "built" ? onAsk
                          : undefined} />
          ))}
        </ul>
      </Section>

      {builds.length > 0 && (
        <Section title="Builds" why={
          "Extensions Steve is building because you asked, with what each has cost so far. "
          + "A finished one waits above for your yes."}>
          {underWay.length === 0 && <p className="form-hint">None under way.</p>}
          <ul className="form-list">
            {underWay.map((build) => (
              <BuildCard key={build.id} build={build} toast={toast} onChanged={changed} />
            ))}
          </ul>
          {ended.length > 0 && (
            <details className="ext-details">
              <summary>Earlier builds ({ended.length})</summary>
              <ul className="form-list">
                {ended.map((build) => (
                  <BuildCard key={build.id} build={build} toast={toast} onChanged={changed} />
                ))}
              </ul>
            </details>
          )}
        </Section>
      )}

      {listing.reports.length > 0 && (
        <Section title="Reports" why={
          "Requests that could not be extensions. Steve sends these nowhere: copy one to pass it on."}>
          <ul className="form-list">
            {listing.reports.map((report) => (
              <ReportCard key={report.id} report={report} toast={toast} onChanged={changed} />
            ))}
          </ul>
        </Section>
      )}
    </div>
  );
}

/* ------------------------------------------------------------- helpers */

const ORIGIN_WORDS: Record<Module["origin"], string> = {
  builtin: "Built in",
  built: "Built by Steve",
  imported: "Imported",
};

const TIER_WORDS: Record<string, string> = {
  sandbox: "runs in the sandbox",
  backend: "runs in Steve's backend",
};

/** "mail and calendar": a list in the user's words. */
function listed(words: string[]): string {
  const plain = words.map((w) => w.replace(/_/g, " "));
  if (plain.length <= 1) return plain.join("");
  return `${plain.slice(0, -1).join(", ")} and ${plain[plain.length - 1]}`;
}

/** What went wrong, under what was tried, in red ink. */
function Problem({ text }: { text: string | null }) {
  return text ? <p className="form-error ext-problem" role="alert">{text}</p> : null;
}

/** A file's bytes as base64, the data URL's prefix taken off. */
function readBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const url = String(reader.result ?? "");
      resolve(url.slice(url.indexOf(",") + 1));
    };
    reader.onerror = () => reject(reader.error ?? new Error("the file could not be read"));
    reader.readAsDataURL(file);
  });
}

/**
 * Offer base64 bytes as a file to save. A Blob and an `<a download>`: no
 * page is loaded and nothing leaves the app, so the content security
 * policy has nothing to allow. The link is the window's own for a moment,
 * then gone.
 */
function download(name: string, base64: string): void {
  const bytes = Uint8Array.from(atob(base64), (c) => c.charCodeAt(0));
  const url = URL.createObjectURL(new Blob([bytes], { type: "application/zip" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  document.body.appendChild(link);
  link.click();
  link.remove();
  // Revoked later, not at once: the download reads the blob after the click returns.
  window.setTimeout(() => URL.revokeObjectURL(url), 60000);
}

/* ------------------------------------------------------- the approval card */

const KIND_WORDS: Record<Admission["kind"], string> = {
  new: "New",
  version: "New version",
  replace: "Replaces the installed one",
};

const MARK_WORDS: Record<string, (value: string) => string> = {
  host: (value) => `reaches ${value}`,
  class: (value) => `provides ${value.replace(/_/g, " ")} accounts`,
  reads: (value) => `reads your ${value.replace(/_/g, " ")}`,
};

/**
 * One card waiting for the user, in section 5's order: core's summary of
 * the grants; for a module Steve built, the request and each grant its
 * words did not name; the module's own words, marked as its own; a server's
 * tools; and the details, closed. A new version shows what changed, with
 * the rest folded away below it.
 *
 * Where the card needs the passphrase and Steve has Windows Hello, a glance
 * does instead, as on an action's card: Hello signs the backend's challenge
 * for this card and the files that were checked (core/lifecycle.py
 * `hello_binding`). Before, a module's card took the passphrase alone, so
 * a user who signed in and said yes to spending with a glance had to find
 * the passphrase here.
 */
function AdmissionCard({ card, toast, onChanged, hello }: {
  card: Admission;
  toast: Say;
  onChanged: () => void;
  hello: string | null;
}) {
  const [words, setWords] = useState("");
  const [secret, setSecret] = useState("");
  // A new version starts from the tools still freed as they were; starting
  // from none made every tool the user had freed a card again.
  const [free, setFree] = useState<string[]>(
    () => card.server?.tools.filter((t) => t.free).map((t) => t.name) ?? []);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [choosing, setChoosing] = useState(false);

  const server = card.server;
  const label = card.own_words.label || card.id;
  // The secret is asked only where the one kept does not go on, unchanged.
  const secretReady = !server?.secret || server.secret_kept || secret.length > 0;
  // Where Steve has Windows Hello, a card that needs the passphrase takes
  // a glance instead when none is typed: one button for either proof, so
  // the choice of on or off is not asked twice over.
  const glance = card.passphrase && !!hello && words.length === 0;
  const ready = (!card.passphrase || words.length > 0 || !!hello) && secretReady;
  // A new version keeps its switch; a new module is installed off unless
  // the user says on, which is the main choice (approving one left it off
  // in the list, where it was easy to miss).
  const chooses = card.kind !== "version";

  const approve = async (on: boolean) => {
    setBusy(true);
    setProblem(null);
    let proof: YesProof = { passphrase: words };
    if (glance) {
      const signed = hello ? await helloSigned(hello, { for: "yes", admission: card.admission })
        : { error: "Windows Hello isn't set up for Steve." };
      if ("error" in signed) {
        setBusy(false);
        setProblem(signed.error === "cancelled" ? "Cancelled." : signed.error);
        return;
      }
      proof = { hello: signed };
    }
    const result = await control.approveModule(card.admission, proof, server ? {
      secrets: server.secret && secret ? { [server.secret]: secret } : {}, free } : undefined, on);
    setBusy(false);
    setWords("");
    if (!result.ok) {
      // The backend says so in words. Its one `needs` here, a sign-in, has
      // signed the app out already (`postAnswer`); the rest are the words.
      if (/no passphrase is set/i.test(result.error ?? "")) setChoosing(true);
      setProblem(result.error ?? "Could not approve it.");
      return;
    }
    setSecret("");
    toast(card.kind === "version" ? `Approved: ${label} runs version ${card.version}.`
      : !on ? `Approved: ${label} is installed, and off until you turn it on.`
      : result.on ? `Approved and on: ${label} is ready to use.`
      : `Approved, but ${label} could not be turned on: ${result.why ?? "it would not start"}. `
        + "Its switch, under Installed, tries again.");
    onChanged();
  };

  const decline = async () => {
    setBusy(true);
    setProblem(null);
    const result = await control.declineModule(card.admission);
    setBusy(false);
    if (!result.ok) {
      setProblem(result.error ?? "Could not decline it.");
      return;
    }
    toast(`Declined: ${label}'s files are deleted.`);
    onChanged();
  };

  // A new version leads with what changed; the rest folds away below it.
  const changed = card.kind === "version" && card.changes !== null;

  return (
    <li className="ext-card ext-asking" data-admission={card.admission}>
      <div className="ext-head">
        <h3 className="ext-title">{label}</h3>
        <span className="form-list-meta">
          {KIND_WORDS[card.kind]} · {ORIGIN_WORDS[card.origin]} · version {card.version}
        </span>
      </div>
      {card.origin === "imported" && (
        <div className="form-warning ext-imported">
          Imported, so unsigned: anyone could have made it. Check its hash against the one its
          author gives you.
          <code className="ext-hash">{card.details.hash}</code>
        </div>
      )}
      {card.kind === "replace" && (
        <p className="form-warning">
          Approving it removes the installed {card.id} first, with its secrets and data.
        </p>
      )}

      {changed && card.changes ? <Changes changes={card.changes} /> : <Summary lines={card.summary} />}

      {card.request !== null && (
        <div className="ext-block ext-request">
          {/* The request is the model's words, which what it read can steer;
              the user's own last message stands beside it. */}
          {card.user_said && (
            <>
              <span className="form-label">
                You said{card.came_from ? ` in ${card.came_from}` : ""}
              </span>
              <blockquote className="ext-quote">“{card.user_said}”</blockquote>
            </>
          )}
          <span className="form-label">
            {card.user_said
              ? "Steve's request, in its own words"
              : `Asked for${card.came_from ? ` in ${card.came_from}` : ""}`}
          </span>
          <blockquote className="ext-quote">“{card.request}”</blockquote>
          {card.marks && card.marks.length > 0 && (
            <p className="form-warning">
              Your request did not name these, which it asks for anyway:{" "}
              {card.marks.map((m) => (MARK_WORDS[m.what] ?? ((v: string) => v))(m.value)).join("; ")}.
            </p>
          )}
        </div>
      )}

      {changed ? (
        <details className="ext-details">
          <summary>All it can do, and its own words</summary>
          <Summary lines={card.summary} />
          <OwnWords words={card.own_words} />
        </details>
      ) : (
        <OwnWords words={card.own_words} />
      )}

      {server && (
        <ServerTools server={server} free={free} onFree={setFree}
                     secret={secret} onSecret={setSecret} busy={busy} />
      )}

      <Details card={card} />

      <Problem text={problem} />
      {choosing && (
        <>
          <p className="form-hint">Choose a passphrase first; approving this one takes it.</p>
          <PassphraseForm isSet={false} onDone={() => { setChoosing(false); setProblem(null); }} />
        </>
      )}
      <div className="form-actions">
        {card.passphrase && (
          <input type="password" className="form-input ext-passphrase" aria-label="Your passphrase"
                 placeholder={hello ? "Your passphrase, or none for Windows Hello"
                   : "Your passphrase"} value={words} disabled={busy}
                 onChange={(e) => setWords(e.target.value)}
                 onKeyDown={(e) => { if (e.key === "Enter" && ready) void approve(chooses); }} />
        )}
        <button type="button" className="primary" disabled={busy || !ready}
                onClick={() => void approve(chooses)}>
          {busy ? "Working…" : chooses ? "Approve and turn on" : "Approve"}
        </button>
        {chooses && (
          <button type="button" disabled={busy || !ready} onClick={() => void approve(false)}>
            Approve, leave off
          </button>
        )}
        <button type="button" className="form-quiet" disabled={busy}
                onClick={() => void decline()}>Decline</button>
      </div>
    </li>
  );
}

/** Core's reading of the manifest, never the module's own. */
function Summary({ lines }: { lines: string[] }) {
  return (
    <div className="ext-block">
      <span className="form-label">What it can do, as Steve reads its manifest</span>
      <ul className="ext-findings">
        {lines.map((line, i) => <li key={i}>{line}</li>)}
      </ul>
    </div>
  );
}

function Changes({ changes }: { changes: VersionChanges }) {
  if (changes.more.length === 0 && changes.fewer.length === 0) {
    return <p className="ext-line">It asks for nothing more than the version you approved.</p>;
  }
  const words = changes.words ?? [];
  return (
    <div className="ext-block">
      {changes.more.length > 0 && (
        <div className="form-warning">
          What this version asks for that the last did not:
          <ul className="ext-findings">{changes.more.map((c) => <li key={c}>{c}</li>)}</ul>
        </div>
      )}
      {/* Words steer what the grants are used for, so a version whose words
          changed shows them whole, before and after, above any passphrase. */}
      {words.length > 0 && (
        <>
          <span className="form-label">Its words for Steve that are new or changed, its own</span>
          {words.map((w) => (
            <div key={w.where} className="ext-block">
              <strong>{w.where}</strong>
              {w.was && (
                <>
                  <span className="form-hint">It said:</span>
                  <pre className="ext-text">{w.was}</pre>
                </>
              )}
              <span className="form-hint">{w.was ? "It now says:" : "It says:"}</span>
              <pre className="ext-text">{w.now}</pre>
            </div>
          ))}
        </>
      )}
      {changes.fewer.length > 0 && (
        <>
          <span className="form-label">What it no longer asks for</span>
          <ul className="ext-findings">{changes.fewer.map((c) => <li key={c}>{c}</li>)}</ul>
        </>
      )}
    </div>
  );
}

/**
 * The module's words, whole, in a block of their own. Its maker wrote
 * them; Steve screened them and holds them to its manifest, but a module
 * may describe itself kindly, so they come after core's summary and say
 * whose they are.
 */
function OwnWords({ words }: { words: Admission["own_words"] }) {
  return (
    <div className="ext-own-words">
      <span className="ext-own-label">The module's own words</span>
      <div><strong>{words.label}</strong>{words.for ? `: ${words.for}` : ""}</div>
      {words.adds && <div>{words.adds}</div>}
      {words.effort && <div className="form-hint">{words.effort}</div>}
      {words.prompt && (
        <>
          <span className="form-hint">Its section for Steve's instructions:</span>
          <pre className="ext-text">{words.prompt}</pre>
        </>
      )}
      {words.skills.map((skill) => (
        <div key={skill.title}>
          <div>Skill <strong>{skill.title}</strong>, when {skill.when}:</div>
          <ol className="ext-findings">{skill.steps.map((s, i) => <li key={i}>{s}</li>)}</ol>
        </div>
      ))}
      {words.attention && (
        <>
          <div>
            Looks at {words.attention.looks_at} every {words.attention.every_min} minutes. What
            it tells Steve to weigh:
          </div>
          <pre className="ext-text">{words.attention.judge}</pre>
        </>
      )}
      {words.routines.map((routine) => (
        <div key={routine.title}>
          <div>
            Routine <strong>{routine.title}</strong>, at {routine.at}
            {routine.days.length > 0 ? ` on ${listed(routine.days)}` : ", every day"}:
          </div>
          <pre className="ext-text">{routine.instruction}</pre>
        </div>
      ))}
    </div>
  );
}

/**
 * An MCP server's tools, in its own words, each with the server's label.
 * A card guards against the model misusing a tool after a page or a mail
 * has steered it, so a write asks every time; the user may free a tool to
 * run without asking, in conversations they are in.
 */
function ServerTools({ server, free, onFree, secret, onSecret, busy }: {
  server: CardServer;
  free: string[];
  onFree: (free: string[]) => void;
  secret: string;
  onSecret: (secret: string) => void;
  busy: boolean;
}) {
  const reads = server.tools.filter((t) => t.read).length;
  return (
    <div className="ext-block">
      <p className="ext-line">
        {server.package
          ? <>The package <code>{server.package}</code>, which Steve installs and runs</>
          : <>The server at <code>{server.url}</code></>}
        {": "}{server.tools.length} tool{server.tools.length === 1 ? "" : "s"}, {reads} labelled
        by the server as reads.
      </p>
      {server.package && (
        <ul className="ext-findings">
          <li>{server.packages ?? 0} packages installed, their files' hash <code>{server.tree}</code></li>
          {server.holds_its_sign_in && <li>It holds its own sign-in, in a folder only it can read.</li>}
          {server.anywhere && (
            <li className="form-error">It may reach any public host, so approving it takes your passphrase.</li>
          )}
        </ul>
      )}
      <p className="form-hint">
        Steve asks you before each use of a tool labelled as a write, and once in a conversation
        for one labelled as a read. Tick a tool to let it run without asking, in conversations
        you are in.
      </p>
      <ul className="ext-tools">
        {server.tools.map((tool) => (
          <li key={tool.name} data-tool={tool.name}>
            <span title={tool.description}>
              <strong>{tool.title || tool.name}</strong>{" "}
              <span className="form-hint">{tool.description}</span>
            </span>
            <span className={tool.read ? "form-list-meta" : "form-list-meta ext-write"}>
              {tool.read ? "read" : "write"}
            </span>
            {tool.offered ? (
              <Check label="Without asking" checked={free.includes(tool.name)}
                     disabled={busy}
                     onChange={(on) => onFree(on ? [...free, tool.name]
                       : free.filter((n) => n !== tool.name))} />
            ) : (
              <span className="form-list-meta" title="Its name does not fit a model's tool name">
                not offered
              </span>
            )}
          </li>
        ))}
      </ul>
      {server.secret && (
        <Field label={`The server's secret (${server.secret})`}
               hint={server.secret_kept ? "Kept from the version before; type one only to replace it." : undefined}>
          <input type="password" className="form-input" autoComplete="new-password" value={secret}
                 disabled={busy} onChange={(e) => onSecret(e.target.value)} />
        </Field>
      )}
    </div>
  );
}

/** Below the card, closed: the manifest whole, the hash, the hosts, the files, the review and the tests. */
function Details({ card }: { card: Admission }) {
  const { details } = card;
  const review = details.review;
  return (
    <details className="ext-details">
      <summary>Details</summary>
      <div className="ext-block">
        <p className="ext-line">Hash: <code className="ext-hash-inline">{details.hash}</code></p>
        <p className="ext-line">
          Hosts: {details.hosts.length > 0 ? listed(details.hosts) : "none"}
          {details.typed ? ", and a host you give it" : ""}
        </p>
        <div className="ext-line">
          {review === null ? "No code, so no review." : (
            <>
              <span className={review.approved ? "" : "form-error"}>
                {review.approved ? "The review found nothing wrong" : "The review advised against it"}
                {review.model ? ` (${review.model})` : ""}:
              </span>{" "}
              {review.reason}
              {!review.whole && (
                <div className="form-error">
                  The code is longer than the reviewer reads, so it read only part of it.
                </div>
              )}
              <div className="form-hint">A review is advice: only your yes turns it on.</div>
            </>
          )}
        </div>
        <p className="ext-line">
          Tests: {details.tests === null ? "no code, so none." : details.tests}
        </p>
        <p className="ext-line">Files: {details.files.join(", ")}</p>
        <pre className="ext-text ext-manifest">{details.manifest}</pre>
      </div>
    </details>
  );
}

/* --------------------------------------------------------------- a module */

const SINGULAR: Record<string, string> = { errors: "error", crashes: "crash", timeouts: "timeout" };

/** Its watch, in words: the calls since it was turned on or changed, and the failures. */
function watchWords(watch: NonNullable<Module["watch"]>): string {
  if (watch.turns === 0) return "No calls since it was turned on or changed.";
  const kinds = Object.entries(watch.by_kind)
    .map(([kind, n]) => `${n} ${n === 1 ? SINGULAR[kind] ?? kind : kind}`);
  const calls = `${watch.turns} call${watch.turns === 1 ? "" : "s"} since it was turned on or changed`;
  return watch.bad === 0 ? `${calls}, none failed.`
    : `${calls}; ${watch.bad} failed (${listed(kinds)}).`;
}

type Open = null | "off" | "remove" | "improve" | { rollback: string; dataFrom: string };

function ModuleCard({ module, waiting, toast, onChanged, onImprove }: {
  module: Module;
  /** Cards waiting on its accounts, which turning it off withdraws. */
  waiting: number;
  toast: Say;
  onChanged: () => void;
  /** Ask Steve for a better version, in the conversation: only a module
   *  Steve built, where it can build. */
  onImprove?: (text: string) => void;
}) {
  const [open, setOpen] = useState<Open>(null);
  const [better, setBetter] = useState("");
  // The ask names the module's id, which `build_module` takes for a new
  // version of it; the label alone left the model to guess one.
  const improve = () => {
    const said = better.trim();
    if (!said || !onImprove) return;
    onImprove(`Improve the ${module.label} extension you built (${module.id}): ${said}`);
    setBetter("");
    setOpen(null);
  };
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const outside = module.origin !== "builtin";

  /** One change at a time: a refusal goes under the card, then the list is read again. */
  const run = async (act: () => Promise<{ ok: boolean; error?: string }>, fallback: string,
                     done?: () => void) => {
    setBusy(true);
    setProblem(null);
    const result = await act();
    setBusy(false);
    if (!result.ok) {
      setProblem(result.error ?? fallback);
    } else {
      setOpen(null);
      done?.();
    }
    onChanged();
  };

  const switchTo = (on: boolean) =>
    run(() => control.switchModule(module.id, on), on ? "Could not turn it on." : "Could not turn it off.");

  const exportIt = async () => {
    setBusy(true);
    setProblem(null);
    const result = await control.exportModule(module.id);
    setBusy(false);
    if (!result.ok || !result.zip || !result.name) {
      setProblem(result.error ?? "Could not export it.");
      return;
    }
    try {
      download(result.name, result.zip);
    } catch (err) {
      // The zip came back but could not be handed on as a file: said here,
      // where the button is, rather than lost as an error no one sees.
      setProblem(`Could not save it: ${String(err)}`);
      return;
    }
    toast(`Exported ${result.name}.`);
  };

  const server = module.server;
  const facts = [
    module.version ? `version ${module.version}` : "",
    TIER_WORDS[module.tier] ?? module.tier,
    module.classes.length > 0 ? `provides ${listed(module.classes)} accounts` : "",
  ].filter(Boolean).join(" · ");
  // What it may reach, said on its row: once approved, it was on no screen.
  const places = [...module.hosts, ...(module.typed ? ["a host you give it"] : [])];

  return (
    <li className="ext-card" data-module={module.id}>
      <div className="ext-head">
        <h3 className="ext-title">{module.label}</h3>
        <span className="form-list-meta">{ORIGIN_WORDS[module.origin]}</span>
      </div>
      {/* Turning it off asks first, since it withdraws waiting cards. */}
      <Check label="On" checked={module.enabled} disabled={busy}
             onChange={(on) => on ? void switchTo(true) : setOpen("off")} />
      {module.for && <p className="form-hint">{module.for}</p>}
      <p className="ext-line">{facts.charAt(0).toUpperCase() + facts.slice(1)}</p>
      <p className="ext-line">
        {module.slots.length > 0 ? `Fills the ${listed(module.slots)} slot${module.slots.length === 1 ? "" : "s"}. ` : ""}
        {places.length > 0 ? `Reaches ${listed(places)}.` : "Reaches no host."}
      </p>
      {module.grants.length > 0 && (
        <details className="ext-details">
          <summary>All it may do</summary>
          <Summary lines={module.grants} />
        </details>
      )}
      {module.watch && <p className="ext-line">{watchWords(module.watch)}</p>}
      {server && (
        <p className="ext-line">
          {server.error ? <span className="form-error">The server: {server.error}.</span> : (
            <>
              {server.package
                ? <>The package <code>{server.package}</code>, which Steve runs</>
                : <>The server at <code>{server.url}</code></>}
              : {server.tools.length} tools
              {server.tools.some((t) => t.free)
                ? `; without asking: ${listed(server.tools.filter((t) => t.free).map((t) => t.name))}`
                : ""}.
            </>
          )}
        </p>
      )}

      {outside && module.versions.length > 0 && (
        <ul className="ext-versions">
          {module.versions.map((v) => (
            <li key={v.version}>
              <span>
                Version {v.version}
                <span className="form-list-meta"> · approved {when(fromSqlite(v.approved_at))}</span>
              </span>
              {v.current ? <span className="form-list-meta">running</span>
                : v.data_from ? (
                  <button type="button" disabled={busy}
                          onClick={() => setOpen({ rollback: v.version, dataFrom: v.data_from ?? "" })}>
                    Roll back
                  </button>
                ) : <span className="form-list-meta">no copy of its data</span>}
            </li>
          ))}
        </ul>
      )}

      <Problem text={problem} />
      {outside && open === null && (
        <div className="form-actions">
          {onImprove && (
            <button type="button" disabled={busy} onClick={() => setOpen("improve")}>Improve</button>
          )}
          <button type="button" disabled={busy} onClick={() => void exportIt()}>Export</button>
          <button type="button" className="danger" disabled={busy} onClick={() => setOpen("remove")}>
            Remove
          </button>
        </div>
      )}

      {open === "improve" && (
        <div className="ext-improve">
          <Field label="What should it do better?"
                 hint="Steve builds a new version from this one; it is checked again, and this one runs until you approve it.">
            <textarea rows={2} value={better} placeholder="take ounces as well as grams"
                      onChange={(e) => setBetter(e.target.value)} />
          </Field>
          <div className="form-actions">
            <button type="button" className="primary" disabled={!better.trim()} onClick={improve}>
              Ask Steve
            </button>
            <button type="button" className="form-quiet" onClick={() => setOpen(null)}>Cancel</button>
          </div>
        </div>
      )}

      {open === "off" && (
        <div className="form-asking">
          <span>
            Its tools, sections and routines leave from the next turn.
            {waiting > 0
              ? ` ${waiting} waiting request${waiting === 1 ? "" : "s"} on its accounts will be cancelled.`
              : ""} Nothing is deleted.
          </span>
          <button type="button" className="danger" disabled={busy}
                  onClick={() => void switchTo(false)}>Turn off</button>
          <button type="button" className="form-quiet" onClick={() => setOpen(null)}>Keep it on</button>
        </div>
      )}

      {open !== null && typeof open === "object" && (
        <div className="form-asking">
          <span>
            Back to version {open.rollback}: its data returns to how it was{" "}
            {when(fromSqlite(open.dataFrom))}, and the versions after it are deleted.
          </span>
          <button type="button" className="danger" disabled={busy}
                  onClick={() => void run(() => control.rollback(module.id, open.rollback),
                    "Could not roll it back.",
                    () => toast(`${module.label} is back on version ${open.rollback}.`))}>
            Roll back
          </button>
          <button type="button" className="form-quiet" onClick={() => setOpen(null)}>Keep this one</button>
        </div>
      )}

      {open === "remove" && (
        <div className="form-asking">
          <span>
            Remove {module.label}? Its files, secrets, accounts and sandbox are deleted. What
            Steve learned from it is kept, unless you say to forget it.
          </span>
          <button type="button" className="danger" disabled={busy}
                  onClick={() => void run(() => control.removeModule(module.id, false),
                    "Could not remove it.", () => toast(`Removed ${module.label}.`))}>
            Remove
          </button>
          <button type="button" className="danger" disabled={busy}
                  onClick={() => void run(() => control.removeModule(module.id, true),
                    "Could not remove it.", () => toast(`Removed ${module.label}.`))}>
            Remove, and forget what Steve learned
          </button>
          <button type="button" className="form-quiet" onClick={() => setOpen(null)}>Keep</button>
        </div>
      )}
    </li>
  );
}

/* ------------------------------------------------------- import and servers */

/**
 * Import a zip: read here, sent as base64, unpacked strictly and admitted
 * by the backend up to a card. An id installed already is refused unless
 * the user chooses Replace, which says first what it deletes. A hook, so
 * its button can sit in the section's head and what it says below it.
 */
function useImportZip(toast: Say, onImported: () => void) {
  const input = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  // A zip refused as installed already, kept to send again with `replace`.
  const [held, setHeld] = useState<{ name: string; zip: string } | null>(null);

  const send = async (name: string, zip: string, replace: boolean) => {
    setBusy(true);
    setProblem(null);
    const result = await control.importModule(zip, replace);
    setBusy(false);
    if (!result.ok) {
      const error = result.error ?? "Could not import it.";
      if (!replace && /installed already/i.test(error)) setHeld({ name, zip });
      else setHeld(null);
      setProblem(error);
      return;
    }
    setHeld(null);
    toast(`Imported ${name}: its card is waiting above.`);
    onImported();
  };

  const picked = async (file: File | undefined) => {
    if (input.current) input.current.value = "";
    if (!file) return;
    try {
      await send(file.name, await readBase64(file), false);
    } catch (err) {
      setProblem(String(err));
    }
  };

  const button = (
    <>
      <input ref={input} type="file" accept=".zip" hidden
             onChange={(e) => void picked(e.target.files?.[0])} />
      <button type="button" className="form-quiet" disabled={busy}
              onClick={() => input.current?.click()}>
        {busy ? "Importing…" : "Import a zip"}
      </button>
    </>
  );
  const said = problem || held ? (
    <div className="ext-said">
      <Problem text={problem} />
      {held ? (
        <div className="form-asking">
          <span>
            Replace it with {held.name}? Replacing deletes the installed module's secrets and
            data first; the new one then waits for your yes.
          </span>
          <button type="button" className="danger" disabled={busy}
                  onClick={() => void send(held.name, held.zip, true)}>
            Replace
          </button>
          <button type="button" className="form-quiet"
                  onClick={() => { setHeld(null); setProblem(null); }}>Keep the installed one</button>
        </div>
      ) : (
        <button type="button" className="form-quiet" onClick={() => setProblem(null)}>OK</button>
      )}
    </div>
  ) : null;
  return { button, said };
}

const NAME = /^[a-z][a-z0-9_]{0,31}$/;

const SERVER_KINDS = [
  ["remote", "A server at an address"],
  ["package", "A package Steve runs"],
] as const;

/**
 * Add an MCP server: one on the web (or on this computer) by its address,
 * or an npm package Steve installs and runs in a container of its own,
 * held to the hosts named here. Either way the backend lists its tools
 * and the server waits as a card.
 */
function AddServerForm({ toast, onAdded, onClose }: {
  toast: Say;
  onAdded: () => void;
  onClose: () => void;
}) {
  const [kind, setKind] = useState<"remote" | "package">("remote");
  const [name, setName] = useState("");
  const [label, setLabel] = useState("");
  const [url, setUrl] = useState("");
  const [secret, setSecret] = useState("");
  const [pkg, setPkg] = useState("");
  const [entry, setEntry] = useState("");
  const [hosts, setHosts] = useState("");
  const [anywhere, setAnywhere] = useState(false);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  const hostList = hosts.split(/[\s,]+/).map((h) => h.trim()).filter(Boolean);
  const ready = NAME.test(name) && (kind === "remote"
    ? url.trim().length > 0
    : /.@\d/.test(pkg.trim()) && entry.trim().length > 0 && (anywhere || hostList.length > 0));

  const add = async () => {
    const server: AddServer = kind === "remote"
      ? { name, url: url.trim(), secret, label: label.trim() }
      : { name, package: pkg.trim(), entry: entry.trim(), hosts: hostList, anywhere,
          label: label.trim() };
    setBusy(true);
    setProblem(null);
    const result = await control.addServer(server);
    setBusy(false);
    if (!result.ok) {
      setProblem(result.error ?? "Could not add it.");
      return;
    }
    setSecret("");
    toast(`${label.trim() || name}: its card is waiting above.`);
    onAdded();
  };

  return (
    <div className="ext-add">
      <Choices label="A server" value={kind} options={SERVER_KINDS} disabled={busy}
               onChange={setKind} />
      <Field label="Name" hint="Small letters, digits and _.">
        <input className="form-input" value={name} placeholder="notes"
               onChange={(e) => setName(e.target.value)} />
      </Field>
      <Field label="Label" hint="Optional.">
        <input className="form-input" value={label} placeholder="Notes"
               onChange={(e) => setLabel(e.target.value)} />
      </Field>
      {kind === "remote" ? (
        <>
          <Field label="Its address">
            <input className="form-input" value={url} placeholder="https://mcp.example/mcp"
                   onChange={(e) => setUrl(e.target.value)} />
          </Field>
          <Field label="Its secret" hint="Optional; sent over HTTPS only.">
            <input type="password" className="form-input" autoComplete="new-password"
                   value={secret} onChange={(e) => setSecret(e.target.value)} />
          </Field>
        </>
      ) : (
        <>
          <Field label="The package" hint="At an exact version at least 7 days old.">
            <input className="form-input" value={pkg} placeholder="some-server@1.2.3"
                   onChange={(e) => setPkg(e.target.value)} />
          </Field>
          <Field label="Its start script" hint="Within the package.">
            <input className="form-input" value={entry} placeholder="dist/index.js"
                   onChange={(e) => setEntry(e.target.value)} />
          </Field>
          <Field label="The hosts it may reach" hint="One per line.">
            <textarea rows={3} value={hosts} placeholder="api.service.example"
                      onChange={(e) => setHosts(e.target.value)} />
          </Field>
          <Check label="It may reach any public host"
                 note="Approving it then takes your passphrase." checked={anywhere}
                 onChange={setAnywhere} />
        </>
      )}
      {busy && <p className="form-hint">Listing its tools{kind === "package" ? ", after installing it" : ""}…</p>}
      <Problem text={problem} />
      <div className="form-actions">
        <button type="button" className="primary" disabled={busy || !ready}
                onClick={() => void add()}>Add</button>
        <button type="button" className="form-quiet" disabled={busy} onClick={onClose}>Cancel</button>
      </div>
    </div>
  );
}

/**
 * The page's first offer: a tool Steve hasn't got. The ask goes to the
 * conversation as the user's own message, since that is where a build is
 * asked about, followed, and said yes to; a form of its own here would be a
 * second way in with none of that. Self-upgrade was Steve's least visible
 * power: nothing on screen said it could be asked for.
 */
function AskForOne({ onAsk }: { onAsk: (text: string) => void }) {
  const [what, setWhat] = useState("");
  const ask = () => {
    const said = what.trim();
    if (!said) return;
    onAsk(`Build me a tool that ${said}`);
    setWhat("");
  };
  return (
    <Section title="Ask for one" why={
      "Ask Steve for a tool it doesn't have, and it builds one, tests it, and asks you "
      + "before turning it on. You follow the build in the conversation."}>
      <Field label="Describe a tool Steve doesn't have" hint="It starts with “Build me a tool that”.">
        <textarea rows={2} value={what} placeholder="tracks my plants and when each needs water"
                  onChange={(e) => setWhat(e.target.value)} />
      </Field>
      <div className="form-actions">
        <button type="button" className="primary" disabled={!what.trim()} onClick={ask}>
          Ask Steve
        </button>
      </div>
    </Section>
  );
}

/* ------------------------------------------------------- builds and reports */

const BUILD_WORDS: Record<Build["state"], string> = {
  needs_allow: "waiting for you",
  running: "being built",
  stopped: "stopped",
  failed: "not admitted",
  cannot: "cannot be a module",
  carded: "card ready",
  dismissed: "dismissed",
};

/** "about $0.80 and 12 minutes": a build's estimate, as the backend says one. */
function estimateWords(build: Build): string {
  const minutes = Math.max(1, Math.round(build.estimate_s / 60));
  return `about ${formatUSD(build.estimate_usd)} and ${minutes} minute${minutes === 1 ? "" : "s"}`;
}

/**
 * A build, with its estimate and what it has spent. One waiting past the
 * day's cap starts only on "Allow this build", which only the app offers:
 * a remote channel could be someone holding the user's phone.
 */
function BuildCard({ build, toast, onChanged }: {
  build: Build;
  toast: Say;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const waiting = build.state === "needs_allow" || build.state === "stopped";

  const act = async (allow: boolean) => {
    setBusy(true);
    setProblem(null);
    const result = allow ? await control.allowBuild(build.id) : await control.dismissBuild(build.id);
    setBusy(false);
    if (!result.ok) {
      setProblem(result.error ?? (allow ? "Could not start it." : "Could not dismiss it."));
      return;
    }
    if (allow) toast("Building it: Steve says in the conversation when its card is ready.");
    onChanged();
  };

  return (
    <li className={waiting ? "ext-card ext-asking" : "ext-card"} data-build={build.id}>
      <div className="ext-head">
        <h3 className="ext-title" title={build.request}>“{build.request}”</h3>
        <span className="form-list-meta">{BUILD_WORDS[build.state]}</span>
      </div>
      <p className="form-hint">
        {build.module ? `A new version of ${build.module}` : "A new module"}
        {build.came_from ? `, asked for in ${build.came_from}` : ""} · {when(fromSqlite(build.created_at))}
      </p>
      <p className="ext-line">
        Estimate: {estimateWords(build)}. Spent so far: {formatUSD(build.cost_usd)}.
      </p>
      {build.state === "needs_allow" && (
        <p className="ext-line">It would pass today's spending cap for building modules.</p>
      )}
      {build.said && <p className="ext-line">{build.said}</p>}
      {build.state === "carded" && <p className="form-hint">Its card is under "Waiting for your yes", unless you have answered it.</p>}
      <Problem text={problem} />
      {waiting && (
        <div className="form-actions">
          <button type="button" className="primary" disabled={busy} onClick={() => void act(true)}>
            {build.state === "stopped" ? "Allow it to go on" : "Allow this build"}
          </button>
          <button type="button" className="form-quiet" disabled={busy}
                  onClick={() => void act(false)}>Dismiss</button>
        </div>
      )}
    </li>
  );
}

/** A slot Steve lacks, or a problem in Steve itself: a redacted text the user may pass on. */
function ReportCard({ report, toast, onChanged }: {
  report: Report;
  toast: Say;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  const dismiss = async () => {
    setBusy(true);
    setProblem(null);
    const result = await control.dismissReport(report.id);
    setBusy(false);
    if (!result.ok) {
      setProblem(result.error ?? "Could not dismiss it.");
      return;
    }
    onChanged();
  };

  return (
    <li className="ext-card" data-report={report.id}>
      <div className="ext-head">
        <h3 className="ext-title">
          {report.kind === "slot" ? "A slot Steve lacks" : "A problem in Steve"}
        </h3>
        <span className="form-list-meta">{when(fromSqlite(report.created_at))}</span>
      </div>
      <p className="form-hint">From the request “{report.request}”</p>
      <pre className="ext-text">{report.text}</pre>
      <Problem text={problem} />
      <div className="form-actions">
        <button type="button" disabled={busy} onClick={() => void navigator.clipboard.writeText(report.text)
          .then(() => toast("Copied."), () => toast("Could not copy it.", false))}>
          Copy
        </button>
        <button type="button" className="form-quiet" disabled={busy}
                onClick={() => void dismiss()}>Dismiss</button>
      </div>
    </li>
  );
}
