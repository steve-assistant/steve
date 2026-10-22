"""Channel message parsing.

The Signal envelope shapes here are the real ones, because the Note to Self
case is genuinely counter-intuitive: a message you send yourself does not
arrive as a `dataMessage` at all. It arrives as a *sync* message reporting
what your phone sent, with the destination set to your own number. Reading
only `dataMessage` produces a channel that connects, reports healthy, and
silently never sees anything.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from channels.base import Capability, Incoming, Outgoing
from channels.signal import SignalChannel

ACCOUNT = "+14125550123"
OTHER = "+14125550187"


@pytest.fixture
def channel():
    return SignalChannel(account=ACCOUNT)


def _sync_envelope(text: str, *, destination: str = ACCOUNT, attachments=None) -> dict:
    """What Signal delivers when you message yourself from your phone."""
    return {
        "envelope": {
            "source": ACCOUNT,
            "sourceNumber": ACCOUNT,
            "timestamp": 1_758_000_000_000,
            "syncMessage": {
                "sentMessage": {
                    "message": text,
                    "destination": destination,
                    "destinationNumber": destination,
                    "timestamp": 1_758_000_000_000,
                    "attachments": attachments or [],
                }
            },
        }
    }


def _data_envelope(text: str, *, sender: str = OTHER) -> dict:
    """An ordinary inbound message from somebody else."""
    return {
        "envelope": {
            "source": sender,
            "sourceNumber": sender,
            "timestamp": 1_758_000_000_001,
            "dataMessage": {"message": text, "timestamp": 1_758_000_000_001},
        }
    }


# --------------------------------------------------------------------------
# Note to Self
# --------------------------------------------------------------------------


def test_a_note_to_self_is_recognised(channel):
    """The whole channel depends on this one shape."""
    message = channel._to_incoming(_sync_envelope("what's on my calendar?"))
    assert message is not None
    assert message.text == "what's on my calendar?"
    assert message.channel == "signal"
    assert message.is_voice is False


def test_a_sync_message_to_someone_else_is_ignored(channel):
    """Your phone syncs everything you send. Only the self-thread is ours."""
    assert channel._to_incoming(_sync_envelope("hi", destination=OTHER)) is None


def test_messages_from_other_people_are_ignored(channel):
    """A linked device sees the whole account. Acting only on Note to Self is
    what keeps a bug from replying to somebody's actual conversation."""
    assert channel._to_incoming(_data_envelope("hey Maya")) is None


def test_other_conversations_can_be_opted_into(channel):
    """The restriction is a policy, not a limitation; prove it is the former."""
    channel.note_to_self_only = False
    message = channel._to_incoming(_data_envelope("hey Maya"))
    assert message is not None
    assert message.sender == OTHER


# --------------------------------------------------------------------------
# Things that are not messages
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "envelope",
    [
        {"envelope": {"receiptMessage": {"when": 1}}},
        {"envelope": {"typingMessage": {"action": "STARTED"}}},
        {"envelope": {"syncMessage": {"readMessages": []}}},
        {"envelope": {}},
        {},
    ],
    ids=["receipt", "typing", "read-sync", "empty-envelope", "empty"],
)
def test_non_messages_are_ignored(channel, envelope):
    """Receipts and typing indicators outnumber real messages on a busy
    account; treating one as a turn would have the agent answer a keystroke."""
    assert channel._to_incoming(envelope) is None


def test_an_empty_message_with_no_attachment_is_ignored(channel):
    assert channel._to_incoming(_sync_envelope("   ")) is None


# --------------------------------------------------------------------------
# Voice notes
# --------------------------------------------------------------------------


def test_a_voice_note_is_marked_for_transcription(channel, tmp_path, monkeypatch):

    audio = tmp_path / "attachments" / "abc123"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"fake ogg")
    monkeypatch.setenv("STEVE_SIGNAL_DATA", str(tmp_path))

    message = channel._to_incoming(
        _sync_envelope(
            "",
            attachments=[
                {"id": "abc123", "contentType": "audio/ogg", "voiceNote": True}
            ],
        )
    )
    assert message is not None
    assert message.is_voice
    assert message.audio_path == audio


