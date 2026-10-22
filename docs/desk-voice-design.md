# The desk's voice, rebuilt: a design

Status: **built, 9 October 2026** (Phase 2), with the maintainer's decisions: D1 = C
(the back-and-forth is the local follow-up window; GPT-Live opens only by
name, "let's talk live"; A stays behind `LIVE_OPENED_BY` in
`voice/engine.py`), a soft tick on every heard wake, and D2 built but off
(`BARGE_IN_WITHOUT_WAKE` in `core/desk.py`). One departure from the design
below: a reply cut by a wake that comes to nothing is **not** resumed -- on
9 October the "hey Steve, stop" that worked was exactly such a capture
(Whisper missed the phrase in the mix), and resuming would have spoken over
the stop the user meant. Where this document and the code differ, `docs/flows.md`
flow 16 describes what runs. Written first as the Phase 1 design, after two
sound tests that day. The maintainer: "it's acting up and being
weird, saying things multiple times, not responding for several minutes,
missing messages ... Looks like the voice tool might need a refactor
entirely/ground-up rework though instead of just patching."

New modules this design proposes are named in **bold** and without a path in
backticks, because they did not exist yet. The phone, whose call code the
desk first borrowed, has since gone from Steve with the server it ran on;
what the desk kept of it is `voice/live.py`, and the phone's own code is
named here without a path.

## 0. In one page

Nearly every failure of 9 October happened at one seam: the hand-over between
the local voice (Whisper, the assistant, Kokoro) and GPT-Live. The local path
worked both times: "are you there?" was answered in two seconds, and a
follow-up with no wake word worked twice. The failures were these:

- **Two voices on one turn.** The assistant both spoke its reply and asked for
  a live conversation with the same words as its opening. GPT-Live was then
  told to say them again.
- **A call that could not close.** GPT-Live streams audio frames all the time,
  silence included. The desk took every frame as speech, so both of its quiet
  rules, and the check for when the user was talking, were held for the whole call.
- **A request dropped on the floor.** The phone's opening watchdog
  (NEVER_SPOKE) closed a call nine seconds in, just as the user
  finished their first sentence. Nothing hands over what was said in a call that
  ends.
- **No way out.** Inside a live conversation, "hey Steve" only paused its voice
  for a second.

The causes are structural:

- the turn's state is spread over six module globals and context variables;
- "speaking" is guessed from what sits in a queue;
- the desk's live mode is the phone's call object with a few parts swapped.

The design replaces the turn-handling core and keeps the parts that work: the
wake detector, Whisper, Kokoro, the echo canceller, notes and the frozen rules.
In its place:

1. **One state machine** with one owner per turn. It is a pure transition
   function, testable without sound or time, inside a thin asyncio shell.
2. **A speaker floor.** Only the turn that owns the speakers can make sound, so
   two voices at once cannot happen by construction. It measures what was
   actually audible, from the energy of the samples played.
3. **"Hey Steve" always reaches the local engine,** in every state, a live
   conversation included.
4. **A live conversation never repeats the local voice.** What the local voice
   said goes into GPT-Live as history; the protocol supports this.
5. **Every way a live conversation ends hands over** whatever the user said that
   was not answered.
6. **At the desk, the words handed to the assistant come from local Whisper,**
   not from GPT-Live's transcript of the room. The original plan promised this,
   and the implementation never did it.

The key decision (section 3) is what carries the back-and-forth.

- **Recommended:** the local conversation carries it by default. GPT-Live opens
  only when the user asks for it by name ("let's talk live").
- This changes the maintainer's stated choice that GPT-Live "takes over automatically", and
  says so plainly.
- The state machine supports either answer. The choice is one constant, not a
  rebuild.

## 1. How the desk worked, as implemented at 887cf59

This section describes the code before the rebuild; the names in it (`desk._turn`,
`runtime.desk_talk` and the rest) are that commit's, and most are gone.

### 1.1 Who owns the microphone and the speakers

| Resource | Opened or written by | Stopped or cleared by |
|---|---|---|
| Microphone (WASAPI) | `voice/listen.py` `Ears._mic` thread, then a queue, then the `Ears._process` thread, then `EchoCanceller.near` (`voice/echo.py`), then `Listener.push` | `Ears.check` (locked, no loopback), `Ears.stop` |
| Push-to-talk mic (PortAudio) | `VoiceSession.recorder` (`voice/pipeline.py`), from the app's websocket `voice_start` | `voice_stop` |
| Loopback | `Ears._loopback` thread into `Loopback.push`; a second one in `Tap._run` for call notes | the same |
| Speakers (one `Player`, `voice/audio.py`) | `VoiceSession._speak`, `_line` (preambles), `_stall`, `say`; `desk._say`, `_chime`, the notes' spoken lines; `DeskAudio.play` (`voice/desk_call.py`) for **every** GPT-Live frame | `VoiceSession.interrupt` and `start_listening`, `DeskAudio.stop_playing`, `DeskAudio.speaking` (clears a "frozen" queue), `Player._ensure_stream` (a reopen clears the queue) |
| GPT-Live's input | `Listener.push` in the `conversation` state, then `DeskAudio.feed`, then `DeskCall._from_phone` | `DeskAudio.end`, the call's close |
| The turn | `desk.handle` takes `_turn_lock`, held from transcription to the end of the spoken reply **and of any live conversation that follows** (minutes) | |

The state of one turn is spread across:

- `desk._state`, a dict with 14 keys: `call`, `echo`, `closing`,
  `notes_in_call`, `note_heard`, `watch`, `tap` and more;
- `_turn_lock` and `_segment_lock`;
- `runtime.desk_talk` and `runtime.desk_notes`, two module globals;
- `runtime.desk_turn` and `runtime.desk_cut_in`, two context
  variables;
- `VoiceSession._cut`, `_cut_at`, `_listening`, `_said` and `_unplayed`;
- `Listener.state`, `base`, `_woke`, `_barge`, `_follow_until`, `speech_at` and
  `_stream`;
- `DeskAudio._played_until` and `_queued_last`;
- in `DeskCall` and the phone's `_Call`: `_active`, `_signs_at`,
  `_drop_until`, `closing`, `heard`, `approval_queued`, `watchdog` and `work`.

Each was added by an audit to close one hole. Six threads touch them:

- the mic, the processing thread, the loopback, the lock watch and the player
  callback;
- and the event loop.

### 1.2 The state machine that actually runs

`Listener.state` (`voice/listen.py`) is changed from the ear's thread (`push`)
and from the event loop (`listen`, `follow_up`, `converse`, `transcribe`,
`forget`):

```
listening --wake on the mic, not on the loopback--> hearing --end of turn--> busy
busy --handler: follow_up()--> follow_up --speech--> hearing (woke=False) --> busy
busy --handler: listen()-----> listening (or transcribing, with a note open)
busy --wake--> hearing (barge_in): VoiceSession.interrupt() stops the reply
busy --handler: converse(feed)--> conversation: every block goes to GPT-Live
conversation --wake--> DeskCall.barge_in(): its voice is dropped for 1 s, nothing else
conversation --handler: converse(None)--> listening
transcribing --speech--> segment --pause--> transcribing   (a note's stretch)
```

