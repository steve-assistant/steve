"""Steve's steps in Tauri's installer (ui/src-tauri/windows/hooks.nsh) delete
what an update would otherwise leave, and nothing else.

An update copies the new version over the old and keeps the folder, so a file
the new version no longer ships would stay: two dist-info folders for one
package, or a module the integrity check refuses. The hook deletes the old
backend and runtime first. Deleting is the dangerous half, so these pin it to
those two folders, to a folder that holds Steve, and to after the running app
was found and closed. The installer itself runs in Windows Sandbox (step 8,
item 9); these read the script, which is what that run would be of.
"""

from __future__ import annotations

import json
import re

from core import identity, paths

TAURI = paths.SOURCE / "ui" / "src-tauri"


# The one check of the running app: by the program's bare name, the only form
# Tauri's check matches (its plugin compares process names).
RUNNING = '!insertmacro CheckIfAppIsRunning "${MAINBINARYNAME}.exe" "${PRODUCTNAME}"'


def _macro(name: str) -> list[str]:
    """The lines of one of the hook's macros, comments and blanks left out."""
    text = (TAURI / "windows" / "hooks.nsh").read_text(encoding="utf-8")
    found = re.search(rf"^!macro {name}\n(.*?)^!macroend", text, re.M | re.S)
    assert found, f"hooks.nsh has no {name}"
    lines = (line.strip() for line in found.group(1).splitlines())
    return [line for line in lines if line and not line.startswith(";")]


def test_the_installer_is_built_with_steves_hooks():
    """A hook file the configuration does not name is never run."""
    config = json.loads((TAURI / "tauri.conf.json").read_text(encoding="utf-8"))
    named = config["bundle"]["windows"]["nsis"]["installerHooks"]
    assert (TAURI / named).resolve() == (TAURI / "windows" / "hooks.nsh").resolve()


def test_the_installer_installs_for_the_user_alone_and_asks_for_no_administrator():
    """The README promises an install that needs no administrator, into the
    user's own folders, and the security model counts on another Windows
    user being unable to change Steve's files. "perMachine" or "both" would
    ask for elevation and put the program where every account reads it.
    Found tested by nothing in step 11's map of features to tests."""
    config = json.loads((TAURI / "tauri.conf.json").read_text(encoding="utf-8"))
    assert config["bundle"]["windows"]["nsis"]["installMode"] == "currentUser"


def test_the_installer_wears_steves_icon_and_a_sidebar_of_nsiss_own_size():
    """Without them the installer shows NSIS's own icon, with "NSIS" in it,
    in its title bar and on every page's header: the first thing a stranger
    sees of Steve (22 October 2026). NSIS stretches a sidebar of another
    size, and shows only an uncompressed bitmap."""
    nsis = json.loads((TAURI / "tauri.conf.json").read_text(encoding="utf-8"))["bundle"][
        "windows"]["nsis"]
    for icon in ("installerIcon", "uninstallerIcon"):
        assert (TAURI / nsis[icon]).resolve() == (TAURI / "icons" / "icon.ico").resolve()
    sidebar = (TAURI / nsis["sidebarImage"]).read_bytes()
    assert sidebar[:2] == b"BM"
    width, height, depth = (int.from_bytes(sidebar[start:end], "little")
                            for start, end in ((18, 22), (22, 26), (28, 30)))
    assert (width, height, depth) == (164, 314, 24)


def test_before_it_copies_the_installer_deletes_only_the_old_backend_and_runtime():
    """Not the install folder, not the app, never the data, which is elsewhere."""
    lines = _macro("NSIS_HOOK_PREINSTALL")
    deleted = [line for line in lines if re.match(r"(RMDir|Delete)\b", line)]
    assert deleted == [r'RMDir /r "$INSTDIR\backend"', r'RMDir /r "$INSTDIR\runtime"']