def test_a_voice_note_survives_having_no_text(channel, tmp_path, monkeypatch):
    """A voice note usually has an empty message body; the earlier
    empty-text check must not discard it."""

    audio = tmp_path / "attachments" / "v1"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"ogg")
    monkeypatch.setenv("STEVE_SIGNAL_DATA", str(tmp_path))

    message = channel._to_incoming(
        _sync_envelope("", attachments=[{"id": "v1", "voiceNote": True}])
    )
    assert message is not None and message.is_voice


def test_an_attachment_that_never_downloaded_is_skipped(channel, tmp_path, monkeypatch):
    """signal-cli reports the attachment before the file exists on disk if
    downloading failed; pointing Whisper at a missing path would crash a turn."""

    monkeypatch.setenv("STEVE_SIGNAL_DATA", str(tmp_path))
    message = channel._to_incoming(
        _sync_envelope("look", attachments=[{"id": "missing", "voiceNote": True}])
    )
    assert message is not None
    assert message.audio_path is None
    assert message.attachments == []


def test_a_photo_is_an_attachment_not_speech(channel, tmp_path, monkeypatch):
    """Only audio should be routed to transcription."""

    image = tmp_path / "attachments" / "img1"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"jpeg")
    monkeypatch.setenv("STEVE_SIGNAL_DATA", str(tmp_path))

    message = channel._to_incoming(
        _sync_envelope("look at this", attachments=[{"id": "img1", "contentType": "image/jpeg"}])
    )
    assert message is not None
    assert message.is_voice is False
    assert message.attachments == [image]
    assert message.images == [image]


def test_a_photo_sent_with_no_words_is_a_message(channel, tmp_path, monkeypatch):
    """It was dropped, unanswered: the check for an empty message knew only
    words and voice notes (13 October 2026)."""
    monkeypatch.setenv("STEVE_SIGNAL_DATA", str(tmp_path))
    _stored(tmp_path, "img1")

    message = channel._to_incoming(
        _sync_envelope("", attachments=[{"id": "img1", "contentType": "image/jpeg"}]))

    assert message is not None and message.images == [tmp_path / "attachments" / "img1"]


def test_a_file_steve_cannot_open_is_named_not_dropped(channel, tmp_path, monkeypatch):
    monkeypatch.setenv("STEVE_SIGNAL_DATA", str(tmp_path))
    _stored(tmp_path, "doc1", "img1")

    message = channel._to_incoming(_sync_envelope("", attachments=[
        {"id": "doc1", "contentType": "application/pdf", "filename": "lease.pdf"},
        {"id": "img1", "contentType": "image/heic"},
    ]))

    assert message is not None and message.images == []
    assert message.unopened == ["lease.pdf, a file Steve cannot open",
                                "a photo in a format Steve cannot read (image/heic)"]


def test_a_voice_note_is_heard_and_a_photo_with_it_is_shown(channel, tmp_path, monkeypatch):
    monkeypatch.setenv("STEVE_SIGNAL_DATA", str(tmp_path))
    _stored(tmp_path, "v1", "img1")

    message = channel._to_incoming(_sync_envelope("", attachments=[
        {"id": "v1", "contentType": "audio/aac", "voiceNote": True},
        {"id": "img1", "contentType": "image/png"},
    ]))

    assert message.audio_path == tmp_path / "attachments" / "v1"
    assert message.images == [tmp_path / "attachments" / "img1"]


@pytest.mark.parametrize("name", ["../keys.json", "..", "sub/x.jpg", "C:x.jpg"])
def test_a_note_to_self_names_no_file_outside_the_attachment_folder(
        channel, tmp_path, monkeypatch, name):
    """A photo's file is read and sent to the model: a name that climbed
    out of the folder would send whatever it named."""
    folder = tmp_path / "signal"
    monkeypatch.setenv("STEVE_SIGNAL_DATA", str(folder))
    _stored(folder, "x.jpg")
    (folder / "keys.json").write_text("{}", encoding="utf-8")
    (folder / "attachments" / "sub").mkdir()
    (folder / "attachments" / "sub" / "x.jpg").write_bytes(b"\xff\xd8\xff")

    message = channel._to_incoming(_sync_envelope(
        "look", attachments=[{"id": name, "contentType": "image/jpeg"}]))

    assert message is not None and message.images == [] and message.attachments == []


