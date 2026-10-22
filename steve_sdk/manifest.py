"""The one parser of a module's manifest, strict about what it accepts.

The loader, step 6's admission, the coder's self-check and the template
tests all read manifests. Four readers with their own ideas would each be
lenient somewhere: a key one ignored ("hosts", misspelled "host") would be
a key another honoured, and a module reviewed under one reading would run
under the other. So there is one reader, here, with the standard library
alone (`tomllib` and dataclasses), and it refuses what it does not know:

- a `manifest_version` or `sdk` other than this package's;
- an unknown key anywhere, so a misspelling is an error, not a silent
  default;
- an id that is not `[a-z][a-z0-9_]{0,31}`, a version that could not name
  a folder, a class that is not one of the four, an engine core does not
  have, a setup step not on the fixed list;
- reading the user's files (`reads`) together with any host: whatever it
  read could leave in any request (the pair rule, 06-modules.md, section 1);
- a tool whose name, class or parameters core could not offer as they are:
  core builds each tool's schema from its typed parameters, so a module
  never hands a model a schema of its own;
- a URL template that would put a field into the scheme, the host or the
  port, or a secret anywhere in a URL.

What a manifest says, and how a module is written, is `docs/modules.md`.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from string import Formatter
from typing import Any
from urllib.parse import urlsplit

from steve_sdk import MANIFEST_VERSION, SDK_VERSION
from steve_sdk.ids import CLASSES

ID = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
# A version, which names a folder (`<data>/modules/<id>/code/<version>/`):
# starting with a digit, so never `..`, and ending with a letter or digit,
# since Windows drops a folder name's trailing dot and "1.0." would be "1.0".
VERSION = re.compile(r"^[0-9](?:[0-9A-Za-z.+-]{0,30}[0-9A-Za-z])?$")
TIERS = ("backend", "sandbox")
ENGINES = ("imap", "caldav", "ics", "mcp", "code")
# The setup steps the app knows how to draw, in no particular order; a
# module lists the ones it needs, in its order (05-connectors.md, section 3).
STEPS = ("address", "guide", "preset", "fields", "classes", "browser_sign_in",
         "server_sign_in", "test", "pin")
FIELD_TYPES = ("text", "address", "host", "secret", "link", "file", "choice", "flag")
# The secret a browser sign-in keeps (a refresh token), held like a field's
# and named in no manifest: core keeps it after checking who signed in.
SIGN_IN = "sign_in"
SENT_COPY = ("server", "steve", "check")
# The forms a field may be checked against, after its spaces are taken
# out: an app password is shown in four groups of four letters.
FORMS = {
    "16 letters": re.compile(r"[A-Za-z]{16}"),
    # A secret that is an address: a calendar's private link. `webcal://` is
    # how Apple and Outlook offer one, and is HTTPS underneath; a name and
    # password before the host is refused.
    "link": re.compile(r"(?:https|webcal)://[^\s/?#@]+(?:[/?#]\S*)?", re.IGNORECASE),
}
# A host as a manifest may fix it: a name, optionally with one leading
# wildcard label ("*.icloud.com"), never an address or a local name.
HOST = re.compile(r"^(\*\.)?([a-z0-9-]+\.)+[a-z]{2,}$")
HOST_PORT = re.compile(r"^([a-z0-9-]+\.)+[a-z]{2,}:\d{1,5}$")
# The HTTP methods a module may declare for a host (`[methods]`). A host it
# declares none for is reached with GET and HEAD alone, which only read.
METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")
READ_ONLY = ("GET", "HEAD")
# ProgIDs that run programs, touch files, reach the network or open the
# microphone: never opened for a module, whatever its manifest lists
# (06-modules.md, section 4). COM is opened outside the module's sandbox,
# so the sandbox's lack of a microphone does not cover it: Windows' speech
# programs (`SAPI.*`) listen on it (SpSharedRecognizer, SpMMAudioIn) and
# write files (SpFileStream). The microphone opens only when the user asks.
# Step 11's audit (B1) found it missed programs made to run things: MMC20's
# ExecuteShellCommand, the Task Scheduler's RegisterTaskDefinition and WMI's
# Win32_Process.Create each ran a command as the user, past the sandbox.
DENIED_PROGIDS = ("wscript.", "shell.", "shellwindows", "scripting.", "scriptcontrol.",
                  "msscriptcontrol.", "htmlfile", "winhttp.", "msxml", "microsoft.xml",
                  "adodb.", "dao.", "cdo.", "sapi.", "mmc20.", "schedule.", "wbemscripting.",
                  "winmgmts", "internetexplorer.")
# Members that send, save, delete, open or change. A manifest lists each it
# uses under `com_actions`, by the end of its path, and a module reaches one
# only in `perform`, on the user's yes; a wildcard never matches one. They
# are compared in any case (`acting`), since COM finds a member by its name in
# any case (IDispatch's GetIDsOfNames): `send` is Outlook's Send. Until the
# review of 15 October they were compared exactly, so a module that listed
# `Items[].send` as a read could send mail with no card, and the members
# that write a file were missing, so `SaveAsFile` passed as a read.
ACTION_MEMBERS = frozenset({
    # Sending, and answering, which sends.
    "Send", "SendMail", "SendAndReceive", "SendForReview", "Post",
    "Forward", "ForwardAsVcal", "ForwardAsVcard", "ForwardAsICal", "ForwardAsBusinessCard",
    "Reply", "ReplyAll", "Respond", "Accept", "Decline",
    # Writing a file wherever the program may: past the module's sandbox,
    # with the user's rights.
    "Save", "SaveAs", "SaveAs2", "SaveAsFile", "SaveCopyAs", "Export",
    "ExportAsFixedFormat", "PrintOut", "Import",
    # Changing or removing what is there.
    "Delete", "Move", "MoveTo", "Copy", "CopyTo", "Remove", "Add", "CreateItem",
    "CreateItemFromTemplate", "Close", "ClearRecurrencePattern",
    # Opening a file, or showing a window; and closing the user's program.
    "Execute", "Display", "Open", "OpenSharedItem", "InsertFile", "Quit",
})
_ACTING = frozenset(member.lower() for member in ACTION_MEMBERS)
# Members that run code, or open what another program or an address names:
# never reached by a module, on the user's yes or not, since a card for the
# module's own action does not show what they would run. Excel's Run,
# Evaluate and ExecuteExcel4Macro run a macro or a formula that calls out; a
# DDE member (`never` matches the prefix) drives another program; VBE and
# VBProject write code into a document; CreateObject and GetObject open any
# program past the deny-list; FollowHyperlink and ShellExecute open what an
# address names. Until step 11's audit (B1) most were actions, reached in
# `perform`, and the rest reads.
NEVER_MEMBERS = frozenset({
    "Run", "Evaluate", "ExecuteExcel4Macro", "MacroOptions", "OnTime", "OnKey",
    "RegisterXLL", "VBE", "VBProject", "CreateObject", "GetObject", "FollowHyperlink",
    "Shell", "ShellExecute", "ExecuteShellCommand", "SendKeys",
})
_NEVER = frozenset(member.lower() for member in NEVER_MEMBERS)
# The programs Steve knows, by ProgID without a version, each with the
# members it knows only read: the methods a module may call, and the values
# it may set, without a card. Any other call or value set is an action
# (`core/modctx.ModuleCom.called`). A program not here may still be named in
# a manifest: its card says Steve does not know it, and every call or value
# set there is an action. Until step 11's audit (B1) a call was a read unless
# ACTION_MEMBERS named it, so MMC20's ExecuteShellCommand ran from the sandbox
# as a read; the maintainer asked that the programs be more than Outlook and
# that a module may name others.
KNOWN_PROGRAMS: dict[str, frozenset[str]] = {
    "outlook.application": frozenset({
        # The profile and its folders, walked, sorted and searched.
        "GetNamespace", "GetDefaultFolder", "GetFolderFromID", "GetItemFromID",
        "GetSharedDefaultFolder", "GetRootFolder", "Folders", "Items", "Item", "Accounts",
        "Stores", "Recipients", "Attachments", "GetFirst", "GetNext", "GetLast",
        "GetPrevious", "Find", "FindNext", "Restrict", "Sort", "GetTable", "GetNextRow",
        "GetConversation", "GetChildren", "GetRootItems",
        # People, as Exchange keeps them.
        "GetOrganizer", "GetExchangeUser", "GetExchangeDistributionList", "GetContact",
        "GetRecipientFromID", "GetAddressEntryFromID", "GetFreeBusy",
        # An item's own properties, by their schema names.
        "GetProperty", "GetProperties",
        # Whether a walk of a calendar includes each occurrence: a view, not the store.
        "IncludeRecurrences=",
    }),
    "excel.application": frozenset({
        "Workbooks", "Worksheets", "Sheets", "Item", "Range", "Cells", "Rows", "Columns",
        "Offset", "Resize", "Find", "FindNext", "FindPrevious", "End", "Intersect", "Union",
        "SpecialCells", "Names", "ListObjects", "ListRows", "ListColumns", "Areas",
    }),
    "word.application": frozenset({
        "Documents", "Item", "Range", "Paragraphs", "Sentences", "Words", "Characters",
        "Tables", "Cell", "Rows", "Columns", "Sections", "Bookmarks", "Exists",
        "ComputeStatistics", "Information",
    }),
    "powerpoint.application": frozenset({
        "Presentations", "Slides", "Shapes", "Placeholders", "Item", "FindBySlideID",
        "FindBySlideID2", "Range", "Paragraphs", "Runs", "Characters", "Lines", "Words",
        "Find", "Cell", "Rows", "Columns",
    }),
}
_KNOWN = {program: frozenset(m.lower() for m in members)
          for program, members in KNOWN_PROGRAMS.items()}
# A ProgID as a literal, such as Outlook.Application or Excel.Application.16.
PROGID = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,39}(\.[A-Za-z0-9_]{1,40}){1,2}$")
# A member path: names, each maybe a collection's items (`[]`), the last
# maybe a setter (`=`) or a wildcard for plain values (`*`).
_SEGMENT = r"[A-Za-z][A-Za-z0-9_]{0,63}(?:\[\])?"
MEMBER_PATH = re.compile(rf"^(?:{_SEGMENT}\.){{0,7}}(?:{_SEGMENT}=?|\*)$")
MAX_TEXT = 300  # a label or a sentence the user is shown
# What a module says it sends to its hosts, shown on its card ("each number
# you format"): a few words. The card named only the hosts until step 11.
MAX_SENDS = 120
# A tool is offered as `mod__<module>__<tool>`, and a model's tool names
# stop at 64 characters: 5 + 32 + 2 + 25.
TOOL_NAME = re.compile(r"[a-z][a-z0-9_]{0,24}")
PARAM_NAME = re.compile(r"[a-z][a-z0-9_]{0,31}")
TOOL_CLASSES = ("read", "action")
PARAM_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool}
MAX_TOOLS = 16
MAX_PARAMS = 12
MAX_DESCRIPTION = 1_000  # what a model reads of a tool, shown whole on the card
# The text slots (06-modules.md, section 1): words a module adds to Steve's
# own. A prompt section sits after core's sections in a block that names
# the module; a skill is offered by `use_skill` beside the night's. Both are
# screened at admission by the injection screen and by core/skills.py's
# rules, and shown whole on the card.
MAX_PROMPT = 1_500
MAX_SKILLS = 8
MAX_SKILL_STEPS = 12  # as core/skills.py holds the night's
# The unattended slots: an attention source, which core asks for what is
# new on its cadence, and routines, which run at their times. No one
# watches either run, so their words for the judge and their instructions
# are held as the text slots' are, and a routine's every action is a card.
MAX_JUDGE = 800
MAX_ROUTINES = 4
MAX_INSTRUCTION = 1_000
ATTENTION_EVERY_MIN = (15, 24 * 60)
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
AT = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")
# A remote MCP server: on a public host the manifest names, over HTTPS; or
# one the user runs on this computer, the one place plain HTTP may go.
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
MAX_MCP_URL = 300

# Each engine's settings, as a manifest's class section or a preset may give
# them. `every_min` is the class's default watch interval for an account.
_ENGINE_KEYS: dict[str, dict[str, type | tuple]] = {
    "imap": {"imap": str, "smtp": str, "starttls": bool, "sent_copy": str,
             "plus_addresses": bool, "username": str, "labels": bool},
    "caldav": {"url": str, "discover": bool},
    "ics": {"due_dates": bool},
    # `runs = "steve"`: Steve installs and runs the profile's server.
    "mcp": {"profiles": list, "path": str, "runs": str},
    # A built-in's own code: its class, and for mail whether its server
    # takes plus-addresses and the host it reads mail from, which the own
    # mailbox's trust is keyed by (core/mailbox.py).
    "code": {"provider": str, "plus_addresses": bool, "mail_host": str},
}
_COMMON_CLASS_KEYS: dict[str, type | tuple] = {"engine": str, "every_min": int,
                                               "read_only": bool}


class ManifestError(ValueError):
    """What is wrong with a manifest, said for the developer who wrote it."""


@dataclass(frozen=True)
class SecretBinding:
    """Where `ctx.http` may put a secret: one header, to one host and port,
    over HTTPS only. A secret with no binding is read only by core's engines
    or by a built-in's own code, by name (`ctx.secret`)."""

    host: str
    header: str
    port: int = 443
    form: str = "{}"


