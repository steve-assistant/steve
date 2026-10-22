"""Only the app changes accounts, their secrets, and who counts as the user; no
model tool can.

An account is where Steve reads and writes the user's mail and calendar, and
its secret is the user's password or token. A tool that could add one could
be talked into connecting a stranger's server, or a prompt in an email into
moving the user's calendar to it. So adding, changing, testing and removing
an account, and writing a secret, are reached only from the app's routes
(`agent/worker/accounts.py`, behind the local-only check and this start's
token), and Steve can only hand the user a link (05-connectors.md, rule 7).

The same holds for the agent's own mailbox and the addresses whose mail can
be the user's instruction (`core/mailbox.choose`, `opt_in`): a tool that
could add a sender could make anyone the user (section 7). Until step 5
they were frozen policy lines; now the app writes them, and nothing else.

And for a module's card (`core/lifecycle.approve`): Steve builds a module
and core checks it, but a tool that could approve one would grant it
whatever it asked, so the yes, a rollback and a removal are the app's too
(06-modules.md, principle 6). The watch may turn a module off, never on.
And a module's export and import (`agent/modshare.py`): an export sends the
user's code away, and a tool that could import one could be handed a zip by
anyone who writes to the user.

These tests read the code: who names the writers in `core/modules.py`,
`core/lifecycle.py`, `core/credstore.py` and `agent/accounts.py`, and who
writes the registry's tables in SQL.
"""

from __future__ import annotations

import ast
import re

from tests.gate import repo_files

CODE = ("agent/", "channels/", "core/", "devtools/", "modules/", "scripts/", "tools/", "voice/")

WRITERS = {
    "core.modules": {"add_account", "rename_account", "set_account_settings", "move_class",
                     "set_account_enabled", "remove_account", "remove_module", "set_enabled"},
    "core.credstore": {"write", "delete", "forget", "use"},
    "agent.accounts": {"add", "edit", "move", "remove", "test"},
    "core.mailbox": {"choose", "stop", "opt_in", "opt_out"},
    "core.lifecycle": {"approve", "decline", "rollback", "remove"},
    "core.sharing": {"export"},
    "agent.modshare": {"export_zip", "import_zip"},
}

# Where a writer may be named: the registry, the lifecycle and the store
# themselves, the app's backend and its routes, and Google's login command,
# which the user runs (it gives way to setup's browser sign-in in item 9);
# sharing's own side, which calls core's export; and the module-building
# eval, which plays the user who approves, switches on and removes what was
# built, in a data folder of its own with a store in memory for the
# Credential Manager, from the command line, and refuses to run in the
# worker's process (`agent/build_eval.run`).
ALLOWED = {"core/modules.py", "core/lifecycle.py", "core/credstore.py", "agent/accounts.py",
           "agent/worker/accounts.py", "modules/google_cloud/auth.py", "core/mailbox.py",
           "agent/modshare.py", "agent/build_eval.py"}
# Where the registry's tables are written in SQL: the registry, and the
# lifecycle that installs a module from outside, switches its versions and
# turns it off when its watch says so.
REGISTRY = {"core/modules.py", "core/lifecycle.py"}

REGISTRY_WRITE = re.compile(
    r"\b(INSERT\s+(OR\s+\w+\s+)?INTO|UPDATE|DELETE\s+FROM|REPLACE\s+INTO)\s+"
    r"(accounts|account_classes|modules)\b", re.IGNORECASE)
MAILBOX_WRITE = re.compile(
    r"\b(INSERT\s+(OR\s+\w+\s+)?INTO|UPDATE|DELETE\s+FROM|REPLACE\s+INTO)\s+"
    r"(own_mailbox|user_addresses)\b", re.IGNORECASE)


def _files():
    for rel in repo_files.python(*CODE):
        yield rel, repo_files.parsed(rel)


def _bound(tree: ast.AST) -> dict[str, str]:
    """The local names bound to a module that has writers."""
    names = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                if f"{node.module}.{alias.name}" in WRITERS:
                    names[alias.asname or alias.name] = f"{node.module}.{alias.name}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in WRITERS and alias.asname:
                    names[alias.asname] = alias.name
    return names


def _named_writers(tree: ast.AST) -> list[str]:
    bound = _bound(tree)
    found = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                and node.attr in WRITERS.get(bound.get(node.value.id, ""), ())):
            found.append(f"{bound[node.value.id]}.{node.attr}, line {node.lineno}")
        elif isinstance(node, ast.ImportFrom) and node.module in WRITERS:
            found += [f"{node.module}.{a.name}, line {node.lineno}" for a in node.names
                      if a.name in WRITERS[node.module]]
    return found


def test_only_the_apps_backend_names_an_account_or_secret_writer():
    found = []
    for rel, tree in _files():
        if rel in ALLOWED:
            continue
        found += [f"{rel}: {w}" for w in _named_writers(tree)]
    assert found == []


def test_the_check_sees_each_way_of_naming_a_writer():
    """The guard above is only as good as what it can see."""
    for text in ("from core import modules\nmodules.add_account(c)",
                 "from core import modules as m\nf = m.remove_account",
                 "from core.credstore import write",
                 "import core.credstore as cs\ncs.forget('x')",
                 "from agent import accounts\nawait accounts.add(c)"):
        assert _named_writers(ast.parse(text)), text
    assert not _named_writers(ast.parse("accounts = []\naccounts.remove(1)"))
    assert _named_writers(ast.parse("from core import mailbox as rules\nrules.opt_in(c, a)"))
    assert _named_writers(ast.parse("from agent import modshare\nmodshare.export_zip(c, 'x')"))
    assert _named_writers(ast.parse("from core.sharing import export"))


def test_no_code_but_the_registry_writes_its_tables():
    found = []
    for rel, tree in _files():
        if rel in REGISTRY:
            continue
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and REGISTRY_WRITE.search(node.value)):
                found.append(f"{rel}, line {node.lineno}: {node.value[:60]!r}")
    assert found == []


def test_no_code_but_the_mailbox_rules_writes_who_counts_as_the_user():
    """The mailbox's account and the user's opted-in addresses are written
    in `core/mailbox.py` alone, whose writers only the app's routes name."""
    found = []
    for rel, tree in _files():
        if rel == "core/mailbox.py":
            continue
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and MAILBOX_WRITE.search(node.value)):
                found.append(f"{rel}, line {node.lineno}: {node.value[:60]!r}")
    assert found == []
