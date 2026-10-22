# The security model

What Steve protects, from whom, and what holds the line. Version 2, written
11 October 2026 for Steve as a product: installed by strangers on their own
Windows machines, with no Steve server and no account. Version 1 described
one person's agent with a cloud relay. Its mechanisms that survive are in
section 3, in brief; the code and the gate tests named there hold the
detail.

Each part says where it stands:
- **holds**: in the code today, pinned by a test;
- **step N**: designed here, built in that step of the plan,
  before the first release;
- **decided**: settled on review, 11 October 2026 (section 7);
- **open**: a gap in the code today, said plainly so that no one takes it
  for a line that holds.

The principle is unchanged: **models suggest, interpret and classify; small
deterministic code holds authority.**

## 1. What Steve protects

| Asset | Where Steve holds it | The harm |
|---|---|---|
| The user's mail, calendars and files | at their providers, reached with an app password, an OAuth token, or Outlook on the machine; extracts, summaries and drafts in Steve's database | read and leaked; sent, changed or deleted in their name |
| What Steve knows about them | the database (conversations, facts, memories, the action ledger), the memories folder, and the logs, which hold some of what was said (a turn's first words, a reply that could not be sent, an event's title), in the data dir (`core/paths.py`) | read by someone else; changed, so Steve acts on false beliefs |
| Their keys and money | the OpenAI key and the providers' passwords and tokens (section 5) | spent, or used outside Steve |
| Their Signal or Telegram account | signal-cli's linked-device keys in the data dir; a Telegram bot token | read: a linked device receives the account's messages, not only Note to Self; written: messages sent as them |
| Their machine | anything the Windows account can touch | files read or changed, programs run, the local network reached |

Steve acts only on Note to Self (`channels/signal.py`), but signal-cli's keys
can read everything the account receives. They are as sensitive as the
phone's own Signal. signal-cli also downloads every chat's attachments;
those of messages Steve ignores are deleted as they arrive. The photos and
voice notes sent to Note to Self are kept a day, so they can be asked about,
then deleted. A photo is sent to OpenAI with the message it came with, as a
voice note is to be heard.

## 2. Who might attack, and what stops them

### 2.1 Text from outside steering a model

- **Who:** anyone who can put words in front of Steve: an email, an
  invitation, a shared document, a web page, a file name, a news item.
- **What they want:** an action the user did not ask for (a reply, a
  forward, an event, a fetch that carries their data out), or a lie told
  to the user.
- **What stops them (holds, section 3):**
  - the screen reads outside text before any model does;
  - a run nobody is watching fetches only URLs it was shown, and has no
    browser but a browsing worker's, kept to the sites it was given; in a
    conversation the user is in, `read_page` and the browser keep the same
    rule once the conversation has read outside text, and a site's search
    box then takes only the user's own words;
  - every outward effect is a card, or inside an envelope the user approved;
  - the critic checks unattended actions against the user's own words;
  - approvals are read by code before any model sees them.
- **Accepted:** a hijacked model can still *say* something false in a
  conversation the user is in.
- **Accepted:** a conversation opens up to three addresses on one site
  that came whole in what it read, and more there on the user's yes, so a
  page with a link for each letter could spell out three letters of what it
  read; a restart of the worker starts the count again.

### 2.2 A buggy or hostile module

- **Who:** a module Steve built for the user, perhaps from a request that
  outside text shaped; one the user imported from someone else, or was
  talked into installing; an MCP server they added.
- **What it wants:** secrets, files outside its folder, a host it did not
  declare, an effect without a card, a foothold in the app's own code.
- **What stops it (step 6, section 4.4):**
  - an AppContainer of its own, less privileged: no network, no
    credentials, none of the user's files, only its own folder and the
    runtime, read-only;
  - a broker that is its only way out, holding it to the hosts and secrets
    its manifest declares;
  - effects only through `ctx.act()`, the same door every tool uses: a
    card, or inside an envelope the user approved;
  - before it runs, its text screened, its code reviewed by a second model
    and its tests run in the sandbox; then the user's approval, with the
    passphrase when it could send what it reads somewhere else (an
    account it provides with its own code counts as one it reads);
  - the backend tier, Steve's own process, refused to any module from
    outside (4.5).
- **What is checked:** the walls from inside
  (`tests/gate/test_module_sandbox.py`); the install folder unwritten
  through a module's whole life (`tests/gate/test_module_life.py`); and,
  with a module in every slot switched off, Steve as it was before it came
  (`tests/gate/test_off_means_original.py`).
- **Accepted:** a desktop program a module drives runs outside the
  AppContainer, with the user's rights. What holds it is what a module may
  call there (4.4); a program Steve does not know is the user's to allow,
  and its card says so.

### 2.3 Other processes and other Windows users on the same machine

**Until step 4.** The supervisor listened on 127.0.0.1:8790, and each worker
on a loopback port of its own. `core/localonly.LocalOnly` checked every
request and websocket handshake. It refused a browser page by its `Origin`,
and DNS rebinding by its `Host`. It let through any request with no
`Origin`: version 1 assumed one user per machine, and a process running as
that user can read the keychain anyway.

On a stranger's machine that assumption fails. Loopback is shared by every
account on the machine: a family PC, a shared lab machine, fast user
switching, a service. Any of them could:
- POST `/actions/decide` and approve a waiting card. It is recorded as the
  app's yes (`agent/worker/routes.action_decide`), and until step 4 the
  app's yes needed no second factor, even for a high-risk card;
- open `/ws` and chat with Steve as the user ("what are my newest emails?");
- rewrite who the user is with `PUT /control/profile`: the identity lines
  that lead every turn, with the user's own authority
  (`agent/worker/control.profile`). A value the screen's patterns flag
  was refused, and every value reached a turn quoted, as data
  (`agent/facts.render_line`), as both still are. A plain lie, such as
  another town or another name, got through;
- call every other route: the control panel (`agent/worker/control.py`), a
  worker's `/signal/send`, the supervisor's `/_sup/swap`;
- take port 8790 before Steve started, and receive what the app sent to it.

**What stops them now (section 4.1):** a token made at each start, readable
only by the user, on every route of the supervisor and of every worker; a
port the system chooses at each start, and a worker's held by the
supervisor from before the worker starts; and a backend that proves itself
to the app just before the app sends anything. The app's own yes on a
high-risk card takes the passphrase besides (4.2), so even the token does not
spend money.

**Out of scope:**
- **A process running as the same Windows user.** It can read everything
  the user can: the Credential Manager, the data dir, the token file. Steve
  cannot defend the account from itself, and does not claim to. The one
  exception is modules, which run as the user but inside an AppContainer
  (2.2).
- **An administrator of the machine.** So is another account allowed to
  use Docker (the `docker-users` group): Docker's engine runs as the
  machine, and such an account can do what an administrator can. Steve
  still runs only the images it built, by the IDs their builds recorded
  (`core/images.py`), so a tag pointed elsewhere starts nothing. Setup
  does not warn of such accounts (step 11's decision): they are rare on a
  personal laptop, and this paragraph says what they can do.

### 2.4 A compromised Signal or Telegram account

- **Who:** someone who took over the account: a SIM swap, a stolen phone, a
  hijacked Telegram session, a linked device the user never noticed.
- **What they can do:** write in the channel Steve treats as the user's.
  They can ask for things, and answer cards.
- **What stops them (step 4, section 4.3):** channel tiers. A remote channel
  answers ordinary cards. A high-risk card needs the app, or a code from an
  authenticator app on another device. Pairing a channel, and anything else
  that changes who may approve, happens only in the app.
- **Accepted:** whoever holds the account can approve an ordinary card (a
  reply, an event), and their requests are the user's requests.
- **Wrong codes are counted** over a rolling day (`core/totp.wrong`, from
  `core/monitor.accept_code`). Five within a day stop every code, by message
  or voice, for 15 minutes, and a stop within a day of the last lasts twice
  as long, up to 24 hours. A right code does not reset the count. A count
  that cannot be read or written, or that a newer release wrote, stops codes
  too. The app still takes the yes, with the passphrase. The count is kept
  in `code_tries.json`, in its second form (`core/totp.STEPS`).
- **A code is six ASCII digits.** Full-width digits are read as ASCII
  (NFKC); another script's digits are no code.
- **Accepted:** three codes are valid at a time (`core/totp.matching_step`).
  Stop after stop leaves a guesser about 35 tries the first day and five a
  day after: about one chance in 10,000 the first day, and about one in 180
  over a year.

### 2.5 An unlocked or stolen laptop

- **Locked:** Windows' sign-in guards the session, and the profile's own
  access rules keep other accounts out of the data dir.
- **Off, or the disk taken out:** only BitLocker or Device Encryption
  guards the disk. Without it, whoever holds the disk reads the data dir as
  plain files: the database, the memories, the logs, signal-cli's keys
  (the Signal link) and an MCP server's home (its sign-in). The Credential
  Manager's secrets and the browser's sessions, under DPAPI, stay
  encrypted under the user's Windows sign-in. Steve adds no encryption of
  its own (decision 1). Setup checks whether the data dir's own drive is
  encrypted, and says so if it is not, naming the Signal link and the
  sign-ins with the conversations (`setup.drive`). It also says when
  another account may read the data dir (`setup.shared`), which a folder
  chosen outside the profile (`STEVE_DATA_DIR`) can allow.
- **Unlocked and left:** app sign-in (section 4.2) gates the app's views and
  its approvals. It locks when Windows locks and on sleep, and after half
  an hour idle if the user turns that on (off by default: Windows' own
  lock is the usual guard of a desk left). A high-risk approval asks
  again, every time.
- **Accepted:** someone at an unlocked Windows session is, to Windows, the
  user (2.3). They could read the token file and drive the API around the
  sign-in. The high-risk re-ask holds against the API alone, because the
  backend checks it (decision 3).
- **Fixed in step 11:** deleting `passphrase.json` at the unlocked desk
  turned the sign-in off and let a new passphrase be chosen without the
  old one, which then answered every high-risk re-ask. Steve now keeps a
  digest of the passphrase file, and of Hello's key, in its database
  (`core/kept.py`): a file deleted or replaced locks the passphrase
  (`core/passphrase.state`, "gone"), and a replaced `hello.json` signs
  nothing. The way back is a reset by Hello or by Windows' own sign-in
  (4.2).
- **Accepted:** someone at the desk with SQLite's tools could clear the
  kept row as well as the file. That takes knowing Steve's schema, not a
  deletion in Explorer, and a stranger at the desk is still, to Windows,
  the user (2.3).
- **Pairing takes the sign-in** once a passphrase is set: linking Signal,
  giving Telegram a bot or pairing it, and setting up the authenticator
  (`agent/worker/pairing.py`, `agent/worker/approvers.py`,
  `signin.refused`). Someone at the unlocked desk while Steve is signed out
  cannot pair a channel of their own. Unlinking and unpairing take none:
  taking an approver away is always open.

### 2.6 A compromised update channel

- **Who:** someone who can change what the app downloads: a hijacked
  release, a swapped add-on, a network in the middle.
- **What stops them (step 8, section 4.5):**
  - updates are signed with a key the app knows, and the updater refuses
    an unsigned release and an older one. The address it asks is step
    12's: until then a build asks no one;
  - what is downloaded later is pinned in the signed release and checked
    before it runs:
    - the add-ons, signal-cli and its Java runtime and the local decision
      model's runtime, Ollama, by SHA-256, and their files again before
      every start (`core/addons.py`);
    - the local readers' models: PIGuard's files by their hashes
      (`core/detector.py`), Tev1 by its manifest's hash (`core/decider.py`);
    - Docker's base images by digest, and the packages built into them by
      hash (`core/images.py`).

    The voice add-on is not offered in v0.1: no release ships it or
    downloads it.
- **Accepted:** whoever holds the signing key can ship anything. It is kept
  offline, never in CI or the repo. v0.1's installer is not code-signed
  (4.5): SmartScreen warns about it, and nothing but where it was
  downloaded from tells a user that an installer is Steve's.

### 2.7 OpenAI and OpenRouter as data processors

Not attackers, but parties that see most of what Steve reads (section 6).
- Steve uses the user's own key, so their organisation's OpenAI data
  controls apply.
- Responses are not stored where the API lets Steve say so: `store: false`
  on every Responses call (`agent/provider.py`, `core/screen.py`,
  `core/websearch.py`, `voice/desk_call.py`). **Holds.**
- OpenAI's own retention for abuse monitoring still applies (up to 30 days
  at the time of writing), unless the account has Zero Data Retention.