@dataclass(frozen=True)
class Field:
    name: str
    type: str
    label: str = ""
    form: str = ""
    optional: bool = False
    choices: tuple[str, ...] = ()
    bind: SecretBinding | None = None
    # The class an answer to this field adds the account to: "Other" mail
    # joins the calendar class only where the user gave a calendar's
    # address (05-connectors.md, section 5), and core names no field.
    joins: str = ""
    # Asked only under Advanced: servers, ports and the words for them
    # (IMAP, CalDAV), which setup's first screen never shows (05-connectors.md,
    # principle 10). A preset fills them; an iCloud user was shown five.
    advanced: bool = False

    @property
    def secret(self) -> bool:
        """Kept in the Credential Manager, never on disk: a secret, and a
        file's content (Google's client file holds its client secret)."""
        return self.type in ("secret", "file")


@dataclass(frozen=True)
class ErrorHint:
    """What a server says when it refuses, and what the user is told
    instead: the module's own words for its provider's habits, so core
    names no provider (05-connectors.md, section 5)."""

    says: str
    tell: str


@dataclass(frozen=True)
class GuideStep:
    text: str
    link: str = ""


@dataclass(frozen=True)
class Preset:
    id: str
    label: str
    domains: tuple[str, ...] = ()
    settings: dict[str, dict[str, Any]] = field(default_factory=dict)  # class -> overrides
    hosts: tuple[str, ...] = ()
    revoke: str = ""
    classes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Setup:
    steps: tuple[str, ...] = ()
    fields: tuple[Field, ...] = ()
    presets: tuple[Preset, ...] = ()
    guide: tuple[GuideStep, ...] = ()
    revoke: str = ""
    errors: tuple[ErrorHint, ...] = ()

    def field(self, name: str) -> Field | None:
        return next((f for f in self.fields if f.name == name), None)