def _stored(folder, *names: str) -> None:
    (folder / "attachments").mkdir(parents=True, exist_ok=True)
    for name in names:
        (folder / "attachments" / name).write_bytes(b"a photo")


def _left(folder) -> set[str]:
    return {p.name for p in (folder / "attachments").iterdir()}


def test_attachments_of_the_users_other_chats_are_deleted_as_they_arrive(
        channel, tmp_path, monkeypatch):
    """signal-cli downloads every chat's attachments, and Steve reads only
    Note to Self: a friend's photos would pile up in the data folder,
    unread and never removed, quotes and thumbnails too."""
    monkeypatch.setenv("STEVE_SIGNAL_DATA", str(tmp_path))
    _stored(tmp_path, "theirs.jpg", "thumb.jpg", "sent-to-a-friend.jpg", "mine.m4a")
    from_a_friend = _data_envelope("look")
    from_a_friend["envelope"]["dataMessage"].update(
        attachments=[{"id": "theirs.jpg", "contentType": "image/jpeg"}],
        quote={"attachments": [{"contentType": "image/jpeg",
                                "thumbnail": {"id": "thumb.jpg"}}]})

    assert channel._to_incoming(from_a_friend) is None
    assert channel._to_incoming(_sync_envelope(
        "for you", destination=OTHER,
        attachments=[{"id": "sent-to-a-friend.jpg", "contentType": "image/jpeg"}])) is None

    assert _left(tmp_path) == {"mine.m4a"}


def test_a_note_to_self_keeps_its_attachments(channel, tmp_path, monkeypatch):
    monkeypatch.setenv("STEVE_SIGNAL_DATA", str(tmp_path))
    _stored(tmp_path, "mine.m4a")

    message = channel._to_incoming(_sync_envelope(
        "", attachments=[{"id": "mine.m4a", "contentType": "audio/mp4", "voiceNote": True}]))

    assert message is not None and _left(tmp_path) == {"mine.m4a"}


@pytest.mark.parametrize("name", ["../keys.json", "..", "sub/x.jpg", "C:x.jpg"])
def test_an_attachment_name_from_the_network_deletes_nothing_outside_its_folder(
        channel, tmp_path, monkeypatch, name):
    folder = tmp_path / "signal"
    monkeypatch.setenv("STEVE_SIGNAL_DATA", str(folder))
    _stored(folder, "x.jpg")
    (folder / "keys.json").write_text("{}", encoding="utf-8")
    (folder / "attachments" / "sub").mkdir()
    (folder / "attachments" / "sub" / "x.jpg").write_bytes(b"kept")
    envelope = _data_envelope("look")
    envelope["envelope"]["dataMessage"]["attachments"] = [{"id": name}]

    channel._to_incoming(envelope)

    assert (folder / "keys.json").exists() and (folder / "attachments" / "sub" / "x.jpg").exists()
    assert (folder / "attachments" / "x.jpg").exists()


# --------------------------------------------------------------------------
# The contract
# --------------------------------------------------------------------------


def test_signal_declares_what_it_can_do(channel):
    for capability in (Capability.TEXT, Capability.VOICE_IN, Capability.VOICE_OUT):
        assert channel.supports(capability)


def test_status_never_leaks_the_phone_number_structure(channel):
    """status() is rendered in the UI and pasted into issues."""
    status = channel.status()
    assert status["channel"] == "signal"
    assert "capabilities" in status


def test_incoming_serialises_without_the_raw_envelope():
    """The raw envelope carries the full Signal payload; it should not end up
    in a log line or a websocket frame by accident."""
    message = Incoming(
        text="hi", channel="signal", sender=ACCOUNT,
        message_id="1", timestamp=1, raw={"secret": "envelope"},
    )
    assert "raw" not in message.as_dict()
    assert "secret" not in str(message.as_dict())


