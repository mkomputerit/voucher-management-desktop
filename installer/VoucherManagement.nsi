; Voucher Management 5.1 managed Windows installer.
; The application payload and the PowerShell deployment contract remain the
; source of truth for ProgramData ACLs and operator-group provisioning.

!ifndef APP_VERSION
  !error "APP_VERSION is required"
!endif
!ifndef APP_FILE_VERSION
  !error "APP_FILE_VERSION is required (X.Y.Z.0)"
!endif
!ifndef PAYLOAD_DIR
  !error "PAYLOAD_DIR is required"
!endif
!ifndef OUTPUT_DIR
  !define OUTPUT_DIR "."
!endif

!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "FileFunc.nsh"
!include "x64.nsh"
!include "nsDialogs.nsh"

!define PRODUCT_NAME "Voucher Management"
!define PRODUCT_EXE "VoucherManagement.exe"
!define PRODUCT_GROUP "Voucher Management Operators"
!define PRODUCT_DATA_DIR "VoucherManagement"
!define UNINSTALL_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\VoucherManagement"
!define PRODUCT_KEY "Software\VoucherManagement"

Unicode true
Name "${PRODUCT_NAME} ${APP_VERSION}"
OutFile "${OUTPUT_DIR}\VoucherManagement-${APP_VERSION}-Setup.exe"
InstallDir "$PROGRAMFILES64\Voucher Management"
RequestExecutionLevel admin
SetCompressor /SOLID lzma
CRCCheck on
ShowInstDetails show
ShowUninstDetails show
BrandingText "${PRODUCT_NAME}"

Icon "${PAYLOAD_DIR}\_internal\assets\VoucherManagement.ico"
UninstallIcon "${PAYLOAD_DIR}\_internal\assets\VoucherManagement.ico"

VIProductVersion "${APP_FILE_VERSION}"
VIAddVersionKey /LANG=1040 "ProductName" "${PRODUCT_NAME}"
VIAddVersionKey /LANG=1040 "FileDescription" "${PRODUCT_NAME} Setup"
VIAddVersionKey /LANG=1040 "FileVersion" "${APP_VERSION}"
VIAddVersionKey /LANG=1040 "ProductVersion" "${APP_VERSION}"
VIAddVersionKey /LANG=1040 "CompanyName" "Voucher Management contributors"
VIAddVersionKey /LANG=1040 "LegalCopyright" "Copyright (c) 2026 Voucher Management contributors"

!define MUI_ABORTWARNING
!define MUI_ICON "${PAYLOAD_DIR}\_internal\assets\VoucherManagement.ico"
!define MUI_UNICON "${PAYLOAD_DIR}\_internal\assets\VoucherManagement.ico"
!define MUI_FINISHPAGE_NOAUTOCLOSE
!define MUI_FINISHPAGE_TEXT "Installazione completata.$\r$\n$\r$\nSe questo utente è stato appena aggiunto al gruppo Voucher Management Operators, disconnettersi da Windows e accedere nuovamente prima del primo avvio."

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "${PAYLOAD_DIR}\LICENSE"
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
UninstPage custom un.PurgePageCreate un.PurgePageLeave
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "Italian"

Var DataRoot
Var OperatorGroup
Var OperatorUser
Var PowerShellPath
Var SkipShortcut
Var PurgeData
Var PurgeCheckbox

Function SetPowerShellPath
  ; NSIS 3.x is a 32-bit process. Sysnative guarantees the 64-bit PowerShell
  ; host on a 64-bit Windows installation, so $env:ProgramFiles semantics and
  ; system administration cmdlets match the reviewed deployment contract.
  StrCpy $PowerShellPath "$WINDIR\Sysnative\WindowsPowerShell\v1.0\powershell.exe"
FunctionEnd