@dataclass(frozen=True)
class ClassSpec:
    """How a module fills one class: the engine it is built on and that
    engine's settings, or `engine = "code"` and the provider in its code."""

    klass: str
    engine: str
    settings: dict[str, Any]
    every_min: int = 15
    read_only: bool = False


@dataclass(frozen=True)
class Param:
    """One of a tool's parameters, of a JSON type core checks."""

    name: str
    type: str
    description: str
    optional: bool = False


@dataclass(frozen=True)
class Tool:
    """A function the module gives the model: `name` in the module's code
    file, called with the context and its parameters. A `read` returns what
    it found; an `action` proposes what it would do through `ctx.act`."""

    name: str
    klass: str
    description: str
    params: tuple[Param, ...] = ()

    def schema(self) -> dict[str, Any]:
        """The JSON schema a model is given, built by core from the types."""
        return {
            "type": "object",
            "properties": {p.name: {"type": p.type, "description": p.description}
                           for p in self.params},
            "required": [p.name for p in self.params if not p.optional],
            "additionalProperties": False,
        }

    def problems(self, args: Any) -> str | None:
        """Why `args` are not what this tool takes, or None."""
        if not isinstance(args, dict):
            return "its arguments are not an object"
        known = {p.name: p for p in self.params}
        if stray := sorted(set(args) - set(known)):
            return f"it takes no {', '.join(stray)}"
        for param in self.params:
            if args.get(param.name) is None:  # a model sends an optional one as null
                if not param.optional:
                    return f"{param.name} is required"
                continue
            value = args[param.name]
            if (isinstance(value, bool) and param.type != "boolean"
                    or not isinstance(value, PARAM_TYPES[param.type])):
                return f"{param.name} is not of type {param.type}"
        return None


@dataclass(frozen=True)
class Skill:
    """A procedure the module gives Steve: what it is, when it applies, its
    steps, and the tools they use. It grants nothing: every action it leads
    to is checked and asked about as it would be without it."""

    title: str
    when: str
    steps: tuple[str, ...]
    tools: tuple[str, ...] = ()


@dataclass(frozen=True)
class Attention:
    """What the module watches for Steve's attention: its code's
    `collect(ctx)`, asked every `every_min` minutes for what is new; the
    words for what it looks at; and its paragraph for the judge, which
    decides what of it the user hears."""

    looks_at: str
    judge: str
    every_min: int = 60


@dataclass(frozen=True)
class Routine:
    """A run the module sets up: its instruction, at a time of day, on the
    days named or every day."""

    title: str
    instruction: str
    at: str
    days: tuple[str, ...] = ()


@dataclass(frozen=True)
class McpServer:
    """An MCP server whose tools the module gives Steve. A remote one: its
    address, and the name of the secret field whose value goes in the
    header that field is bound to. Or one Steve installs from npm and runs
    in its container (06-modules.md, section 7): its package at one exact
    version, the script it starts from, the moment the registry is read as
    of (`before`), the installed tree's hash as approved, and whether it
    may reach any public host rather than the manifest's on 443. The tools
    are not here: they are the module's `mcp-tools.json`, as the server
    listed them when the user approved."""

    url: str = ""
    secret: str = ""
    package: str = ""
    entry: str = ""
    args: tuple[str, ...] = ()
    before: str = ""
    tree: str = ""
    anywhere: bool = False

    @property
    def local(self) -> bool:
        return bool(self.package)

    @property
    def host(self) -> str:
        return (urlsplit(self.url).hostname or "").lower()

    @property
    def port(self) -> int:
        split = urlsplit(self.url)
        return split.port or (443 if split.scheme == "https" else 80)

    @property
    def loopback(self) -> bool:
        return self.host in LOOPBACK_HOSTS


