"""The supervisor's control plane, as the agent asks it for something: a swap.

The model scout's switches by hand (`python -m agent.models_scout`) are put
into the running worker by a swap, which only the supervisor can make. This
is the one door to it, so the tests have one thing to fence
(tests/gate/fences.py) and the yardstick one thing to refuse
(agent/autonomy_eval.py). It lived in self-upgrade v1's `agent/upgrade.py`,
which merged a change and swapped onto it, until step 6 deleted that.
"""

from __future__ import annotations


def post_supervisor(path: str, body: dict, timeout: float = 300.0) -> dict:
    """POST to the supervisor on this machine, with its token once it has
    proved itself (core/localapi.py)."""
    from core import localapi

    _, answer = localapi.call("POST", path, body, timeout=timeout)
    return answer if isinstance(answer, dict) else {"ok": False, "error": str(answer)}
