"""Every host Steve's shipped code names is in the security model's account
of what leaves the machine (docs/security-model.md, section 6).

That table is what a user is told: everything Steve sends, and where. It
was kept by hand, and nothing held the code to it. It said the local
readers' downloads would be gone by the end of step 3, and they stayed; a
source added to the attention loop, or a download to an add-on, could reach
a host the table never named, and the table would still read as complete.
Now each row names its hosts, and a host named in shipped code that no row
names fails here.

A module's hosts are in its manifest, mostly not as URLs (`imap.gmail.com:993`,
`*.icloud.com`), so a manifest is read as core reads it (`Manifest.fixed_hosts`,
for each preset). A library a module declares reaches hosts on its own that
no scan can see; `LIBRARIES` says what each reaches, and a module declaring
one not there fails until someone has looked.

What this cannot see: a host made at run time (a feed the user gives, a page
they ask about), and one signal-cli reaches on its own. The table names those
by hand.
"""

from __future__ import annotations

import ast
import re
import tomllib
from collections import defaultdict
from functools import cache
from pathlib import Path

from core.engines import mcp
from steve_sdk.manifest import load
from tests.gate import repo_files

REPO = Path(__file__).resolve().parents[2]
MODEL = REPO / "docs" / "security-model.md"
# What ships: the backend's packages, the SDK, and the app's own code.
SHIPPED = ("agent", "channels", "core", "modules", "steve_sdk", "tools", "voice", "ui/src",
           "ui/src-tauri/src")
SCRIPTS = (".ts", ".tsx", ".rs")

# What each library a built-in module declares (`libraries`) reaches by
# itself, looked up in its source; the module's own code names the rest.
LIBRARIES = {
    "googleapiclient": ("*.googleapis.com",),  # each API at its own name under it
    "google": ("oauth2.googleapis.com",),  # google.auth renews the access token there
    "google_auth_oauthlib": ("oauth2.googleapis.com",),  # the sign-in's code, exchanged;
    # its page, accounts.google.com, opens in the user's own browser
    "google_auth_httplib2": (),  # a transport: it reaches what it is handed
    "httplib2": (),
    "docx": (),  # a Word file, built in memory
}

# Named in the code, and never reached by Steve.
NEVER_REACHED = {
    "schemas.microsoft.com": "the name of an Outlook property, which Outlook reads on the "
                             "machine (modules/outlook_classic/outlook.py)",
    "mail.google.com": "a link to the user's draft, which they open in their own browser "
                       "(modules/google_cloud/gmail.py)",
}

_URL = re.compile(r"\b(?:https?|wss?)://([A-Za-z0-9.-]*[A-Za-z0-9])")
# Names that never leave the machine, or are nobody's (RFC 2606, RFC 6761).
_LOCAL = {"localhost", "127.0.0.1", "0.0.0.0"}
_NOBODYS = ("example.com", "example.net", "example.org", "example", "test", "invalid",
            "localhost")
_HOST = re.compile(r"(\*\.)?[a-z0-9-]+(\.[a-z0-9-]+)+")


def _leaves(host: str) -> bool:
    return host not in _LOCAL and not any(
        host == name or host.endswith("." + name) for name in _NOBODYS)


def _hosts_in(texts) -> set[str]:
    return {host.lower() for text in texts for host in _URL.findall(text)
            if _leaves(host.lower())}


def hosts_in_python(source: str) -> set[str]:
    return _hosts_in_tree(ast.parse(source))


def _hosts_in_tree(tree: ast.Module) -> set[str]:
    """The hosts in a module's strings: not its comments, which the parser
    drops, nor its docstrings, which describe rather than reach."""
    described = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                described.add(id(first.value))
    return _hosts_in(node.value for node in ast.walk(tree)
                     if isinstance(node, ast.Constant) and isinstance(node.value, str)
                     and id(node) not in described)


