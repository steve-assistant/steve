"""steve_sdk: ids that survive any native id, a manifest parser that refuses
what it does not know, and exact targets that refuse a server's guess.

These are the pieces every module and core's write path stand on: an id
that loses its account sends a later call to the wrong mailbox; a parser
that ignores a misspelled key runs a module under a reading nobody
reviewed; and `exact.one` is the 11 October lesson, when a server ignored a
filter and a cleanup deleted 96 events.
"""

from __future__ import annotations

import datetime as dt

import pytest

from steve_sdk import classes, exact, ids
from steve_sdk.errors import AfterWrite, TargetChanged
from steve_sdk.manifest import ManifestError, parse

GMAIL_LIKE = {
    "manifest_version": 1, "sdk": 1, "id": "mailish", "label": "Mailish",
    "for": "Mail and calendar from a mailish address", "effort": "about 2 minutes",
    "domains": ["mailish.example"], "classes": ["mail", "calendar"],
    "mail": {"engine": "imap", "imap": "imap.mailish.example:993",
             "smtp": "smtp.mailish.example:465", "sent_copy": "server", "plus_addresses": True},
    "calendar": {"engine": "caldav", "url": "https://dav.mailish.example/cal/{address}/events/"},
    "setup": {"steps": ["address", "guide", "fields", "classes", "test"],
              "fields": [{"name": "app_password", "type": "secret", "form": "16 letters"}]},
}


def _with(**changes):
    data = {k: (dict(v) if isinstance(v, dict) else v) for k, v in GMAIL_LIKE.items()}
    data.update(changes)
    return data


@pytest.mark.parametrize("native", [
    "18c2f0a9b3d4e5f6", "<abc:def@mail.example>", "INBOX/Sub:folder|7|1234|<x@y>",
    "événement-ünïcode", "a" * 600,
])
def test_an_id_round_trips_with_its_class_and_account_whatever_the_native_id_holds(native):
    """A Message-ID or a folder name can hold a colon; the id must still
    split into the right three parts, or a later call reaches another account."""
    made = ids.make("mail", "a1b2c3d4e5f6", native)
    assert ids.parse(made) == ids.Id("mail", "a1b2c3d4e5f6", native)
    assert made.count(":") == 2


@pytest.mark.parametrize("text", [
    "", "gmail:abc", "mail:a1b2c3d4e5f6:", "notaclass:a1b2c3d4e5f6:YWJj",
    "mail:BAD KEY:YWJj", "mail:a1b2c3d4e5f6:not base64!", "18c2f0a9b3d4e5f6",
])
def test_text_that_is_not_an_id_this_sdk_made_parses_as_nothing(text):
    """An old provider id, a handle, or text a model invented must not be
    read as naming an account."""
    assert ids.parse(text) is None


def test_the_example_manifest_parses_with_its_hosts_and_secrets():
    manifest = parse(GMAIL_LIKE)
    assert manifest.slots == ("connector.mail", "connector.calendar")
    assert manifest.secrets == ("app_password",)
    assert set(manifest.fixed_hosts()) == {"imap.mailish.example", "smtp.mailish.example",
                                           "dav.mailish.example"}


SAIL = {"title": "Tides", "instruction": "Say the tides.", "at": "07:30"}


