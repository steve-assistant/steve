/* Settings (back-pages.md, Steve): how the app looks and reads (Reading,
   foundations.md, Accessibility), the language Steve writes in and the
   region it reads, the app itself (starting with Windows, updates), the
   hotkeys, the voice, listening notes, and the passphrase and the
   authenticator. Each saves as it is changed.

   Reading had no place in the app: its choices were kept (appSettings.ts)
   but nothing set them. Language and region were knobs no page showed. */

import { useState } from "react";
import {
  HOTKEY_CHOICES,
  hotkeyLabel,
  isTauri,
  type VoiceWarm,
} from "./api";
import { type AppSettings } from "./appSettings";
import { Approvers } from "./Approvers";
import { control, type Knob } from "./control";
import { type UpdateOffer } from "./desktop";
import { Check, Choices, Field, KnobField, Saved, Section } from "./Form";
import { saveKnob, usePolled, type Said } from "./pages";

type AppChange = Partial<Record<keyof AppSettings, string | null>>;

const THEMES = [["auto", "As Windows"], ["paper", "Paper"], ["ink", "Ink"],
                ["ink-in-quiet-hours", "Ink in quiet hours"]] as const;
const FACES = [["newsreader", "Newsreader"], ["atkinson", "Atkinson Hyperlegible"]] as const;
const HANDS = [["hand", "Handwriting"], ["plain", "Plain"]] as const;
const SIZES = [["normal", "Normal"], ["larger", "Larger"]] as const;

const LANGUAGE_KNOBS = ["LANGUAGE", "REGION", "UNITS"];

function useSaved(): [Said | null, (error: string | null, ok?: string) => void] {
  const [said, setSaid] = useState<Said | null>(null);
  return [said, (error, ok = "Saved") =>
    setSaid((was) => ({ text: error ?? ok, ok: error === null, n: (was?.n ?? 0) + 1 }))];
}

/** One of the app's own choices, as words in a row. */
function AppChoice<K extends "theme" | "face" | "hand" | "size">({
  name, label, options, app, onApp,
}: {
  name: K;
  label: string;
  options: readonly (readonly [AppSettings[K], string])[];
  app: AppSettings;
  onApp: (changes: AppChange) => Promise<string | null>;
}) {
  const [said, saved] = useSaved();
  return (
    <div className="form-knob" data-setting={name}>
      <Choices label={label} value={app[name]} options={options}
               onChange={async (value) => saved(await onApp({ [name]: value }))} />
      <div className="form-knob-foot"><Saved said={said} /></div>
    </div>
  );
}

/** One of the app's on-or-off choices, as a box to tick. */
function AppSwitch({ name, label, note, app, onApp }: {
  name: "desk_card" | "desk_card_text" | "desk_card_on_top";
  label: string;
  note?: string;
  app: AppSettings;
  onApp: (changes: AppChange) => Promise<string | null>;
}) {
  const [said, saved] = useSaved();
  return (
    <div className="form-knob" data-setting={name}>
      <Check label={label} note={note} checked={app[name] === "on"}
             onChange={async (on) => saved(await onApp({ [name]: on ? "on" : "off" }))} />
      <div className="form-knob-foot"><Saved said={said} /></div>
    </div>
  );
}

function HotkeyProblem({ problem }: { problem?: string | null }) {
  if (!problem) return null;
  const taken = /already registered|in use|HotKey/i.test(problem);
  return (
    <span className="form-error">
      {taken
        ? "Another app holds this hotkey. Pick another, or free it in that app."
        : `This hotkey isn't working: ${problem}`}
    </span>
  );
}