# --------------------------------------------------------------------------
# Blocking
# --------------------------------------------------------------------------
#
# signal-cli takes an exclusive lock on the account directory, so any call
# made while a daemon is running waits for it -- and on Windows the launcher
# is a .bat, so `timeout=` does not save you either: killing the shim leaves
# the JVM holding the pipes. A single blocking call from async code took the
# whole worker's event loop down, health endpoint included.


def test_constructing_a_channel_does_not_shell_out(monkeypatch):
    """__init__ runs inside the worker's startup path. Resolving the account
    here is what hung it."""
    import channels.signal as mod

    def forbidden(*a, **k):
        raise AssertionError("SignalChannel.__init__ shelled out to signal-cli")

    monkeypatch.setattr(mod, "accounts", forbidden)
    monkeypatch.setattr(mod.subprocess, "run", forbidden)
    monkeypatch.setattr(mod.subprocess, "Popen", forbidden)

    channel = mod.SignalChannel()
    assert channel.account is None  # resolved by start(), not here


async def test_start_reaps_before_it_reads_the_account(monkeypatch):
    """Order matters: a leftover JVM holds the lock, so asking which accounts
    exist before reaping is a call that never returns."""
    import channels.signal as mod

    order = []
    monkeypatch.setattr(mod, "unready", lambda: order.append("checked"))
    monkeypatch.setattr(mod, "reap_daemons", lambda: order.append("reap") or [])
    monkeypatch.setattr(mod, "accounts", lambda: order.append("accounts") or [])

    channel = mod.SignalChannel()
    with pytest.raises(RuntimeError, match="no linked Signal account"):
        await channel.start()

    assert order == ["checked", "reap", "accounts"]


async def test_a_changed_add_on_is_refused_at_every_start_and_link_before_anything_runs(
        monkeypatch):
    """Only the worker's first start was checked: the restart after a crash
    (`_revive`, which comes back through `start`) and a link made from
    Channels ran signal-cli unchecked, so a jar replaced in between ran
    holding the user's Signal keys."""
    import channels.signal as mod

    why = "Java has changed since it was installed, so it is not run; set it up again"
    monkeypatch.setattr(mod, "unready", lambda: why)

    def ran(*a, **k):
        raise AssertionError("signal-cli was run")

    monkeypatch.setattr(mod, "reap_daemons", ran)
    monkeypatch.setattr(mod, "accounts", ran)
    monkeypatch.setattr(mod.subprocess, "Popen", ran)

    with pytest.raises(mod.Unready, match="^Java has changed"):
        await mod.SignalChannel().start()
    with pytest.raises(mod.Unready, match="^Java has changed"):
        mod.start_link()


def test_a_hung_listaccounts_is_killed_not_waited_on(monkeypatch):
    """`subprocess.run(timeout=...)` is not enough -- the JVM survives the
    shim and keeps the pipe open, so the call blocks past its own timeout."""
    import channels.signal as mod

    killed = []

    class Hanging:
        pid = 4321
        returncode = None

        def communicate(self, timeout=None):
            raise mod.subprocess.TimeoutExpired("signal-cli", timeout or 0)

        def poll(self):
            return None

    monkeypatch.setattr(mod, "installed", lambda: True)
    monkeypatch.setattr(mod.subprocess, "Popen", lambda *a, **k: Hanging())
    monkeypatch.setattr(mod, "_kill_tree", lambda proc: killed.append(proc.pid))

    assert mod.accounts() == []
    assert killed == [4321], "left a signal-cli JVM holding the account lock"