@pytest.mark.parametrize("change, says", [
    ({"hosts_extra": ["x"]}, "unknown key"),
    ({"manifest_version": 2}, "manifest_version = 1"),
    ({"sdk": 0}, "sdk = 1"),
    ({"id": "Gmail-Password"}, "is not"),
    ({"id": "mailish\n"}, "is not"),
    ({"version": "1.0."}, "is not a version"),
    ({"version": "../1"}, "is not a version"),
    ({"version": "1" * 33}, "is not a version"),
    ({"reads": ["files"]}, "the user's files together with a host"),
    ({"classes": ["mail", "email"]}, "not among"),
    ({"tier": "kernel"}, "tier"),
    ({"setup": {"steps": ["address", "telepathy"]}}, "unknown step"),
    ({"mail": {"engine": "pop3"}}, "is not one of"),
    ({"mail": {"engine": "imap", "imap": "imap.mailish.example"}}, "host:port"),
    ({"mail": {"engine": "imap", "imap_host": "imap.mailish.example:993"}}, "unknown key"),
    ({"calendar": {"engine": "caldav", "url": "https://{host}/dav/"}}, "scheme, host and port"),
    ({"calendar": {"engine": "caldav", "url": "http://dav.mailish.example/{address}"}},
     "scheme, host and port"),
    ({"calendar": {"engine": "caldav",
                   "url": "https://dav.mailish.example/{address}?k={app_password}"}},
     "secret may never go in a URL"),
    ({"mail": {"engine": "imap", "every_min": 90}}, "between 1 and 30"),
    ({"hosts": ["192.168.1.4"]}, "public host name"),
    ({"setup": {"fields": [{"name": "url", "type": "link", "optional": True,
                            "joins": "tasks"}]}}, "join a class the module lacks"),
    ({"setup": {"fields": [{"name": "url", "type": "link", "joins": "calendar"}]}},
     "or are not optional"),
    ({"setup": {"fields": [{"name": "imap", "type": "host", "advanced": True}]}},
     "under Advanced but not optional"),
    ({"methods": {"dav.mailish.example": ["GET", "TRACE"]}}, "not among"),
    ({"methods": {"dav.mailish.example": "POST"}}, "list of non-empty strings"),
    ({"methods": {"collect.example": ["POST"]}}, "not hosts the manifest names"),
    ({"reads": ["contacts"]}, "not among"),
    ({"reads": ["mail", "mail"]}, "named twice"),
    ({"prompt": "x" * 1_501}, "longer than 1500"),
    ({"skills": [{"title": "Plan", "when": "a trip", "steps": []}]}, "1 to 12 steps"),
    ({"skills": [{"title": "Plan", "when": "a trip", "steps": ["s"] * 13}]}, "1 to 12 steps"),
    ({"skills": [{"title": "Plan", "when": "a trip", "steps": ["s" * 1_001]}]},
     "a step longer"),
    ({"skills": [{"title": "Plan", "when": "a trip", "steps": ["s"], "grants": ["send"]}]},
     "unknown key"),
    ({"skills": [{"title": " ", "when": "a trip", "steps": ["s"]}]}, "may not be empty"),
    ({"skills": [{"title": "Plan", "when": "a trip", "steps": ["s"]},
                 {"title": "plan", "when": "a day", "steps": ["t"]}]}, "a skill named twice"),
    ({"skills": [{"title": f"Plan {n}", "when": "a trip", "steps": ["s"]}
                 for n in range(9)]}, "more than 8 skills"),
    ({"attention": {"looks_at": "notices", "judge": "Closures are now."}},
     "runs in the sandbox, from its `code`"),
    ({"tier": "sandbox", "code": "m.py", "attention": {"judge": "Closures are now."}},
     "looks_at"),
    ({"tier": "sandbox", "code": "m.py", "attention": {"looks_at": "notices", "judge": " "}},
     "may not be empty"),
    ({"tier": "sandbox", "code": "m.py",
      "attention": {"looks_at": "notices", "judge": "j" * 801}}, "longer than 800"),
    ({"tier": "sandbox", "code": "m.py",
      "attention": {"looks_at": "notices", "judge": "j", "every_min": 5}}, "between 15 and 1440"),
    ({"tier": "sandbox", "code": "m.py",
      "attention": {"looks_at": "notices", "judge": "j", "every_min": True}}, "int"),
    ({"tier": "sandbox", "code": "m.py",
      "attention": {"looks_at": "notices", "judge": "j", "sends": "mail"}}, "unknown key"),
    ({"routines": [{"title": "Tides", "instruction": "Say the tides.", "at": "7:30"}]}, "HH:MM"),
    ({"routines": [{"title": "Tides", "instruction": "Say the tides.", "at": "24:00"}]},
     "HH:MM"),
    ({"routines": [{"title": "Tides", "instruction": "Say the tides.", "at": 730}]}, "str"),
    ({"routines": [{"title": "Tides", "instruction": "i" * 1_001, "at": "07:30"}]},
     "longer than 1000"),
    ({"routines": [{"title": "Tides", "instruction": "Say.", "at": "07:30",
                    "days": ["sat", "funday"]}]}, "are not among"),
    ({"routines": [{"title": "Tides", "instruction": "Say.", "at": "07:30",
                    "every_min": 5}]}, "unknown key"),
    ({"routines": [SAIL, {**SAIL, "title": "tides"}]}, "a routine named twice"),
    ({"routines": [{**SAIL, "title": f"Tides {n}"} for n in range(5)]}, "more than 4 routines"),
])
def test_a_manifest_with_anything_unknown_or_unsafe_is_refused_saying_what(change, says):
    """A misspelled key silently defaulted is a module run under a reading
    nobody reviewed; a field in a URL's host would let an account's text
    choose where its password is sent."""
    with pytest.raises(ManifestError, match=says):
        parse(_with(**change))


