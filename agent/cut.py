"""Cutting text short, and saying so: moved to `steve_sdk/cut.py`.

A module may cut text the same way core does, and a module imports only
`steve_sdk` (tests/gate/test_module_imports.py), so the helper lives there.
Core's callers keep this name.
"""

from steve_sdk.cut import MARK, cut, keeping_links

__all__ = ["MARK", "cut", "keeping_links"]