@dataclass(frozen=True)
class Manifest:
    id: str
    version: str
    label: str
    for_: str
    classes: tuple[str, ...]
    specs: dict[str, ClassSpec]
    setup: Setup
    tier: str = "sandbox"
    adds: str = ""
    effort: str = ""
    domains: tuple[str, ...] = ()
    hosts: tuple[str, ...] = ()
    sends: str = ""
    code: str = ""
    libraries: tuple[str, ...] = ()
    com: tuple[str, ...] = ()
    com_members: tuple[str, ...] = ()
    com_actions: tuple[str, ...] = ()
    health: str = "test"
    path: Path | None = None
    # Host pattern to the methods the broker lets the module use there.
    methods: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # The classes whose items the module reads through `connector.read`.
    reads: tuple[str, ...] = ()
    tools: tuple[Tool, ...] = ()
    # The text slots: a section for Steve's instructions, and skills.
    prompt: str = ""
    skills: tuple[Skill, ...] = ()
    # The unattended slots.
    attention: Attention | None = None
    routines: tuple[Routine, ...] = ()
    mcp: McpServer | None = None

    @property
    def slots(self) -> tuple[str, ...]:
        return (tuple(f"connector.{c}" for c in self.classes) + (("tool",) if self.tools else ())
                + (("skill",) if self.skills else ()) + (("prompt",) if self.prompt else ())
                + (("attention",) if self.attention else ())
                + (("routine",) if self.routines else ()) + (("mcp",) if self.mcp else ()))

    @property
    def secrets(self) -> tuple[str, ...]:
        """The secrets each account holds, by name: its secret fields, and
        its sign-in's where setup has one."""
        found = tuple(f.name for f in self.setup.fields if f.secret)
        return found + ((SIGN_IN,) if "browser_sign_in" in self.setup.steps else ())

    def preset(self, preset_id: str | None) -> Preset | None:
        return next((p for p in self.setup.presets if p.id == preset_id), None)

    def settings(self, klass: str, preset_id: str | None = None) -> dict[str, Any]:
        """A class's engine settings, with a preset's overrides on top."""
        spec = self.specs[klass]
        out = dict(spec.settings)
        if (preset := self.preset(preset_id)) is not None:
            out.update(preset.settings.get(klass, {}))
        return out

    def methods_for(self, host: str) -> tuple[str, ...]:
        """The methods declared for `host`, matched as `hosts` are
        (`*.example.com` covers names under it), or GET and HEAD."""
        name = (host or "").lower().rstrip(".")
        if name in self.methods:
            return self.methods[name]
        for pattern, said in self.methods.items():
            if pattern.startswith("*.") and name.endswith(pattern[1:]):
                return said
        return READ_ONLY

    def fixed_hosts(self, preset_id: str | None = None) -> tuple[str, ...]:
        """Every host the manifest itself fixes, for a preset if given: its
        `hosts`, the hosts in its engines' settings, and its bindings'."""
        found: list[str] = list(self.hosts)
        if (preset := self.preset(preset_id)) is not None:
            found += preset.hosts
        for klass in self.classes:
            for key, value in self.settings(klass, preset_id).items():
                if key in ("imap", "smtp") and isinstance(value, str):
                    found.append(value.rsplit(":", 1)[0])
                elif key == "url" and isinstance(value, str):
                    host = urlsplit(value).hostname
                    if host:
                        found.append(host.lower())
        for f in self.setup.fields:
            if f.bind is not None:
                found.append(f.bind.host)
        return tuple(dict.fromkeys(h.lower() for h in found))


# --- reading ---------------------------------------------------------------

def _where(path: Path | str | None, key: str) -> str:
    return f"{path or 'manifest'}: {key}"


def _check_keys(table: dict, allowed: dict, where: str) -> None:
    unknown = sorted(set(table) - set(allowed))
    if unknown:
        raise ManifestError(f"{where}: unknown key(s) {unknown}")
    for key, value in table.items():
        want = allowed[key]
        # bool is an int to Python; a manifest's `every_min = true` is not.
        if want is int and isinstance(value, bool) or not isinstance(value, want):
            names = want.__name__ if isinstance(want, type) else "/".join(t.__name__ for t in want)
            raise ManifestError(f"{where}.{key}: expected {names}, got {type(value).__name__}")


def _text(value: str, where: str, *, required: bool = False, longest: int = MAX_TEXT) -> str:
    value = value.strip()
    if required and not value:
        raise ManifestError(f"{where}: may not be empty")
    if len(value) > longest:
        raise ManifestError(f"{where}: longer than {longest} characters")
    return value


def _strings(value: list, where: str) -> tuple[str, ...]:
    if not isinstance(value, list | tuple) or not all(isinstance(v, str) and v.strip()
                                                       for v in value):
        raise ManifestError(f"{where}: expected a list of non-empty strings")
    return tuple(v.strip() for v in value)


def _host(value: str, where: str) -> str:
    host = value.strip().lower()
    if not HOST.match(host):
        raise ManifestError(f"{where}: {value!r} is not a public host name")
    return host


def _https(value: str, where: str) -> str:
    if value and urlsplit(value).scheme != "https":
        raise ManifestError(f"{where}: a link must be https")
    return value


def _com(data: dict, path: Path | str | None
         ) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """A manifest's COM: its ProgIDs, literals none of which is on the
    deny-list; the member paths it reads and sets, none ending in an action;
    and its actions, each named in full. `core/modctx.ModuleCom` holds a
    module to them, in the backend and through the broker alike."""
    progids = _strings(data.get("com", []), _where(path, "com"))
    members = _strings(data.get("com_members", []), _where(path, "com_members"))
    actions = _strings(data.get("com_actions", []), _where(path, "com_actions"))
    for progid in progids:
        if not PROGID.match(progid):
            raise ManifestError(f"{_where(path, 'com')}: {progid!r} is not a ProgID, such as "
                                f"Excel.Application; a ProgID is named in full")
        if progid.lower().startswith(DENIED_PROGIDS):
            raise ManifestError(f"{_where(path, 'com')}: {progid} runs programs, touches files, "
                                f"reaches the network or opens the microphone, and is never "
                                f"opened for a module")
    for key, paths in (("com_members", members), ("com_actions", actions)):
        if paths and not progids:
            raise ManifestError(f"{_where(path, key)}: members of a program listed in `com`, "
                                f"which lists none")
        for member in paths:
            if not MEMBER_PATH.match(member) or (key == "com_actions" and "*" in member):
                raise ManifestError(f"{_where(path, key)}: {member!r} is not a member path, "
                                    f"such as Items.Restrict"
                                    + (", named in full" if key == "com_actions" else ""))
            if any(never(step) for step in member.split(".")):
                raise ManifestError(f"{_where(path, key)}: {member} runs code, or opens what "
                                    f"another program or an address names, and is never "
                                    f"reached by a module")
    readers = [known(progid) or frozenset() for progid in progids]
    for member in members:
        last = member.rsplit(".", 1)[-1]
        if acting(last):
            raise ManifestError(f"{_where(path, 'com_members')}: {member} sends, saves, "
                                f"deletes or runs: list it under com_actions, which a module "
                                f"reaches only on the user's yes")
        if last.endswith("=") and not any(last.lower() in reads for reads in readers):
            raise ManifestError(f"{_where(path, 'com_members')}: {member} changes a value, "
                                f"which Steve does not know to be harmless: list it under "
                                f"com_actions, which a module reaches only on the user's yes")
    return progids, members, actions


def acting(step: str) -> bool:
    """Whether one step of a member path (`Send`, `Items[]`, `Subject=`)
    sends, saves, deletes, opens or runs, in whatever case it is written."""
    return step.removesuffix("=").removesuffix("[]").lower() in _ACTING


def never(step: str) -> bool:
    """Whether one step of a member path runs code or opens what another
    program or an address names (`NEVER_MEMBERS`), in whatever case."""
    name = step.removesuffix("=").removesuffix("[]").lower()
    return name in _NEVER or name.startswith("dde")


