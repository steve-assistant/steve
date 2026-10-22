/* Channels (back-pages.md, Connections): how Steve reaches the user away
   from the desk, Signal or Telegram, and the mailbox the user can forward
   mail to. They were on Home, among the day's counts. */

import { control } from "./control";
import { Saved, Section } from "./Form";
import { usePolled, useSaid } from "./pages";
import { SignalPanel } from "./Signal";
import { TelegramPanel } from "./Telegram";

export function Channels({ onScreen }: { onScreen: boolean }) {
  const { data } = usePolled((signal) => control.overview(signal), { everyMs: 30000, onScreen });
  const [said, say] = useSaid();
  const mailbox = data?.mailbox;

  return (
    <div className="panel channels">
      <p className="page-said"><Saved said={said} /></p>
      <p className="form-hint">
        A channel lets you message Steve, and answer its cards, from your phone. One is
        enough. Signal takes its app on your phone and a QR code scanned from here; Telegram
        takes its app, a bot you make in it, and a code you send that bot.
      </p>
      <SignalPanel />
      <TelegramPanel />
      {mailbox?.address && (
        <Section title="Steve’s mailbox" why={
          "Forward it a mail with a note (“deal with this”), or copy it into a thread. Only "
          + "mail from your own addresses counts as asking; anyone else’s is ignored, or read "
          + "as part of a thread you brought it into."}>
          <p>
            <code className="mailbox-address">{mailbox.address}</code>{" "}
            <button onClick={() => void navigator.clipboard.writeText(mailbox.address)
              .then(() => say("Copied"), () => say("Couldn’t copy it.", false))}>
              Copy
            </button>
          </p>
          <p className="form-hint">
            {mailbox.enabled
              ? `Today: ${mailbox.today.instruction} from you, ${mailbox.today.thread} in your threads, ${mailbox.today.ignored} ignored.`
              : "Not switched on yet."}
          </p>
        </Section>
      )}
    </div>
  );
}