def hosts_in_script(source: str) -> set[str]:
    """The hosts in TypeScript or Rust, its comments cut first. A `//` right
    after a colon is a URL's, not a comment's."""
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    source = re.sub(r"(?m)(^|[^:])//.*$", r"\1", source)
    return _hosts_in([source])


def _strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        value = list(value.values())
    if isinstance(value, list):
        return [text for item in value for text in _strings(item)]
    return []


def hosts_in_toml(path: Path) -> set[str]:
    """The hosts in a TOML file's URLs; for a module's manifest every
    host it fixes, in each preset: its `hosts`, its servers as `name:port`,
    its bindings', and the host a mail engine trusts (`mail_host`); and for
    an MCP server's profile, the hosts the relay opens to the server Steve
    runs (`[package] hosts`), which name no scheme."""
    hosts = _hosts_in(_strings(tomllib.loads(path.read_text(encoding="utf-8"))))
    if path.parent.name == "profiles":
        package = mcp.load(path).package
        hosts.update(package.hosts if package is not None else ())
    if path.name == "manifest.toml":
        manifest = load(path)
        for preset in (None, *(p.id for p in manifest.setup.presets)):
            hosts.update(manifest.fixed_hosts(preset))
            trusted = (manifest.settings(klass, preset).get("mail_host")
                       for klass in manifest.classes)
            hosts.update(host for host in trusted if isinstance(host, str))
    return {host.lower() for host in hosts if _leaves(host.lower())}


def manifests() -> list[Path]:
    return sorted((REPO / "modules").glob("*/manifest.toml"))


@cache
def named() -> dict[str, set[str]]:
    """Each host the shipped code names, and the files that name it."""
    found: dict[str, set[str]] = defaultdict(set)
    for name in repo_files.listed():
        if not name.startswith(tuple(f"{top}/" for top in SHIPPED)):
            continue
        path = REPO / name
        if path.suffix == ".py":
            hosts = _hosts_in_tree(repo_files.parsed(name))
        elif path.suffix in SCRIPTS:
            hosts = hosts_in_script(repo_files.text(name) or "")
        elif path.suffix == ".toml":
            hosts = hosts_in_toml(path)
        else:
            continue
        for host in hosts:
            found[host].add(name)
    for path in manifests():
        for library in load(path).libraries:
            for host in LIBRARIES.get(library, ()):
                found[host].add(f"{path.relative_to(REPO).as_posix()} ({library})")
    return found


def listed_in(model: str) -> set[str]:
    """The hosts section 6 names: each table's Hosts column, every
    backticked name in it that is a host. `*.` names a domain's every
    host."""
    section = re.split(r"(?m)^## 6\. ", model, maxsplit=1)[1]
    section = re.split(r"(?m)^## ", section, maxsplit=1)[0]
    hosts: set[str] = set()
    column = None
    for line in section.splitlines():
        if not line.startswith("|"):
            column = None
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if column is None:
            column = cells.index("Hosts") if "Hosts" in cells else -1
            continue
        if column < 0 or set(cells[0]) <= set("-: "):
            continue
        hosts.update(name for name in re.findall(r"`([^`]+)`", cells[column])
                     if _HOST.fullmatch(name))
    return hosts


def covered(host: str, listed: set[str]) -> bool:
    return host in listed or any(
        name.startswith("*.") and host.endswith(name[1:]) for name in listed)


def test_every_host_the_shipped_code_names_is_in_the_security_models_table():
    """The table is what the user reads as the whole list. A host the code
    names and the table does not is a destination they were never told
    of: add its row (or its host to a row) in section 6."""
    listed = listed_in(MODEL.read_text(encoding="utf-8"))
    missing = {host: sorted(files) for host, files in named().items()
               if host not in NEVER_REACHED and not covered(host, listed)}
    assert not missing, f"named in shipped code, and in no row of section 6: {missing}"


def test_a_host_set_aside_as_never_reached_is_still_named():
    """An exemption whose code has gone would be a hole kept open for the
    next host of that name."""
    stale = set(NEVER_REACHED) - set(named())
    assert not stale, f"no shipped code names these any more: {sorted(stale)}"