def known(progid: str) -> frozenset[str] | None:
    """The members Steve knows only read in `progid`'s program, in lower
    case, or None for a program it does not know. A version
    (`Excel.Application.16`) is the program's."""
    return _KNOWN.get(".".join(progid.strip().lower().split(".")[:2]))


def template_fields(template: str) -> tuple[str, ...]:
    return tuple(name for _, name, _, _ in Formatter().parse(template) if name)


def check_url_template(template: str, secrets: tuple[str, ...], where: str) -> str:
    """A URL template takes non-secret fields, and only into its path and
    query: `{address}` may pick a calendar, never the host it is sent to."""
    names = template_fields(template)
    leaked = sorted(set(names) & set(secrets))
    if leaked:
        raise ManifestError(f"{where}: a secret may never go in a URL ({leaked})")
    blank = template
    for name in names:
        blank = blank.replace("{" + name + "}", "")
    parts = urlsplit(template)
    if parts.scheme != "https" or "{" in parts.scheme + parts.netloc:
        raise ManifestError(f"{where}: an https URL whose scheme, host and port are fixed")
    if not urlsplit(blank).hostname:
        raise ManifestError(f"{where}: a URL with no host")
    return template


def _binding(table: dict, where: str) -> SecretBinding:
    _check_keys(table, {"host": str, "header": str, "port": int, "form": str}, where)
    if "host" not in table or "header" not in table:
        raise ManifestError(f"{where}: a binding names its host and its header")
    header = table["header"].strip()
    if not re.fullmatch(r"[A-Za-z0-9-]+", header):
        raise ManifestError(f"{where}.header: not a header name")
    form = table.get("form", "{}")
    if form.count("{}") != 1:
        raise ManifestError(f"{where}.form: holds the secret exactly once, as {{}}")
    return SecretBinding(host=_host(table["host"], f"{where}.host"), header=header,
                         port=table.get("port", 443), form=form)


def _form(table: dict, kind: str, bind: SecretBinding | None, where: str) -> str:
    """A field's form: one of FORMS, checked at setup. (A binding's
    `form` is another thing: where its secret goes in a header.)"""
    form = _text(table.get("form", ""), f"{where}.form")
    if form and form not in FORMS:
        raise ManifestError(f"{where}.form: {form!r} is not one of {sorted(FORMS)}")
    if form and kind not in ("text", "secret"):
        raise ManifestError(f"{where}.form: only text and secrets have a form")
    return form


def _field(table: dict, where: str) -> Field:
    _check_keys(table, {"name": str, "type": str, "label": str, "form": str,
                        "optional": bool, "choices": list, "bind": dict, "joins": str,
                        "advanced": bool}, where)
    name = table.get("name", "")
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", name):
        raise ManifestError(f"{where}.name: {name!r} is not a field name")
    if name == "hosts":
        raise ManifestError(f"{where}.name: 'hosts' is where setup pins the servers typed")
    if name == SIGN_IN:
        raise ManifestError(f"{where}.name: {SIGN_IN!r} is where a browser sign-in is kept")
    kind = table.get("type", "")
    if kind not in FIELD_TYPES:
        raise ManifestError(f"{where}.type: {kind!r} is not one of {FIELD_TYPES}")
    bind = _binding(table["bind"], f"{where}.bind") if "bind" in table else None
    if bind is not None and kind != "secret":
        raise ManifestError(f"{where}.bind: only a secret is bound to a header")
    return Field(name=name, type=kind, label=_text(table.get("label", ""), f"{where}.label"),
                 form=_form(table, kind, bind, where),
                 optional=table.get("optional", False),
                 choices=_strings(table.get("choices", []), f"{where}.choices"), bind=bind,
                 joins=table.get("joins", ""), advanced=table.get("advanced", False))


def _engine_settings(engine: str, table: dict, where: str) -> dict[str, Any]:
    allowed = _ENGINE_KEYS[engine]
    _check_keys(table, allowed, where)
    out = dict(table)
    for key in ("imap", "smtp"):
        if key in out and not HOST_PORT.match(out[key].lower()):
            raise ManifestError(f"{where}.{key}: expected host:port, got {out[key]!r}")
    if "mail_host" in out and not HOST.match(out["mail_host"].lower()):
        raise ManifestError(f"{where}.mail_host: expected a host, got {out['mail_host']!r}")
    if "sent_copy" in out and out["sent_copy"] not in SENT_COPY:
        raise ManifestError(f"{where}.sent_copy: one of {SENT_COPY}")
    if "username" in out and out["username"] not in ("address", "local"):
        raise ManifestError(f"{where}.username: 'address' or 'local'")
    return out


def _class_spec(klass: str, table: dict, secrets: tuple[str, ...], where: str) -> ClassSpec:
    engine = table.get("engine", "")
    if engine not in ENGINES:
        raise ManifestError(f"{where}.engine: {engine!r} is not one of {ENGINES}")
    rest = {k: v for k, v in table.items() if k not in _COMMON_CLASS_KEYS}
    _check_keys({k: v for k, v in table.items() if k in _COMMON_CLASS_KEYS},
                _COMMON_CLASS_KEYS, where)
    settings = _engine_settings(engine, rest, where)
    if "url" in settings:
        check_url_template(settings["url"], secrets, f"{where}.url")
    every = table.get("every_min", 15)
    if not 1 <= every <= 30:
        raise ManifestError(f"{where}.every_min: between 1 and 30")
    return ClassSpec(klass=klass, engine=engine, settings=settings, every_min=every,
                     read_only=table.get("read_only", False))


def _preset(table: dict, classes: tuple[str, ...], specs: dict[str, ClassSpec],
            secrets: tuple[str, ...], where: str) -> Preset:
    allowed: dict[str, type | tuple] = {"id": str, "label": str, "domains": list,
                                        "hosts": list, "revoke": str, "classes": list}
    allowed.update({c: dict for c in classes})
    _check_keys(table, allowed, where)
    preset_id = table.get("id", "")
    if not ID.fullmatch(preset_id):
        raise ManifestError(f"{where}.id: {preset_id!r} is not an id")
    settings = {}
    for klass in classes:
        if klass in table:
            overrides = _engine_settings(specs[klass].engine, table[klass], f"{where}.{klass}")
            if "url" in overrides:
                check_url_template(overrides["url"], secrets, f"{where}.{klass}.url")
            settings[klass] = overrides
    preset_classes = _strings(table.get("classes", []), f"{where}.classes")
    if not set(preset_classes) <= set(classes):
        raise ManifestError(f"{where}.classes: not among the module's classes")
    return Preset(id=preset_id,
                  label=_text(table.get("label", ""), f"{where}.label", required=True),
                  domains=tuple(d.lower() for d in _strings(table.get("domains", []),
                                                            f"{where}.domains")),
                  settings=settings,
                  hosts=tuple(_host(h, f"{where}.hosts") for h in table.get("hosts", [])),
                  revoke=_https(table.get("revoke", ""), f"{where}.revoke"),
                  classes=preset_classes)


