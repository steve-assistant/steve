; Steve's steps in Tauri's NSIS installer (docs/plan/08-packaging.md, sections
; 5 and 6), included at the top of its installer.nsi.
;
; An update copies the new files over the old, and keeps the folder: Tauri's
; installer, run with /UPDATE, never uninstalls first. A file the new version
; no longer ships would then stay. Spike S2 found two dist-info folders for one
; package left that way, which confuse importlib.metadata, and the app's
; integrity check (ui/src-tauri/src/integrity.rs) would refuse a module the
; new version did not ship. So the backend and the module runtime are deleted
; whole before the copy; the data is elsewhere (%LOCALAPPDATA%\Steve) and is
; never touched here.

Var SteveTries

; Where Steve is installed: %LOCALAPPDATA%\Programs\Steve, Windows' folder for
; one user's programs (the plan's D7). Tauri's installer would choose
; %LOCALAPPDATA%\Steve for a per-user install, and that is Steve's data folder
; (core/paths.data_dir): an uninstall keeping the data could not tell the
; program's files from it, and one deleting the data would delete the program
; it runs. Tauri defines PRODUCTNAME only after it includes this file, so the
; name is written out; tests/unit/test_installer.py holds it to the product's.
!define STEVE_DATA "$LOCALAPPDATA\Steve"
!define STEVE_HOME "$LOCALAPPDATA\Programs\Steve"

; An older Steve never replaces a newer one. Tauri's `allowDowngrades: false`
; only greys out one choice on its reinstall page: "Uninstall before
; installing" there ran the newer uninstaller, then installed the older
; version, whose worker refuses the newer database, so Steve would not start;
; and its silent check reads a register no silent install has set. So the
; version installed is compared here, before the reinstall page and again
; before the copy. Tauri defines VERSION and UNINSTKEY after it includes this
; file, so the version is read from tauri.conf.json, and the key written out;
; tests/unit/test_installer.py holds both to Tauri's. Its own comparison is a
; plugin it loads later too; WordFunc's is loaded already.
!searchparse /file "${__FILEDIR__}\..\tauri.conf.json" `"version": "` STEVE_VERSION `"`
!define STEVE_UNINSTKEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\Steve"
Function SteveRefuseOlder
  ReadRegStr $R9 HKCU "${STEVE_UNINSTKEY}" "DisplayVersion"
  ${If} $R9 != ""
    ${VersionCompare} "${STEVE_VERSION}" "$R9" $R8
    ${If} $R8 = 2
      MessageBox MB_OK|MB_ICONSTOP "Steve $R9 is installed, which is newer than this installer's ${STEVE_VERSION}. Steve can't go back a version: the older one can't read what the newer one has written." /SD IDOK
      SetErrorLevel 2
      Quit
    ${EndIf}
  ${EndIf}
FunctionEnd

; Before the folder page shows, once Tauri's .onInit has chosen its default,
; or the folder of the install already there.
!define MUI_CUSTOMFUNCTION_GUIINIT SteveHome
Function SteveHome
  Call SteveRefuseOlder
  ${If} $INSTDIR == "${STEVE_DATA}"
    StrCpy $INSTDIR "${STEVE_HOME}"
  ${EndIf}
FunctionEnd