Function .onInit
  ${If} ${RunningX64}
  ${Else}
    MessageBox MB_ICONSTOP|MB_OK "${PRODUCT_NAME} richiede Windows a 64 bit."
    Abort
  ${EndIf}

  StrCpy $INSTDIR "$PROGRAMFILES64\Voucher Management"
  StrCpy $DataRoot "$COMMONAPPDATA\${PRODUCT_DATA_DIR}"
  StrCpy $OperatorGroup "${PRODUCT_GROUP}"
  StrCpy $OperatorUser ""
  StrCpy $SkipShortcut "0"
  Call SetPowerShellPath

  ${GetParameters} $R0

  ${GetOptions} $R0 "/INSTALLROOT=" $R1
  ${If} $R1 != ""
    StrCpy $INSTDIR $R1
  ${EndIf}

  ${GetOptions} $R0 "/DATAROOT=" $R1
  ${If} $R1 != ""
    StrCpy $DataRoot $R1
  ${EndIf}

  ${GetOptions} $R0 "/OPERATORGROUP=" $R1
  ${If} $R1 != ""
    StrCpy $OperatorGroup $R1
  ${EndIf}

  ${GetOptions} $R0 "/OPERATORUSER=" $R1
  ${If} $R1 != ""
    StrCpy $OperatorUser $R1
  ${EndIf}

  ${GetOptions} $R0 "/SKIPSHORTCUT=" $R1
  ${If} $R1 == "1"
    StrCpy $SkipShortcut "1"
  ${EndIf}
FunctionEnd

Section "Voucher Management" SEC_MAIN
  SectionIn RO

  SetOutPath "$PLUGINSDIR\payload"
  File /r "${PAYLOAD_DIR}\*"

  StrCpy $R2 '$\"$PowerShellPath$\" -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $\"$PLUGINSDIR\payload\Install-VoucherManagement.ps1$\" -SourcePath $\"$PLUGINSDIR\payload$\" -InstallRoot $\"$INSTDIR$\" -DataRoot $\"$DataRoot$\" -OperatorGroup $\"$OperatorGroup$\"'

  ${If} $OperatorUser != ""
    StrCpy $R2 '$R2 -OperatorUser $\"$OperatorUser$\"'
  ${EndIf}

  ${If} $SkipShortcut == "1"
    StrCpy $R2 '$R2 -SkipShortcut'
  ${EndIf}

  DetailPrint "Configurazione installazione condivisa..."
  nsExec::ExecToLog $R2
  Pop $R3
  ${If} $R3 != "0"
    MessageBox MB_ICONSTOP|MB_OK "Installazione non riuscita durante la configurazione di Windows. Codice: $R3.$\r$\n$\r$\nChiudere Voucher Management se è in esecuzione e riprovare come amministratore."
    Abort
  ${EndIf}

  SetOutPath "$INSTDIR"
  WriteUninstaller "$INSTDIR\Uninstall.exe"

  WriteRegStr HKLM "${UNINSTALL_KEY}" "DisplayName" "${PRODUCT_NAME}"
  WriteRegStr HKLM "${UNINSTALL_KEY}" "DisplayVersion" "${APP_VERSION}"
  WriteRegStr HKLM "${UNINSTALL_KEY}" "Publisher" "Voucher Management contributors"
  WriteRegStr HKLM "${UNINSTALL_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKLM "${UNINSTALL_KEY}" "DisplayIcon" "$INSTDIR\${PRODUCT_EXE}"
  WriteRegStr HKLM "${UNINSTALL_KEY}" "UninstallString" '$\"$INSTDIR\Uninstall.exe$\"'
  WriteRegStr HKLM "${UNINSTALL_KEY}" "QuietUninstallString" '$\"$INSTDIR\Uninstall.exe$\" /S'
  WriteRegDWORD HKLM "${UNINSTALL_KEY}" "NoModify" 1
  WriteRegDWORD HKLM "${UNINSTALL_KEY}" "NoRepair" 1

  WriteRegStr HKLM "${PRODUCT_KEY}" "InstallRoot" "$INSTDIR"
  WriteRegStr HKLM "${PRODUCT_KEY}" "DataRoot" "$DataRoot"
  WriteRegStr HKLM "${PRODUCT_KEY}" "OperatorGroup" "$OperatorGroup"