def _setup(table: dict, classes: tuple[str, ...], specs: dict[str, ClassSpec],
           where: str) -> Setup:
    _check_keys(table, {"steps": list, "fields": list, "presets": list, "guide": list,
                        "revoke": str, "errors": list}, where)
    steps = _strings(table.get("steps", []), f"{where}.steps")
    if bad := [s for s in steps if s not in STEPS]:
        raise ManifestError(f"{where}.steps: unknown step(s) {bad}; the steps are {STEPS}")
    fields = tuple(_field(t, f"{where}.fields[{i}]") for i, t in enumerate(table.get("fields", [])))
    if len({f.name for f in fields}) != len(fields):
        raise ManifestError(f"{where}.fields: a field name used twice")
    if bad := [f.name for f in fields if f.joins and (f.joins not in classes or not f.optional)]:
        raise ManifestError(f"{where}.fields: {bad} join a class the module lacks, or are "
                            "not optional")
    # A required field folded away would leave Add greyed out for a reason
    # the user cannot see.
    if bad := [f.name for f in fields if f.advanced and not f.optional]:
        raise ManifestError(f"{where}.fields: {bad} are under Advanced but not optional")
    secrets = tuple(f.name for f in fields if f.secret)
    presets = tuple(_preset(t, classes, specs, secrets, f"{where}.presets[{i}]")
                    for i, t in enumerate(table.get("presets", [])))
    guide = []
    for i, t in enumerate(table.get("guide", [])):
        _check_keys(t, {"text": str, "link": str}, f"{where}.guide[{i}]")
        guide.append(GuideStep(text=_text(t.get("text", ""), f"{where}.guide[{i}].text",
                                          required=True),
                               link=_https(t.get("link", ""), f"{where}.guide[{i}].link")))
    errors = []
    for i, t in enumerate(table.get("errors", [])):
        _check_keys(t, {"says": str, "tell": str}, f"{where}.errors[{i}]")
        errors.append(ErrorHint(
            says=_text(t.get("says", ""), f"{where}.errors[{i}].says", required=True),
            tell=_text(t.get("tell", ""), f"{where}.errors[{i}].tell", required=True)))
    return Setup(steps=steps, fields=fields, presets=presets, guide=tuple(guide),
                 revoke=_https(table.get("revoke", ""), f"{where}.revoke"),
                 errors=tuple(errors))


_TOP: dict[str, type | tuple] = {
    "manifest_version": int, "sdk": int, "id": str, "version": str, "label": str, "for": str,
    "adds": str, "effort": str, "domains": list, "classes": list, "tier": str, "code": str,
    "libraries": list, "com": list, "com_members": list, "com_actions": list, "hosts": list,
    "sends": str, "setup": dict, "health": str, "methods": dict, "reads": list, "tools": list,
    "prompt": str, "skills": list, "attention": dict, "routines": list, "mcp": dict,
    **{c: dict for c in CLASSES},
}
_SKILL: dict[str, type | tuple] = {"title": str, "when": str, "steps": list, "tools": list}
_ATTENTION: dict[str, type | tuple] = {"looks_at": str, "judge": str, "every_min": int}
_ROUTINE: dict[str, type | tuple] = {"title": str, "instruction": str, "at": str,
                                     "days": list}
_TOOL: dict[str, type | tuple] = {"name": str, "class": str, "description": str,
                                  "params": list}
_PARAM: dict[str, type | tuple] = {"name": str, "type": str, "description": str,
                                   "optional": bool}


def _description(value: str, where: str) -> str:
    value = value.strip()
    if not value:
        raise ManifestError(f"{where}: may not be empty")
    if len(value) > MAX_DESCRIPTION:
        raise ManifestError(f"{where}: longer than {MAX_DESCRIPTION} characters")
    return value


def _param(table: Any, where: str) -> Param:
    if not isinstance(table, dict):
        raise ManifestError(f"{where}: expected a table")
    _check_keys(table, _PARAM, where)
    name = table.get("name", "")
    if not PARAM_NAME.fullmatch(name) or name == "ctx":
        raise ManifestError(f"{where}.name: {name!r} is not [a-z][a-z0-9_]{{0,31}}, or is ctx")
    kind = table.get("type", "")
    if kind not in PARAM_TYPES:
        raise ManifestError(f"{where}.type: {kind!r} is not one of {tuple(PARAM_TYPES)}")
    return Param(name=name, type=kind,
                 description=_text(table.get("description", ""), f"{where}.description",
                                   required=True),
                 optional=table.get("optional", False))


def _tools(value: list, where: str) -> tuple[Tool, ...]:
    if len(value) > MAX_TOOLS:
        raise ManifestError(f"{where}: more than {MAX_TOOLS} tools")
    tools = []
    for i, table in enumerate(value):
        at = f"{where}[{i}]"
        if not isinstance(table, dict):
            raise ManifestError(f"{at}: expected a table")
        _check_keys(table, _TOOL, at)
        name = table.get("name", "")
        if not TOOL_NAME.fullmatch(name) or name == "perform":
            raise ManifestError(f"{at}.name: {name!r} is not [a-z][a-z0-9_]{{0,24}}, or is "
                                f"perform, which carries out what a card approved")
        klass = table.get("class", "")
        if klass not in TOOL_CLASSES:
            raise ManifestError(f"{at}.class: {klass!r} is not one of {TOOL_CLASSES}")
        params = table.get("params", [])
        if len(params) > MAX_PARAMS:
            raise ManifestError(f"{at}.params: more than {MAX_PARAMS}")
        parsed = tuple(_param(p, f"{at}.params[{j}]") for j, p in enumerate(params))
        if len({p.name for p in parsed}) != len(parsed):
            raise ManifestError(f"{at}.params: a parameter named twice")
        tools.append(Tool(name=name, klass=klass,
                          description=_description(table.get("description", ""),
                                                   f"{at}.description"),
                          params=parsed))
    if len({t.name for t in tools}) != len(tools):
        raise ManifestError(f"{where}: a tool named twice")
    return tuple(tools)