def test_reaping_only_touches_our_own_account_directory(monkeypatch):
    """Matched on the config path so a signal-cli the user runs for their own
    purposes is not collateral."""
    import channels.signal as mod

    monkeypatch.setattr(mod.sys, "platform", "win32")
    ours = f"java -classpath x --config {mod.signal_data()} daemon"
    listing = json.dumps(
        [
            {"ProcessId": 11, "CommandLine": ours},
            {"ProcessId": 22, "CommandLine": "java -jar somebody-elses-signal-cli.jar"},
            {"ProcessId": 33, "CommandLine": None},
        ]
    )

    calls = []

    def fake_run(cmd, **kwargs):
        if cmd[0] == "powershell":
            return subprocess.CompletedProcess(cmd, 0, stdout=listing, stderr="")
        calls.append(cmd[2])  # taskkill /PID <pid>
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    assert mod.reap_daemons() == [11]
    assert calls == ["11"]


# --------------------------------------------------------------------------
# Sending
# --------------------------------------------------------------------------


async def _captured_send(channel, reply):
    """Run send() against a stubbed RPC and return the params it built."""
    seen = {}

    async def fake_rpc(method, params=None):
        seen["method"] = method
        seen["params"] = params or {}
        return {}

    channel._rpc = fake_rpc
    assert await channel.send(reply) is True
    return seen["params"]


async def test_a_reply_to_them_is_an_ordinary_message_so_their_phone_buzzes(channel):
    """`noteToSelf` is signal-cli's silent path -- "without notification" --
    and every reply and ping went that way, unseen until the user opened Signal."""
    params = await _captured_send(channel, Outgoing(text="Your 4pm moved to 5"))

    assert params["recipient"] == [ACCOUNT]
    assert params["notifySelf"] is True
    assert "noteToSelf" not in params


async def test_a_message_to_someone_else_is_not_flagged_as_to_themselves(channel):
    channel.note_to_self_only = False
    seen = {}

    async def fake_rpc(method, params=None):
        seen.update(params or {})
        return {}

    channel._rpc = fake_rpc
    await channel.send(Outgoing(text="hi"), to=OTHER)

    assert seen["recipient"] == [OTHER] and "notifySelf" not in seen


async def test_its_own_message_coming_back_is_not_read_as_theirs(channel):
    """Sent as an ordinary message to their own number, a reply read back as
    their would be answered, and the answer would come back too."""
    async def fake_rpc(method, params=None):
        return {"timestamp": 1_758_000_000_000}

    channel._rpc = fake_rpc
    await channel.send(Outgoing(text="what's on my calendar?"))

    assert channel._to_incoming(_sync_envelope("what's on my calendar?")) is None
    channel.note_to_self_only = False
    echo = _data_envelope("what's on my calendar?", sender=ACCOUNT)
    echo["envelope"]["timestamp"] = echo["envelope"]["dataMessage"]["timestamp"] = 1_758_000_000_000
    assert channel._to_incoming(echo) is None


async def test_what_was_sent_is_remembered_by_its_timestamp(channel):
    """A reaction names its target only by signal-cli's timestamp for it, so
    this is the only way to learn whether a thumbs-up was on a card."""
    async def fake_rpc(method, params=None):
        return {"timestamp": 1695460000123, "results": []}

    channel._rpc = fake_rpc
    await channel.send(Outgoing(text="Reply YES MAPLE to go ahead"))

    assert channel.sent_text(1695460000123) == "Reply YES MAPLE to go ahead"
    assert channel.sent_text(42) is None


async def test_which_sends_were_cards_is_remembered_with_them(channel, monkeypatch):
    """A reaction answers a card only on a message sent as one: text that
    merely holds a card's line is not one (step 11's audit)."""
    from channels import signal as signal_module

    monkeypatch.setattr(signal_module, "SENT_REMEMBERED", 2)
    stamps = iter(range(1, 10))

    async def fake_rpc(method, params=None):
        return {"timestamp": next(stamps)}

    channel._rpc = fake_rpc
    await channel.send(Outgoing(text="Reply YES MAPLE to go ahead, NO MAPLE to drop it.", card=7))
    await channel.send(Outgoing(text="Want it? Reply YES COPPER to go ahead."))

    assert channel.sent_card(1) == 7 and channel.sent_card(2) is None
    await channel.send(Outgoing(text="Done."))
    assert channel.sent_card(1) is None and channel._cards == {}