- Web search (`core/websearch.py`) sends OpenAI the query and the user's
  country (`core/language.py`), never the conversation, and nothing nearer
  than the country. Per OpenAI's data controls guide (checked 12 October
  2026):
  - OpenAI processes web search only in the United States or Europe, so a
    project whose data stays in another region does not keep its searches
    there;
  - search with live web access is outside OpenAI's HIPAA terms.
  - Where the user has set a Brave key, Brave gets the query and the
    country, under its own terms.
- Jev, the decision model, is reached through OpenRouter, not OpenAI. It
  sees what the screen, the critic and the other decision sites ask it
  about. Every request asks OpenRouter to route only to endpoints that keep
  nothing and collect nothing (`core/decide.py`). **Holds.**
- Each endpoint Steve calls was checked against OpenAI's data controls
  on 20 October 2026. `PRIVACY.md`, the privacy statement Setup shows
  on its first page, says what each keeps; it names every host
  section 6 names (`tests/unit/test_privacy_statement.py`). **Holds.**

### 2.8 A third-party connector or MCP server

- **Who:** the author of a connector Steve did not write: an MCP server,
  run on the machine or reached at a URL, or a hosted connector. Or whoever
  later takes over one, through its updates.
- **What it can do:** a server on the machine is a program running as the
  user, with everything their account can touch. Any server sees each
  request Steve sends it. And it writes its own tool descriptions and
  results, which a model reads.
- **Today (holds):**
  - **A connector module is one of Steve's own,** in `modules/`, reviewed
    and frozen with core (`core/policy.yaml`), **or one from outside,**
    admitted on the user's yes and run in the module sandbox (4.4). It
    may import only `steve_sdk` and the libraries its manifest declares
    (`tests/gate/test_module_imports.py`); one from outside, the
    standard library and `steve_sdk` alone. It reaches the network only
    through its context, to the hosts its manifest fixes or the user typed
    at setup (`core/modctx.py`, `core/egress.py`). Its secrets come from
    the Credential Manager through the same context, and every change it
    makes goes through core's write path (`core/writes.py`), on condition
    or by card.
  - **One MCP server:** Outlook.com is reached through an MCP server
    (`modules/outlook_mcp`): either one Steve installs and runs in a
    container of its own (4.4), or one the user runs on this computer, at
    the loopback address they type. Either way the server holds the
    user's Microsoft sign-in itself.
    - The one Steve runs is signed in by a code the user types on
      Microsoft's own page; Steve shows the code only for the page the
      profile names. The address it signed in as must be the account's
      before its tools are pinned.
    - Steve calls it only through a profile written for that server
      (`modules/outlook_mcp/profiles/softeria.toml`), which names each
      tool; a server no profile fits is refused.
    - The tool list is pinned when the user tests the account. A list
      changed since stops every call until they test it again
      (`core/engines/mcp.py`).
    - The server's tool descriptions never reach a model. What its tools
      return is mapped into mail and events, which reach a model only as
      any mail and event does: screened as outside text (2.1).
    - Updates and deletes take a card every time, the item read again
      just before.
  - **Remote servers the user adds** (step 6, `agent/mcptools.py`) are
    modules with no code, reached through the module's broker
    (`core/sandbox/broker.Local`): POST to the one host the card named,
    over HTTPS unless the server is on this computer, with the secret
    filled into its bound header there alone.
    - Their tools, descriptions and schemas are screened and shown on the
      card, and pinned by fingerprint. A tool added or changed stops every
      call until the user approves the change, on a card naming it; a
      list that only lost tools goes on.
    - A tool the server labels a write is a card every call. One labelled
      a read is asked once in a conversation the user is in, then runs
      there for half an hour. With no one watching, every call is a card.
      Tools the user freed at approval skip the card in their own
      conversations alone.
    - What a tool returns, or says when it fails, is screened before a model
      reads it (2.1).
  - LinkedIn, whose MCP server Steve learned from, was rebuilt inside the
    browser add-on rather than run as it was (`agent/toolset/linkedin.py`).
- **What holds for the servers the user adds (step 6):**
  - a server on the machine runs inside the module sandbox (4.4), or not
    at all. Its only network is a pipe to core's relay, which connects
    only to the hosts its manifest names, and checks the name the server's
    TLS handshake asks for;
  - a remote one is a host the user approved, reached through the broker;
  - its tool descriptions, results and errors are outside text, screened
    before a model reads them (2.1). So is what a module says when it fails
    (`agent/modtools.py`, `agent/mcptools.py`, `agent/modconnect.py`);
  - a tool that changes anything is an effect: a card, or inside an
    envelope the user approved;
  - it never holds the user's password or keys. The broker puts a remote
    one's secret into each request. A server Steve runs keeps only the
    sign-in it was given, in its own home, which no other container can
    read (5).
- **Accepted:** a server the user approved can lie in what it returns, and
  sees what they ask of it.

## 3. Carried over unchanged

All of this **holds** today, except what is marked open.

**One door for effects.** A tool that changes anything calls
`agent/actions.act`. That asks `core/monitor.gate`, which applies
`core/consent.classify` under the rules in `core/consent.yaml`. The answer is
one of three:
- **Free,** and recorded: what is private and reversible; an email to the
  user's own address; what a routine or a goal does inside the envelope
  the user approved for it (`core/monitor.routine_gate`,
  `core/monitor.goal_gate`); and, in the user's own conversation, a write
  carded only because the account cannot write on condition, when Jev
  judges the condition unneeded (`core/lostupdate.py`).
- **A card.**
- **Refused:** a worker asking for more than a worker may, and anything
  that would write to the agent's own mailbox, whoever asks
  (`core/monitor.mailbox_refusal`).

The rules and the monitor are frozen
(`core/policy.yaml`), so no change Steve makes to itself can rewrite them.
What Steve learns (memories, lessons, the prompts the night may edit)
reaches what a model says, never what the rules allow. Pinned by
`tests/gate/test_every_tool_is_classified.py` (no tool reaches a model
unclassified), `tests/gate/test_what_review_protected.py` (what is frozen),
`tests/gate/test_workers_stay_private.py`, `tests/gate/test_lost_update.py`
and `tests/gate/test_writes.py` (a write held to one exact target).

**Approvals are read before any model, and bound to what was asked.**
- A card is a row: the tool, its arguments as they were when the user was
  asked, a token word and an expiry.
- Code reads the answer (`agent/approvals.parse_reply`, in
  `agent/worker/answering.intercept_approval`) before any model sees the
  message, and only on channels where the user is present
  (`agent/worker/answering.APPROVAL_CHANNELS`, and the app).
- Runs nobody watches cannot answer a card: the night, jobs, workers and
  reminders are none of those channels, and a channel with no ceiling
  approves nothing (`core/monitor.CEILINGS`).
- A yes runs the stored arguments, once (`agent/approvals.claim`). Nothing
  in the reply changes them. A decided card cannot be decided again, and an
  expired one cannot be approved. An irreversible action waits a short hold
  for an undo.
- A card shows whole what a yes agrees to: whom it reaches, the values, the
  code. An email waits whole in the mailbox's Drafts, and a card that would
  quote one is not asked at all (`agent/approvals.quoted_body`).
- A routine's or goal's card puts its targets alone on the first line. What
  the model wrote follows on labelled lines, with hidden characters dropped
  and quotation marks folded (`agent/approvals.one_line`), and a target
  containing a hidden character is refused
  (`core/monitor.envelope_refusal`). Changing its instruction or schedule
  asks again.
- Pinned by `tests/gate/test_approval_binding.py`,
  `tests/gate/test_cards.py` and `tests/gate/test_call_policy.py`.

**High-risk cards need a second factor.** A tool marked `tier: high` in
`core/consent.yaml` needs, with the yes, its channel's second factor: over a
channel, the six digits from an authenticator app (`core/totp.py`); in the
app, the passphrase (`core/passphrase.py`). Each is checked when the yes is
recorded and again when the action runs (`core/monitor.may_decide`,
`core/monitor.may_run`), and is bound to the card's arguments as they were
(`core/monitor.accept_code`, `core/monitor.accept_passphrase`). A code is
bound to one card and one 30-second step, a step counts once, and only one
high-risk card is open at a time. Neither factor stands in for the other.
Pinned by `tests/gate/test_high_risk_approval.py` and
`tests/gate/test_who_may_approve.py`.

