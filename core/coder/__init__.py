"""The coding pipeline: a coder, an independent test builder and a judge.

Frozen, like everything under core/. The agent may change the agent; it may
not change the thing that writes its code, tests it and judges it -- a
harness it could edit is one where it could give itself a shell or pick a
softer judge. See docs/self-upgrade.md.
"""