async def test_only_the_most_recent_sends_are_remembered(channel, monkeypatch):
    from channels import signal as signal_module

    monkeypatch.setattr(signal_module, "SENT_REMEMBERED", 3)
    stamps = iter(range(1, 10))

    async def fake_rpc(method, params=None):
        return {"timestamp": next(stamps)}

    channel._rpc = fake_rpc
    for i in range(5):
        await channel.send(Outgoing(text=f"message {i}"))

    assert channel.sent_text(1) is None
    assert channel.sent_text(5) == "message 4"


async def test_a_text_reply_carries_no_attachment_keys(channel):
    params = await _captured_send(channel, Outgoing(text="just words"))
    assert "attachment" not in params
    assert "voiceNote" not in params
    assert params["message"] == "just words"


async def test_markdown_is_stripped_on_the_way_out(channel):
    """Signal carries formatting as style ranges, not syntax."""
    params = await _captured_send(channel, Outgoing(text="it is **63F** now"))
    assert params["message"] == "it is 63F now"


# --------------------------------------------------------------------------
# Platform assumptions
# --------------------------------------------------------------------------
#
# This module once ran on Linux too, and three separate deploys failed on
# things that were only ever true of the laptop. These pin the ones in this
# file.


def test_java_home_is_only_forced_on_windows(monkeypatch):
    """A Windows path exported on Linux produces "JAVA_HOME is set to an
    invalid directory" and a link attempt that returns no URI -- on a machine
    with a perfectly good Java 25 installed."""
    import channels.signal as mod

    monkeypatch.setattr(mod.sys, "platform", "linux")
    monkeypatch.setenv("JAVA_HOME", "/usr/lib/jvm/temurin-25-jre-amd64")
    assert mod._env()["JAVA_HOME"] == "/usr/lib/jvm/temurin-25-jre-amd64"


def test_windows_pins_its_jdk_to_the_add_on_regardless_of_the_environment(monkeypatch):
    """The original reason for pinning still holds where it applies: several
    JDKs are usually installed and the default is too old for signal-cli.
    It was a fixed Adoptium path that only one laptop had; it is the
    add-on's Java now (core/addons.py)."""
    import channels.signal as mod
    from core import addons

    monkeypatch.setattr(mod.sys, "platform", "win32")
    monkeypatch.setenv("JAVA_HOME", r"C:\Some\Old\jdk-23")
    assert mod._env()["JAVA_HOME"] == str(addons.home(addons.JAVA))


def test_a_linux_host_without_java_home_is_left_alone(monkeypatch):
    """`java` on PATH is the normal Linux case; inventing a JAVA_HOME would
    override a working install with a guess."""
    import channels.signal as mod

    monkeypatch.setattr(mod.sys, "platform", "linux")
    monkeypatch.delenv("JAVA_HOME", raising=False)
    assert "JAVA_HOME" not in mod._env()


def test_no_windows_path_is_exported_unconditionally():
    """The general form of the bug. A drive-letter path assigned outside a
    win32 branch is a laptop assumption that will travel to any other
    platform."""
    import re
    from pathlib import Path

    source = Path("channels/signal.py").read_text(encoding="utf-8")
    for match in re.finditer(r'^([A-Z_]+)\s*=\s*r?"[A-Za-z]:\\?', source, re.M):
        name = match.group(1)
        assert "WINDOWS" in name, (
            f"{name} is a Windows path with a platform-neutral name; either "
            "rename it so the assumption is visible, or make it conditional"
        )


def test_the_add_on_runs_on_its_own_java_with_every_jar_and_no_launcher(monkeypatch):
    """signal-cli's launcher .bat names its eighty jars on one line, and
    under the installed path (`...\\Programs\\Steve\\addons\\...`) cmd
    refuses a line that long, so no start would work. Java reads `lib\\*`."""
    import channels.signal as mod
    from core import addons

    monkeypatch.delenv(mod.CLI_ENV, raising=False)
    monkeypatch.setattr(mod.sys, "platform", "win32")

    java, *flags, main, config, folder, command = mod._cli("listAccounts")

    assert java == str(addons.home(addons.JAVA) / "bin" / "java.exe")
    assert flags == ["--enable-native-access=ALL-UNNAMED", "-classpath",
                     str(addons.home(addons.SIGNAL_CLI) / "lib" / "*")]
    assert (main, config, folder, command) == ("org.asamk.signal.Main", "--config",
                                               str(mod.signal_data()), "listAccounts")