**The injection screen.** Everything someone else wrote is screened before a
model reads it: mail bodies and threads; the sender, subject and preview the
attention judge sees; pages and search results; shared documents; calendar
entries others wrote; newsletters, news headlines and the names of files
a search lists; the senders, subjects and snippets a mail search lists,
screened as the attention judge's previews are; the names and subjects the
people ledger keeps from mail, wherever a tool shows them; the sent mail a
draft is modelled on, whose replies carry their senders' subjects; the
notes of a meeting or a call; the browser's pages and downloads; what a
module or an MCP server returns, and what it says when it fails.
`agent/screening.py` is the one way outside text reaches a model, and it
goes through `core/screen.py`. Every model tool written into the toolset
says whether its results can carry outside text (`agent/screening.OUTSIDE`,
`agent/screening.OWN`), and the wrapper each is registered through marks
the conversation after a call of one that can
(`agent/screening.read_through`); `tests/gate/test_every_tool_is_classified.py`
fails on a tool that says neither. A few mark it themselves, only when what
they return holds someone else's words (`agent/screening.LEFT_TO_THE_TOOL`):
a web search the screen passes, and an agenda of the user's own events,
leave the conversation free. **Open:** `recall` marks nothing, though some
of what it returns was kept from what a turn read. Module admission puts a module's words to
`core/screen.py` directly (`agent/modadmit.py`, `agent/modbuild.py`): they
are judged there, not read.
- A deterministic pre-screen looks for hidden characters, "ignore your
  instructions", role markers, words addressed to an AI, and approval
  look-alikes. A classifier, Jev (TypeSafe's decision model, through
  OpenRouter), then sorts the item.
  When it cannot answer (an outage, or a spending cap reached), the local
  readers decide if they are installed.
- The model reads the item as it came; or an extract, written by a model
  with no tools from the item alone, and screened again; or a note that it
  was withheld, naming its kind and its address or host, never its words.
  A flagged item's extract is shown only when its own screen passes it. An
  item no screen could judge is read as its extract, whose screen is down
  too: withholding those would leave a plain install reading nothing.
- A module's words are screened at admission, in two parts. Its words for
  the user are asked what outside text is. Its words for Steve -- tools'
  descriptions, its section of his instructions, skills, routines -- are
  instructions by design, so they are asked whether they keep to the
  module's own job (`screen.INSTRUCTIONS`): sending, revealing or deleting
  something, acting unasked, or hiding from the user is past it. The
  patterns hold for both. With Jev down, only the local decision model
  reads them: an injection detector would flag every instruction.
- A verdict is made once per item and kept. An item nothing could judge
  (no answer from the classifier, and no local reader) is read as a flagged
  one is: an extract, or withheld if none can be made. A screen that fails
  withholds.