SectionEnd

Function un.onInit
  StrCpy $DataRoot "$COMMONAPPDATA\${PRODUCT_DATA_DIR}"
  StrCpy $OperatorGroup "${PRODUCT_GROUP}"
  StrCpy $PurgeData "0"

  ReadRegStr $R0 HKLM "${PRODUCT_KEY}" "DataRoot"
  ${If} $R0 != ""
    StrCpy $DataRoot $R0
  ${EndIf}

  ReadRegStr $R0 HKLM "${PRODUCT_KEY}" "OperatorGroup"
  ${If} $R0 != ""
    StrCpy $OperatorGroup $R0
  ${EndIf}

  ${GetParameters} $R0
  ${GetOptions} $R0 "/PURGEDATA=" $R1
  ${If} $R1 == "1"
    StrCpy $PurgeData "1"
  ${EndIf}

  Call un.SetPowerShellPath
FunctionEnd

Function un.SetPowerShellPath
  StrCpy $PowerShellPath "$WINDIR\Sysnative\WindowsPowerShell\v1.0\powershell.exe"
FunctionEnd

Function un.PurgePageCreate
  ${If} $PurgeData == "1"
    Abort
  ${EndIf}

  !insertmacro MUI_HEADER_TEXT "Dati dell'applicazione" "Scegliere se conservare i dati condivisi."
  nsDialogs::Create 1018
  Pop $R0
  ${If} $R0 == error
    Abort
  ${EndIf}

  ${NSD_CreateLabel} 0 0 100% 38u "Per sicurezza, la disinstallazione conserva normalmente database, PDF, configurazione, backup locali e gruppo operatori in ProgramData."
  Pop $R0

  ${NSD_CreateCheckbox} 0 52u 100% 24u "Rimuovi anche tutti i dati condivisi e il gruppo operatori"
  Pop $PurgeCheckbox
  ${NSD_Uncheck} $PurgeCheckbox

  ${NSD_CreateLabel} 0 82u 100% 42u "Attenzione: selezionare questa opzione solo se si vuole eliminare definitivamente l'archivio locale di Voucher Management da questo PC."
  Pop $R0

  nsDialogs::Show
FunctionEnd

Function un.PurgePageLeave
  ${NSD_GetState} $PurgeCheckbox $R0
  ${If} $R0 == ${BST_CHECKED}
    MessageBox MB_ICONEXCLAMATION|MB_YESNO|MB_DEFBUTTON2 "Confermi la rimozione definitiva dei dati condivisi di Voucher Management da questo PC?" IDYES +2
      Abort
    StrCpy $PurgeData "1"
  ${Else}
    StrCpy $PurgeData "0"
  ${EndIf}
FunctionEnd

Section "Uninstall"
  StrCpy $R2 '$\"$PowerShellPath$\" -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $\"$INSTDIR\Uninstall-VoucherManagement.ps1$\" -InstallRoot $\"$INSTDIR$\" -DataRoot $\"$DataRoot$\" -OperatorGroup $\"$OperatorGroup$\" -SkipShortcut -KeepProgramFiles'

  ${If} $PurgeData == "1"
    StrCpy $R2 '$R2 -RemoveData'
  ${EndIf}

  DetailPrint "Verifica chiusura applicazione e dati..."
  nsExec::ExecToLog $R2
  Pop $R3
  ${If} $R3 != "0"
    MessageBox MB_ICONSTOP|MB_OK "Disinstallazione interrotta. Chiudere Voucher Management e riprovare. Codice: $R3."
    Abort
  ${EndIf}

  SetShellVarContext all
  Delete "$SMPROGRAMS\Voucher Management.lnk"

  DeleteRegKey HKLM "${UNINSTALL_KEY}"
  DeleteRegKey HKLM "${PRODUCT_KEY}"

  RMDir /r "$INSTDIR"
SectionEnd