def test_a_program_named_in_the_environment_is_run_as_named_and_not_checked(
        monkeypatch, tmp_path):
    """A test's fake, or a developer's own signal-cli: run as it is, with
    nothing hashed, as long as it is there."""
    import channels.signal as mod

    program = tmp_path / "signal-cli.bat"
    monkeypatch.setenv(mod.CLI_ENV, str(program))

    assert mod.unready() == f"signal-cli is not at {program}" and not mod.installed()
    program.write_text("@echo off\n", encoding="utf-8")
    assert mod.unready() is None and mod.installed()
    assert mod._cli("listAccounts")[:2] == [str(program), "--config"]


def test_an_install_runs_the_add_on_whatever_program_the_environment_names(
        monkeypatch, tmp_path):
    """Anything could name a program in the user's environment, and it
    would run holding their Signal keys, unchecked: only a source tree has
    a test's fake or a developer's own."""
    import channels.signal as mod
    from core import addons

    program = tmp_path / "signal-cli.bat"
    program.write_text("@echo off\n", encoding="utf-8")
    monkeypatch.setenv(mod.CLI_ENV, str(program))
    monkeypatch.setattr(mod.paths, "is_source_tree", lambda: False)

    assert mod.override() is None
    assert mod.signal_cli() == addons.home(addons.SIGNAL_CLI)


@pytest.mark.parametrize("name", sorted(["JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS",
                                         "_JAVA_OPTIONS"]))
def test_signal_clis_java_is_given_no_option_that_loads_code_into_it(monkeypatch, name):
    """Java reads these at every start, and `-javaagent:` in any of them
    runs code inside it, holding the user's Signal keys."""
    import channels.signal as mod

    monkeypatch.setenv(name, "-javaagent:C:/elsewhere/agent.jar")
    monkeypatch.setenv("STEVE_SIGNAL_DATA", "kept")

    env = mod._env()

    assert name not in env and env["STEVE_SIGNAL_DATA"] == "kept"


def test_the_add_ons_are_checked_before_signal_cli_is_run(monkeypatch):
    """A jar changed since it was installed runs holding the user's Signal
    keys. `unready` is asked before every start and every link."""
    import channels.signal as mod
    from core import addons

    monkeypatch.delenv(mod.CLI_ENV, raising=False)
    checked = []

    def verify(addon):
        checked.append(addon.name)
        return "Java has changed since it was installed" if addon is addons.JAVA else None

    monkeypatch.setattr(addons, "verify", verify)

    assert mod.unready() == "Java has changed since it was installed"
    assert checked == ["signal-cli", "java"]


# --------------------------------------------------------------------------
# Reactions
# --------------------------------------------------------------------------


async def test_a_reaction_targets_the_message_by_author_and_timestamp(channel):
    """Signal has no message ids on the wire -- a reaction names the author
    and the message's own timestamp, which is why Incoming carries both."""
    seen = {}

    async def fake_rpc(method, params=None):
        seen["method"] = method
        seen["params"] = params
        return {}

    channel._rpc = fake_rpc
    message = Incoming(
        text="hi", channel="signal", sender=ACCOUNT, message_id="1",
        timestamp=1_758_000_000_000,
    )
    await channel.react(message, "\N{EYES}")

    assert seen["method"] == "sendReaction"
    assert seen["params"]["targetAuthor"] == ACCOUNT
    assert seen["params"]["targetTimestamp"] == 1_758_000_000_000
    assert seen["params"]["remove"] is False
    assert seen["params"]["noteToSelf"] is True


