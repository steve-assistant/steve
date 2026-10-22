"""What a module may import: the only door from a module into Steve.

Mail, calendar, files and tasks were reached through code that named its
provider at every turn: three mailboxes were names in about seventy files
(`docs/plan/05-context.md`, section 6), each provider had its own ids,
errors and checks, and one wrote its deletes with a bare EXPUNGE that
removed every message marked deleted, not the one meant. Adding a provider
meant touching a dozen files, and nothing stopped a new one from skipping a
check the others made.

So a provider is now a module (`modules/<id>/`), and this package is all a
module's code may import (tests/gate/test_module_imports.py holds it): the
manifest's one parser, the class interfaces a provider fills, ids, the one
set of errors, the exact-target helpers, and `World` for fakes. Everything
around a provider's few methods -- the write path, ids, errors, health --
runs in core (`core/writes.py`), so no provider can do it differently.

It needs only the standard library (and `tzdata`, which Windows needs for
zones), so that step 6's sandbox can give a module this and nothing else.
"""

from __future__ import annotations

# The versions a module says it was written for (`manifest_version`, `sdk`).
# A manifest asking for another is refused, never guessed at.
SDK_VERSION = 1
MANIFEST_VERSION = 1