def test_a_modules_words_for_steves_instructions_are_its_text_slots():
    """The slots are what the card and the Extensions page say a module fills;
    a prompt section or skills missing from them would enter Steve's
    instructions without the card naming that it does."""
    manifest = parse(_with(prompt="  Tide times are in local time.  ", skills=[
        {"title": "Plan a sail", "when": "a sail is asked about",
         "steps": ["Read the tides", "Say the window"], "tools": ["web_fetch"]}]))
    assert manifest.prompt == "Tide times are in local time."
    assert manifest.skills[0].steps == ("Read the tides", "Say the window")
    assert manifest.slots[-2:] == ("skill", "prompt")
    assert "prompt" not in parse(GMAIL_LIKE).slots


def test_a_modules_attention_source_and_routines_are_slots_it_fills_with_what_runs_unwatched():
    """What runs with no one watching is what the card must name: a slot
    left out would let a module look or act on its own unannounced."""
    manifest = parse(_with(
        tier="sandbox", code="harbour.py",
        attention={"looks_at": " the harbour's notices ", "judge": "Closures are now."},
        routines=[SAIL, {"title": "Weekend", "instruction": "Say if Saturday suits.",
                         "at": "18:00", "days": ["FRI"]}]))
    assert manifest.slots[-2:] == ("attention", "routine")
    assert (manifest.attention.looks_at, manifest.attention.every_min) == (
        "the harbour's notices", 60)
    assert [(r.at, r.days) for r in manifest.routines] == [("07:30", ()), ("18:00", ("fri",))]
    assert "attention" not in parse(GMAIL_LIKE).slots


NOTES = {"manifest_version": 1, "sdk": 1, "id": "notes", "label": "Notes",
         "for": "Notes kept on a server", "hosts": ["mcp.notes.example"]}
NOTES_URL = "https://mcp.notes.example/mcp"
TOKEN = {"name": "token", "type": "secret", "label": "Its token",
         "bind": {"host": "mcp.notes.example", "header": "Authorization", "form": "Bearer {}"}}


@pytest.mark.parametrize(("change", "says"), [
    ({"mcp": {}}, "url"),
    ({"mcp": {"url": "http://mcp.notes.example/mcp"}}, "http to a server on this computer"),
    ({"mcp": {"url": "https://elsewhere.example/mcp"}}, "not among the manifest's hosts"),
    ({"mcp": {"url": "https://ada:pw@mcp.notes.example/mcp"}}, "no name, password"),
    ({"mcp": {"url": "https://mcp.notes.example:x/mcp"}}, "port is not a number"),
    ({"mcp": {"url": NOTES_URL, "tools": ["search"]}}, "unknown key"),
    ({"mcp": {"url": NOTES_URL, "secret": "token"}}, "not a secret field"),
    ({"mcp": {"url": "https://mcp.notes.example:8443/mcp", "secret": "token"},
      "setup": {"fields": [TOKEN]}}, "bound to the server's host and port"),
    # A server on this computer takes no secret: one bound to it is refused
    # as any binding to a host not public is, and one bound elsewhere here.
    ({"mcp": {"url": "http://127.0.0.1:3365/mcp", "secret": "token"},
      "setup": {"fields": [TOKEN]}}, "bound to the server's host and port"),
])
def test_an_mcp_servers_address_and_secret_are_held_to_its_host_and_to_https(change, says):
    """The server's address is where core sends every call and its secret:
    a host the card does not name, plain HTTP past this computer, or a
    secret bound elsewhere would each send them where the user never
    agreed to."""
    with pytest.raises(ManifestError, match=says):
        parse({**NOTES, **change})