- Pinned by `tests/gate/test_injection_screen.py` (what it decides) and
  `tests/gate/test_screen_enforces.py` (each path into a model from the
  user's accounts, the web and the browser; a new one belongs there). A
  module's and an MCP server's answers and errors are pinned beside their
  code: `tests/unit/test_modtools.py`, `tests/unit/test_mcp_servers.py`
  and `tests/unit/test_modconnect.py`.

**Egress.** Every fetch of an address a model chose (`read_page`, the
browser through its gate) refuses, on every redirect hop, a bare IP
address, a local name, credentials in the URL, any scheme but http(s), and
a host that resolves to a private address (`core/egress.refusal`). The
connection goes to an address just vetted, never to the name again, so a
name that answers somewhere public for the check and somewhere private
after has no second lookup to win (`tools/web.py`). So a page cannot reach
the backend, the router or the LAN. The attention loop's feeds (weather,
news, the trips' geocoder) go to fixed hosts and follow their redirects
without that check (`agent/attention/fetch.py`).

A run nobody watches fetches only URLs it was shown: returned by its own
search this turn, or written by the user in the conversation the job came
from. Never one it composed, where data would ride (`core/egress.unseen`).
Pinned by `tests/gate/test_injection_containment.py`, which drives the
tools the way a fully hijacked model would. In a conversation the user is
in, `read_page` keeps to what it was shown too, once the conversation has
read an email, a page or anything else from outside, for the rest of the
conversation (`agent/toolset/web.py` `unshown`): what it searched up, the
user's own words, and the addresses that came whole in what it read. A
chat that has only searched the web stays free while the screen passes the
results, addresses included; a search it does not pass gives hosts alone
and counts as outside text read. Pinned by
`tests/unit/test_watched_reading.py` and `tests/gate/test_screen_enforces.py`.

The browser keeps the same rule (`agent/toolset/browser.py`): after outside
text it opens only a shown address, and a click goes only to one, the
links on a page the screen passed counting as shown. A site's search box
then takes only words the user wrote in the conversation
(`agent/screening.py` `theirs`), since what is typed there goes to the
site, and a page can call any field its search box; anything else is a
submit plan, and a card. Pinned by the same test file.

Addresses that came whole are free a few at a time: a page listing a
link for each letter could otherwise spell out what the conversation
read, in the order the model opens them. A conversation opens three on
one site (a host's last two labels, so subdomains are one site), then
asks the user (`agent/screening.py` `spelled`, `WHOLE_PER_HOST`); their
yes to `allow_links`, a card, opens that site's for half an hour. An
address the user wrote counts against nothing. Pinned by
`tests/unit/test_watched_reading.py`.

**The critic.** For each action an unattended run proposes,
`core/critic.review` asks a separate model whether it serves the user's own
words (a routine's instruction, a goal's ask, the task they set) or works
against them. The critic never sees what the run read. An action it does not
plainly allow, or could not review, becomes a card that says why. It
enforces (`critic.mode: enforce` in `core/policy.yaml`, since 5 October
2026). Set to `shadow`, it records each review and changes nothing; nothing
turns it off. Pinned by `tests/gate/test_critic.py`.

**A write without a condition.** On an account that cannot make a delete
or a move conditional on the item being as it was read, the card is that
condition. In the user's own conversation, and only for a card asked for
that reason alone, `core/lostupdate.judge` asks Jev whether a change made
elsewhere meanwhile is plausible enough to ask about. It goes without the
card only on a sure answer, at the critic's bar; unsure, unanswered or
unreachable, it asks. Only that verdict lets `core/writes.py` write without
a condition and without a card. A worker, a routine, a goal or an
unattended session keeps the card. Pinned by `tests/gate/test_lost_update.py`
and `tests/gate/test_writes.py`.

**Routines and goals: approved once, run many times.** Their outward reach is
fixed before they read anything: the targets the user approved on one card
(`core/monitor.routine_gate`, `core/monitor.goal_gate`). Outside text can
fill in what goes to an approved target, never change the target.
High-risk tools are never routinable. Pinned by
`tests/gate/test_routines.py` and `tests/gate/test_goals.py`.

**Also carried over:**
- **Whether it worked.** After an action reports success, `agent/receipts.py`
  reads the result back where it lives and compares. A disagreement is a
  failure the model cannot call done.
- **Steve's own mailbox.** Mail to Steve's address is the user's
  instruction only if it is from an address they opted in, and a copy of
  it is in that account's Sent folder, its text then read from that copy
  (`core/mailbox.from_user`). Without a Sent copy, only where the provider
  ties a From to its sign-in (`core/mailbox.TRUST`, today Gmail's own
  domains) does its DMARC pass count; a custom domain's counts only if the
  user says only they send as it. A Message-ID gives one instruction. The
  mailbox and the opted-in addresses are written by the app alone. Steve
  never writes to its own mailbox. Pinned by `tests/gate/test_mailbox.py`.
- **Skills.** A saved method is text written from the user's words and the
  run's tool calls only, held to frozen rules (`core/skills.py`: nothing new
  to reach, nothing that sets aside the user's say). It grants nothing.
- **Settings.** The app turns knobs only within bounds frozen in
  `core/knobs.py` (`tests/gate/test_knobs.py`). Nothing in the control panel
  widens what Steve may do.

### What later steps change underneath

The rules above stay. What sits under them changes:
- **Step 3.** The classifier behind the screen, the critic, the browser's
  click judge, the split check and the meeting follow-up becomes one call to
  Jev, whose OpenRouter key Steve now requires beside OpenAI's. (OpenAI's
  Decisions API was measured in its place and caught far fewer attacks.)
  Each keeps the fallback it had when Jev was down: the critic gives a card; the screen asks the local readers if they
  are installed, and otherwise reads the item only as an extract, withheld
  if no extract can be made; a click becomes a card; a task is not split.
  The local readers and Docker stay optional. Web search runs on OpenAI's
  search tool, on the same key; Brave's, which is faster, stays optional.
- **Step 4.** The relay goes, and with it version 1's sections on what a
  relay holds and could approve. Signal runs on the machine. Phone calls go,
  and so does `place_call`, one of the two high-risk tools (decision 5).
- **Step 6.** The coding sandbox's Windows account and its git clone give
  way to the module sandbox. Self-upgrade never touches the app's own
  code.

### The browser add-on

Off by default. The user turns it on in the app, with the `BROWSER` switch
(`core/knobs.py`), and the frozen policy must allow it as well (`browser:`
in `core/policy.yaml`). A background task browses only with
`BACKGROUND_BROWSING` on too. Web apps run in a sandboxed browser, in Docker
on the user's machine, never in their own browser or profile.

LinkedIn's tools (`agent/toolset/linkedin.py`) are optional. They are
offered only with the browser on and a LinkedIn login the user saved. They
build each address themselves, only on linkedin.com, and can only open,
scroll and read: no tool there can press a button. Loads are paced, so the
user's account is not spent.

Running code (`run_code`, the `RUN_CODE` switch) uses the same Docker, in a
container of its own (`tools/code_exec.py`): no network, a read-only root,
capped memory and processes. Code that asks for the network is a card that
shows the code whole.

| Boundary | Held by |
|---|---|
| Nothing of the user's inside | images Steve built, run by the IDs their builds recorded, on bases pinned by digest with packages pinned by hash (`core/images.py`); a fresh, empty profile per session; no mounts, ports, environment or credentials; a read-only root, no capabilities, a non-root user, memory, CPU and process caps (`core/browser/sandbox.py`) |
| No way out but the gate | a per-session internal network with DNS pointed at nothing; the gate (`core/browser/gate.py`) applies the egress rules, ports 80 and 443 only, and connects to the address it checked |
| No port to drive it by | the driver (`core/browser/driver.py`) speaks JSON over `docker exec`, with a fixed list of commands |
| Typing is disclosure | free only into a search box, and once the conversation has read outside text, only the user's own words from it (`agent/toolset/browser.py` `browse_search`); anything else is a submit plan, and a card. A search box is what the page says it is (its role, type, name or label: `core/browser/driver.py`), so a page made for it can mark any plain field as one: the words are held to the user's for that reason |
| A submit is the user's | the card shows the site, every value and the button; the driver checks the page again before it acts |
| No spending | a submit that looks like a payment is refused |
| Logins | passwords in the Credential Manager, sessions under DPAPI (`core/browser/vault.py`); typed only on their own site, never seen by a model; the first sign-in is the user's own, in the app's sign-in window |
| Who may browse | the user's own conversations; and clean-room browsing workers, which have no mail, calendar, memory or login |

Pinned by `tests/gate/test_browser_contained.py`,
`tests/gate/test_browser_consent.py`, `tests/gate/test_browser_files.py` and
`tests/gate/test_browser_logins.py`. `python -m core.browser check` proves
the sandbox from the side a page is on.

### The desk's microphone

Push-to-talk is core (step 7): the microphone's audio goes to OpenAI's
speech-to-text only while the user holds the button, or to the add-on's
local Whisper when a developer chooses it (`agent/worker/links._local_voice`),
which then never falls back to OpenAI. The backend asks too, not the app
alone: a press opens the microphone only with the app's live session, once
a passphrase is set, and never while the screen is locked
(`agent/worker/routes._why_not_listen`). Call notes are the other way sound
leaves: started from the tray's menu or Settings' Voice, they send what the
laptop plays (a call, a video), not the microphone, to the same
speech-to-text with no button held, a stretch at a time, until stopped, and
keep no sound (`voice/listen.Tap`). They need the voice add-on to cut the
sound into stretches, so v0.1, which does not offer it, refuses them
(`agent/worker/desk.tap`). Hands-free listening ("hey Steve") is not offered in v0.1
(`agent/worker/desk.offered`); in its own step it becomes the voice
add-on's, and its rules are frozen in `core/desk.py`:
- **The machine's own sound is not the user.** A wake word heard on the
  speakers' loopback as well as the microphone is ignored, and so is a yes
  said while Steve was speaking (`core/desk.wake_counts`,
  `core/desk.capture_counts`, `core/desk.approval_counts`). Steve never says
  its own wake phrase (`core/desk.never_spoken`). Its own words, heard back,
  are taken out before anything is judged (`core/desk.own_words_removed`).
- **Nothing before the phrase.** Until the wake detector fires, the room is
  heard by it alone, with about 1.6 seconds held in memory.
- **No sound is kept,** and the microphone is closed while the screen is
  locked or no one is signed in to the app (`core/desk.may_listen`).
- **Notes are not requests.** A dictation, a lecture or a call is transcribed
  into a note and summarised by a call with no tools. Only the user's own
  spoken turn can start notes of the room
  (`agent/worker/desk.room_refusal`).
- A live conversation opens only when asked for by name, and streams to
  OpenAI with storage off.

Pinned by `tests/gate/test_desk.py`.

## 4. New in the product

### 4.1 The local API token (built in step 4)

- **Made at each start.** Once it is listening, and before the first
  worker starts, the supervisor makes 32 random bytes and writes them,
  with the port and its pid, to `backend.json` in the run dir
  (`core/localapi.publish`). The file's access list names the
  current user alone, with inheritance off, so a data dir moved onto a
  shared folder does not widen it; so does the run dir's, so another
  account cannot move it aside and put its own file, and token, in its
  place (`core/localapi.owner_only`). A new token each start means a leaked
  one dies at the next restart.
- **A port the system chooses.** The supervisor binds 127.0.0.1 on a port
  the system picks at each start, for itself alone, and listens before the
  port is named anywhere (`core/worker_handle.listening`). So nothing can be on
  Steve's port before it, and two Windows accounts on one PC can each run
  Steve.
- **A worker's port is held from its spawn.** The supervisor binds each
  worker's port, the same way, before the worker starts, hands the socket
  to it, and keeps its own copy until the worker's shutdown
  (`core/worker_handle.listening`). So the token the supervisor sends
  while it waits for a worker can only reach that worker. signal-cli and
  the local decision model bind their own ports; each is trusted only once
  the TCP table shows the port is held by the process Steve started, or
  its children (`core/localonly.served_by`).
- **Required everywhere.** Every HTTP route and websocket of the supervisor
  and of every worker needs it: in an `Authorization` header, or, on a
  websocket, in the handshake's subprotocol, since a webview cannot set
  headers there. Never in a URL, which lands in logs. Compared in constant
  time. A refused request is logged without it.
- **One place.** The check is in `core/localonly.LocalOnly`, which is
  already outermost, so no route needs code of its own. Two requests pass
  without it, and neither reaches any other route: the proof, whose own
  route answers the proof and nothing else (`core/supervisor.sup_prove`),
  and a CORS preflight, which the supervisor's CORS middleware answers
  with headers alone.
  `tests/gate/test_local_token.py` lists every route and websocket of the
  supervisor and of a worker from the apps themselves, and proves each
  refuses without the token. A route added later cannot skip it.
- **The app's copy.** The app's Rust side reads the file and hands the token
  to its own windows (`backend` in `ui/src-tauri/src/lib.rs`). The browser's
  live view, once a page the backend served in a frame, is drawn by the app
  itself (`ui/src/LiveView.tsx`), since a frame cannot carry the token.
  Workers read the same file as they start.
- **The backend proves itself first.** A program could take the port a
  stale run file names, and the app would then send the token, the
  passphrase and the user's messages to it. So the app sends a fresh nonce
  first, and sends nothing else until the backend answers with an HMAC of
  the nonce and the port it listens on, under the token (`core/localapi.proven`
  for the backend's own tools). Something squatting the port cannot answer;
  one relaying the nonce to the real backend gets back the proof for the
  real backend's port, not its own. A proof is not kept for long: whatever
  the app sends (the passphrase, a yes, a key) goes to a backend proven
  just before, and a closed socket, the first sign of a restart, drops the
  last proof for the reads too (`ui/src/api.ts`,
  `tests/unit/ui/test_signin_screen.py`). The token a read carries dies
  with the backend that made it.
- **The other checks stay.** The token stops other accounts. `Origin` stops a
  page in a browser the user runs (`core/localonly.ALLOWED_ORIGINS`). `Host`
  stops rebinding (`core/localonly.refusal`).

### 4.2 App sign-in (built in step 9, with the passphrase built in step 4)

Step 4 built the part that guards money: the passphrase on a high-risk yes,
checked by the backend. Step 9 built the sign-in and Windows Hello.

- **Set up in onboarding:** the passphrase, then Windows Hello where the
  laptop has it (`ui/src/Setup.tsx`). The passphrase is never stored; the
  backend keeps its scrypt hash in the data dir, and limits wrong guesses.
  Hello's key is made by Windows under the name `steve.<install>.hello`
  (`core/identity.hello_key`); the backend keeps only its public half, an
  RSA key of 2048 bits or more, in `hello.json`, and registering or
  forgetting it takes the passphrase (`core/hello.py`). Both files are the
  current user's alone, as the run file is, whatever the data dir grants
  (`core/settingsfile.save`).
- **What it gates:** every view of the app, and approving in the app. Once
  a passphrase is set, the app shows the sign-in before anything else
  (`ui/src/SignIn.tsx`), with Hello asked for at once where it is set up.
  Signing in gives a session from the backend (`agent/worker/signin.py`),
  which keeps only its digest, in memory: a restart of the backend ends it.
  The shell holds the session for every window (`ui/src-tauri/src/signin.rs`)
  and keeps it alive every minute. An approval from the app needs a live
  session (`/actions/decide` refuses one without it, saying `needs:
  "signin"`). Signed out, the quick bar and the desk card show nothing of
  Steve's and send the user to the app.
- **When it ends:** when Windows locks, on sleep, and when the app quits;
  and after 30 minutes without input if the user turned that on in
  Settings (the `SIGN_OUT_IDLE` switch, off by default since 19 October
  2026, which the keep-alive's answer carries to the shell each minute).
  The shell judges each from Windows:
  - the lock is the change from unlocked to locked, so a laptop already
    locked when the app started, as in the tests, keeps the session it
    signs in with;
  - sleep is the wall clock running ahead of the unbiased clock, which
    stops while the laptop sleeps, by 10 seconds or more;
  - idle counts from the later of the last input and the sign-in.
  The backend also lets a session lapse after three minutes without its
  keep-alive, for a shell that died without saying so.
- **A high-risk approval asks again, every time.** Built in step 4 with
  the passphrase: it is typed with the yes, checked by the backend against
  its scrypt hash (`core/passphrase.py`), and bound to the card's id and a
  hash of its stored arguments (`core/monitor.accept_passphrase`). Five
  wrong tries lock it for 15 minutes, counted in its file, and nothing typed
  is not a try. Changing it takes the old one. Windows Hello does the same
  with a signature (`core/monitor.accept_hello`):
  - the backend gives a challenge naming Steve, the purpose and the card's
    id and argument hash, with a nonce used once and good for two minutes;
  - Windows signs it only after the user's face, finger or PIN;
  - the backend checks the signature against the registered key, and that
    the card still has the arguments the challenge named.
  Either way, a process that holds the token cannot approve a high-risk
  card on its own (decision 3). Until step 4 the app's yes needed no
  second factor. A module's card that needs the passphrase takes Hello's
  signature too, bound to the card and the hash of the files that were
  checked (`core/lifecycle.hello_binding`).
- **A forgotten passphrase** is reset in Settings with Windows Hello, where
  it is set up for Steve: the key's signature over a `reset` challenge
  stands in for the old passphrase, as it does at a yes, since the key was
  registered with it (`core/hello.reset_passphrase`). It lifts a lock too.
  Without Hello, it is reset with Windows' own sign-in, from Settings or
  from the sign-in page (`core/winsignin.prove`): Windows' credential
  dialog, the account's name filled in, the password checked by Windows
  (`LogonUser`), and the account signed in must be this one. Step 7 set
  that prompt aside, because the file's deletion proved as much; once
  deleting the file locks rather than unsets (2.5, step 11), it is the
  owner's way back. The password passes through the backend's memory for
  that check alone and is never kept.
- **What it is not:** encryption (decision 1). The sign-in guards what the
  app shows and approves, not the data dir, which the Windows account can
  read.
- **Notifications:** a toast can be read on the lock screen. Since step 4
  it shows the message while Windows is unlocked, and only what kind of
  message it is ("Steve: a reminder") while it is locked, or when Windows
  cannot say (`ui/src-tauri/src/toast.rs`). A toast made while Windows
  was unlocked keeps its text in Windows' notification history, outside
  the data dir. **Fixed in step 11:** every toast is in Steve's own group
  and expires after a day; ending the sign-in, for any reason, removes the
  group, and so does uninstalling (`--clear-notifications`). Only Steve's
  group: a build from a checkout shows its toasts under PowerShell's name,
  and the user's other toasts there are not Steve's to remove.
- **Accepted:** until the sign-in ends, or for a day, a toast's text is in
  the history, as any app's is. The Windows account is the boundary
  (2.3).

### 4.3 Channel trust tiers (built in step 4)

| Channel | Who can write in it | An ordinary card | A high-risk card |
|---|---|---|---|
| The app | the person at the machine | a click | the passphrase, again (Windows Hello too, with step 9's sign-in) |
| Signal: Note to Self on a linked device | the account's own devices | "YES WORD", or a reaction | the yes with an authenticator code; without one set up, the app only |
| Telegram: the user's own bot, paired to one private chat | whoever holds the Telegram account | "YES WORD", or the card's Yes button | as for Signal: the card has no buttons, and takes "YES WORD" with the code |
| The desk's voice | whoever speaks at the desk | a spoken yes | the yes with the code, spoken |
| SMS, runs nobody watches, anything else | anyone, or nobody | never | never |

The table is core's (`core/monitor.CEILINGS`), and `by`, recorded with each
yes, names the channel it really came from, so the yes is judged again by
it when the action runs.

On Signal and Telegram, a voice note is written down and then read, marked
as spoken (`channels/bridge.py`). It answers a card only by saying the
card's word: a spoken "yes" alone is asked about, since a recording of
anyone saying it is easy to come by, and the word is known only to someone
reading the card. The desk's voice always asks for the word. A high-risk
card still needs the code.

A reply decides a card only when it is plainly a yes or a no to it
(`agent/approvals.parse_reply`). A short reply that names a waiting card's
word but is not plainly an answer is asked about: Steve says which card it
is, its whole first line, and how to answer. That covers a question mark in
any script, any word beyond a few fillers, a yes and a no together, and the
word in look-alike letters ("yes maplе" with a Cyrillic е) or with a
character no one sees inside it, which is never an answer. A reply naming
two cards' words ("yes cobalt undo maple") decides neither, and asks for
one at a time. Polite words count only at a reply's ends, and never beside a
lone verb: "send thanks" is asked about. A refusal is read after Unicode
folding, however its apostrophe falls. "pending" is answered by the code:
each waiting card's word and whole first line.

A typed bare "yes", with no word, decides a card only when exactly one is
waiting and the card is still the last thing said in the chat it was sent
to (`agent/approvals.moved_on_since`): the user has written nothing
anywhere since, and Steve has said nothing in that chat since the card was
asked, the reply of the turn that asked it included. Otherwise Steve asks
"Is that about MAPLE?" instead. **Accepted:** the chat's times are SQLite's,
to the second, so a message of the user's in the same second as the card
does not count as after it.

A reaction or a button answers only a message that was sent as a card, and
names that card's own word, on its last line (`Channel.sent_card`,
`channels/bridge.reaction_as_answer`). A card's line inside a reply, or
inside another card's description, answers nothing
(`tests/gate/test_who_may_approve.py`). It also carries that card's id, and
decides that card or none (`approvals.parse_reply`): a word is given to a
new card once its own is answered, so a late press would otherwise decide
the newer card. A card's words are ones no ordinary message says
(`core/consent.TOKEN_WORDS`): minerals, birds and beasts, and parts of
buildings and machines, not foods, places or verbs. The next card's word is
drawn at random from the older half of the free words, a word never used
counting as the oldest, or from every word never used while they are more
than half (`core/consent.token_for`). So it cannot be told in advance, and
a "YES MAPLE" typed late cannot meet a newer MAPLE until at least half the
other words have had a turn.

**The model never learns a waiting card's word.** Every request to a model
has each waiting card's word replaced by "[the card's word]": its history,
the tool results, and the notes of cards it asked for
(`agent/provider.Unworded`, `agent/cardwords.mask`). It reads open and
withdrawn cards as "[card]" (`agent/closing.py`), and its receipt for a
card carries no word (`agent/approvals._receipt`). So the model can name a
card only by guessing, with one exception, accepted below: what the user
writes with the word glued or changed ("yescinder", "cinders"), and any
image, such as a screenshot of a card, reaches it as written.

**Only a card says its word on the phone, and only its own.** What Signal
and Telegram send is checked as it is sent, after Markdown is turned into
plain text (`channels/plaintext.shown`, `agent/cardwords.sendable`):

- a card keeps its own word, and has every other waiting card's word taken
  out;
- anything else has every waiting card's word taken out, but for the
  code's own lines naming a card (the briefing's list, its closing
  question, and the reply asking which card was meant), matched exactly;
- what no one sees is not sent, except the joiners an emoji needs, and a
  blank filler is sent as the space it shows.

The word is found however it is dressed: case, look-alike letters
(Cyrillic, Greek, full-width, mathematical, small capitals), accents,
spacers and joiners, Markdown left behind, keycaps, symbols. A spoken reply
is redacted the same way (`agent/worker/answering.voice_reply`); its live
transcript on the desk is not, and need not be, since the model does not
know the word.

**What a card shows that someone else wrote is on one line, after a
label.** An event's title, a mail's To, Cc, Bcc, subject and attachments, a
task, a sheet's range: each goes through `agent/approvals.one_line`, so
none can start a line that poses as the card's own. **The code's lines
naming a card carry its whole first line, never cut short:** the list, the
question and the reply asking which card was meant (`agent/cardwords.listed`,
`agent/approvals._which`). Telegram keeps each such line whole in one
message, and a line too long for any message is sent with the word taken
out (`channels/telegram._chunks`). A card's first line does not always say
whom its yes reaches (accepted, below).

**UNDO stops a yes during its hold, and not once its call has begun or may
have.** `agent/approvals._calling` marks the card as the call starts, in
the same write that checks it is still to run. An UNDO after that, or more
than the hold and five seconds (`agent/approvals.CALL_MARGIN_SECONDS`)
after the card was claimed, marked or not, is told "too late", and the
card's status is what the call did. One exception, accepted below: a card
claimed by a worker that does not mark its call can still be taken back
for five seconds after that call may have begun. What a call spent counts against the
day's limit however it ended (`agent/approvals.spent_since`).

**A routine's or goal's fixed targets are plain ASCII** (`core/monitor`).
An address in another alphabet is refused, since mom@exаmple.com, with a
Cyrillic а, shows exactly as the real one. An international domain is
written in its xn-- form.

**An answer from Signal or Telegram counts only over the link it was read
over, if that is still up as it is recorded:** for Signal, the same
bridge; for Telegram, the same bridge and the same pairing
(`agent/worker/links.answers_from`, `channels/bridge.READ_OVER`). An
answer read before an unpair does not count, even once a chat, or the same
one, is paired again. A channel name Steve does not know never counts.

**Accepted:**

- A card's word can be guessed. A reply naming a word no card has yet is
  not redacted, and could pose as a card that later draws that word. The
  draw makes that one in 23 at best, or one in as many words as have never
  been used, if more.
- The model is not sent a waiting card's word as a word, but it is sent the
  word glued to other letters or changed ("yescinder", "cinders",
  "cinder1"), and images, such as a screenshot of a card, unmasked. Masking
  inside words would garble ordinary ones ("ember" in "remember"). What the
  phone is sent is still redacted, so the model can use such a word only
  after its card stops waiting.
- A card's first line does not always say whom its yes reaches, and the
  list, the question and the reply asking which card show only that line:
  a new event (its invites are on line 3), a draft's send (To on line 2),
  deleting a draft, deleting an event (its guests on line 3), a grouped
  card ("N on one card"), and a card asked again. The whole card says it.
- An UNDO can take back a card after its call may have begun, for up to
  five seconds, when the worker that claimed it does not mark the call's
  start: an old worker during a swap, or a card running when migration 25
  came. Such a worker calls at the end of the 30-second hold, and UNDO is
  accepted until 35 seconds, so an UNDO in that gap says "Stopped" though
  the mail may have gone.
- A note or answer passed on to an approved goal is not bound into its
  yes. In plain text, it can ask the goal for something new within the
  targets its card allowed ("also send the team the quarterly figures"), and
  the goal's sessions take it as the user's instruction, with no new card.
  It may hold nothing no one sees (`agent/goals.answer`).
- The app's own inbox and chat show the outbox's notices as written; the
  redaction is on the way to the phone.
- A word spread over several lines, or with more than three characters
  between its letters, is not looked for; nor are look-alike letters from
  scripts the fold does not know (Cherokee, Lisu, enclosed or squared
  letters).

- **Pairing is in the app.** Signal links by a QR code. Telegram pairs by
  six random digits the app shows, sent to the user's bot within ten
  minutes: the first private chat to send them is paired, and five wrong
  codes withdraw them (`channels/telegram.py`). Every other chat, a group or
  another bot is ignored, logged once per chat. A button press counts only
  from the paired chat and the person in it.
- **Only the app changes who may approve:** pairing and unpairing a channel,
  sign-in, the authenticator, a module's grants (decision 5). A remote
  channel can still unpair itself, which is how a user cuts off a stolen
  phone. Unpairing stops the chat answering first, then takes back what it
  set going (`agent/approvals.forget_chat`): its waiting cards are
  withdrawn, the routines and goals approved from it are paused, and its
  yeses, held, running or standing, are undone. A routine turned on again
  in the app asks before each send. An answer from a chat unlinked while it
  was being read does not count (`agent/approvals.decide`,
  `agent/worker/links.answers_from`), and a yes undone while its action
  runs stays undone (`agent/approvals._finish`). **Accepted:** an undo
  cannot unmake a call already made.
- **A user can lower a channel's ceiling,** for example Telegram for
  notifications only (`APPROVE_ON_SIGNAL`, `APPROVE_ON_TELEGRAM`,
  `APPROVE_BY_VOICE` in `core/knobs.py`). `core/monitor.ceiling` takes the
  lower of the setting and the table, so nothing written there raises one.
- **The authenticator's secret is shown once, in the app,** and never sent
  to any channel (`agent/worker/approvers.py`). It is kept only once a code
  made from it comes back, and replacing it in the app takes the
  passphrase, since whoever holds it can spend from the phone. The command
  line's `python -m core.totp setup --replace` takes none: it runs as the
  user, whom 2.3 leaves out of scope.
- **Telegram is not end-to-end encrypted.** Its servers read every card and
  every reply (section 6). Setup says so plainly and recommends Signal
  (decision 8).

### 4.4 Modules: capabilities and the sandbox (step 6)

- **Kinds.** A skill is text. A tool is Python written against a small SDK,
  its only import surface. A routine or source is configuration for what
  Steve already runs.
- **A manifest declares what it needs:** network domains, its own storage,
  connector reads, effects. The user approves the list in the app, on a card
  that shows it whole. A new version that asks for more asks again, and so
  does one whose words for the model changed: its card shows them as they
  were and as they are.
- **Isolation.** A less-privileged
  AppContainer with only `registryRead`, falling back to a plain
  AppContainer. Either way: no network, no loopback, no Credential Manager,
  and files only in its own folder. A job object caps memory, CPU, processes
  and wall-clock time. The environment is scrubbed. No admin is needed.
- **The broker is the only way out.** It speaks JSON-RPC over stdin and
  stdout, and:
  - fetches only HTTPS from declared and approved hosts, under the egress
    rules, re-checked on every redirect, with size, time and rate caps;
  - reads connectors only as declared; text that comes back to a model is
    screened as outside text;
  - turns `ctx.act()` into `agent/actions.act` on a channel nobody watches,
    so a module's effect is a card or inside an envelope, and it can
    never answer a card;
  - puts credentials into the requests it makes itself. The module never
    holds one;
  - drives a desktop program for it (`com.call`), only the ProgIDs and
    member paths its manifest lists. The program runs outside the
    AppContainer, with the user's rights, so what may be called there is
    the wall (`steve_sdk/manifest.py`, held by `core/modctx.ModuleCom`):
    - a program made to run things is never opened (`DENIED_PROGIDS`: the
      script host, the shell, script controls, file scripting, the HTTP
      and XML objects, ADO, DAO, CDO, Windows' speech, MMC, the Task
      Scheduler, WMI and Internet Explorer);
    - a member that runs code, or opens what another program or an
      address names, is never reached, not even in `perform`
      (`NEVER_MEMBERS`: macros, formulas, DDE, the VBA editor,
      `CreateObject`, `FollowHyperlink` and the like);
    - in a program Steve knows (Outlook, Excel, Word and PowerPoint:
      `KNOWN_PROGRAMS`), a method it knows only reads is called freely;
      any other call, and any value set but a view's, is an action;
    - a module may name any other program, and its approval card says
      Steve does not know it; every call or value set there is an action;
    - an action is reached only in `perform`, on a card's yes, and the
      approval card names each. A member that sends, saves, deletes or
      opens is an action wherever it is (`ACTION_MEMBERS`). Members are
      matched in any case, since COM finds a member in any case.

    It keeps the program's objects on a thread of its own and hands the
    module plain values and handles. Outlook's own security prompt can
    still hold a call; the call then fails in 30 seconds, saying why.
  - **Accepted:** reading a value runs whatever the program does to answer
    it, and an object that does not describe its members may answer a
    read of a method's name by calling it. In a program Steve does not
    know, the user's yes to the module is their yes to that.
- **An MCP server Steve runs** (`core/sandbox/servers.py`). An npm package,
  named at one exact version at least seven days old, installed by npm in
  a container of its own, as the registry stood when that version came
  out, with scripts off. Its tree's hash is on the card and checked before
  every start; a new version or tree asks again.
  - It runs in a container of its own for each module and account, and
    holds its own sign-in in its own folder: the one place on disk a
    secret may be (`tests/gate/test_no_secret_on_disk.py`).
  - Its only network is a named pipe to a relay in core
    (`core/sandbox/relay.py`). The relay connects only to the hosts the
    manifest names, on 443, checks the name the server's TLS asks for, and
    refuses an encrypted one. A server that may reach any public host
    needs the passphrase.
  - Only a module Steve ships names the package of a server its
    connector runs (a profile's `[package]`). Admission refuses one from
    outside (`core/lifecycle._runs_a_server`), and the engine starts none
    for a module without the backend tier (`core/engines/mcp.py`): its
    hosts would be on no card, and its version's age checked by no one.
  - What it writes to its error stream is capped, and scrubbed of
    credential shapes before it is kept.
  - It is stopped when idle, when its module is off, and when the worker
    gives up its duties; removal deletes its files and its container.
- **Who starts one.** The user, by asking in the app or a conversation; or a
  night proposal the user accepts. The coder's workspace is the module's
  folder, its tests run in the same sandbox, and admission is the user's
  card.
- **Lifecycle.** Watched like an upgrade, disabled automatically when it
  fails, revocable, and rolled back to an earlier version from the app.
- **Sharing** (`core/sharing.py`, from the app alone):
  - An export is one version's code folder: never the module's state,
    accounts or secrets. A file that names the user, or someone they write
    to, by full name or address, stops it, saying where.
  - An import is unpacked by a strict extractor into a quarantine folder.
    It refuses an absolute path, `..`, a link, a name Windows gives a
    device or changes on the way to disk, and a zip over the module caps
    or unpacking to over a hundred times its size, before anything is
    written.
  - The module then goes through admission as one from outside, untrusted,
    its hash on the card. Its new user approves it afresh and gives it
    their own secrets. No signatures in v0.1.
- **Without an AppContainer, modules' code does not run** (decision 6).
  Skills still do.
- **Accepted** (S1's residual risks):
  - registry keys under the user's hive that every app package may read;
  - world-readable system files;
  - the kernel's window-manager surface;
  - the broker itself, the one piece that parses what a module says. It
    is kept small, and step 11 fuzzes it.
  - an MCP server Steve runs holds the account's sign-in itself, and can
    send what it reads to any host its manifest names. The card names
    those hosts, and the address it signed in as is checked before its
    tools are pinned.

### 4.5 Install integrity and updates (step 8)

- **Install per user, with no admin,** to a folder of its own that is not
  inside the data dir (decision 7). The bundled Python and Steve's code live
  there; data lives in the data dir; neither holds the other.
- **No code from the data dir runs outside a container.** A module's code,
  and Node with the MCP servers it runs, run only in an AppContainer (4.4).
  The backend tier, Steve's own process, holds the built-ins alone, which
  ship in the install folder; the loader refuses a module from outside
  that asks for it, and says why. A later need for it must pass a stricter
  screen, written here now and built with its tests when a developer adds
  the first need:
  1. a named need, from a fixed list that grows only by a developer's
     change. A protocol on raw sockets is not one: code in the backend
     cannot be held to its declared hosts;
  2. the import rules (`core/sandbox/imports.py`): only `steve_sdk`, the
     standard library and what the need allows, and no back doors (ctypes,
     subprocess, `exec`, `importlib`, the environment);
  3. a second model's review of the code, keyed to its hash;
  4. its tests passing in the sandbox first, with the need faked;
  5. the user's approval with the passphrase, for every version;
  6. the backend loading it only if its files hash to what was approved,
     the one exception this rule would ever make.

  And for Steve's own code:
  - Python starts cut off from the user's: no `PYTHONPATH` and no user
    site-packages. An install's supervisor starts with `-I`, which keeps
    its current folder off the path too. Workers start with `-E -s`, in
    the folder of Steve's own code, which is then on their path
    (`core/worker_handle.spawn_worker`);
  - add-ons (signal-cli and its Java runtime, and the local decision
    model's runtime, Ollama; the voice add-on is not offered in v0.1) go
    to an
    add-ons folder beside the install, checked against a SHA-256 hash fixed
    in the signed release before they run. Since step 4, signal-cli and its
    Java are downloaded there (`paths.addons_dir`) when the user chooses
    Signal, pinned in `core/addons.py` (frozen): each archive by its hash
    before it is unpacked, and the files it unpacked to again before every
    start (`addons.verify`). A file changed since is not run, and the app
    says so. Since step 11, Ollama is installed the same way
    (`core/decider.py` `RUNTIME_ADDON`), and checked before every start of
    its server; a changed one is not run, and the screen decides without
    it. It had been unpacked into the model cache, in the data folder, with
    only its presence checked.
- **A manifest of every file under `backend\` and `runtime\`,** the
  bundled Python and Steve's code, with its SHA-256, signed with the update
  key (`ui/src-tauri/src/integrity.rs`). The app's own executable and the
  add-ons are not in it.
  - Before the backend starts, the app checks the manifest's signature,
    every file, and that no module, DLL or `.pth` under `backend\` is one
    the manifest does not name (`before_start`). A full pass takes about 3
    seconds. Until step 11 only the
    files Windows loads as code were checked first, and the rest while the
    backend ran, so a changed `.py` file ran once before the check caught
    it.
  - A mismatch stops the backend, names the file, and offers a repair: the
    same release downloaded again through the updater. Until the update
    address is set (step 12), a repair ends in "Updates are not set up in
    this build."
  - What it catches: a corrupt file, a half-applied update, a file an old
    version left behind, an add-on or module writing where it must not.
  - What it does not: a process running as the user, which could rewrite
    the checker itself (2.3).
- **Updates** come from signed releases on GitHub over HTTPS, through
  Tauri's updater, from step 12, which gives it the address to ask
  (`ui/src-tauri/tauri.conf.json` has none, so a check ends in "Updates are
  not set up in this build."). It refuses an unsigned release and an older
  version. Before the installer runs, the app stops the backend and waits
  up to ten seconds for its processes to end, ending what is left meanwhile
  (`ui/src-tauri/src/backend.rs`). Past that, it logs the fact and the
  installer runs anyway; its pre-install step stops the install if old
  files are still held open, rather than mix two versions.
- **The signing key** is made offline and never enters CI or the repo.
  Losing it strands every install on its version; leaking it hands them all
  over. Authenticode signing, for SmartScreen and Smart App Control, costs
  money and is a separate decision.

## 5. Secrets

| Secret | Where | Status |
|---|---|---|
| The OpenAI key | the Credential Manager, service `steve` (`core/identity.KEYCHAIN`) | holds |
| An account's secret fields: mail passwords and app passwords, a feed's private link, an MCP server's secret, Google's OAuth client (its id, secret and project, never the file) | the Credential Manager, under `steve-modules/` by the module's and the account's keys (`core/credstore.py`), written only by setup (`agent/accounts.py`); a module reads only the names its manifest declares | holds (step 5) |
| A Google account's sign-in | its refresh token alone, and when it ends, in the Credential Manager beside the client (`agent/accounts.sign_in`, kept only when the address that signed in is the account's); the hourly access token in memory (`modules/google_cloud/auth.Account`) | holds (step 5); `google_token.json` and `google_granted_at.json` are gone |
| The Telegram bot token | the Credential Manager, `TELEGRAM_BOT_TOKEN`, checked with Telegram before it is kept; never logged, and cut out of the errors that would show it (`channels/telegram.BotApi`) | holds |
| The authenticator's secret | the Credential Manager, service `steve`, `APPROVAL_TOTP_SECRET`; shown once, in the app | holds |
| Browser logins | the Credential Manager, service `steve-browser-logins` (`core/browser/vault.py`); sessions under DPAPI | holds (add-on) |
| signal-cli's account keys | signal-cli's own files in the data dir, since it cannot use the Credential Manager; plain to whoever reads a disk that is not encrypted (2.5) | holds |
| The local API token | a user-only file in the run dir, new each start (`core/localapi.publish`) | holds |
| The passphrase | never stored; its scrypt hash is in the data dir (`core/passphrase.path`) | holds |
| An MCP server Steve runs: its own sign-in | its home in the data dir, as the server wrote it, deleted with it (`core/sandbox/servers.py`). No other container may read it; the user's own programs can, and so can whoever reads a disk that is not encrypted (2.5) | holds |

**Rules:**
- **Never in the database, a log, a card, a prompt or a model's context.**
  `core/secrets.status` reports whether a key is set, never its value. Step
  10's fake world plants a canary value in every secret, and fails if one
  appears in the database, the logs, or a request to the fake OpenAI. Every
  log record the worker and the supervisor make is scrubbed as it is made
  (`core/logscrub.py`): each secret Steve has read or stored, by value, and
  anything named or shaped as a token, in the message and the traceback. A
  dead worker's last lines are scrubbed again before they are kept, since
  what it printed past logging is in its log as written.
- **The installed app reads only the Credential Manager** (decision 2).
  `core/secrets.get` falls back to the environment, which suits a checkout
  (development, CI). But the environment is shared by every program the user
  runs, and can hold another program's key.
- **Setup recommends an OpenAI project key with its own monthly budget,** set
  in OpenAI's dashboard, and restricted to the endpoints Steve uses where
  OpenAI allows it. A leaked key or a runaway loop is then capped by OpenAI as
  well as by Steve's own caps (step 3).
- **An app password opens the whole mailbox and calendar.**
  Gmail's setup says so, and
  links to where it is revoked (`modules/gmail_password/manifest.toml`).
  Since step 11 every secret's field links to where it is revoked, from
  the manifest's `revoke`, a provider's own before its module's
  (`modules/imap/manifest.toml`), in setup and when a new password is
  entered.
- **Modules never hold a secret** (4.4): the broker puts an account's
  secret into the request it makes for the module. The one exception is an
  MCP server Steve runs, which keeps the sign-in it was given in its own
  home (`tests/gate/test_no_secret_on_disk.py`, `tests/gate/test_local_servers.py`).
- **Uninstalling** removes what Steve put in the account outside its own
  folder (`core/uninstall.py`): this install's AppContainer profiles, its
  `SteveWake.<install>` task when it runs this install's Python, and the
  containers labelled with its install id. With "Delete the application
  data" ticked it also deletes the data dir, in its default place only, the
  Windows Hello key, the browser's images unless another install's profiles
  show it may use them, and the credentials Steve made: `steve`, `<key>@steve`
  for each key Steve writes (`core/secrets.KNOWN_KEYS`, which alone may be
  written there), `steve-browser-logins`, `<site>@steve-browser-logins`, and
  each name starting `steve-modules/`. The keyring library cannot list what a
  service holds, so the uninstaller lists every credential from Windows and
  matches each name against those (`core/uninstall.our_secret`). It never
  deletes by `steve*`, which another program's names could match. An update
  runs none of this (step 8).

## 6. What leaves the machine

Each row names its hosts. `tests/gate/test_hosts_named.py` fails on a host
named in shipped code that no row names, a module's manifest included (its
servers, its presets' and its links), and on a library a module declares
without saying what it reaches (`LIBRARIES` there). A host signal-cli
reaches on its own, or one the user gives (a feed, a page, a server), is
written here by hand.

| Destination | Hosts | What goes | Controls |
|---|---|---|---|
| OpenAI | `api.openai.com` | conversation turns; what tools read, as the screen allows; memories recalled; memories, facts, messages and recall queries, to embed (`core/embed.py`); flagged items, to be extracted; push-to-talk audio, voice notes, and replies to speak; web search queries; a live conversation's audio; Steve's own code and the change asked of it, for the coding pipeline (paused) | the user's key; `store: false`; spend caps; Zero Data Retention if their account has it |
| Brave Search | `api.search.brave.com` | web search queries, each the query and the user's country | only with a Brave key; spend caps; OpenAI's search when Brave has no answer (`core/websearch.py`) |
| OpenRouter, for Jev (TypeSafe's decision model) | `openrouter.ai` | the questions of the screen, the critic, the browser's click check, task splitting and the meeting follow-up, with the item, action, page or meeting each is about; a look at the key's credit | the user's key; every request routed only to endpoints that keep and collect nothing (`core/decide.py`); spend caps |
| Their mail provider | Gmail's API (`*.googleapis.com`) and sign-in (`accounts.google.com`); the IMAP and SMTP servers of Gmail (`imap.gmail.com`, `smtp.gmail.com`), iCloud (`imap.mail.me.com`, `smtp.mail.me.com`), Fastmail (`imap.fastmail.com`, `smtp.fastmail.com`) and Yahoo (`imap.mail.yahoo.com`, `smtp.mail.yahoo.com`); the servers the user types for other mail; for Outlook.com, an MCP server: one Steve runs in a container of its own, which reaches Microsoft's sign-in (`login.microsoftonline.com`) and Graph (`graph.microsoft.com`) and nothing else, or one the user runs on this computer, which then reaches Microsoft itself, not as Steve | sign-in; reads; drafts; mail sent after a card | TLS, and over IMAP and SMTP never in the clear: TLS from the first byte, or STARTTLS refused if not offered (`core/engines/imap.py`); a server the user typed kept to the kind of address it had at setup, public, at home or on this computer (`egress.pin_kind`); a server Steve runs reaches the network only through its relay, which opens a connection to the hosts its manifest names, on 443, and checks the name the server's TLS asks for (`core/sandbox/relay.py`); their own account's controls |
| Their calendar provider, and ICS feed hosts | `*.googleapis.com`; the CalDAV servers of Gmail's legacy address (`www.google.com`), iCloud (`caldav.icloud.com`, which answers with the account's own server under `*.icloud.com`), Fastmail (`caldav.fastmail.com`) and Yahoo (`caldav.calendar.yahoo.com`); the CalDAV server the user types; the feeds the user gives | reads; new events; changes to events without guests, each on condition that the event is as it was read, and others after a card | HTTPS only; an address a CalDAV server returns on a host the module did not declare is refused before the password goes there (`core/engines/caldav.py`); a typed server kept to the kind it had at setup |
| Their file provider (Google Drive, with their own client) | `*.googleapis.com` | reads; changes after a card | the same |
| Account pages, opened by the user | Google Cloud's console (`console.cloud.google.com`), Google's account pages (`myaccount.google.com`), Apple's (`account.apple.com`), Fastmail's (`app.fastmail.com`), Yahoo's (`login.yahoo.com`), Microsoft's device sign-in (`microsoft.com`), OpenAI's billing page (`platform.openai.com`), an MCP server's instructions (`github.com`), and Docker Desktop's download page (`www.docker.com`) | nothing from Steve: a link the user opens in their own browser, from a module's setup or health, from setup's Docker page, or from a note's fix ("Top up", when OpenAI's balance is used up), to set up a client, make an app password, sign a server Steve runs in with the code it shows, take Steve's access back, or fix their account (an API not turned on, a test user missing) | a page of their own account; the app opens only an https link, in their browser (`open_link` in `ui/src-tauri/src/lib.rs`); health keeps only an https link (`core/health.py`) |
| Outlook desktop | none: Outlook is on the machine | nothing new: Steve talks to Outlook on the machine, which syncs as it always does | Outlook's own |
| Signal | Signal's servers (`*.signal.org`), as signal-cli reaches them | cards, briefings, replies; end-to-end encrypted, with metadata seen by Signal's servers; the photos and voice notes the user sends come back from them | pairing in the app |
| Telegram | `api.telegram.org` | the same, readable by Telegram's servers; the photos and voice notes the user sends come back from them | pairing in the app; a plain warning |
| Websites | the ones the user asks about, a search returned, or something Steve read gave whole; in a conversation the user is in, before it has read outside text, any address its model writes; in the browser, any address its model opens or a page links to (2.1) | fetches of those pages: the address, and the machine's IP address | `core/egress.py`; the browser's gate |
| Weather and news services | `api.open-meteo.com`, `geocoding-api.open-meteo.com`, `api.weather.gov`, `news.google.com` | to Open-Meteo or weather.gov, the home place's coordinates; to Open-Meteo's geocoder, the places named in calendar events, to find trips; to Google News, the topics the user follows | only once the user gives a home place or topics (decision 9) |
| Hosts a module declared | the ones it declared | what the module sends, through the broker; what an MCP server Steve runs sends, through its relay | approved per module; a server that may reach any public host needs the passphrase |
| npm's registry | `registry.npmjs.org` | for an MCP server Steve runs: the package's name, to learn when its version was published, and the downloads of it and its dependencies, by npm in a container of its own (`core/sandbox/servers.py`) | only once the user adds such a server or chooses that Steve runs one; a version at least 7 days old, installed as the registry stood then, scripts off, its tree hashed onto the card and checked before every start |
| GitHub | `github.com`, and the hosts its downloads come from (`objects.githubusercontent.com`, `release-assets.githubusercontent.com`) | downloads of signal-cli and its Java when the user chooses Signal (`core/addons.py`); update checks (step 12; a build without the update address asks no one): the app's version and the machine's IP address | each download checked against its pinned hash before it is unpacked, and its files before every start; signed releases |
| Docker's registries, and PyPI | `mcr.microsoft.com` (the browser's image), Docker Hub (`*.docker.io`, Python's image); PyPI (`pypi.org`, `files.pythonhosted.org`) for the packages built into them | base images, pinned by digest: the browser's, and Python's for running code; the Python packages each image's build installs, pinned by hash (`core/images.py`) | only once the user switches on the browser or running code (`core/knobs.py`) |
| LinkedIn | `www.linkedin.com` | through the sandboxed browser, as the user: the profiles, companies, searches and jobs they asked about | the browser switch and a login they saved; read-only and paced (`agent/toolset/linkedin.py`) |
| A trace collector | the one `STEVE_TRACE_ENDPOINT` names | each model call's shape; its text only to a collector on the machine itself (`agent/tracing.py`) | only with `STEVE_TRACE_ENDPOINT` set, for developers |

Telnyx's texting, the stretch goal, is not built: it waits until after
v0.1.

**Set up by hand,** a checkout also reaches:

| Destination | Hosts | What goes | When |
|---|---|---|---|
| Hugging Face | `huggingface.co`, and the hosts its downloads come from (`*.huggingface.co`, `*.hf.co`) | model downloads: the local injection reader's (`core/detector.py`), the voice models' (`voice/turns.py`), the wake word's training data (`voice/wake_train.py`); the address, and the machine's IP address | `python -m core.detector setup`, or Install on the app's What Steve may do; the voice add-on's first use; `python -m voice.wake_train --download` |
| GitHub and Ollama's registry | `github.com`, `registry.ollama.ai` | the local decision model's runtime and model (`core/decider.py`) | `python -m core.decider setup`, or Install on the app's What Steve may do |
| OpenSLR | `www.openslr.org` | LibriSpeech's readers, as the wake word's training data | `python -m voice.wake_train --download` |
| Node.js | `nodejs.org` | Node for the module runtime, checked against its pinned hash before it is unpacked (`core/sandbox/runtime.py`); an install ships it instead (step 8) | `python -m core.sandbox build` |
| httpbin | `httpbin.org` | the sandboxed browser's check by hand: a form, its headers | `python -m core.browser` |

**Nothing else.** No telemetry: Steve sends no analytics, usage counts or
crash reports.

## 7. Decisions

Settled on review, 11 October 2026: each recommendation was accepted. Where
each one is built is in the step plans.

1. **Data at rest: rely on Windows.** The profile's access rules, plus
   BitLocker or Device Encryption, which setup checks for. Secrets go in
   the Credential Manager (section 5); the database is not encrypted. A key
   kept on the same machine, for the same user, adds little to BitLocker,
   and an encrypted SQLite build would have to carry sqlite-vec.
2. **The installed app reads secrets only from the Credential Manager.** The
   environment fallback stays for checkouts.
3. **The backend checks a high-risk yes from the app,** by a Windows Hello
   signature bound to the card, or the passphrase. The alternative, a prompt
   in the UI alone, is skipped by anything that holds the token.
4. **The authenticator is optional.** Without it, high-risk cards are
   answered in the app only. With it, also on Signal and Telegram.
5. **What is high-risk in v0.1.** With calls cut, only `purchase` is left,
   and payments are not built. Decided:
   - high-risk stays "money";
   - app-only, never remote even with a code: pairing a channel, changing
     sign-in or the authenticator, and a module's grants;
   - nothing else. Each addition costs a re-ask, and a re-ask asked often
     stops being read.
6. **Without an AppContainer, modules' code does not run.** S1 found no
   admin-free fallback that blocks the network.
7. **Install to `%LOCALAPPDATA%\Programs\Steve`,** per user. S2's layout put
   the backend in `%LOCALAPPDATA%\Steve\backend`, which is the data dir, so
   code and data would share a folder. A per-machine install would protect
   the code from the user's own processes, but needs admin, and those
   processes are out of scope (2.3). The installer's folder page refuses
   the data folder, a folder inside it and one that holds it, and the
   backend will not start when the two overlap (`core/paths.overlap`), for
   a silent install or a data folder set by `STEVE_DATA_DIR`. Until step 11
   only the data folder itself was refused.
8. **Telegram is allowed, with a plain warning** that its servers read
   everything. Signal is the recommended channel.
9. **Weather and news stay, off until the user gives them something.** They
   send a location, the places in calendar events, and interests to
   Open-Meteo or weather.gov, and to Google News. The plan's list of what
   leaves the machine missed them; they are in the table now.

## 8. The checks that hold it

**Today (holds):**
- `tests/gate/test_every_tool_is_classified.py`,
  `tests/gate/test_workers_stay_private.py`, `tests/gate/test_lost_update.py`,
  `tests/gate/test_writes.py`: the one door, with no tool unclassified, a
  worker kept to what is private and reversible, a write without a
  condition asked unless Jev is sure, and every write held to one exact
  target;
- `tests/gate/test_approval_binding.py`, `tests/gate/test_cards.py`,
  `tests/gate/test_high_risk_approval.py`, `tests/gate/test_call_policy.py`:
  approvals;
- `tests/gate/test_injection_screen.py`, `tests/gate/test_screen_enforces.py`,
  `tests/gate/test_injection_containment.py`,
  `tests/unit/test_watched_reading.py`: outside text, and egress; a
  module's and an MCP server's answers and errors in
  `tests/unit/test_modtools.py`, `tests/unit/test_mcp_servers.py` and
  `tests/unit/test_modconnect.py`;
- `tests/gate/test_who_the_user_is.py`: only the user's own sources write
  who the user is (name, pronouns, home town, occupation), and only theirs
  are read;
- `tests/gate/test_critic.py`, `tests/gate/test_routines.py`,
  `tests/gate/test_goals.py`, `tests/gate/test_mailbox.py`: unattended runs;
- `tests/gate/test_what_review_protected.py`, `tests/gate/test_knobs.py`: the
  frozen rules;
- `tests/gate/test_browser_contained.py` and the browser's other gate tests,
  with a session run only from the images Steve built, by ID, on bases
  pinned by digest and packages pinned by hash, as the code tool's are
  (`tests/unit/test_code_exec.py`);
  `tests/gate/test_desk.py`;
- `tests/gate/test_identity.py`, `tests/gate/test_paths.py`: Steve never
  reaches another install's port, keychain or data;
- `tests/gate/test_local_token.py`: every route and websocket of the
  supervisor and of a worker, listed from the apps themselves, refuses a
  request without the token, and the proof is bound to the port (the app's
  side is tested in `ui/src-tauri/src/lib.rs`); the run file, its folder,
  `passphrase.json` and `hello.json` are the current user's alone;
- `tests/unit/test_worker_port.py`: a worker's port is held from its spawn
  until its shutdown; `tests/unit/test_port_owner.py`, with a squatter in
  `tests/unit/test_fake_signal_cli.py` and in
  `tests/gate/test_local_decider.py`: signal-cli and the local model are
  trusted only on a port they hold;
- `tests/unit/ui/test_signin_screen.py`: what the app sends goes to a
  backend proven just before, and a backend that restarted is proven again;
- `tests/unit/test_push_to_talk.py`: a press opens no microphone without the
  live session, once a passphrase is set, or while the screen is locked;
- `tests/unit/test_signin.py` and the tests in `ui/src-tauri/src/signin.rs`:
  the sign-in, Windows Hello, the count of wrong guesses, and what ends a
  session (Windows locking, sleep, idle time);
- `tests/unit/test_setup.py`: setup says plainly whether the system drive is
  encrypted (2.5);
- `tests/unit/test_uninstall.py`: uninstall takes only what this install
  made, and nothing another install or program did;
- `tests/gate/test_who_may_approve.py`: each channel's ceiling, a round trip
  on each channel, a high-risk card refused on each without its second
  factor, the app's included, and no tool reaching what decides who may
  approve;
- `tests/gate/test_hosts_named.py`: every host named in shipped code is in
  section 6's table;
- `tests/gate/test_no_secret_on_disk.py`: no account's secret on disk
  outside the Credential Manager;
- modules (step 6): `tests/gate/test_module_sandbox.py`, each wall tried
  from inside -- the app's files and another module's folder, the
  Credential Manager and the user's files, the network and this
  computer's ports, a second process, memory and time;
  `tests/gate/test_module_broker_sandbox.py`, each way out through the
  broker -- an undeclared host or secret, a redirect;
  `tests/gate/test_module_imports.py`, imports past `steve_sdk` and the
  standard library; `tests/gate/test_local_servers.py`, an MCP server
  Steve runs reaching its hosts and nothing else;
  `tests/gate/test_module_com.py`, COM held to the manifest and the
  deny-list; `tests/gate/test_shared_modules.py`, a shared zip;
  `tests/gate/test_module_life.py` and
  `tests/gate/test_off_means_original.py`, as in 2.2;
  `tests/gate/test_module_admission_sandbox.py`, a module's own tests run
  in a container of their own and judged on pytest's sealed verdict;
- the install (step 8): the tests in `ui/src-tauri/src/integrity.rs`, a
  list changed or signed by another key refused, a changed, missing or
  stray file named, and code checked before the backend starts; in
  `ui/src-tauri/src/update.rs`, only a newer release offered, never an
  older one, and a signed one required, and (step 12) Tauri's own
  updater fetching from a server on 127.0.0.1: a release signed by
  another key or by none, an installer changed after signing, and an old
  installer announced as newer, all refused; in
  `ui/src-tauri/src/backend.rs`, an install run only from its own Python
  and folder, a damaged one never
  started, and an update waiting for the job to empty;
  `tests/unit/test_installer.py`, the installer's hooks;
  `tests/unit/test_installed_layout.py`, an install's code and data
  folders; `tests/gate/test_addons.py` and `tests/unit/test_server_signal.py`,
  an add-on with the wrong hash not run, and the app saying why;
- canaries (step 10): every secret in the fake world is a canary
  (`tests/fakes/world.py`, `tests/unit/test_fake_world.py`), and every
  end-to-end scenario ends by scanning the data folder, the database and
  the logs among it, and what each fake was sent, for one out of its place
  (`tests/e2e/conftest.py`).

**To build, by step:**
- **Step 11:** the audit, against this document.
- **Step 12:** the update address. (The test of the updater's own fetch
  is built: see the install, above.)

Every guard test is shown failing on the code before its fix
(`python -m devtools.prove`).