def _skills(value: list, where: str) -> tuple[Skill, ...]:
    if len(value) > MAX_SKILLS:
        raise ManifestError(f"{where}: more than {MAX_SKILLS} skills")
    found = []
    for i, table in enumerate(value):
        at = f"{where}[{i}]"
        if not isinstance(table, dict):
            raise ManifestError(f"{at}: expected a table")
        _check_keys(table, _SKILL, at)
        steps = _strings(table.get("steps", []), f"{at}.steps")
        if not steps or len(steps) > MAX_SKILL_STEPS:
            raise ManifestError(f"{at}.steps: 1 to {MAX_SKILL_STEPS} steps")
        if any(len(step) > MAX_DESCRIPTION for step in steps):
            raise ManifestError(f"{at}.steps: a step longer than {MAX_DESCRIPTION} characters")
        found.append(Skill(title=_text(table.get("title", ""), f"{at}.title", required=True),
                           when=_text(table.get("when", ""), f"{at}.when", required=True),
                           steps=steps, tools=_strings(table.get("tools", []), f"{at}.tools")))
    if len({s.title.lower() for s in found}) != len(found):
        raise ManifestError(f"{where}: a skill named twice")
    return tuple(found)


def _attention(table: dict | None, where: str) -> Attention | None:
    if table is None:
        return None
    _check_keys(table, _ATTENTION, where)
    every = table.get("every_min", 60)
    low, high = ATTENTION_EVERY_MIN
    if not low <= every <= high:
        raise ManifestError(f"{where}.every_min: between {low} and {high}")
    judge = _text(table.get("judge", ""), f"{where}.judge", required=True, longest=MAX_JUDGE)
    return Attention(looks_at=_text(table.get("looks_at", ""), f"{where}.looks_at",
                                    required=True),
                     judge=judge, every_min=every)


def _routines(value: list, where: str) -> tuple[Routine, ...]:
    if len(value) > MAX_ROUTINES:
        raise ManifestError(f"{where}: more than {MAX_ROUTINES} routines")
    found = []
    for i, table in enumerate(value):
        at = f"{where}[{i}]"
        if not isinstance(table, dict):
            raise ManifestError(f"{at}: expected a table")
        _check_keys(table, _ROUTINE, at)
        instruction = _text(table.get("instruction", ""), f"{at}.instruction", required=True,
                            longest=MAX_INSTRUCTION)
        if not AT.fullmatch(table.get("at", "")):
            raise ManifestError(f"{at}.at: a time of day as HH:MM")
        days = tuple(d.lower() for d in _strings(table.get("days", []), f"{at}.days"))
        if stray := sorted(set(days) - set(DAYS)):
            raise ManifestError(f"{at}.days: {stray} are not among {DAYS}")
        found.append(Routine(title=_text(table.get("title", ""), f"{at}.title", required=True),
                             instruction=instruction, at=table["at"], days=days))
    if len({r.title.lower() for r in found}) != len(found):
        raise ManifestError(f"{where}: a routine named twice")
    return tuple(found)


_NPM = re.compile(r"(@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*@\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?")
_MOMENT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z")


def _mcp(table: dict | None, where: str) -> McpServer | None:
    if table is None:
        return None
    _check_keys(table, {"url": str, "secret": str, "package": str, "entry": str, "args": list,
                        "before": str, "tree": str, "anywhere": bool}, where)
    if "package" in table:
        return _mcp_package(table, where)
    url = _text(table.get("url", ""), f"{where}.url", required=True, longest=MAX_MCP_URL)
    split = urlsplit(url)
    try:
        split.port  # noqa: B018 - a port that is not a number raises
    except ValueError:
        raise ManifestError(f"{where}.url: its port is not a number") from None
    server = McpServer(url=url, secret=table.get("secret", ""))
    if split.username or split.password or split.fragment or not server.host:
        raise ManifestError(f"{where}.url: an address with a host, and no name, password "
                            f"or #fragment in it")
    if split.scheme != "https" and not (split.scheme == "http" and server.loopback):
        raise ManifestError(f"{where}.url: https, or http to a server on this computer "
                            f"({', '.join(LOOPBACK_HOSTS)})")
    return server


def _mcp_package(table: dict, where: str) -> McpServer:
    """A server Steve installs and runs: written by Steve as the user adds
    it, so each part is checked as Steve wrote it, and none may be left out."""
    if "url" in table or "secret" in table:
        raise ManifestError(f"{where}: a server is reached at a url or installed as a "
                            f"package, not both; a package's server holds its own sign-in")
    package = table["package"]
    if not _NPM.fullmatch(package):
        raise ManifestError(f"{where}.package: {package!r} is not an npm package at one "
                            f"exact version, as name@1.2.3")
    entry = _text(table.get("entry", ""), f"{where}.entry", required=True)
    if entry.startswith(("/", "\\")) or ".." in entry.replace("\\", "/").split("/") \
            or ":" in entry or not entry.endswith((".js", ".cjs", ".mjs")):
        raise ManifestError(f"{where}.entry: a script within the package")
    before = table.get("before", "")
    if not _MOMENT.fullmatch(before):
        raise ManifestError(f"{where}.before: the moment its version was published, as the "
                            f"registry gives it")
    tree = table.get("tree", "")
    if not re.fullmatch(r"[0-9a-f]{64}", tree):
        raise ManifestError(f"{where}.tree: the installed tree's hash, as approved")
    return McpServer(package=package, entry=entry,
                     args=tuple(_strings(table.get("args", []), f"{where}.args")),
                     before=before, tree=tree, anywhere=table.get("anywhere", False))


def _mcp_fits(manifest: Manifest, where: str) -> None:
    """A server's host is one the manifest names, unless it is this
    computer's; its secret is a secret field bound to that host and port,
    so the broker fills it there alone. A binding's host is public and
    reached over HTTPS, so a server on this computer takes no secret."""
    server = manifest.mcp
    if server is None:
        return
    if server.local:
        # Its pipe is held to names, exactly: a pattern would let it name
        # any host under a domain, and a server on this computer none.
        if not server.anywhere and not manifest.hosts:
            raise ManifestError(f"{where}: a server Steve runs reaches the manifest's hosts, "
                                f"and it names none")
        if any(h.startswith("*.") for h in manifest.hosts):
            raise ManifestError(f"{where}: a server Steve runs reaches hosts by their exact "
                                f"names, not a pattern")
        return
    if not server.loopback and server.host not in manifest.hosts:
        raise ManifestError(f"{where}.url: {server.host} is not among the manifest's hosts")
    if not server.secret:
        return
    found = next((f for f in manifest.setup.fields if f.name == server.secret), None)
    if found is None or found.type != "secret":
        raise ManifestError(f"{where}.secret: {server.secret!r} is not a secret field")
    bind = found.bind
    if bind is None or bind.host != server.host or bind.port != server.port:
        raise ManifestError(f"{where}.secret: {server.secret!r} is bound to the server's "
                            f"host and port")