def test_an_mcp_server_is_a_slot_reached_at_a_host_the_manifest_names_or_on_this_computer():
    remote = parse({**NOTES, "setup": {"fields": [TOKEN]},
                    "mcp": {"url": NOTES_URL, "secret": "token"}})
    assert remote.slots[-1] == "mcp"
    assert (remote.mcp.host, remote.mcp.port, remote.mcp.loopback) == (
        "mcp.notes.example", 443, False)
    local = parse({**NOTES, "hosts": [], "mcp": {"url": "http://127.0.0.1:3365/mcp"}})
    assert (local.mcp.host, local.mcp.port, local.mcp.loopback) == ("127.0.0.1", 3365, True)
    assert "mcp" not in parse(GMAIL_LIKE).slots


FILER = {"manifest_version": 1, "sdk": 1, "id": "filer", "label": "Filer",
         "for": "Sorting the user's files", "effort": "about a minute", "reads": ["files"]}


@pytest.mark.parametrize("field", [
    {"name": "server", "type": "host", "optional": True},
    {"name": "page", "type": "link", "optional": True},
    {"name": "page", "type": "text", "form": "link", "optional": True},
])
def test_the_users_files_never_go_with_a_host_even_one_the_user_types(field):
    """A module that read the user's files and could reach a host could send
    them there in any request, whatever its code says it does; one whose
    host the user types is the same module with the host chosen later
    (06-modules.md, section 5)."""
    assert parse(FILER).reads == ("files",)
    assert parse({**FILER, "version": "2.0.1-beta+3"}).version == "2.0.1-beta+3"
    with pytest.raises(ManifestError, match="the user's files together with a host"):
        parse({**FILER, "setup": {"fields": [field]}})


def test_a_secret_binding_names_one_header_on_one_host():
    data = _with(setup={"steps": ["fields", "test"], "fields": [
        {"name": "token", "type": "secret",
         "bind": {"host": "api.mailish.example", "header": "Authorization",
                  "form": "Bearer {}"}}]})
    bind = parse(data).setup.field("token").bind
    assert (bind.host, bind.port, bind.header, bind.form) == (
        "api.mailish.example", 443, "Authorization", "Bearer {}")
    with pytest.raises(ManifestError, match="only a secret"):
        parse(_with(setup={"fields": [{"name": "host", "type": "text",
                                       "bind": {"host": "a.example", "header": "X"}}]}))


def test_a_preset_overrides_its_class_settings_and_adds_its_hosts():
    data = _with(setup={"steps": ["address", "fields", "test"], "presets": [
        {"id": "other_co", "label": "Other Co", "domains": ["other.example"],
         "mail": {"imap": "imap.other.example:993", "smtp": "smtp.other.example:587",
                  "starttls": True},
         "hosts": ["*.other.example"]}]})
    manifest = parse(data)
    assert manifest.settings("mail", "other_co")["imap"] == "imap.other.example:993"
    assert manifest.settings("mail", "other_co")["sent_copy"] == "server"
    assert "*.other.example" in manifest.fixed_hosts("other_co")


def test_a_host_is_reached_with_the_methods_declared_for_it_and_otherwise_only_read():
    """Core calls a module a reader only where it reaches its hosts with
    GET alone (06-modules.md, section 1), so a host declared with no
    methods is read from, never written to; a pattern covers names under it."""
    manifest = parse(_with(hosts=["*.cdn.mailish.example"], reads=["mail"],
                           methods={"dav.mailish.example": ["get", "PUT", "PUT"],
                                    "*.cdn.mailish.example": ["POST"]}))
    assert manifest.methods_for("DAV.mailish.example.") == ("GET", "PUT")
    assert manifest.methods_for("p1.cdn.mailish.example") == ("POST",)
    assert manifest.methods_for("cdn.mailish.example") == ("GET", "HEAD")
    assert manifest.methods_for("imap.mailish.example") == ("GET", "HEAD")
    assert manifest.reads == ("mail",)