export interface SettingsProps {
  app: AppSettings | null;
  onApp: (changes: AppChange) => Promise<string | null>;
  onScreen: boolean;
  voice: VoiceWarm | null;
  /** The one hotkey: the one bound, why it isn't working, and why it is
   *  not the default when the user chose none. */
  hotkey: { bound: string | null; problem: string | null; fellBack: string | null };
  startsAtLogin: boolean | null;
  onStartsAtLogin: (on: boolean) => Promise<string | null>;
  /** A newer release found, waiting for Install. */
  update: UpdateOffer | null;
  updateBusy: boolean;
  /** Look now: what was found, said in a line. */
  onCheckUpdate: () => Promise<string>;
  /** Install: what went wrong, or null as the installer starts. */
  onUpdate: () => Promise<string | null>;
  tapping: boolean;
  onTap: (on: boolean) => Promise<string | null>;
  /** The voice add-on that call notes need is installed. */
  addOn: boolean;
}

export function Settings({
  app, onApp, onScreen, voice, hotkey, startsAtLogin, onStartsAtLogin, update,
  updateBusy, onCheckUpdate, onUpdate, tapping, onTap, addOn,
}: SettingsProps) {
  const autonomy = usePolled((signal) => control.autonomy(signal), { everyMs: 60000, onScreen });
  const knobs = LANGUAGE_KNOBS.map((name) => autonomy.data?.knobs.find((k) => k.name === name))
    .filter((k): k is Knob => Boolean(k));
  const signOut = autonomy.data?.knobs.find((k) => k.name === "SIGN_OUT_IDLE");
  const saveKnobHere = async (name: string, value: number | string | null) => {
    const error = await saveKnob(name, value);
    await autonomy.again();
    return error;
  };
  const [loginSaid, loginSaved] = useSaved();
  const [updateSaid, updateSaved] = useSaved();
  const [looked, setLooked] = useState<Said | null>(null);
  const [keysSaid, keysSaved] = useSaved();
  const [tapSaid, tapSaved] = useSaved();

  return (
    <div className="panel form-page settings">
      {app && (
        <Section title="Reading" why={
          "How the pages look and read. The handwriting is for the day’s headings and the "
          + "notes; plain sets them in the text face."}>
          <AppChoice name="theme" label="Theme" options={THEMES} app={app} onApp={onApp} />
          <AppChoice name="face" label="Text face" options={FACES} app={app} onApp={onApp} />
          <AppChoice name="hand" label="Headings and notes" options={HANDS} app={app} onApp={onApp} />
          <AppChoice name="size" label="Size" options={SIZES} app={app} onApp={onApp} />
        </Section>
      )}

      <Section title="Language and region" why={
        "Windows’ own until you choose. Steve writes in the language, and reads the news and "
        + "searches for the region."}>
        {knobs.map((knob) => <KnobField key={knob.name} knob={knob} onSave={saveKnobHere} />)}
      </Section>

      <Section title="This app" why={
        "Closing the window keeps Steve in the tray; Quit is in the menu and on the tray "
        + "icon’s."}>
        {startsAtLogin !== null && (
          <div className="form-knob" data-setting="starts_at_login">
            <Check label="Start with Windows, in the tray" checked={startsAtLogin}
                   note="Steve’s backend starts with the app."
                   onChange={async (on) => loginSaved(await onStartsAtLogin(on))} />
            <div className="form-knob-foot"><Saved said={loginSaid} /></div>
          </div>
        )}
        {app && (
          <div className="form-knob" data-setting="update_checks">
            <Check label="Look for a newer Steve at start and once a day"
                   checked={app.update_checks === "on"}
                   note={"Each look tells the server that holds Steve’s releases this computer’s "
                         + "address. A newer version installs only when you press Install, and "
                         + "its signature is checked first."}
                   onChange={async (on) => updateSaved(await onApp({ update_checks: on ? "on" : "off" }))} />
            <div className="form-knob-foot">
              <button type="button" className="form-quiet" disabled={updateBusy}
                      onClick={async () => {
                        const found = await onCheckUpdate();
                        setLooked((was) => ({ text: found, ok: true, n: (was?.n ?? 0) + 1 }));
                      }}>
                {updateBusy ? "Looking…" : "Look now"}
              </button>
              <Saved said={looked ?? updateSaid} />
            </div>
          </div>
        )}
        {update && !update.repair && (
          <div className="settings-update" data-setting="update">
            <p>
              Steve {update.version} is ready to install.{" "}
              <span className="form-hint">
                {update.notes ? `${update.notes} ` : ""}
                The app stops the backend, installs it and starts again; your data is kept.
              </span>
            </p>
            <button type="button" className="form-main" disabled={updateBusy}
                    onClick={async () => {
                      const error = await onUpdate();
                      if (error) updateSaved(error);
                    }}>
              {updateBusy ? "Installing…" : "Install"}
            </button>
          </div>
        )}
      </Section>

      {app && (
        <Section title="The hotkey" why={
          isTauri()
            ? "Tap it to open the quick bar, over whatever you are doing; hold it to talk, and "
              + "let go to send. It works while another window has focus. Fn can’t be one: "
              + "the keyboard keeps it."
            : "The hotkey needs the desktop app; in a browser, use the mic button."}>
          <Field label="The hotkey" hint={hotkey.fellBack}>
            <select className="form-select" value={app.hotkey ?? hotkey.bound ?? ""}
                    disabled={!isTauri()}
                    onChange={async (e) => keysSaved(await onApp({ hotkey: e.target.value }))}>
              {HOTKEY_CHOICES.map((choice) => (
                <option key={choice} value={choice}>{hotkeyLabel(choice)}</option>
              ))}
            </select>
          </Field>
          <HotkeyProblem problem={hotkey.problem} />
          <Saved said={keysSaid} />
        </Section>
      )}

      {app && (
        <Section title="The desk card" why={
          "Steve’s mark on your desktop. Point at it to see what Steve is doing, what’s next "
          + "and what needs you; click it for today, hold it to talk, right-click it to hide "
          + "it. It stays out of the way of full-screen apps and presentations."}>
          <AppSwitch name="desk_card" label="Show the desk card" app={app} onApp={onApp} />
          <AppSwitch name="desk_card_text" label="Show what Steve is doing on the desk card"
                     note={"Anyone passing your screen can read it. Off, the card shows only "
                           + "the time of your next event and how many notes wait."}
                     app={app} onApp={onApp} />
          <AppSwitch name="desk_card_on_top" label="Keep it in front of other windows"
                     app={app} onApp={onApp} />
        </Section>
      )}

      <Section title="Voice" why={
        voice?.state === "ready"
          ? `Speech is ready: heard on ${voice.stt?.device}, spoken on ${voice.tts?.device}.`
          : voice?.state === "cold" ? "Speech loads when you first talk."
          : voice?.state === "warming" ? "Speech is loading."
          : `Speech isn’t available${voice?.error ? `: ${voice.error}` : ""}.`}>
        <div className="form-knob" data-setting="tap">
          {/* Without the add-on it is said, not offered: pressed, it only
              brought a refusal. */}
          <Check label="Take notes on what the laptop plays" checked={tapping}
                 disabled={!addOn && !tapping}
                 note={addOn
                   ? "A call or a video: Steve listens to the laptop’s sound, not the "
                     + "microphone, and writes up notes when you stop. Also at the "
                     + "sidebar’s foot and on the tray icon’s menu."
                   : "Needs the voice add-on, which isn’t installed."}
                 onChange={async (on) => tapSaved(await onTap(on))} />
          <div className="form-knob-foot"><Saved said={tapSaid} /></div>
        </div>
      </Section>

      <Section title="Signing in and saying yes" why={
        "Signing in to Steve, and a yes to spending, take more than a tap: your passphrase or "
        + "Windows Hello here, and the code from your authenticator app by message or voice. "
        + "Only this app changes these."}>
        {signOut && <KnobField knob={signOut} onSave={saveKnobHere} />}
        <Approvers />
      </Section>
    </div>
  );
}
