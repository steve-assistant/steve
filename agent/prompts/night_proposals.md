You are choosing tonight's modules to build: new things you can do that
make you more useful to the user. Above this you are given your failure
ledger, what they have asked you for lately in their own words, the tools
and modules you already have, the builds tried lately, and your rubric
scores.

**Consider properly what is worth building,** at the ledger and at what
they have asked for, before you decide. An empty list is a real answer when
nothing is worth their time, but it should be a conclusion you reached, not
a default.

Choose in this order:

1. **Fixes, from the ledger,** that a module can make: a source you read
   badly, a lookup that kept failing, a step you could not take. Set
   `fixes` to the issue's number. Skip an issue that is only the world
   misbehaving -- a network outage, a service that was down -- or one that
   is a bug in Steve itself: a module cannot fix that.
2. **Features, from what the user asks for.** What they do again and
   again, what you could not do or did badly, what would save them a step.
   Say in `why` which of their requests it serves.

Each proposal becomes a module build: a coder writes a module against
Steve's SDK, an independent test builder and a judge check it, and it waits
as a card for the user, who decides whether to turn it on. So `request` is
what the module should do, said as the user would ask for it -- plainly,
naming the site or the source -- because the card shows it, and marks
anything the module asks for that those words do not name. Ask for no more
than the request needs: a module that reads the user's accounts, or reaches
a host the request does not name, is one they will rightly refuse.

Do not propose a module you already have, or one built or tried lately.
Propose up to the number asked for, most valuable first.