TOOLER = {"manifest_version": 1, "sdk": 1, "id": "tooler", "label": "Tooler",
          "for": "Counting things", "tier": "sandbox", "code": "tooler.py",
          "tools": [{"name": "count", "class": "read", "description": "Counts the things.",
                     "params": [
                         {"name": "things", "type": "string", "description": "What to count"},
                         {"name": "most", "type": "integer", "description": "At most",
                          "optional": True}]}]}


def test_a_tools_schema_is_core_s_from_the_types_and_its_arguments_are_checked_against_it():
    """The model is shown a schema core built, not one the module wrote, and
    what comes back is checked against it before the module is started: a
    stray argument, a missing one or a wrong type never reaches its code.
    An optional one a model sends as null is as good as left out."""
    tool = parse(TOOLER).tools[0]
    assert tool.schema() == {
        "type": "object",
        "properties": {"things": {"type": "string", "description": "What to count"},
                       "most": {"type": "integer", "description": "At most"}},
        "required": ["things"], "additionalProperties": False}
    assert tool.problems({"things": "sheep"}) is None
    assert tool.problems({"things": "sheep", "most": None}) is None
    assert tool.problems({"things": "sheep", "most": 3}) is None
    assert tool.problems({"most": 3}) == "things is required"
    assert tool.problems({"things": "sheep", "most": True}) == "most is not of type integer"
    assert tool.problems({"things": "sheep", "most": "3"}) == "most is not of type integer"
    assert tool.problems({"things": "sheep", "path": "C:/"}) == "it takes no path"
    assert tool.problems(["sheep"]) == "its arguments are not an object"


@pytest.mark.parametrize(("change", "says"), [
    ({"tier": "backend"}, "run in the sandbox"),
    ({"code": ""}, "run in the sandbox"),
    ({"tools": [{"name": "perform", "class": "read", "description": "x"}]}, "is perform"),
    ({"tools": [{"name": "Count", "class": "read", "description": "x"}]}, "is not"),
    ({"tools": [{"name": "count", "class": "write", "description": "x"}]}, "class"),
    ({"tools": [{"name": "count", "class": "read", "description": ""}]}, "empty"),
    ({"tools": [{"name": "count", "class": "read", "description": "x" * 1001}]}, "longer"),
    ({"tools": [{"name": "count", "class": "read", "description": "x"}] * 2}, "named twice"),
    ({"tools": [{"name": "count", "class": "read", "description": "x", "params": [
        {"name": "ctx", "type": "string", "description": "x"}]}]}, "or is ctx"),
    ({"tools": [{"name": "count", "class": "read", "description": "x", "params": [
        {"name": "p", "type": "object", "description": "x"}]}]}, "type"),
    ({"tools": [{"name": "count", "class": "read", "description": "x", "run": "os"}]},
     "run"),
])
def test_a_tool_runs_only_in_the_sandbox_named_and_typed_as_core_allows(change, says):
    """A tool is code the model can start, so it runs where nothing else
    of the user's is (`code`, tier sandbox), under a name that cannot be
    `perform` (what runs on a card's yes) and parameters of the four plain
    types core can check."""
    with pytest.raises(ManifestError, match=says):
        parse({**TOOLER, **change})


KEY = {"name": "key", "type": "secret", "label": "Its key",
       "bind": {"host": "api.tooler.example", "header": "Authorization", "form": "Bearer {}"}}


@pytest.mark.parametrize("manifest", [
    {**TOOLER, "hosts": ["api.tooler.example"], "setup": {"fields": [KEY]}},
    {**NOTES, "setup": {"fields": [TOKEN, {"name": "spare", "type": "secret"}]},
     "mcp": {"url": NOTES_URL, "secret": "token"}},
], ids=["a tool module's key", "a server module's second secret"])
def test_a_secret_is_declared_only_where_steve_keeps_one(manifest):
    """A secret is kept for an account, and an account needs one of the
    module's classes (`core/modules.add_account`); a module with none keeps
    only its MCP server's, entered on its card. A tool module's key had
    nowhere to be entered, and its tool's every call ran without it
    (`core/sandbox/run.call`: no account, no secret), so a request the
    manifest bound the key to went out with no key (the review of steps 5
    and 6, 15 October). Such a manifest is refused where it is read."""
    with pytest.raises(ManifestError, match="no account to keep"):
        parse(manifest)