async def test_removing_a_reaction_sets_the_remove_flag(channel):
    seen = {}

    async def fake_rpc(method, params=None):
        seen.update(params or {})
        return {}

    channel._rpc = fake_rpc
    message = Incoming(text="", channel="signal", sender=ACCOUNT,
                       message_id="1", timestamp=123)
    await channel.react(message, "\N{EYES}", remove=True)
    assert seen["remove"] is True


async def test_a_message_with_no_timestamp_is_not_reacted_to(channel):
    """Signal cannot address a reaction without one, and sending a malformed
    request is worse than skipping the decoration."""
    called = []

    async def fake_rpc(method, params=None):
        called.append(method)
        return {}

    channel._rpc = fake_rpc
    message = Incoming(text="hi", channel="signal", sender=ACCOUNT,
                       message_id="1", timestamp=0)
    await channel.react(message, "\N{EYES}")
    assert called == []


def test_signal_declares_that_it_can_react(channel):
    assert channel.supports(Capability.REACTIONS)


# --------------------------------------------------------------------------
# Telling two link failures apart
# --------------------------------------------------------------------------


def test_half_linked_state_is_reported_as_such(monkeypatch, tmp_path):
    """The failure that cost an evening. signal-cli writes the account entry
    before the primary device confirms, so anything that kills the process in
    between leaves an account that exists and is not registered -- which
    `listAccounts` reports as nothing at all, identical to never scanning."""
    import channels.signal as mod

    data = tmp_path / "data"
    data.mkdir()
    (data / "accounts.json").write_text(
        json.dumps({"accounts": [{"number": "+14125550123", "path": "1"}]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("STEVE_SIGNAL_DATA", str(tmp_path))

    assert mod.known_accounts() == ["+14125550123"]


def test_no_state_at_all_reads_as_never_scanned(monkeypatch, tmp_path):
    import channels.signal as mod

    monkeypatch.setenv("STEVE_SIGNAL_DATA", str(tmp_path))
    assert mod.known_accounts() == []


def test_unreadable_account_state_is_not_a_crash(monkeypatch, tmp_path):
    import channels.signal as mod

    data = tmp_path / "data"
    data.mkdir()
    (data / "accounts.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("STEVE_SIGNAL_DATA", str(tmp_path))
    assert mod.known_accounts() == []


# --------------------------------------------------------------------------
# Reactions arriving
# --------------------------------------------------------------------------


def _envelope(payload: dict, *, sync: bool = True, account: str = "+1555") -> dict:
    inner = {"destinationNumber": account, **payload} if sync else payload
    body = {"syncMessage": {"sentMessage": inner}} if sync else {"dataMessage": inner}
    return {"envelope": {"sourceNumber": account, "timestamp": 1_700_000_000_000, **body}}


def test_a_reaction_is_no_longer_dropped():
    """It was. `_to_incoming` returned None for anything with no text and no
    attachment, so every reaction the user sent went nowhere -- which is why
    one-tap approval could not exist."""
    channel = SignalChannel()
    channel.account = "+1555"

    message = channel._to_incoming(
        _envelope({
            "reaction": {
                "emoji": "👍",
                "targetAuthor": "+1555",
                "targetSentTimestamp": 1_699_999_000_000,
                "isRemove": False,
            }
        })
    )

    assert message is not None
    assert message.reaction == "👍"
    # signal-cli takes `targetTimestamp` when sending and reports
    # `targetSentTimestamp` when receiving. The asymmetry is real.
    assert message.reacted_to == 1_699_999_000_000


def test_removing_a_reaction_is_not_an_answer():
    """Taking a thumbs-up back off a card must not read as a second yes."""
    channel = SignalChannel()
    channel.account = "+1555"

    message = channel._to_incoming(
        _envelope({
            "reaction": {
                "emoji": "👍",
                "targetSentTimestamp": 1_699_999_000_000,
                "isRemove": True,
            }
        })
    )

    assert message is None


def test_an_ordinary_message_still_carries_no_reaction():
    channel = SignalChannel()
    channel.account = "+1555"

    message = channel._to_incoming(_envelope({"message": "what's on my calendar?"}))

    assert message is not None
    assert message.reaction is None
    assert message.text == "what's on my calendar?"