; Whether the install folder and the data folder overlap: one is the other,
; or holds it. $R0 is 1 if they do. Compared as Windows compares them,
; without case, each with one trailing backslash, so Steve2 is not inside
; Steve; the backend checks again with the folders resolved
; (core/paths.overlap), for an install that shows no folder page. Until
; step 11 only the data folder itself was refused (the audit's F8).
Function SteveOverlapsData
  Push $R1
  Push $R2
  Push $R3
  StrCpy $R1 $INSTDIR "" -1
  ${If} $R1 == "\"
    StrCpy $R1 $INSTDIR -1
  ${Else}
    StrCpy $R1 $INSTDIR
  ${EndIf}
  StrCpy $R1 "$R1\"
  StrCpy $R2 "${STEVE_DATA}\"
  StrCpy $R0 0
  StrLen $R3 $R2
  StrCpy $R3 $R1 $R3
  ${If} $R3 == $R2
    StrCpy $R0 1
  ${EndIf}
  StrLen $R3 $R1
  StrCpy $R3 $R2 $R3
  ${If} $R3 == $R1
    StrCpy $R0 1
  ${EndIf}
  Pop $R3
  Pop $R2
  Pop $R1
FunctionEnd

; On the folder page, Next stays greyed out on the data folder, a folder
; inside it, or one that holds it.
Function .onVerifyInstDir
  Push $R0
  Call SteveOverlapsData
  ${If} $R0 = 1
    Pop $R0
    Abort
  ${EndIf}
  Pop $R0
FunctionEnd

!macro NSIS_HOOK_PREINSTALL
  ; A silent install shows no page and runs no SteveHome.
  Call SteveRefuseOlder
  ; Tauri's SetOutPath has only made the data folder, which Steve makes anyway.
  ${If} $INSTDIR == "${STEVE_DATA}"
    StrCpy $INSTDIR "${STEVE_HOME}"
    SetOutPath $INSTDIR
  ${EndIf}

  ; Tauri's own check of the running app comes after this hook. Run first
  ; here: deleting the backend under a running app would leave half of it.
  ; By the program's bare name, the only form the check matches: given a
  ; full path it matched nothing, and nothing was closed. Never by
  ; python.exe, which would close every Python the user runs: the backend
  ; ends with the app, in its job, and the delete below waits for its files.
  !insertmacro CheckIfAppIsRunning "${MAINBINARYNAME}.exe" "${PRODUCTNAME}"

  ; Only in a folder holding Steve: a first install into a folder chosen by
  ; hand never deletes what it finds there.
  ${If} ${FileExists} "$INSTDIR\${MAINBINARYNAME}.exe"
    StrCpy $SteveTries 0
    steve_delete_old:
      RMDir /r "$INSTDIR\backend"
      RMDir /r "$INSTDIR\runtime"
      ${If} ${FileExists} "$INSTDIR\backend\*.*"
      ${OrIf} ${FileExists} "$INSTDIR\runtime\*.*"
        ; A process ending lets go of its files a moment later.
        IntOp $SteveTries $SteveTries + 1
        ${If} $SteveTries < 20
          Sleep 500
          Goto steve_delete_old
        ${EndIf}
        Abort "The old version of ${PRODUCTNAME} is still in use. Close it and run the installer again."
      ${EndIf}
  ${EndIf}
!macroend

; Removing Steve. core/uninstall.py removes what Steve put in the user's
; account outside this folder: the sandbox profiles, the wake task, Docker's
; containers and, with Tauri's "Delete the application data" box, the data,
; the secrets and the browser's images. It runs on the bundled Python, so
; before the files go. An update runs none of it: Tauri's installer, run with
; /UPDATE, skips its reinstall page and never runs the old uninstaller, so the
; data, the profiles and the secrets are the new version's. (A reinstall by
; hand where the user picks "uninstall first" runs the old uninstaller without
; /UPDATE: a removal, as they chose.) The $UpdateMode checks below stay for an
; uninstaller that is ever run with /UPDATE.
!macro NSIS_HOOK_PREUNINSTALL
  ${If} $UpdateMode <> 1
    ; Closing the app ends the backend with it: the backend runs in the
    ; app's job, which ends everything in it when the app goes. Before
    ; anything is removed, by the bare name (see PREINSTALL): checked by
    ; path, Steve ran on while its data was deleted under it, and a Cancel
    ; at Tauri's later check left it installed without its secrets.
    !insertmacro CheckIfAppIsRunning "${MAINBINARYNAME}.exe" "${PRODUCTNAME}"
    ; Only in a folder the marker shows is an installed Steve's.
    ${If} ${FileExists} "$INSTDIR\backend\app\installed.json"
      ${If} $DeleteAppDataCheckboxState = 1
        nsExec::ExecToLog '"$INSTDIR\backend\python.exe" -I -B -m core.uninstall --data'
        Pop $0
        MessageBox MB_OK|MB_ICONINFORMATION "If you linked Signal to ${PRODUCTNAME}, your phone still lists it as a linked device. Remove it there, in Signal's settings." /SD IDOK
      ${Else}
        nsExec::ExecToLog '"$INSTDIR\backend\python.exe" -I -B -m core.uninstall'
        Pop $0
      ${EndIf}
      ; What it could not remove is in the log it printed; the user is told.
      ${If} $0 <> 0
        MessageBox MB_OK|MB_ICONEXCLAMATION "Some of what ${PRODUCTNAME} put on this computer could not be removed. The details are in the uninstaller's log (Show details)." /SD IDOK
      ${EndIf}
      ; Tauri deletes only the files it installed, one by one. The add-ons
      ; Steve downloads later (signal-cli, its Java) are here too, and a
      ; file left in the backend or the runtime would keep the folder.
      RMDir /r "$INSTDIR\addons"
      RMDir /r "$INSTDIR\backend"
      RMDir /r "$INSTDIR\runtime"
    ${EndIf}
  ${EndIf}
!macroend