def test_it_deletes_only_in_a_folder_that_holds_steve_once_the_running_app_is_closed():
    """A first install into a folder chosen by hand must never empty it, and a
    backend deleted under the running app would be left half there."""
    lines = _macro("NSIS_HOOK_PREINSTALL")
    first_delete = next(i for i, line in enumerate(lines) if line.startswith("RMDir"))
    guard = r'${If} ${FileExists} "$INSTDIR\${MAINBINARYNAME}.exe"'
    assert guard in lines[:first_delete]
    closed = [line for line in lines[:first_delete]
              if line.startswith("!insertmacro CheckIfAppIsRunning")]
    assert closed == [RUNNING]


def test_files_still_held_open_stop_the_install_rather_than_mix_two_versions():
    lines = _macro("NSIS_HOOK_PREINSTALL")
    assert any(line.startswith("Abort ") for line in lines)


def _hooks() -> str:
    return (TAURI / "windows" / "hooks.nsh").read_text(encoding="utf-8")


def _defined(name: str) -> str:
    found = re.search(rf"^!define {name} (.*)$", _hooks(), re.M)
    assert found, f"hooks.nsh defines no {name}"
    return found.group(1).strip('"')


def _function(name: str) -> list[str]:
    found = re.search(rf"^Function {re.escape(name)}\n(.*?)^FunctionEnd", _hooks(), re.M | re.S)
    assert found, f"hooks.nsh has no function {name}"
    return [line.strip() for line in found.group(1).splitlines() if line.strip()]


def test_steve_is_never_installed_into_its_own_data_folder():
    """Tauri's per-user default is %LOCALAPPDATA%\\<product>, which is where
    core/paths keeps an install's data. The program goes to Programs\\."""
    config = json.loads((TAURI / "tauri.conf.json").read_text(encoding="utf-8"))
    product = config["productName"]
    assert product == identity.NAME
    assert _defined("STEVE_DATA") == rf"$LOCALAPPDATA\{product}"
    assert _defined("STEVE_HOME") == rf"$LOCALAPPDATA\Programs\{product}"

    # Before the folder page; on it; and for an install that shows none.
    assert _defined("MUI_CUSTOMFUNCTION_GUIINIT") == "SteveHome"
    moved = ['${If} $INSTDIR == "${STEVE_DATA}"', 'StrCpy $INSTDIR "${STEVE_HOME}"']
    assert _function("SteveHome")[1:3] == moved
    assert _function(".onVerifyInstDir") == [
        "Push $R0", "Call SteveOverlapsData", "${If} $R0 = 1", "Pop $R0", "Abort", "${EndIf}",
        "Pop $R0"]
    # The data folder, inside it, or holding it; each with one trailing
    # backslash, so Steve2 is apart from Steve.
    overlaps = _function("SteveOverlapsData")
    assert 'StrCpy $R1 "$R1\\"' in overlaps and 'StrCpy $R2 "${STEVE_DATA}\\"' in overlaps
    assert overlaps.count("StrCpy $R0 1") == 2
    assert _macro("NSIS_HOOK_PREINSTALL")[1:4] == [*moved, "SetOutPath $INSTDIR"]