def test_every_library_a_module_declares_says_what_it_reaches():
    """A library reaches hosts the code never names: Google's client found
    its own token endpoint. One no one has looked at would be a destination
    the table could not hold; one listed and no longer declared would be a
    row of the table kept for nothing."""
    declared = {library for path in manifests() for library in load(path).libraries}
    assert not declared - set(LIBRARIES), "say what these reach, in LIBRARIES"
    assert not set(LIBRARIES) - declared, "no module declares these any more"


def test_a_manifest_is_read_for_the_hosts_it_fixes_not_only_its_links(tmp_path):
    """Most of a module's hosts are not URLs (`imap.mail.me.com:993`,
    `*.icloud.com`), so a scan of links alone would pass a manifest whose
    servers no row names."""
    path = tmp_path / "manifest.toml"
    path.write_text('''manifest_version = 1
sdk = 1
id = "mailish"
label = "Mailish"
for = "Tests"
classes = ["mail"]

[mail]
engine = "imap"
imap = "imap.one.org:993"
smtp = "smtp.one.org:465"

[setup]
steps = ["address", "preset", "fields", "test"]
fields = [{ name = "password", type = "secret" }]
revoke = "https://account.two.org/security"

[[setup.presets]]
id = "three"
label = "Three"
hosts = ["*.three.org"]
mail = { imap = "imap.three.org:993", smtp = "smtp.example.com:587" }
''', encoding="utf-8", newline="\n")
    assert hosts_in_toml(path) == {"imap.one.org", "smtp.one.org", "account.two.org",
                                   "*.three.org", "imap.three.org"}


def test_a_servers_profile_is_read_for_the_hosts_its_package_may_reach():
    """A profile names the hosts its server may reach bare, with no scheme,
    and the relay opens exactly those. Read for links alone, they were in
    no check until step 6's item 16: a profile could add a host no row
    names, and pass. Read here as core reads them (`core/engines/mcp.load`)."""
    checked = 0
    for path in sorted((REPO / "modules").glob("*/profiles/*.toml")):
        package = tomllib.loads(path.read_text(encoding="utf-8")).get("package") or {}
        bare = {host.lower() for host in package.get("hosts", [])}
        assert bare <= hosts_in_toml(path), path
        checked += len(bare)
    assert checked, "a built-in server's profile names the hosts it reaches"


def test_the_scan_reads_strings_and_skips_comments_and_docstrings():
    """A scan that missed f-strings, or read every comment's link as a
    destination, would pass or fail for the wrong reason."""
    python = '''"""See https://docs.one.org for why."""
# https://two.org in a comment
REACHED = "https://api.three.org/v1"
TEMPLATE = f"https://four.org/{name}/x"
LOCAL = "http://127.0.0.1:8000"
PLACEHOLDER = "https://portal.example.com"
def f():
    """https://five.org, described."""
    return "wss://six.org/socket"
'''
    assert hosts_in_python(python) == {"api.three.org", "four.org", "six.org"}

    script = '''// https://one.org
/* https://two.org
   over lines */
const url = "https://three.org/x"; // https://four.org
let socket = `wss://five.org/${path}`;
const mine = `https://${host}/`;
const test = "https://a.test";
'''
    assert hosts_in_script(script) == {"three.org", "five.org"}


def test_the_table_is_read_by_its_hosts_column_alone():
    """Only the Hosts column counts: a host mentioned in a row's prose is
    not the row naming it."""
    model = '''## 6. What leaves the machine

| Destination | Hosts | What goes |
|---|---|---|
| One | `api.one.org`, and `*.cdn.org` | to `not-a-host.org`, `core/x.py` |
| Two | the ones the user gives | nothing |

| Destination | What goes |
|---|---|
| Three | `three.org` |

## 7. Next
| Four | `four.org` | x |
'''
    listed = listed_in(model)
    assert listed == {"api.one.org", "*.cdn.org"}
    assert covered("files.cdn.org", listed)
    assert not covered("cdn.org.evil.org", listed)
