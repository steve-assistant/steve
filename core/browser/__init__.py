"""The sandboxed browser's frozen half: where it runs (`sandbox`), its only
way out (`gate`), the only thing that touches a page (`driver`), and the
doctor that proves it holds (`python -m core.browser check`). The agent's
side -- sessions and tools -- is `agent/browser/` and
`agent/toolset/browser.py`. The design is the browser add-on in docs/security-model.md.
"""

from __future__ import annotations


def enabled() -> bool:
    """Whether the browser's tools may run: the frozen policy allows it
    (`browser.enabled` in core/policy.yaml, the ceiling) and the user has
    turned it on (the BROWSER switch, core/knobs.py, off until they do)."""
    from core import knobs
    from core.policy import load_policy

    try:
        return bool(load_policy().browser.enabled) and knobs.on("BROWSER")
    except Exception:  # noqa: BLE001 - an unreadable policy is not a yes
        return False


def background() -> bool:
    """Whether a background task's lead may start a clean-room browsing
    worker: with the browser on, the policy allowing it
    (`browser.background`), and the user's BACKGROUND_BROWSING switch on."""
    from core import knobs
    from core.policy import load_policy

    try:
        policy = load_policy().browser
        return bool(policy.background) and enabled() and knobs.on("BACKGROUND_BROWSING")
    except Exception:  # noqa: BLE001 - an unreadable policy is not a yes
        return False
