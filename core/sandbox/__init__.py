"""The modules' sandbox: code from outside runs in an AppContainer, or not at all.

A module's code is written by someone other than the user: by Steve, from a
request, or by whoever shared it. Run as the user, it could read everything
the user can: their files, the Credential Manager that holds their mail
password and API keys, and any network address, including the backend's
own loopback port. A review of the code before it runs catches what the
reviewer sees, and code can be written so a reviewer does not see it.

So the walls are Windows', not a review's (06-modules.md, section 2;
spike S1, docs/plan/spikes/s1-appcontainer.md):

- each module runs in an AppContainer profile of its own
  (`profiles.py`), less-privileged (LPAC) where Windows allows, with no
  network capability: no internet, no loopback, no private network;
- it can read its code and the module runtime, write its `state/` folder,
  and nothing else (`grants.py`, `runtime.py`);
- it runs in a job (`job.py`): one process, a memory cap, a CPU cap, no
  UI, and a wall-clock kill per call enforced from outside (`launch.py`);
- its only ways out are its stdio, to the broker, and for a local server a
  named pipe held to the container (`pipe.py`).

Support is probed at each start (`probe.py`, decision D6): without an
AppContainer, code modules do not run, and their cards say why.
"""
