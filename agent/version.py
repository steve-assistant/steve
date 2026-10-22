"""The worker's version, which is Steve's one version (`core.identity.VERSION`).

Bumping it is the cheapest proof that a blue-green swap replaced the running
code: the health endpoint reports it, so a test can assert the version
changed without restarting the supervisor.
"""

from core.identity import VERSION as STEVE_VERSION

__all__ = ["STEVE_VERSION"]
