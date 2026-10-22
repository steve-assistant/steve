"""devtools/sandbox_check.py: the rig that installs, updates and uninstalls
Steve in Windows Sandbox (step 8, item 9).

The run itself needs Windows Sandbox and half an hour; these hold what can
go wrong before it starts. The sandbox must hear nothing and reach nothing.
The second installer's build changes the version files, which must come
back byte for byte, or the next commit ships a version nobody chose. And
the script inside names Steve's folders, task, registry keys and secrets
by hand, so each is held to the code's own.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

import pytest

from core import identity, localapi, paths, uninstall
from devtools import package, sandbox_check

INSIDE = sandbox_check.INSIDE.read_text(encoding="ascii")


def _checkout(tmp_path: Path) -> Path:
    """A checkout holding only the files the version lives in."""
    checkout = tmp_path / "checkout"
    for name in (*sandbox_check.VERSION_FILES, "ui/src-tauri/Cargo.lock"):
        (checkout / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(paths.SOURCE / name, checkout / name)
    return checkout


def test_the_sandbox_hears_nothing_reaches_nothing_and_writes_only_its_results(tmp_path):
    """The rig runs Steve with no key and no network: nothing it does may
    leave the sandbox but the results, and the microphone is the user's."""
    config = ET.fromstring(sandbox_check.wsb_config(tmp_path / "in", tmp_path / "rig",
                                                    tmp_path / "out"))
    for name in ("AudioInput", "VideoInput", "Networking", "ClipboardRedirection",
                 "PrinterRedirection"):
        assert config.findtext(name) == "Disable", name
    mapped = {folder.findtext("SandboxFolder"): (folder.findtext("HostFolder"),
                                                 folder.findtext("ReadOnly"))
              for folder in config.iter("MappedFolder")}
    assert mapped == {
        r"C:\steve\installers": (str(tmp_path / "in"), "true"),
        r"C:\steve\rig": (str(tmp_path / "rig"), "true"),
        r"C:\steve\results": (str(tmp_path / "out"), "false"),
    }
    command = config.findtext("LogonCommand/Command")
    assert command.endswith(rf"-File C:\steve\rig\{sandbox_check.INSIDE.name}")


def test_session_ps_sandbox_reaches_the_network_but_never_hears_and_runs_nothing_by_itself(
        tmp_path):
    """The maintainer's own run needs a real model and a pasted key, so the
    network and the clipboard are on; the microphone is still theirs, and
    the installers they test are the ones the rig built, unchanged."""
    checkout = tmp_path / "checkout"
    installers = sandbox_check.base(checkout) / "installers"
    installers.mkdir(parents=True)
    (installers / "versions.json").write_text("{}", encoding="utf-8")
    launched: list[Path] = []

    made = sandbox_check.open_session(checkout=checkout, launch=launched.append,
                                      say=lambda line: None)

    assert launched == [made]
    config = ET.fromstring(made.read_text(encoding="utf-8"))
    for name in ("AudioInput", "VideoInput", "PrinterRedirection"):
        assert config.findtext(name) == "Disable", name
    for name in ("Networking", "ClipboardRedirection"):
        assert config.findtext(name) == "Default", name
    assert [(folder.findtext("SandboxFolder"), folder.findtext("HostFolder"),
             folder.findtext("ReadOnly")) for folder in config.iter("MappedFolder")] == [
        (r"C:\steve\installers", str(installers), "true")]
    assert config.find("LogonCommand") is None


def test_the_second_build_says_the_next_version_and_every_file_comes_back_byte_for_byte(tmp_path):
    """A version file left bumped would ship 0.1.1 from a tree that says
    0.1.0 in git; Cargo.lock is rewritten by cargo and comes back too."""
    checkout = _checkout(tmp_path)
    names = [*sandbox_check.VERSION_FILES, "ui/src-tauri/Cargo.lock"]
    before = {name: (checkout / name).read_bytes() for name in names}
    later = sandbox_check.next_patch(identity.VERSION)

    with pytest.raises(RuntimeError), sandbox_check.bumped(checkout, later):
        assert set(package.versions(checkout).values()) == {later}
        assert f'VERSION = "{later}"' in (checkout / "core/identity.py").read_text("utf-8")
        (checkout / "ui/src-tauri/Cargo.lock").write_text("rewritten by cargo", encoding="utf-8")
        raise RuntimeError("the build failed")

    assert {name: (checkout / name).read_bytes() for name in names} == before