def test_an_installs_data_folder_is_the_one_the_installer_keeps_clear(monkeypatch, tmp_path):
    monkeypatch.delenv(paths.DATA_DIR_ENV, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(paths, "layout", lambda: "installed")
    assert paths.data_dir() == tmp_path / identity.NAME


def test_an_update_runs_none_of_steves_uninstall():
    """The installer runs the old uninstaller with /UPDATE to replace the
    files; the data, the profiles and the secrets belong to the new version."""
    lines = _macro("NSIS_HOOK_PREUNINSTALL")
    assert lines[0] == "${If} $UpdateMode <> 1" and lines[-1] == "${EndIf}"
    depth = 0
    for line in lines[:-1]:
        depth += line.startswith("${If}") - line.startswith("${EndIf}")
        assert depth >= 1, f"{line!r} runs outside the update guard"


def test_uninstall_runs_core_uninstall_and_deletes_the_data_only_when_asked():
    lines = _macro("NSIS_HOOK_PREUNINSTALL")
    runs = [line for line in lines if line.startswith("nsExec::ExecToLog")]
    python = r'"$INSTDIR\backend\python.exe" -I -B -m core.uninstall'
    assert runs == [f"nsExec::ExecToLog '{python} --data'", f"nsExec::ExecToLog '{python}'"]
    asked = lines.index("${If} $DeleteAppDataCheckboxState = 1")
    otherwise = lines.index("${Else}", asked)
    assert asked < lines.index(runs[0]) < otherwise < lines.index(runs[1])


def test_uninstall_closes_the_app_then_deletes_only_steves_own_folders():
    """Tauri deletes the files it installed, not the add-ons downloaded later;
    the folders go whole, and only where the marker says this is Steve."""
    lines = _macro("NSIS_HOOK_PREUNINSTALL")
    marker = r'${If} ${FileExists} "$INSTDIR\backend\app\installed.json"'
    closed = [i for i, line in enumerate(lines)
              if line.startswith("!insertmacro CheckIfAppIsRunning")]
    deleted = [line for line in lines if re.match(r"(RMDir|Delete)\b", line)]
    assert deleted == [rf'RMDir /r "$INSTDIR\{name}"' for name in ("addons", "backend", "runtime")]
    assert max(closed) < lines.index(marker) < lines.index(deleted[0])
    assert all(lines.index(run) < lines.index(deleted[0])
               for run in lines if run.startswith("nsExec::"))
    assert [lines[i] for i in closed] == [RUNNING]


def test_the_running_app_is_looked_for_by_its_bare_name_and_never_as_python():
    """Tauri's check compares process names, so a full path never matched,
    and uninstall deleted the data under a running Steve (the review of
    steps 7 to 9, finding 5). Looking for python.exe would close every
    Python the user runs."""
    checks = [line.strip() for line in _hooks().splitlines()
              if line.strip().startswith("!insertmacro CheckIfAppIsRunning")]
    assert checks == [RUNNING, RUNNING]


def test_uninstall_says_so_when_something_could_not_be_removed():
    """core.uninstall's exit code was dropped: data a running backend held
    stayed behind, and the user was told nothing."""
    lines = _macro("NSIS_HOOK_PREUNINSTALL")
    told = lines.index("${If} $0 <> 0")
    assert lines[told + 1].startswith("MessageBox ") and "/SD IDOK" in lines[told + 1]
    assert told > max(i for i, line in enumerate(lines) if line == "Pop $0")


def test_an_older_installer_never_replaces_a_newer_steve():
    """Tauri's `allowDowngrades: false` greyed out one choice on its
    reinstall page; "Uninstall before installing" then ran the newer
    uninstaller and installed the older version, whose worker refuses the
    newer database (finding 10). The installed version is compared before
    any page and again before the copy, which a silent install reaches."""
    assert _function("SteveHome")[0] == "Call SteveRefuseOlder"
    assert _macro("NSIS_HOOK_PREINSTALL")[0] == "Call SteveRefuseOlder"
    refuse = _function("SteveRefuseOlder")
    assert refuse[:3] == ['ReadRegStr $R9 HKCU "${STEVE_UNINSTKEY}" "DisplayVersion"',
                          '${If} $R9 != ""',
                          '${VersionCompare} "${STEVE_VERSION}" "$R9" $R8']
    # VersionCompare says 2 when the second, the one installed, is newer.
    newer = refuse.index("${If} $R8 = 2")
    assert refuse[newer + 1].startswith("MessageBox ") and refuse[newer + 3] == "Quit"


def test_the_version_and_key_the_hooks_write_out_are_tauris_own():
    """Tauri defines VERSION and UNINSTKEY after it includes the hooks, so
    they read the version from tauri.conf.json and write the key out."""
    config_file = TAURI / "tauri.conf.json"
    config = json.loads(config_file.read_text(encoding="utf-8"))
    read = ('!searchparse /file "${__FILEDIR__}\\..\\tauri.conf.json" '
            '`"version": "` STEVE_VERSION `"`')
    assert read in _hooks().splitlines(), "hooks.nsh no longer reads the version this way"
    # !searchparse takes the first line that holds the pattern.
    first = next(line for line in config_file.read_text(encoding="utf-8").splitlines()
                 if '"version": "' in line)
    assert first.split('"version": "', 1)[1].split('"', 1)[0] == config["version"]
    product = config["productName"]
    assert _defined("STEVE_UNINSTKEY") == (
        rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{product}")