The handler's side, `desk._turn`, runs these steps in order:

1. Whisper, then `core/desk.wake_phrase`.
2. "Stop listening" and "never mind".
3. A cut-in stop (`desk.cut_in_stop`).
4. Notes.
5. Approvals.
6. The `desk._TALK` words.
7. Cut-in noise.
8. `VoiceSession.respond`. It speaks the reply as it is written and waits for
   the speakers to drain.
9. `runtime.desk_notes`, then `runtime.desk_talk`, then
   `desk._converse`. The live conversation runs inside the turn
   lock.
10. `follow_up()`.

Nothing outside this order knows what state the desk is in. `/voice/status`
reports `speaking` as `Player.is_playing`, which means only that the queue is
not empty.

### 1.3 Where each failure comes from

**Test 1 (08:03-08:11, before the day's fixes).** Log
`worker-20261008-205102-...`; messages 2639-2650.

| What the user heard | The evidence | The cause |
|---|---|---|
| "Hey Steve, stop" over an 82-second answer became a live conversation | Message 2640, "I'll talk."; the turn line says `outcome=live_by_assistant` | A cut-in's capture mixes their phrase with the desk's own voice in the pre-roll. `desk._turn` sent it to the model, and the model called `agent/toolset/desk.talk_it_through`. Patched since (`desk.cut_in_stop`, `cut_in_noise`, `runtime.desk_cut_in`). Underneath, "stop talking" and "here is a new request" are still one capture. |
| The opening said twice | The local reply (message 2643) was spoken (`spoken_s=8.9`); the live transcript begins with the same sentence | See "two voices", below |
| "Hey Steve, stop" inside the call did not stop it | The live transcript has "Casey stop", then "Hey, Steve, let's talk through..."; `barge_ins=2` | In the `conversation` state, a wake calls only `DeskCall.barge_in`. Its words then reached the assistant as GPT-Live heard them: "Casey stop Hey, Steve, let's talk through what I have coming up this week" (message 2644) |
| The call never closed: 326 s, until hands-free was switched off; `speaking: true` throughout | `a live conversation ended: ... reason=hands-free off seconds=326` | See "silent frames", below |

**Test 2 (15:25-15:33, with the day's fixes).** Log
`worker-20261009-085624-...`; messages 2653-2668.

| What the user heard | The evidence | The cause |
|---|---|---|
| "Hey Steve, stop" over a long answer worked | `outcome=false_wake barge_in=True laptop_s=2.0` | Whisper missed the phrase in the mixed pre-roll, so the capture was dropped, and silence was right by luck |
| "Talk through my week": the local voice spoke the opening, then GPT-Live opened | 2657 `talk_it_through`; 2658, a 6.4 s local reply | Two voices: the model was given a tool whose instructions say "`opening` is your first line in it. Keep your reply to this turn to one short sentence". The desk speaks that reply, then `voice/desk_call.session_start` adds "Open by saying this", and `talk` sends "Say now, in close to these words: {opening}". The design repeats the opening on purpose. |
| The user answered ("Let's go day by day ... look at my email too"), and it was lost | Row 2659 has their words and no word of GPT-Live's. The log has `gpt-live said nothing in 9s; handing the call over`, then `handed_over=0`. The clock line has `words=8s audio=9s` | `DeskCall` inherits the phone's `_Call`, whose opening watchdog nudges at 3 s and 6 s and gives up at 9 s (OPENING_GIVE_UP_S) unless GPT-Live has said a word. That was written for outbound calls that open in silence. At the desk the user spoke from about the first second to the ninth. GPT-Live rightly held its turn while the user talked, and the watchdog ended the call as the user finished. Then `_Call.run` cancelled the outstanding work, and nothing reads what was still unhanded (`Heard.since_handoff`) when a call ends. Their words survive only in a `live_conversation` row, which is never replayed, so at 2664 the assistant apologised without knowing what the user had asked. |
| "Hey Steve" twice in the next two minutes: nothing | Neither appears in the log at all | **Unexplained, and the code is why.** Every wake the detector fires leaves a line (a turn, a false wake, or "a wake the laptop itself played"), so it did not fire, or the ear was deaf. Four paths would leave no trace: (1) `Ears.check` closing the mic for "no loopback"; (2) an exception in `Ears._mic`, which sleeps a second; (3) a score under the head's 0.9, since near misses are not logged; (4) the soundcard "data discontinuity" warnings, 25 in that day's two logs. The design makes each of them visible (section 2.11). |
| "Yeah, go through it": the local voice gave a 15 s summary, then GPT-Live said it again | 2666 `talk_it_through`; 2667, the local reply; row 2668 holds the same summary as GPT-Live's only words | Two voices, the same as above. This time the "opening" was the whole answer. |
| Then about 2.5 minutes of nothing, until the switch | `held_open_by speaking=150s work=0s him=0s audio=150s words=0s`; `barge_ins=0` | See "silent frames", below. The user could not end it: `words=0s` and `barge_ins=0` mean GPT-Live heard no word and the wake detector no "hey Steve". Either the user was silent, waiting, or the mic path was not hearing them; the record cannot tell which. |
| Alt+Home may not have switched it off | No `hands-free listening` line before the main session's at 15:32:28; every `/voice/listen` call logs one | Nothing reached the worker. The app toggles from its own cached state (`ui/src/App.tsx`, `toggleHandsFree`: `!handsFreeRef.current?.on`). Whatever happened in the app, two defects are real: a stale cache sends the wrong direction, and `agent/worker/routes.voice_listen` calls `desk.switch`, which calls `Ears.stop`, which joins four threads for up to 3 s each **on the event loop**. That breaks `docs/protocols.md` §2: up to 12 s of a frozen worker. `desk.start` loads ONNX models on the loop too. |

**Silent frames: GPT-Live's output is a continuous stream.** I tested your
hypothesis against the code, the provider's spec and the stored evidence.

- **The code.** `DeskCall._to_phone` hands every `session.output_audio.delta` to
  `DeskAudio.play`. That sets `_played_until` to now plus the queued seconds,
  and marks that audio arrived. So for as long as frames arrive, three things
  hold:
  - `DeskAudio.talking` and `speaking` are true. The first holds the second
    quiet rule (`SILENT_S`); the second holds the first (`IDLE_S`).
  - `DeskAudio.him` returns 0, since any speech of theirs falls "inside its own
    voice". So `him=0s` says nothing about whether the user spoke.
  - The player's queue is never empty, so `/voice/status` says `speaking: true`.
- **The stored evidence.**
  - Test 2's first call logged `audio=9s` in a 12-second call. Its transcript
    row has no word of GPT-Live's, and the opening watchdog fired, which it
    does only when no output transcript has arrived. That is nine seconds of
    audio frames with no speech.
  - The second call logged `audio=150s` in 151 s. Its transcript holds one
    utterance of about 43 words, some 15 s.
  - Test 1's call stayed open about five minutes after its last words the same
    way.
- **The protocol.** The OpenAI SDK in the venv (3.16.2) carries the Live API's
  types, generated from its spec (`openai/types/live/` in site-packages).
  - Output audio deltas on the primary websocket have no timestamps: "Decode
    and play primary WebSocket chunks in delivery order".
  - On the sideband, "Dropped output frames leave gaps between reflected
    ranges". A frame timeline with gaps only where frames were dropped is a
    continuous stream.
  - The phone's design notes and its call module's docstring both record that
    it "paces its output in real time ... in 100 ms chunks".
  - The SDK's own transcript grouper ends an assistant turn after 2,000 ms with
    no assistant transcript (`assistant_silence_ms`), not by audio. So does the
    phone's simulator ("Its transcript is paced with its speech").
- **What cannot be verified:** whether the frames are digital zeros or
  low-level noise. No audio is kept, by design.
- **The one alternative:** two minutes of real speech with no transcript. The user
  would have heard it. The user heard silence.
- **Verdict:** confirmed as far as the record allows. The design does not
  depend on it: it measures every frame's energy, and uses the transcript's
  timestamps as a second witness (section 2.5).

On the phone the same stream is harmless. Frames go straight to the carrier,
and nothing there infers speech from them.

**Two more things the record shows:**

- **The assistant was handed GPT-Live's transcript of the room.** `_Call.handoff`
  sends `Heard.take()`, which is GPT-Live's `session.input_transcript`.
  - The original plan's guard 6 said "at the desk it uses the local Whisper
    transcript of the mic, not GPT-Live's own". The gate test pins only the
    weaker rule: its input transcript, never its output
    (`test_a_desk_conversation_hands_the_laptop_user_words_never_its_own`).
  - "Casey stop" reached the assistant that way.
  - So, perhaps, did "Today, you have two" (test 1). That could be their own words
    or its voice coming back; the record cannot tell which.
- **Silent drops.** Three places drop audio when full, and none logs it:
  - the ear's queue (`queue.Full` is suppressed in `Ears._mic`);
  - `DeskAudio.mic` (`QueueFull` is suppressed);
  - the follow-up path, which logs only its count.

### 1.4 Checking the structural problems in the brief

| Claim | Verdict |
|---|---|
| Two brains on one turn | **Confirmed.** `talk_it_through` makes one model turn produce a spoken reply and a second voice's opening. GPT-Live is then a second conversational model with its own memory of the call, paraphrasing the assistant's answers ("tell them in a sentence or two in your own words"). |
| The live mode inherits the phone bridge | **Confirmed, with one change.** The audio format was already overridden (24 kHz PCM). What it inherits: <ul><li>the opening watchdog;</li><li>"every payload is speech";</li><li>"its input transcript is their words" (on the phone it is the only transcript there is);</li><li>`_Call.run`'s ending, which cancels outstanding work and hands nothing over.</li></ul> |
| State spread across globals and context variables | **Confirmed** (section 1.1): six places, changed from six threads. |
| "Who is speaking" is inferred | **Confirmed.** `Player.is_playing` is "the queue is not empty". `DeskAudio.speaking` is "frames arrived, or the queue moved". Nothing measures what was audible. |
| Ending a call does not hand over | **Confirmed.** Neither `_Call.close`, `_Call.run`, nor `desk._converse` reads the unhanded words. |
| About 6,100 lines | **Confirmed:** `voice/*.py` 4,201 (697 of them `voice/wake_train.py`), `desk.py` 1,141, `agent/toolset/desk.py` 90, `core/desk.py` 135, the phone's call module 539. |

## 2. The new architecture

### 2.1 Principles

1. **One state machine, one place.** The desk's state is one value, changed
   only by one function, `transition(state, event) -> (state, actions)`, on the
   event loop. Threads only report events (a wake, a capture, a frame played, a
   GPT-Live event); they never change the state.
2. **One owner per turn, and the speakers obey it.** Every sound goes through a
   floor that knows the current owner. A sound from anyone else is dropped and
   logged.
3. **Measured, not inferred.**
   - "It is speaking" means samples above a loudness floor came out of the
     player, plus the echo tail.
   - "The user is speaking" means the microphone's voice detector after the echo
     canceller, outside its own audible voice.
   - "The laptop is speaking" means the loopback's voice detector.
4. **The wake phrase is a control in every state.**
5. **Nothing the user says is dropped in silence.** Every capture gets an outcome
   (answered, handed over, or dropped for a named reason) and one log line.
6. **No blocking on the loop.** Opening or closing devices and loading models
   run in threads (`docs/protocols.md` §2).
7. **The privacy boundary stays in the ear.** The ear hands on audio only in a
   mode the engine set after a counted wake, a follow-up window, a note the user
   started, or a live conversation the user asked for. That is where
   `tests/gate/test_desk.py` pins it.

### 2.2 The pieces

| Piece | Where | What it is |
|---|---|---|
| **Engine** | new, **voice/engine.py** | The desk's state machine: a pure `transition` over frozen dataclasses, and an asyncio shell that runs its actions and feeds back their results as events. Replaces the turn logic in `desk.py` and the states in `Listener`. |
| **Floor** | new, **voice/floor.py** | Who may make sound, and what was audible: wraps the one `Player`, tags every chunk with its owner, and reads the energy of what was really played. |
| **Ear** | `voice/listen.py`, reworked | `Ears`, `Loopback`, `Vad`, `Segmenter` and `Tap` are kept. `Listener` is replaced by an ear that runs the canceller, the wake detector and the voice detector on the ear's thread and emits events. What it may gather is a mode set by the engine, swapped atomically, never a state shared across threads. |
| **Local turn** | `voice/pipeline.py`, slimmed | The reply spoken as it is written (`_Reply`, `voice/speaking.py`): it plays through the floor under its turn, so a cut turn cannot sound. `VoiceSession` keeps the models and the push-to-talk recorder. |
| **Live client** | `voice/desk_call.py`, rewritten | The desk's own GPT-Live session. It no longer subclasses the phone's `_Call`; it shares only what is truly the same (section 2.10). |
| **Glue** | `desk.py`, slimmed | The switch, `/voice/status`, building the engine from the real parts, the adapter to `agent/worker/answering.voice_reply`, notes, live minutes and the transcript row, the tap. |
| **Rules** | `core/desk.py`, frozen | Unchanged, plus at most one new rule (section 5.2). |
| Kept as they are | `voice/wake.py`, `voice/wake_train.py`, `voice/stt.py`, `voice/tts.py`, `voice/tts_server.py`, `voice/speakable.py`, `voice/turns.py`, `voice/echo.py`, `voice/speaking.py`, `agent/notes.py`, the phone's call module | No change to the phone, so no deploy of its server. |

### 2.3 States and events

The engine's state is one immutable value:

- **`mode`**, exactly one of:
  - `off`;
  - `paused(reason)`: locked, no loopback, no microphone, warming or a swap;
  - `idle`;
  - `capture(kind)`: a wake, a follow-up, a barge-in or push-to-talk;
  - `transcribe(capture)`;
  - `local(turn)`: `thinking`, then `speaking`;
  - `follow(until)`;
  - `live(call)`: `connecting`, then `open`, then `closing`.
- **`note`**: none, a dictation, the room, or a call. It is orthogonal: it
  decides what the ear hands on in `idle` and `follow`.
- **`floor`**: none, a turn, a call or a chime. It belongs to `mode`; it is not
  separate state.
- **Counters** for `/voice/status`.

The events:

- **From the ear:** `Wake(at, score, laptop_too)`, `NearWake(score)` (logged
  only), `HisSpeech(start|end, at)` (reported only in modes that ask for it),
  `Captured(id, heard)`, `Segment(heard)`, `Mic(open|closed, reason)`.
- **From the floor:** `Audible(owner, start|end, at)`.
- **From a local turn:** `AgentText`, `AgentTool`, `AgentRestart`,
  `AgentDone(turn, reply, asks)`, `AgentFailed`.
- **From a live call:** `LiveReady`, `LiveSpeech(start_ms, end_ms)` (its output
  transcript's timestamps), `LiveDelegation(id)`, `LiveAnswered(id)`,
  `LiveClosed(reason)`.
- **Control:** `Switch(on|off|toggle, by)`, `Locked`, `Unlocked`,
  `Swap`, `HotkeyDown`, `HotkeyUp(recording)`.
- **Time:** `Tick(now)`. Every timer is a deadline in the state, compared on a
  tick, so tests drive time by hand.

The main transitions:

| From | Event | To | Actions |
|---|---|---|---|
| off | `Switch(on)` | paused(opening) | open the ear in a thread |
| paused | `Mic(open)` | idle | set the ear's mode to "wake only" |
| any but off | `Locked`, or `Mic(closed)` | paused(reason) | end a live call (hand-over to the thread, section 2.9); stop the voice; drop a capture in flight |
| any | `Switch(off)` | off | the same, then close the ear in a thread; a falling two-note chime |
| idle, follow | `Wake`, the user's | capture(wake) | a soft 60 ms tick, so the user knows they were heard; the ear gathers |
| idle, follow | `Wake`, the laptop's too | unchanged | log |
| capture | `Captured` | transcribe | Whisper, in a thread |
| transcribe | not the phrase / the phrase alone / "stop listening" / "never mind" / a cut-in stop or cut-in noise / notes / an approval while the laptop talks / live asked by name | idle / follow (with a chime) / off / idle / idle / idle with a note / follow / live(connecting) | as today, but each is one row of one table |
| transcribe | anything else | local(thinking) | ask the assistant, with the turn as the floor's owner |
| local | `AgentText` | local(speaking) | play each sentence **through the floor, under this turn** |
| local | `Wake`, the user's | capture(barge) | the floor cuts the turn (section 2.6); the agent's run finishes unheard; `cut_short` records what was heard |
| local | `AgentDone`, and the floor has drained | follow | the assistant's `take_notes` and live asks are read from the turn object (section 2.4) |
| local | thinking longer than 10 s / 45 s | unchanged | "Still on it." / "This is taking a while; I'll say when it's done." |
| follow | `HisSpeech(start)` with the laptop quiet | capture(follow) | |
| follow | deadline (`core/desk.FOLLOW_UP_S`) | idle | |
| live | `Wake`, the user's | capture(wake), once the live call is closing | end the live call with hand-over; its voice stops at once |
| live | the quiet rules, a limit, or `LiveClosed` | follow, or local if a hand-over is owed | section 2.9 |
| any | `HotkeyDown` | capture(ptt) | stop whatever is talking (a live call ends with hand-over) |

Every state has a maximum dwell: capture 20 s (`core/desk.MAX_CAPTURE_S`),
transcribe 10 s, a local turn 180 s, follow 10 s, live 15 minutes. A state that
outlives it is logged and returned to idle, with its turn closed as failed. "Not
responding for minutes" becomes a property a test can check (section 4.3).

### 2.4 One owner per turn

A **turn** is an object: an id, the source (a wake, a follow-up, a barge-in,
push-to-talk, a live hand-over), the request, whether it is a cut-in, its asks
(a live conversation, notes of the room) and its outcome.

**One context variable replaces four.** `runtime.desk_talk`,
`runtime.desk_notes`, `runtime.desk_turn` and
`runtime.desk_cut_in` become a single context variable holding the
current desk turn.

- `take_notes` (and `talk_it_through`, if it survives) record their ask on that
  object.
- The engine reads the ask at `AgentDone` for that turn and no other.
- An ask cannot outlive its turn, because it lives on the turn.
- That retires the whole class of "left set for the next wake" fixes:
  `desk._drop_asks` and `desk.hotkey_turn_over`.
- A push-to-talk turn is a desk turn with source "hotkey", read the same way.

The **floor** is the only way to the speakers:

- `claim(owner)` stops the previous owner's sound before returning.
  `play(owner, audio)` from anyone but the current owner is dropped and logged.
  `release(owner)` gives the floor back.
- A cut turn's later sentences, a stall line that fires after a barge-in, a
  GPT-Live frame after its call has ended, a chime in a live call: none can
  sound.
- Before, each of these was guarded by its own flag (`_cut`, `_drop_until`,
  `listening`).
- A false barge-in can resume: the floor keeps a stopped turn's unplayed
  sentences for 3 s. If the capture turns out to be a false wake or noise, the
  rest of the reply is queued again.

**Two agent runs never share the desk's conversation at once.** A barge-in's
request waits for the run it cut to finish, as today. If that wait passes
1.5 s, it says "One moment." Cancelling a run that has used only read-only
tools, so the wait ends at once, is a Phase 2 question for `agent/loop.py`, not
part of this design.

### 2.5 What "it is speaking" and "The user is speaking" mean

**It is speaking.**

- The player's callback already sees every block it plays. The floor tags each
  queued chunk with its owner. For each block played, it records the owner and
  the block's RMS.
- `audible_until(owner)` is the last block above the audible floor (about
  -50 dBFS), plus the output latency, plus `ECHO_TAIL_S` (0.35 s).
- A live call's silent frames are queued like any other, so the timing of its
  speech is kept, but they are never audible.
- Two consequences follow:
  - the player's queue being full means nothing any more;
  - a stream that stops calling back is noticed by the floor (no callback for
    1 s with sound queued), reopened, and logged. That replaces
    `QUEUE_FROZEN_S`.
- **For GPT-Live there is a second witness.** Its output transcript arrives with
  `start_ms` and `end_ms` on the session's timeline. Each call logs the seconds
  of audible frames against the seconds of transcript. A mismatch (sound with
  no words for more than 10 s) is a warning, `live: audio without words`. It
  would have named today's failure at once.

**The user is speaking.**

- The voice detector (Silero, `voice/listen.Vad`) runs on the microphone after
  the canceller. Outside the desk's audible window, that is them or the room.
- Inside it, their speech counts only with evidence of double talk: speech for
  at least 0.35 s whose energy after cancellation is well above the residual
  echo expected from the far end's level. The engine learns that ratio during
  the desk's own speech in each session.
- This decides two things:
  - whether a live call is being kept alive by them;
  - whether speech over the desk's voice is a barge-in (decision D2,
    section 3.2).

**Other laptop sound** (a video, a call) is the loopback's voice detector, as
today (`voice/listen.Loopback.speech_between`).

- To tell other sound from the desk's own voice while that voice plays, the
  loopback can get its own canceller: the same AEC3, with the player's output
  as its far end, on a digital path where cancellation is near exact.
- This is needed only for D2. Without it, laptop speech during the desk's voice
  stays "the laptop was talking", which is today's rule.

### 2.6 Local turns, follow-ups and barge-in

- **A wake over the desk's voice** (`local` speaking):
  - the floor cuts the turn at once;
  - the 60 ms tick plays;
  - the capture is gathered with its pre-roll, as today;
  - the rest is the same table: `core/desk.wake_phrase`, then a cut-in stop
    (answered by silence), cut-in noise (dropped, and the cut reply resumed if
    within 3 s), or a request.
- **Its own words are taken out of a cut-in's request.** The engine holds, as
  text, the sentences the desk was saying in the last few seconds (it queued
  them).
  - A cut-in transcript that is mostly the desk's own words ("Today, you
    have...") is echo, not a request.
  - Words matching what it just said are removed before anything is decided.
    "I'll talk." could not have come out of that.
  - This is a new frozen rule (section 5.2), because it is what stops the desk
    instructing itself.
- **Follow-ups** are unchanged: 10 s after the floor is free, speech with the
  laptop quiet starts a capture (`core/desk.capture_counts`). The window starts
  from `audible_until` plus the tail, not from "the queue drained".
- **Plain speech over the desk's voice** (no wake word) is decision D2. With D2:
  - 0.35 s of double-talk speech ducks the voice by 12 dB;
  - 0.6 s stops it and starts a capture;
  - shorter speech lets the voice come back up.

  Without D2, the wake phrase is the only way in over its voice, as today.

### 2.7 Notes, approvals, the switch and a swap

- **Notes.**
  - "Note that..." and dictation stay answered by code.
  - The room and call notes are unchanged in substance: `agent/notes.py`,
    `voice/listen.Tap`, the summary with no tools.
  - A note open sets the ear's mode to hand on segments, and the engine routes
    each one to the note. It is never a request.
  - Room notes asked of the assistant are read from the turn object at
    `AgentDone` (`desk.room_refusal` is kept as it is). Starting
    them ends a live call first.
- **Approvals** keep both frozen checks.
  - A yes said while the laptop played speech, the desk's own voice included,
    goes to the screen (`core/desk.approval_counts`).
  - In a live call, the yes must be speech heard outside its own voice
    (`core/desk.heard_user`).
  - The yes is now local Whisper's text of a segment the ear heard outside its
    voice, so the second check is met by construction rather than by a
    timestamp comparison alone.
- **The switch.**
  - `/voice/listen` accepts `{"toggle": true}` and a `by`, so the hotkey cannot
    send a stale direction. Each request is logged with its source.
  - Turning off plays a falling chime, so the user knows it took.
  - Opening and closing the ear run in a thread.
- **A swap** (`agent/worker/duties.give_up_duties`) ends a live call with
  hand-over to the thread and stops the voice. A note carries on as today; it
  is in the database.

### 2.8 Opening a live conversation without a second voice

The protocol supports this. A Live session can start with **history**: the
`session.input` list in `session.start` takes developer, user and assistant
text messages, up to 128 messages and 8,192 tokens
(`openai/types/live/session_config_param.py` in the SDK). The desk passes its
recent turns there, instead of pasting them into the instructions with "Open by
saying this".

Two cases.

1. **The user asked for it by name** ("let's talk live about my week").
   - The local voice says nothing but a short chime.
   - The session starts with their request as the last user message, and one
     instruction: answer it now.
   - GPT-Live is the only voice.
   - If it has not made an audible sound in 6 s (4 s after the user asked, since
     the retest of 9 October 2026), and the user has not spoken since,
     it gets one nudge. At 10 s the call ends **with hand-over**: their request
     becomes a local turn.
   - This is the only opening watchdog. It never ends a call the user is talking in.
2. **The assistant asked for it** (only under decision D1 = A).
   - The local reply is spoken in full, and that **is** the opening.
   - The session starts with that reply as an assistant message and no
     instruction to speak.
   - The microphone is not streamed until the local voice and its tail are
     done.
   - GPT-Live then waits for them, as anyone who has just asked a question
     would.
   - There is no watchdog: if the user never answers, the quiet rule closes the call.

Either way, the connection is made while the local turn finishes, not after it.

- A call with nothing in it costs a few seconds of a minute, and the voice is
  ready when the floor is free.
- On the speakers without a working canceller, its input is muted with the
  protocol's own `session.input_audio.mute` while its voice is audible,
  instead of a stream of zeros, and unmuted after the tail.
- On headphones, or with the canceller working, the stream stays open: full
  duplex, their choice.

### 2.9 Ending a live conversation, and handing over what the user said

**The words.**

- While a call is open, the ear also hands every stretch of their speech to local
  Whisper. That is the same voice detector, outside the call's audible voice or
  with double-talk evidence, and not mostly overlapping other laptop speech.
- Each stretch becomes an utterance with its time.
- When GPT-Live delegates (`session.delegation.created`), the hand-over text is
  **these utterances since the last hand-over**, never GPT-Live's transcript.
  - If a stretch is still being transcribed, the hand-over waits up to 1.5 s
    for it. Whisper takes about 0.4 s warm; GPT-Live delegates about 2 s after
    The user stops (measured on the phone).
  - If there is nothing, GPT-Live is told to ask them again, as today's
    `NOTHING_HEARD` does.
- An utterance is **answered** once a hand-over included it, or once GPT-Live
  has been audible after it (it replied in conversation).

**Closing** is one function, called by every way a call ends: quiet, a limit, a
wake, the switch, the lock, a swap, GPT-Live closing, or an error. It does
four things.

1. It stops streaming and stops its voice. If GPT-Live is saying goodbye and
   nothing interrupted it, the goodbye finishes.
2. It waits up to 1.5 s for transcription in flight. An answer the assistant is
   still working on is kept: it is spoken by the local voice when it arrives,
   instead of by GPT-Live.
3. It finds the unanswered utterances: real words, not "thanks", "bye" or a
   "hmm".
   - **If the desk is staying on**, they become a local turn at once. The
     request in test 2 would have been answered here.
   - **If it is going off, locked or swapping**, they are written to the
     desk's thread as their words, marked as said in a live conversation that
     ended before an answer. The next turn sees them, and the assistant does
     not apologise blind.
4. Minutes, the usage row and the transcript row are written as today
   (`desk._write_live`, `_keep_transcript`). Then follow, so the user can
   carry on locally without the wake word.

**The quiet rules,** now on measured signals:

- **First rule:** close after `IDLE_S` (20 s) of none of these:
  - its audible voice;
  - their speech;
  - work in hand.
- **Second rule:** close after `SILENT_S` (45 s) with no **word**, from either
  local Whisper of them or its output transcript, and no work.

Silent frames cannot hold either rule any more, and nothing depends on the
player's queue.

### 2.10 The desk's GPT-Live client and the phone's

The phone keeps its bridge and its `_Call` **unchanged**, so this rework
needs no deploy of the server the phone runs on. The desk's client stops
subclassing `_Call`.

**What is truly the same, and shared:**

- connecting (now `voice/live.connect`) and the start timeout;
- the voice (now `voice/live.VOICE`, the maintainer's pick);
- `voice/live.LiveUnavailable`;
- answers shaped for speech (`voice/speakable.py`, cut at a sentence by
  `agent/cut.py`);
- one hand-over at a time, in order;
- `SAME_REQUEST_S` and the hand-over limit (`call_policy.MAX_TURNS_PER_CALL`);
- the price (`agent/models.py` `LIVE_VOICE`).

**What is different, and separate:**

| Behaviour | Phone | Desk |
|---|---|---|
| Audio | PCMU passed through | 24 kHz PCM, after the canceller; frames' energy measured |
| An opening in silence | nudge, then give up at 9 s (outbound calls) | only when the user asked by name and neither side has spoken; ends with hand-over |
| Whose words are handed over | GPT-Live's input transcript (the only one there is) | local Whisper of the microphone |
| Interrupting it | GPT-Live's own turn-taking | the same, and "hey Steve" ends the call and returns to the local engine |
| When it ends | the frozen 5-minute call length | the quiet rules, 15 minutes, the day's minutes |
| What it ends with | the carrier hangs up | hand-over of anything unanswered |
| Approvals | the phone server's approval word | the laptop's own `agent/approvals.py` parse against waiting cards (as `desk._ask` does), with `heard_user` |

### 2.11 What the log will say

Numbers, states and reasons, never words (as `desk._log_turn`
today):

- `desk: idle -> capture(wake) turn=41`, one line per transition, with how long
  the last state lasted;
- `ear: wake score=0.93 counted|laptop`, and `ear: near wake score=0.71` for a
  window that scored at least 0.5 but under the threshold, with no wake within
  2 s;
- `ear: mic open|closed reason=locked|no loopback|error|switch`;
- per minute, `ear: dropped blocks=N discontinuities=N`, with the soundcard
  warnings counted rather than printed;
- per call:
  - the counts of each GPT-Live event type;
  - audible seconds against transcript seconds;
  - their speech seconds;
  - hand-overs;
  - unanswered utterances handed over at the end;
- `/voice/status` gains:
  - `engine` (mode and owner);
  - `audible`, from the floor; it replaces `speaking`, which was the queue;
  - the last twenty transitions;
  - the last switch request with its source.

Then the next sound test can say why a "hey Steve" did nothing.

## 3. The key decision: what carries the back-and-forth

### 3.1 D1: GPT-Live, or a local conversation

The local conversation already exists in part. After every reply the desk
listens 10 s without the wake word, and on 9 October that worked twice. A local
conversation mode is that window, plus:

- barge-in that never loses a reply (section 2.6);
- "still on it" lines for long lookups;
- and, with D2, speech over its voice without the wake word.

It is one brain, one voice and one thread.

| | **A. GPT-Live stays the automatic engine** (the maintainer's choice), rebuilt as in section 2 | **B. Local only**: GPT-Live removed from the desk | **C. Local by default; GPT-Live when the user asks for it by name** |
|---|---|---|---|
| **Latency, their data** (calendar, mail: most of what the user asks) | slowest. GPT-Live's end of turn (about 2.2 s, measured on the phone), "Checking.", then the same assistant (2 to 10 s), then its retelling (0.7 s after the answer) | fastest. End of turn about 0.5 s (the judge, today's logs), Whisper about 0.4 s warm, then the first sentence streamed (2 to 4.5 s to first audio in today's turns) | as B |
| **Latency, small talk** | about 1 s | about 3 s | as B by default; about 1 s live |
| **Cost** | $0.05 a minute open, and a call opened by the assistant runs until the quiet rule (20 s minimum) | about $0.002 to $0.01 a turn | as B, plus their deliberate live minutes |
| **Reliability** | the seam that failed every way it could on 9 October. Fixable, but it keeps: <ul><li>a cloud socket;</li><li>undocumented behaviour (continuous frames);</li><li>its own speech recognition on echo-cancelled speakers;</li><li>its own turn-taking.</li></ul> | every part ran fine on 9 October; the remaining risk is local and testable end to end | B's reliability for ordinary use; live is an explicit, separate mode |
| **One brain** | no: a second model with its own memory of the call, paraphrasing | yes | yes, except while the user has chosen live |
| **One voice** | a voice change mid-exchange (am_echo to cedar) each time it switches | yes | yes, until the user asks |
| **Natural back-and-forth** | best: real duplex, interruptions, back-channels | turn-based, a few seconds a turn; barge-in by the wake phrase (or by voice with D2) | B's, with live on demand |
| **Room audio to OpenAI** | while open, every time it decides | never | only when the user says so |
| **The maintainer's stated choice** ("GPT-Live takes over automatically" for back-and-forth) | kept | reversed | changed: no automatic switch; the back-and-forth itself is automatic, in the local voice |
| **Build** | the most: the engine, the live client and the assistant's live ask | the least: the live client deleted | the engine and the live client; `talk_it_through` deleted |

**Recommendation: C.** This changes the maintainer's choice that GPT-Live takes over
automatically, and the maintainer should hear why, plainly.

- What the maintainer wanted from GPT-Live was a back-and-forth without the wake word.
- The local follow-up window now gives them exactly that, without changing
  voice, brain or cost.
- Every automatic switch is a hand-over between two models. Hand-overs are
  where both of the day's tests broke: the duplicate voice, the lost request,
  the call that could not close.
- For what the user actually asks at the desk (their calendar, their mail, their week),
  GPT-Live is slower, because it relays the same assistant.
- C keeps GPT-Live, with its duplex and the voice the user picked, for when the user wants
  to talk freely: "let's talk live".

If the maintainer prefers A after hearing that, the engine supports it as it stands: case 2
of section 2.8 is the assistant's ask. That is one constant (who may open
live: them, or them and the assistant), not a rebuild.

### 3.2 D2: speech over its voice without the wake word

The maintainer chose full duplex on the speakers so that they can talk over it.

- **In a live call,** GPT-Live already lets them talk over it (with the
  canceller working).
- **For the local voice,** talking over it needs `core/desk.capture_counts` to
  stop counting the desk's **own** voice against a capture without the wake
  word:
  - other laptop speech would still block it;
  - the capture would also have to pass the own-words rule (section 2.6);
  - its speech would have to show double-talk evidence (section 2.5).

That keeps the guarantee that the laptop's own sound is not them, by
measurement instead of refusal. The live call has relied on the same thing
since 7 October (the desk's microphone in `docs/security-model.md`). It is still a change to how a
frozen guarantee is kept.

**Recommendation:**

- build the measurement in Phase 2;
- ship with D2 **off**: the wake phrase stays the way in over its voice, as
  today;
- turn D2 on only after a sound test has logged the canceller's residual on their
  speakers;
- that change is a reviewed one-line flip in `core/desk.py`.

### 3.3 Not proposed

- **A longer follow-up window.** `core/desk.FOLLOW_UP_S` stays 10 s: it bounds
  how long the desk takes speech without the wake word.
- **Speech captured while it is thinking.** Only the wake phrase is heard then,
  as today; "still on it" tells them it is busy.

## 4. Test plan

### 4.1 A scenario simulator

A world in which the whole desk runs on a virtual clock, with no sound device,
no model and no network. It lives in the unit tests, as a helper module beside
them. Its parts:

- **The room.** A script of timed sound events:
  - their speech, with the words the user said;
  - the wake phrase;
  - silence;
  - keyboard noise;
  - another person;
  - a video or a call on the laptop, with its words.

  It renders 100 ms blocks at 16 kHz. Each block carries markers: a wake
  marker for the fake detector (as `tests/gate/test_desk.py` already does), and
  energy the fake voice detector reads.
- **The acoustics.** The microphone is their speech, plus the desk's played sound
  at a set echo gain and delay, plus other laptop sound and noise. The fake
  canceller removes the desk's echo down to a set residual (-25 dB, or "failed",
  or "failed at minute 2").
- **The loopback.** Exactly what the fake player played, plus other laptop
  audio.
- **Whisper.**
  - It returns the words of the speech overlapping a capture.
  - The desk's own words appear too when its residual is above a set level.
  - A barge-in's pre-roll can be garbled on purpose: "hey Steve, stop" becomes
    "I'll talk."
- **The assistant.** A scripted `voice_reply`:
  - text streamed in deltas;
  - tool calls (preambles);
  - slow first tokens;
  - restarts (an escalation);
  - failures;
  - `take_notes` and live asks recorded on the turn.
- **Kokoro.** A duration from the text's length; chunks queued like real ones.
- **The player.** Real queue semantics, with callbacks driven by the virtual
  clock. It can stall.
- **GPT-Live.** A fake socket that behaves like the real one:
  - **output frames every 100 ms, always**: silence as zeros or low noise, and
    speech as energy with output transcript deltas carrying `start_ms` and
    `end_ms`;
  - input transcript deltas of what reached it, its own echo included when the
    canceller leaks;
  - `session.delegation.created` some seconds after the user stops;
  - a slow first response (6 s);
  - never speaking;
  - stopping when the user talks over it;
  - honouring `session.input_audio.mute`;
  - `session.closed` with each reason, errors and dropped sockets.

The engine's core is a pure function, so a scenario is a list of events and a
fold. Thousands of steps run in milliseconds, and nothing sleeps. The asyncio
shell gets a handful of tests of its own, which count events rather than time
them (`docs/testing.md`).

### 4.2 Today's two sessions, replayed

Each is a scenario written from the timeline in section 1.3, asserting what
should have happened.

- **Test 1:**
  - an 82-second answer;
  - "hey Steve, stop" garbled to "I'll talk.": silence within one block, no
    model call, no live call;
  - then "let's talk live about my week": GPT-Live answers, and its silent
    frames follow;
  - The user is silent: the call closes within `IDLE_S` plus a tick;
  - "hey Steve, stop" inside a call: the call ends at once, and the local
    engine owns the floor.
- **Test 2:**
  - "talk through my week": a local turn; under D1 = A, a live call with the
    reply as history and no second voice;
  - The user answers at once for 8 s, and GPT-Live is slow (6 s): no watchdog close,
    and their words are handed over as local Whisper's text;
  - a variant where GPT-Live never answers: at the end their request becomes a
    local turn and is answered;
  - "hey Steve" twice with the wake marker's score just under the threshold:
    two `near wake` lines;
  - "Yeah, go through it": answered once, by one voice;
  - 150 s of silent frames: closed on quiet.

### 4.3 Properties, over seeded random scenarios

Random interleavings of their speech, wakes, laptop audio, slow agents and
GPT-Live behaviour, checked after every step:

- **One voice at a time.** Audible blocks from two owners never overlap, and no
  sentence is queued twice.
- **Nothing lost.** Every capture and every live utterance with real words ends
  answered, handed over or written to the thread, or dropped for a reason that
  is logged.
- **Never stuck.** No state outlives its dwell; a live call closes within
  `IDLE_S` plus a tick of its last real signal.
- **Privacy.**
  - No audio reaches the fake GPT-Live outside a live call.
  - No transcription happens outside a counted wake, a follow-up window, a note
    or a live call.
  - No capture is handed on in idle without a wake.
- **No self-instruction.** No request text is mostly the desk's own words from
  the last few seconds.
- **Every capture leaves exactly one log line.**

### 4.4 The gate, rewritten guarantee by guarantee

`tests/gate/test_desk.py` (frozen) calls internals that this design removes:

- `desk._handle`, `desk._ask`,
  `desk._converse` and `desk._state`;
- `voice/listen.Listener`;
- `voice/desk_call.DeskAudio` and `DeskCall`.

It is rewritten against the engine's public surface and the ear, keeping every
test's guarantee. These tests are kept in substance:

- talk without the phrase reaches nothing, and only the pre-roll is held;
- the wake hands on only what followed;
- the laptop's own wake is ignored;
- a follow-up over laptop speech is not theirs;
- the phrase rule's table;
- the agent never says the phrase, and a reply is spoken without it;
- a capture without the phrase reaches no model;
- the request after the phrase reaches the assistant;
- a yes with the laptop talking approves nothing;
- it starts switched off;
- the mic is closed while locked, and closed without the loopback;
- no sound is written;
- a quick note and a dictation reach no model;
- the lecture summary has no tools;
- the live call:
  - is muted on the speakers without a working canceller;
  - is open on headphones;
  - a yes in it needs speech outside its voice;
  - a yes over other laptop audio approves nothing;
  - a request that is not a yes needs no such speech;
- room notes start only on their own spoken request;
- what the laptop plays is a note, never a request;
- the tap has no wake detector.

And strengthened:

- **Their words, by local Whisper.** A live call hands the assistant local
  Whisper's text of the microphone. Neither GPT-Live's input nor its output
  transcript is ever handed over.
- **Its own words are not a request** (the new rule, section 5.2).
- **"Hey Steve" always reaches the local engine.** In a live call it ends the
  call: a stuck call can always be escaped.
- **Silent frames are not its voice.** A call streaming silence closes on
  quiet, so the room's sound does not stream to OpenAI for minutes.

### 4.5 Checks around it

- `python -m devtools.mutate` on each guard: a mutation of each rule, and of
  each floor and ear check, must be caught. A new test is shown to fail without
  its change (`python -m devtools.prove`).
- The cross-cutting checks, each run:
  - `tests/unit/test_test_quality.py`: no source-text searches, and no
    sub-second upper bounds on time;
  - `tests/unit/test_server_actions.py`;
  - `tests/unit/test_docs_are_true.py`;
  - `tests/unit/test_offloop.py`: every new `async def`;
  - `tests/unit/test_repo_hygiene.py`: LF.
- **CPU.** I run targeted tests and `python -m devtools.check` while building.
  The simulator needs no model, so the desk's tests stay in seconds. I run the
  full suite (`-n 3`) once, near the end. I will build it myself rather than fan
  it out.

## 5. Migration

### 5.1 What goes, what stays, what changes

- **Deleted.** These go:
  - `desk._turn`, `_handle`, `_converse`, `_ask`, `_barge_in`,
    `_drop_asks` and `hotkey_turn_over`, and the turn half of `_state`; the
    engine takes their place;
  - `Listener` (`voice/listen.py`);
  - `DeskAudio` and `DeskCall` (`voice/desk_call.py`), with their speaking
    heuristics (`QUEUE_FROZEN_S`, `_played_until` as "speech");
  - the four desk names in `runtime.py`, which become one context
    variable;
  - under C, `talk_it_through` (`agent/toolset/desk.py`).

  The unit tests of what is deleted are rewritten, not loosened:
  `tests/unit/test_listen.py`, `tests/unit/test_desk_call.py` and the desk half
  of `tests/unit/test_voice.py`.
- **Kept as they are:** the wake detector and its training, Whisper, Kokoro,
  the canceller, the end-of-turn judge, the speaking rules, `agent/notes.py`
  and `take_notes`, the tap, the phone's call module, and the knobs
  (`DESK_LIVE_MIN`).
- **New:** the engine and the floor (section 2.2). They go in `core/policy.yaml`
  `review_required`, beside the voice files already there.
- **Changed in the app:** the hotkey sends `toggle`, and the app is rebuilt and
  reinstalled.
- **Size.** About 2,000 lines are replaced (the turn half of
  `desk.py`, `Listener`, `voice/desk_call.py`, and the turn half of
  `voice/pipeline.py`). The new code is a similar amount in fewer places: an
  engine of about 500 lines, a floor of 150, a live client of 350, and the
  simulator about 500.

### 5.2 Changes to frozen files, each with its reason

1. **`tests/gate/test_desk.py`, rewritten** (section 4.4). Every guarantee is
   kept; three are strengthened, and one is new. It has to change because the
   functions it drives are the ones being replaced.
2. **`core/desk.py`, one new rule:** a capture that overlapped the desk's own
   voice is judged on their words with the desk's own recent words removed. If
   nothing of theirs is left, it is dropped. This is a strengthening: today
   nothing stops the desk's own words, heard back, from becoming a request.
   Under D2, a second new rule would decide when a capture without the wake
   word may overlap the desk's own voice. It ships off; turning it on is a
   reviewed one-line flip.
3. **`core/policy.yaml`:** `review_required` gains the engine and the floor.
   `voice/desk_call.py` keeps its entry, since the file stays.
4. **Unchanged:** `pyproject.toml` and `uv.lock` (no new dependency; livekit,
   soundcard, soxr, onnxruntime and faster-whisper are already there),
   `core/knobs.py`, and `tests/gate/test_call_policy.py` (the phone is
   untouched).

After those edits: `python -m core.policy --write-lock`, and `python -m
core.policy` must say "frozen core intact".

### 5.3 Docs

- `docs/flows.md` flow 16 is rewritten around the state table.
- `docs/architecture.md`: the voice rows, and the note that the phone's
  call module no longer runs on both machines.
- `docs/security-model.md`, the desk's microphone:
  - local Whisper for hand-overs;
  - the own-words rule;
  - "hey Steve" in a live call;
  - D2 if it is turned on;
  - the review list.
- `docs/testing.md`: the simulator.
- This document is cut down to the decisions, once built.

### 5.4 Order of work

The work goes in this worktree, in commits that each pass `python -m
devtools.check`:

1. the floor, with audibility measured, tested against today's player;
2. the ear's modes and events, with the gate's ear tests;
3. the engine core, with the simulator and the local scenarios;
4. the shell and the glue: the switch, status, notes and push-to-talk;
5. the live client and its scenarios;
6. the gate rewrite, the new rule, and a relock;
7. docs;
8. the full suite.

Then one merge after review, and a worker swap. The phone's server needs
no deploy.
If the swap goes wrong, revert the merge and swap again.

### 5.5 Risks

- **The canceller on their speakers is unmeasured.** It decides:
  - whether full duplex holds for live calls;
  - whether D2 can ever be turned on;
  - possibly whether their "hey Steve" survives the canceller.

  The new logging measures it in the next test.
- **The missed wakes are unexplained.** If the next test shows `near wake`
  lines, the threshold or their own recordings (`voice/wake_train.py
  --retrain`) are the fix, not this design.
- **GPT-Live's continuous frames** are inferred from the record, not from a
  spec sentence. The design measures energy and transcript time, so it holds
  either way.
- **A rewrite of a frozen gate** is the change most in need of a careful human
  read. Section 4.4 is the checklist for it.
- **Push-to-talk and the call notes** ride on the same player and the same
  thread. Both get scenarios, so they do not regress quietly.
- **A barge-in during a long lookup** still waits for that lookup's run to end
  (section 2.4). "One moment." covers it; cancelling the run is a later
  question.

### 5.6 The maintainer's next sound test: three minutes

Before starting, check that `/voice/status` shows `engine: idle` and the
`echo` stats. Then:

1. **0:00. Alt+Home on.** You hear the rising chime.
2. **0:10. Say "Hey Steve."** A tick, then the "go on" chime. Ask "what's the
   weather?" When it answers, say "and tomorrow?" with no wake word.
   - Listen for: a tick on every wake, and an answer to both questions.
3. **0:40. Say "Hey Steve, tell me about the history of tea."** About five
   seconds into the answer, say "hey Steve, stop."
   - Listen for: silence within half a second, and nothing more said.
4. **1:00. Say "Hey Steve, let's talk through my week."** Answer its question
   straight away, then say nothing for 25 s.
   - Under C, listen for: one voice throughout, and no wake word needed for
     your answer.
5. **1:40 (only under A or C). Say "Hey Steve, let's talk live."** Talk over it
   once. Then say "hey Steve, stop."
   - Listen for: the call ends at once, and the local voice is back.
6. **2:15. Play a video with speech, say nothing for 10 s, then say "Hey Steve,
   note that the plumber comes Thursday."**
   - Listen for: no response to the video, and "Noted."
7. **2:40. Alt+Home off.** You hear the falling chime. Say "hey Steve": it
   should do nothing.

Afterwards, read these from the log:

- the `desk:` transitions;
- every `ear: wake` and `ear: near wake` line;
- `ear: dropped blocks`;
- the live call's frame and transcript seconds;
- the unanswered utterances it handed over.

Every "it didn't hear me" then has an answer in numbers.

## 6. What needs the maintainer

- **D1.** Who carries the back-and-forth.
  - Recommended: C, the local conversation by default and GPT-Live when the user asks
    for it by name.
  - A keeps the maintainer's original choice; B is the smallest.
- **D2.** Talking over the local voice without "hey Steve".
  - Recommended: built, but off until a sound test has measured the canceller
    on their speakers.
- **Confirming two things:**
  - that "let's talk live" (or another phrase of their choosing) is the words
    that open GPT-Live under C;
  - that a short tick on every counted wake is welcome. It tells them at once
    whether they were heard, which on 9 October the maintainer could not tell.
