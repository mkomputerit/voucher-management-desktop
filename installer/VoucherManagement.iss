#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

#define AppName "Voucher Management"
#define AppPublisher "Voucher Management contributors"
#define AppExeName "VoucherManagement.exe"

[Setup]
AppId={{9A246B2C-E26E-4D9A-90E4-CCDB0BA5A731}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL=https://github.com/mkomputerit/voucher-management-desktop
AppSupportURL=https://github.com/mkomputerit/voucher-management-desktop/issues
AppUpdatesURL=https://github.com/mkomputerit/voucher-management-desktop/releases
DefaultDirName={autopf}\Voucher Management
DefaultGroupName=Voucher Management
DisableProgramGroupPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist\installer
OutputBaseFilename=VoucherManagement-{#AppVersion}-Setup-Windows-x64
SetupIconFile=..\dist\VoucherManagement\_internal\assets\VoucherManagement.ico
LicenseFile=..\LICENSE
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=no
RestartApplications=no
UsePreviousAppDir=yes
UninstallDisplayIcon={app}\{#AppExeName}
VersionInfoVersion={#AppVersion}
VersionInfoProductVersion={#AppVersion}
VersionInfoProductName={#AppName}
VersionInfoCompany={#AppPublisher}
VersionInfoDescription=Voucher Management Windows Setup
VersionInfoCopyright=Copyright (c) 2026 Voucher Management contributors
MinVersion=10.0.22000
SetupLogging=yes

[Tasks]
Name: "desktopicon"; Description: "Crea un collegamento sul desktop"; GroupDescription: "Collegamenti:"; Flags: unchecked

[Files]
Source: "..\dist\VoucherManagement\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{commonprograms}\Voucher Management"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"
Name: "{commondesktop}\Voucher Management"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Code]
var
  OperatorPage: TInputQueryWizardPage;

function DefaultOperatorUser: String;
var
  DomainName: String;
begin
  DomainName := GetEnv('USERDOMAIN');
  if DomainName = '' then
    DomainName := GetComputerNameString;
  Result := DomainName + '\' + GetUserNameString;
end;

function CommandLineOperatorUser: String;
begin
  Result := ExpandConstant('{param:OPERATORUSER|}');
end;

function SelectedOperatorUser(Param: String): String;
var
  Value: String;
begin
  Value := CommandLineOperatorUser;
  if Value = '' then
  begin
    if OperatorPage <> nil then
      Value := Trim(OperatorPage.Values[0]);
  end;
  if Value = '' then
    Value := DefaultOperatorUser;
  Result := Value;
end;

function SelectedDataRoot: String;
begin
  Result := ExpandConstant('{param:DATAROOT|}');
  if Result = '' then
    Result := ExpandConstant('{commonappdata}\VoucherManagement');
end;

function SelectedOperatorGroup: String;
begin
  Result := ExpandConstant('{param:OPERATORGROUP|}');
  if Result = '' then
    Result := 'Voucher Management Operators';
end;

procedure InitializeWizard;
var
  InitialUser: String;
begin
  OperatorPage := CreateInputQueryPage(
    wpSelectDir,
    'Utente operatore',
    'Scegli l''account Windows autorizzato',
    'Inserisci l''account che userà Voucher Management. ' +
    'Puoi usare NOMEPC\utente oppure DOMINIO\utente.'
  );
  OperatorPage.Add('Account Windows:', False);
  InitialUser := CommandLineOperatorUser;
  if InitialUser = '' then
    InitialUser := DefaultOperatorUser;
  OperatorPage.Values[0] := InitialUser;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (CurPageID = OperatorPage.ID) and
     (Trim(OperatorPage.Values[0]) = '') then
  begin
    MsgBox(
      'Indicare l''account Windows che userà Voucher Management.',
      mbError,
      MB_OK
    );
    Result := False;
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
  Args: String;
begin
  Result := '';
  Args :=
    '-NoProfile -ExecutionPolicy Bypass -Command ' +
    '"if (Get-Process -Name ''VoucherManagement'' -ErrorAction SilentlyContinue) ' +
    '{ exit 2 } else { exit 0 }"';

  if not Exec(
    ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
    Args,
    '',
    SW_HIDE,
    ewWaitUntilTerminated,
    ResultCode
  ) then
  begin
    Result := 'Impossibile verificare se Voucher Management è in esecuzione.';
    exit;
  end;

  if ResultCode = 2 then
  begin
    Result :=
      'Voucher Management è aperto. Chiudilo normalmente prima di ' +
      'installare o aggiornare, così il backup di chiusura può completarsi.';
    exit;
  end;

  if ResultCode <> 0 then
    Result := 'Verifica preliminare dell''installazione non riuscita.';
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
  ScriptPath: String;
  Args: String;
  OperatorUser: String;
begin
  if CurStep <> ssPostInstall then
    exit;

  ScriptPath := ExpandConstant('{app}\Install-VoucherManagement.ps1');
  OperatorUser := SelectedOperatorUser('');

  Args :=
    '-NoProfile -ExecutionPolicy Bypass -File "' + ScriptPath + '" ' +
    '-ConfigureOnly ' +
    '-InstallRoot "' + ExpandConstant('{app}') + '" ' +
    '-DataRoot "' + SelectedDataRoot + '" ' +
    '-OperatorGroup "' + SelectedOperatorGroup + '" ' +
    '-OperatorUser "' + OperatorUser + '" ' +
    '-SkipShortcut';

  if not Exec(
    ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
    Args,
    '',
    SW_HIDE,
    ewWaitUntilTerminated,
    ResultCode
  ) then
    RaiseException(
      'Impossibile avviare la configurazione dei permessi condivisi.'
    );

  if ResultCode <> 0 then
    RaiseException(
      'La configurazione dei permessi condivisi non è riuscita. ' +
      'Codice uscita: ' + IntToStr(ResultCode)
    );
end;