def test_only_each_files_own_version_changes(tmp_path):
    """The first version a file states is its own; a later table's, such as
    a tool's settings, is not, and stays."""
    checkout = _checkout(tmp_path)
    pyproject = checkout / "pyproject.toml"
    pyproject.write_text(pyproject.read_text("utf-8")
                         + f'\n[tool.example]\nversion = "{identity.VERSION}"\n',
                         encoding="utf-8", newline="\n")
    later = sandbox_check.next_patch(identity.VERSION)
    with sandbox_check.bumped(checkout, later):
        assert pyproject.read_text("utf-8").endswith(f'version = "{identity.VERSION}"\n')
        for name in sandbox_check.VERSION_FILES:
            old = (paths.SOURCE / name).read_text("utf-8").splitlines()
            new = (checkout / name).read_text("utf-8").splitlines()[:len(old)]
            changed = [(a, b) for a, b in zip(old, new, strict=True) if a != b]
            assert len(changed) == 1, name
            assert changed[0][1] == changed[0][0].replace(identity.VERSION, later), name


def test_a_tree_with_uncommitted_changes_builds_nothing(tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(package, "commit_of", lambda checkout: ("abc1234", True))
    with pytest.raises(package.BuildError, match="commit first"):
        sandbox_check.build_pair(tmp_path / "key", checkout=_checkout(tmp_path),
                                 run=lambda *a, **k: calls.append(a), say=lambda line: None)
    assert calls == []


class _Builds:
    """Each command of a release build, made to leave what the real one does."""

    def __init__(self, checkout: Path):
        self.checkout = checkout
        self.calls: list[tuple[str, str, bool]] = []

    def __call__(self, args, **kwargs):
        staging = self.checkout / "build" / "staging"
        version = package.versions(self.checkout)["pyproject.toml"]
        if args[1:4] == ["-m", "devtools.package", "build"]:
            shutil.rmtree(staging, ignore_errors=True)
            (staging / "backend" / "app").mkdir(parents=True)
            self.calls.append(("build", version, "--dirty" in args))
        elif args[1:4] == ["-m", "devtools.package", "manifest"]:
            self.calls.append(("manifest", version, (staging / sandbox_check.LEFTOVER).exists()))
        else:
            assert Path(args[0]).name == "tauri.cmd" and kwargs["env"]["CARGO_BUILD_JOBS"] == "3"
            made = (self.checkout / "ui" / "src-tauri" / "target" / "release" / "bundle" / "nsis"
                    / f"Steve_{version}_x64-setup.exe")
            made.parent.mkdir(parents=True, exist_ok=True)
            made.write_bytes(b'MZ <requestedExecutionLevel level="asInvoker" uiAccess="false"/>')
            self.calls.append(("tauri", version, False))
        return subprocess.CompletedProcess(args, 0)


def test_the_older_installer_carries_the_leftover_and_the_newer_is_one_patch_on(tmp_path,
                                                                                monkeypatch):
    """The update must be shown to delete a file the new version does not
    ship, so only the first build has one."""
    checkout = _checkout(tmp_path)
    monkeypatch.setattr(package, "commit_of", lambda checkout: ("abc1234", False))
    builds = _Builds(checkout)
    old, new = identity.VERSION, sandbox_check.next_patch(identity.VERSION)

    plan = sandbox_check.build_pair(tmp_path / "key", checkout=checkout, run=builds,
                                    say=lambda line: None)

    assert builds.calls == [("build", old, False), ("manifest", old, True), ("tauri", old, False),
                            ("build", new, True), ("manifest", new, False), ("tauri", new, False)]
    installers = sandbox_check.base(checkout) / "installers"
    assert sorted(p.name for p in installers.iterdir()) == sorted(
        [f"Steve_{old}_x64-setup.exe", f"Steve_{new}_x64-setup.exe", "versions.json"])
    assert plan == json.loads((installers / "versions.json").read_text("utf-8"))
    assert plan["execution_level"] == "asInvoker" and plan["leftover"] == sandbox_check.LEFTOVER
    assert set(package.versions(checkout).values()) == {old}


def test_a_pair_built_with_a_key_of_its_own_trusts_that_keys_public_half(tmp_path, monkeypatch):
    """CI's installer job signs with a key made for its run, as the release
    key never leaves the maintainer. The list must be checked against that
    key, and the app built to trust it, or the update step fails on every
    file."""
    checkout = _checkout(tmp_path)
    monkeypatch.setattr(package, "commit_of", lambda checkout: ("abc1234", False))
    builds = _Builds(checkout)
    asked: list[list[str]] = []

    def run(args, **kwargs):
        asked.append([str(a) for a in args])
        return builds(args, **kwargs)

    pub = tmp_path / "update.key.pub"
    pub.write_text("dGhlIHJ1bidzIG93biBrZXk=\n", encoding="ascii")
    sandbox_check.build_pair(tmp_path / "update.key", pubkey=pub, checkout=checkout, run=run,
                             say=lambda line: None)

    manifests = [a for a in asked if a[1:4] == ["-m", "devtools.package", "manifest"]]
    assert [a[-2:] for a in manifests] == [["--pubkey", str(pub)]] * 2
    tauris = [a for a in asked if Path(a[0]).name == "tauri.cmd"]
    assert len(tauris) == 2
    for tauri in tauris:
        configs = [tauri[i + 1] for i, arg in enumerate(tauri) if arg == "--config"]
        assert configs[0] == "src-tauri/tauri.release.conf.json" and len(configs) == 2
        merged = json.loads(Path(configs[1]).read_text(encoding="utf-8"))
        assert merged == {"plugins": {"updater": {"pubkey": "dGhlIHJ1bidzIG93biBrZXk="}}}


def test_the_maintainers_build_trusts_the_checkouts_own_key(tmp_path, monkeypatch):
    checkout = _checkout(tmp_path)
    monkeypatch.setattr(package, "commit_of", lambda checkout: ("abc1234", False))
    builds = _Builds(checkout)
    asked: list[list[str]] = []

    def run(args, **kwargs):
        asked.append([str(a) for a in args])
        return builds(args, **kwargs)

    sandbox_check.build_pair(tmp_path / "update.key", checkout=checkout, run=run,
                             say=lambda line: None)

    assert not any("--pubkey" in a for a in asked)
    assert [a[2:] for a in asked if Path(a[0]).name == "tauri.cmd"] == [
        ["--config", "src-tauri/tauri.release.conf.json"]] * 2


def test_build_passes_the_public_key_file_it_is_given(tmp_path, monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(sandbox_check, "build_pair",
                        lambda key, **kwargs: seen.update(kwargs, key=key) or {"old": 1, "new": 2})

    assert sandbox_check.main(["build", "--key", str(tmp_path / "k"),
                               "--pubkey", str(tmp_path / "k.pub")]) == 0
    assert seen == {"key": tmp_path / "k", "pubkey": tmp_path / "k.pub"}


def test_a_run_waits_for_the_rig_to_say_it_is_done(tmp_path):
    checkout = tmp_path / "checkout"
    installers = sandbox_check.base(checkout) / "installers"
    installers.mkdir(parents=True)
    (installers / "versions.json").write_text("{}", encoding="utf-8")
    launched: list[Path] = []

    def launch(config: Path) -> None:
        launched.append(config)
        # Windows PowerShell 5.1 writes UTF-8 with a byte-order mark.
        results = sandbox_check.base(checkout) / "results" / "results.json"
        results.write_text(json.dumps({"steps": [], "done": True}), encoding="utf-8-sig")

    webview2 = tmp_path / "MicrosoftEdgeWebView2RuntimeInstallerX64.exe"
    webview2.write_bytes(b"MZ offline WebView2")

    data = sandbox_check.run_sandbox(checkout=checkout, webview2=webview2, launch=launch,
                                     say=lambda line: None, timeout=6)
    assert data == {"steps": [], "done": True}
    assert launched == [sandbox_check.base(checkout) / "steve-check.wsb"]
    rig = sandbox_check.base(checkout) / "rig"
    assert (rig / sandbox_check.INSIDE.name).read_bytes() == sandbox_check.INSIDE.read_bytes()
    # The rig installs WebView2 from this name when the sandbox lacks it.
    assert (rig / sandbox_check.WEBVIEW2_SETUP).read_bytes() == webview2.read_bytes()
    assert f"Join-Path $Mount 'rig\\{sandbox_check.WEBVIEW2_SETUP}'" in INSIDE


def test_the_report_names_each_failure_and_what_an_uninstall_left():
    data = {"done": False, "steps": [
        {"name": "install", "ok": True, "seconds": 12.5, "detail": "into C:\\x"},
        {"name": "uninstall", "ok": False, "seconds": 3, "detail": "the task SteveWake is left",
         "left": {"task": True, "credentials": ["steve-spike"]}},
    ]}
    plan = {"old": "0.1.0", "new": "0.1.1", "execution_level": "asInvoker", "commit": "abc1234def"}
    text = sandbox_check.report(data, plan, on=date(2026, 10, 19))
    assert "Run on 2026-10-19" in text and "`abc1234`" in text
    assert "**The run did not finish.**" in text
    assert "| uninstall | **failed** | 3 | the task SteveWake is left |" in text
    assert "## Left after: uninstall" in text and "- task: True" in text
    assert text.endswith("1 steps passed, 1 failed.\n")


def test_a_failed_run_prints_its_report_where_ci_keeps_it(tmp_path, monkeypatch, capsys):
    """CI's runner goes with its job, and the report file with it: the
    installer job's first failure (22 October 2026) left "wrote report.md"
    in its log, and nothing that said which step broke the install."""
    monkeypatch.setattr(sandbox_check, "base", lambda checkout=None: tmp_path)
    (tmp_path / "installers").mkdir()
    (tmp_path / "installers" / "versions.json").write_text(json.dumps(
        {"old": "0.1.0", "new": "0.1.1", "execution_level": "asInvoker"}), encoding="utf-8")
    (tmp_path / "results").mkdir()
    results = tmp_path / "results" / "results.json"

    results.write_text(json.dumps({"done": True, "steps": [
        {"name": "uninstall", "ok": False, "seconds": 3, "detail": "no profile"}]}),
        encoding="utf-8")
    assert sandbox_check.main(["report"]) == 1
    assert "| uninstall | **failed** | 3 | no profile |" in capsys.readouterr().out

    results.write_text(json.dumps({"done": True, "steps": [
        {"name": "uninstall", "ok": True, "seconds": 3, "detail": "gone"}]}), encoding="utf-8")
    assert sandbox_check.main(["report"]) == 0
    assert "| uninstall |" not in capsys.readouterr().out


def _ps(name: str) -> str:
    found = re.search(rf"^\${name} = '([^']*)'", INSIDE, re.M)
    assert found, f"the rig sets no ${name}"
    return found.group(1)


def test_the_rig_looks_where_steve_puts_things():
    """The script names by hand what Steve's code computes: a renamed task,
    folder or key would make every step pass by finding nothing."""
    assert _ps("Task") == identity.wake_task("*")
    assert _ps("Mappings") == "HKCU:\\" + uninstall.MAPPINGS
    assert "Join-Path $env:LOCALAPPDATA 'Programs\\Steve'" in INSIDE
    assert f"Join-Path $env:LOCALAPPDATA '{identity.NAME}'" in INSIDE
    assert f"Join-Path $Data '{paths.run_dir().name}\\{localapi.RUN_FILE}'" in INSIDE
    assert f"Uninstall\\{identity.NAME}'" in INSIDE


def test_the_rig_shuts_its_machine_down_at_the_end_unless_told_to_stay_up():
    """The shutdown throws Windows Sandbox away; on CI's runner it would end
    the job before the results were copied out (.github/workflows/ci.yml)."""
    end = INSIDE[INSIDE.rindex("} finally {"):]
    shutdowns = [line.strip() for line in end.splitlines() if "shutdown.exe" in line]
    assert shutdowns == ["if ($env:STEVE_RIG_STAYS_UP -ne '1') { shutdown.exe /s /t 5 }"]
    assert INSIDE.count("shutdown.exe") == 1


def _steps() -> dict[str, str]:
    """Each of the rig's steps, its name to its body."""
    parts = re.split(r"^  Step ", INSIDE, flags=re.M)[1:]
    return {part.split(" {", 1)[0].strip("'\""): part for part in parts}


def test_the_rig_updates_installs_and_uninstalls_over_a_running_steve():
    """Every install and uninstall here stopped Steve first, so none could
    see an installer delete Steve's files while it ran (review of steps 7
    to 9, findings 5 and 29). Each of these starts it, and checks that its
    processes were closed."""
    steps = _steps()
    over = [name for name in steps if "running" in name and "refused" not in name]
    assert len(over) == 3, over
    for name in over:
        assert "Ensure-Running" in steps[name] and "Still $was" in steps[name], name
        assert "Stop-Steve" not in steps[name], name


def test_an_older_installer_is_tried_three_ways_over_a_running_steve_and_changes_nothing():
    """Tried only with /S, a refusal by its window or under /P, where a
    message box waits for its OK, went unchecked (findings 10 and 29)."""
    steps = _steps()
    refused = [body for name, body in steps.items() if "is refused over a running" in name]
    assert all("Ensure-Running" in body and "Unchanged $was" in body for body in refused)
    ways = sorted(re.search(r"(?:Install \$Old|Refused-Older) (@\([^)]*\))", body).group(1)
                  for body in refused)
    assert ways == ["@('/P')", "@('/S')", "@()"]


def test_the_rigs_secrets_are_steves_and_its_near_misses_are_not():
    def listed(name: str) -> list[str]:
        found = re.search(rf"^\${name} = @\(([^)]*)\)", INSIDE, re.M)
        assert found, name
        return re.findall(r"'([^']+)'", found.group(1))
    ours, not_ours = listed("OursSecrets"), listed("NotOursSecrets")
    assert ours and all(uninstall.our_secret(target) for target in ours)
    assert not_ours and not any(uninstall.our_secret(target) for target in not_ours)
    services = {target.rpartition("@")[2] for target in ours}
    assert set(identity.KEYRING_SERVICES) <= services | set(ours)
