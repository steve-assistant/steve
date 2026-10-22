# Security

Steve reads your mail, your calendar and the web, and can act for you. A
flaw in it can cost you more than a crash, so reports are welcome.

## Reporting a problem

- Report it privately, through this repository's Security tab ("Report a
  vulnerability"). Please don't open a public issue for it.
- Say what you did, what Steve did, and which version you ran. A text,
  page or email that makes it happen helps most.
- Leave out your own keys, passwords and mail. A made-up account shows the
  problem as well.
- You'll get a reply within a week. Once a fix is released, the report is
  made public with it, crediting you if you want.

## What counts

`docs/security-model.md` names who Steve is built to stand against
(section 2) and what holds against each. A way past any of it is a
vulnerability. For example:

- text Steve reads (a page, an email, a file) making it act, or send
  something out, without a card you approved;
- a module or a connector reaching beyond what you granted it;
- another program or Windows user on the computer reaching Steve's
  backend, its secrets or its data;
- a phone channel deciding a card it was not shown, or a high-risk card
  without its second factor;
- an update, an add-on or a model file that Steve runs without checking it.

These are known and accepted, not vulnerabilities:

- a model fooled by what it read can still *say* something false in a
  conversation you are in;
- someone with administrator rights on the computer, or signed in as you,
  can read what you can;
- a desktop program you let a module drive, which its card names as one
  Steve does not know, does what that program does when a value is read
  from it.

## Gaps known today

Each is marked **open** in `docs/security-model.md`, where it is said in
full. They are being fixed; this list shrinks as each lands.

- **What Steve remembers** (2.1). Some of the facts Steve keeps were taken
  from what a conversation read. Recalling them does not count as reading
  outside text, so the conversation can still open any address afterwards.