def parse(data: dict, path: Path | str | None = None) -> Manifest:
    """A manifest from its parsed TOML, or ManifestError saying what is wrong."""
    where = str(path or "manifest")
    _check_keys(data, _TOP, where)
    for key, want in (("manifest_version", MANIFEST_VERSION), ("sdk", SDK_VERSION)):
        if data.get(key) != want:
            raise ManifestError(f"{_where(path, key)}: this Steve reads {key} = {want}, "
                                f"not {data.get(key)!r}")
    module_id = data.get("id", "")
    if not ID.fullmatch(module_id):
        raise ManifestError(f"{_where(path, 'id')}: {module_id!r} is not [a-z][a-z0-9_]{{0,31}}")
    classes = _strings(data.get("classes", []), _where(path, "classes"))
    if bad := [c for c in classes if c not in CLASSES]:
        raise ManifestError(f"{_where(path, 'classes')}: {bad} not among {CLASSES}")
    if len(set(classes)) != len(classes):
        raise ManifestError(f"{_where(path, 'classes')}: a class named twice")
    if extra := [c for c in CLASSES if c in data and c not in classes]:
        raise ManifestError(f"{where}: a section for {extra}, which `classes` does not list")
    tier = data.get("tier", "sandbox")
    if tier not in TIERS:
        raise ManifestError(f"{_where(path, 'tier')}: one of {TIERS}")
    setup_table = data.get("setup", {})
    secrets = tuple(f.get("name", "") for f in setup_table.get("fields", [])
                    if isinstance(f, dict) and f.get("type") == "secret")
    specs = {}
    for klass in classes:
        if klass not in data:
            raise ManifestError(f"{where}: `classes` lists {klass!r}, with no [{klass}] section")
        specs[klass] = _class_spec(klass, data[klass], secrets, f"{where}.{klass}")
    code = data.get("code", "")
    if any(s.engine == "code" for s in specs.values()) and not code:
        raise ManifestError(f"{where}: a class built on code needs `code`, the file it is in")
    if code and not re.fullmatch(r"[a-z][a-z0-9_]*\.py", code):
        raise ManifestError(f"{_where(path, 'code')}: a .py file beside the manifest")
    version = _text(data.get("version", "1"), _where(path, "version"), required=True)
    if not VERSION.fullmatch(version):
        raise ManifestError(f"{_where(path, 'version')}: {version!r} is not a version: up to "
                            f"32 letters, digits, dots, pluses and dashes, from a digit to a "
                            f"letter or digit")
    reads = _strings(data.get("reads", []), _where(path, "reads"))
    if bad := [c for c in reads if c not in CLASSES]:
        raise ManifestError(f"{_where(path, 'reads')}: {bad} not among {CLASSES}")
    if len(set(reads)) != len(reads):
        raise ManifestError(f"{_where(path, 'reads')}: a class named twice")
    com, com_members, com_actions = _com(data, path)
    tools = _tools(data.get("tools", []), _where(path, "tools"))
    if tools and (tier != "sandbox" or not code):
        raise ManifestError(f"{_where(path, 'tools')}: a module's tools run in the sandbox, "
                            f"from its `code`")
    attention = _attention(data.get("attention"), _where(path, "attention"))
    if attention and (tier != "sandbox" or not code):
        raise ManifestError(f"{_where(path, 'attention')}: a module's attention source runs "
                            f"in the sandbox, from its `code`")
    prompt = data.get("prompt", "").strip()
    if len(prompt) > MAX_PROMPT:
        raise ManifestError(f"{_where(path, 'prompt')}: longer than {MAX_PROMPT} characters")
    manifest = Manifest(
        id=module_id,
        version=version,
        label=_text(data.get("label", ""), _where(path, "label"), required=True),
        for_=_text(data.get("for", ""), _where(path, "for"), required=True),
        classes=classes, specs=specs,
        setup=_setup(setup_table, classes, specs, _where(path, "setup")),
        tier=tier,
        adds=_text(data.get("adds", ""), _where(path, "adds")),
        effort=_text(data.get("effort", ""), _where(path, "effort")),
        domains=tuple(d.lower() for d in _strings(data.get("domains", []),
                                                  _where(path, "domains"))),
        hosts=tuple(_host(h, _where(path, "hosts")) for h in data.get("hosts", [])),
        sends=_text(data.get("sends", ""), _where(path, "sends"), longest=MAX_SENDS),
        code=code,
        libraries=_strings(data.get("libraries", []), _where(path, "libraries")),
        com=com, com_members=com_members, com_actions=com_actions,
        health=_text(data.get("health", "test"), _where(path, "health")),
        path=Path(path) if path else None,
        methods=_methods(data.get("methods", {}), _where(path, "methods")),
        reads=reads,
        tools=tools,
        prompt=prompt,
        skills=_skills(data.get("skills", []), _where(path, "skills")),
        attention=attention,
        routines=_routines(data.get("routines", []), _where(path, "routines")),
        mcp=_mcp(data.get("mcp"), _where(path, "mcp")),
    )
    _mcp_fits(manifest, _where(path, "mcp"))
    # A secret is kept for an account (`core/modules.add_account`), and an
    # account needs a class; a module with none keeps only its MCP server's,
    # entered on its card. Any other would never be entered, and its tool's
    # calls, which have no account, would run without it.
    own = {manifest.mcp.secret} if manifest.mcp is not None and manifest.mcp.secret else set()
    if not classes and (unkept := [s for s in manifest.secrets if s not in own]):
        raise ManifestError(f"{_where(path, 'setup')}: {unkept} have no account to keep them "
                            f"in: a module's secrets are its accounts', and it has no class")
    known = {h for p in (None, *(p.id for p in manifest.setup.presets))
             for h in manifest.fixed_hosts(p)}
    if stray := sorted(set(manifest.methods) - known):
        raise ManifestError(f"{_where(path, 'methods')}: {stray} are not hosts the "
                            f"manifest names")
    # The pair rule, once `toolgate`'s for a self-written tool's network and
    # filesystem: the user's files and a way out, together, are exfiltration
    # however the code reads. A host the user types, or a link they give, is
    # a way out as much as one the manifest fixes.
    typed = any(f.type in ("host", "link") or f.form == "link" for f in manifest.setup.fields)
    if "files" in reads and (known or typed):
        raise ManifestError(f"{_where(path, 'reads')}: the user's files together with a host: "
                            f"what it read could leave in any request")
    return manifest


def _methods(table: dict, where: str) -> dict[str, tuple[str, ...]]:
    out = {}
    for host, said in table.items():
        name = _host(str(host), where)
        methods = tuple(m.upper() for m in _strings(said, f"{where}.{name}"))
        if bad := [m for m in methods if m not in METHODS]:
            raise ManifestError(f"{where}.{name}: {bad} not among {METHODS}")
        out[name] = tuple(dict.fromkeys(methods))
    return out


def load(path: Path) -> Manifest:
    """Read and parse `manifest.toml` at `path`."""
    try:
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ManifestError(f"{path}: not TOML: {exc}") from None
    return parse(data, path)