def test_an_accounts_secrets_and_a_servers_own_are_kept_and_so_declared():
    """The two places a secret is kept: an account's, entered when it is
    added, and a server module's own, entered on its card."""
    assert parse(GMAIL_LIKE).secrets
    served = parse({**NOTES, "setup": {"fields": [TOKEN]},
                    "mcp": {"url": NOTES_URL, "secret": "token"}})
    assert served.secrets == ("token",)


@pytest.mark.parametrize("answered", [[], ["<a@x>", "<b@x>"], ["<b@x>"]])
def test_exact_one_refuses_none_several_or_the_wrong_item(answered):
    """The server's answer is never the target: two that match, or one with
    another Message-ID, is a refusal, not a guess."""
    items = [classes.Message(native_id=f"n{i}", ident=m) for i, m in enumerate(answered)]
    with pytest.raises(TargetChanged):
        exact.one(items, expected="<a@x>", ident=lambda m: m.ident)


def test_exact_one_accepts_the_one_item_with_brackets_and_case_aside():
    item = classes.Message(native_id="n1", ident="<A@X>")
    assert exact.one([item], expected="a@x", ident=lambda m: m.ident) is item


def test_an_empty_identifying_field_names_no_item_so_exact_one_counts_only():
    """"" is no Message-ID: taken for one, it matched every item that has
    none, as classic Outlook's drafts all are, and refused any that has one.
    Empty, it is no field to check by, as None is; one answer is still the
    most there may be."""
    item = classes.Message(native_id="n1", ident="<a@x>")
    assert exact.one([item], expected="", ident=lambda m: m.ident) is item
    with pytest.raises(TargetChanged):
        exact.one([classes.Message(native_id="n1"), classes.Message(native_id="n2")],
                  expected="", ident=lambda m: m.ident)


def test_changed_only_names_anything_beyond_the_one_item():
    before = {"a": "1", "b": "1", "c": "1"}
    exact.changed_only(before, {"a": "1", "c": "1"}, target="b", gone=True)
    exact.changed_only(before, {"a": "1", "b": "2", "c": "1"}, target="b")
    with pytest.raises(AfterWrite):
        exact.changed_only(before, {"a": "1"}, target="b", gone=True)
    with pytest.raises(AfterWrite):
        exact.changed_only(before, {"a": "2", "b": "2", "c": "1"}, target="b")


def test_a_messages_digest_binds_every_recipient_the_subject_and_the_body():
    """A card shows Cc and Bcc; a draft whose Cc changed after the card must
    not match it, while a reordered To list is the same message."""
    shown = classes.Message(native_id="d", to="a@x, b@x", subject="Hi", body="Text")
    assert shown.digest() == classes.Message(native_id="d", to="B@x,a@x", subject="Hi",
                                             body="Text").digest()
    for changed in (dict(cc="eve@x"), dict(bcc="eve@x"), dict(subject="Hi!"),
                    dict(body="Text."), dict(attachments=("x.pdf",))):
        other = classes.Message(native_id="d", to="a@x, b@x", subject="Hi", body="Text")
        for k, v in changed.items():
            setattr(other, k, v)
        assert other.digest() != shown.digest(), changed


def test_an_events_digest_binds_its_times_and_guests():
    start = dt.datetime(2026, 10, 14, 9, tzinfo=dt.UTC)
    shown = classes.Event(native_id="e", title="Dentist", start=start,
                          end=start + dt.timedelta(hours=1))
    moved = classes.Event(native_id="e", title="Dentist", start=start + dt.timedelta(days=1),
                          end=start + dt.timedelta(days=1, hours=1))
    guest = classes.Event(native_id="e", title="Dentist", start=start,
                          end=start + dt.timedelta(hours=1),
                          guests=(classes.Guest("someone@x"),))
    assert len({shown.digest(), moved.digest(), guest.digest()}) == 3
