[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$SetupPath
)

$ErrorActionPreference = "Stop"

$setup = [IO.Path]::GetFullPath($SetupPath)
if (-not (Test-Path -LiteralPath $setup -PathType Leaf)) {
    throw "Setup.exe non trovato: $setup"
}

$root = Join-Path $env:RUNNER_TEMP ("voucher-management-setup-test-" + [Guid]::NewGuid().ToString("N"))
$installRoot = Join-Path $root "Program Files\Voucher Management"
$dataRoot = Join-Path $root "ProgramData\VoucherManagement"
$groupName = "VMSetup-" + [Guid]::NewGuid().ToString("N").Substring(0, 12)
$operatorUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name

function Get-AllowRightsBySid {
    param([string]$Path)
    $acl = Get-Acl -LiteralPath $Path
    $rights = @{}
    foreach ($ace in $acl.Access) {
        if ($ace.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow) {
            continue
        }
        $sid = $ace.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
        if (-not $rights.ContainsKey($sid)) {
            $rights[$sid] = [Security.AccessControl.FileSystemRights]0
        }
        $rights[$sid] = $rights[$sid] -bor $ace.FileSystemRights
    }
    return @{
        Protected = $acl.AreAccessRulesProtected
        Rights = $rights
    }
}

function Assert-Rights {
    param(
        [hashtable]$Rights,
        [string]$Sid,
        [Security.AccessControl.FileSystemRights]$Expected
    )
    if (-not $Rights.ContainsKey($Sid)) {
        throw "ACL mancante per SID $Sid"
    }
    if (($Rights[$Sid] -band $Expected) -ne $Expected) {
        throw "ACL insufficiente per SID $Sid"
    }
}

function Invoke-Setup {
    $arguments = @(
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/SP-",
        "/DIR=$installRoot",
        "/OPERATORUSER=$operatorUser",
        "/DATAROOT=$dataRoot",
        "/OPERATORGROUP=$groupName"
    )
    & $setup @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Setup.exe non riuscito. Codice uscita: $LASTEXITCODE"
    }
}

try {
    New-Item -ItemType Directory -Force -Path $root | Out-Null
    Invoke-Setup

    $exe = Join-Path $installRoot "VoucherManagement.exe"
    if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {
        throw "L'installer non ha installato VoucherManagement.exe."
    }

    $markerPath = Join-Path $installRoot "voucher-management-deployment.json"
    if (-not (Test-Path -LiteralPath $markerPath -PathType Leaf)) {
        throw "Marker shared deployment mancante."
    }
    $marker = Get-Content -LiteralPath $markerPath -Raw | ConvertFrom-Json
    if ($marker.format -ne 1 -or $marker.mode -ne "shared_programdata") {
        throw "Marker shared deployment non valido."
    }

    $group = Get-LocalGroup -Name $groupName -ErrorAction Stop
    $member = Get-LocalGroupMember -Group $groupName -ErrorAction Stop |
        Where-Object { $_.Name -ieq $operatorUser }
    if (-not $member) {
        throw "L'account operatore non è stato aggiunto al gruppo."
    }

    $aclState = Get-AllowRightsBySid -Path $dataRoot
    if (-not $aclState.Protected) {
        throw "Le ACL del DataRoot ereditano ancora dal parent."
    }
    Assert-Rights -Rights $aclState.Rights -Sid "S-1-5-18" -Expected ([Security.AccessControl.FileSystemRights]::FullControl)
    Assert-Rights -Rights $aclState.Rights -Sid "S-1-5-32-544" -Expected ([Security.AccessControl.FileSystemRights]::FullControl)
    Assert-Rights -Rights $aclState.Rights -Sid $group.SID.Value -Expected ([Security.AccessControl.FileSystemRights]::Modify)

    foreach ($forbiddenSid in @("S-1-1-0", "S-1-5-11")) {
        if ($aclState.Rights.ContainsKey($forbiddenSid)) {
            throw "ACL troppo ampia: SID vietato $forbiddenSid presente."
        }
    }

    $uninstallEntry = Get-ChildItem "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall" |
        Get-ItemProperty |
        Where-Object { $_.DisplayName -eq "Voucher Management" } |
        Select-Object -First 1
    if (-not $uninstallEntry) {
        throw "Voucher Management non risulta registrato in Installed Apps."
    }

    $sentinel = Join-Path $dataRoot "setup-upgrade-preserves-data.txt"
    Set-Content -LiteralPath $sentinel -Value "preserve" -Encoding ascii
    Invoke-Setup

    if (-not (Test-Path -LiteralPath $sentinel -PathType Leaf)) {
        throw "L'aggiornamento tramite Setup.exe ha cancellato ProgramData."
    }

    $uninstaller = Get-ChildItem -LiteralPath $installRoot -Filter "unins*.exe" |
        Select-Object -First 1
    if (-not $uninstaller) {
        throw "Disinstallatore Inno Setup non trovato."
    }

    & $uninstaller.FullName "/VERYSILENT" "/SUPPRESSMSGBOXES" "/NORESTART"
    if ($LASTEXITCODE -ne 0) {
        throw "Disinstallazione Setup.exe non riuscita: $LASTEXITCODE"
    }

    $deadline = [DateTime]::UtcNow.AddSeconds(20)
    while (
        (Test-Path -LiteralPath $installRoot) -and
        [DateTime]::UtcNow -lt $deadline
    ) {
        Start-Sleep -Milliseconds 250
    }
    if (Test-Path -LiteralPath $installRoot) {
        throw "La disinstallazione non ha rimosso i file programma."
    }
    if (-not (Test-Path -LiteralPath $dataRoot -PathType Container)) {
        throw "La disinstallazione predefinita ha rimosso i dati condivisi."
    }
    if (-not (Get-LocalGroup -Name $groupName -ErrorAction SilentlyContinue)) {
        throw "La disinstallazione predefinita ha rimosso il gruppo operatori."
    }

    Write-Host "Native Windows Setup integration test OK"
}
finally {
    Get-Process -Name "VoucherManagement" -ErrorAction SilentlyContinue |
        Stop-Process -Force -ErrorAction SilentlyContinue

    if (Test-Path -LiteralPath $installRoot) {
        $cleanupUninstaller = Get-ChildItem -LiteralPath $installRoot -Filter "unins*.exe" -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($cleanupUninstaller) {
            & $cleanupUninstaller.FullName "/VERYSILENT" "/SUPPRESSMSGBOXES" "/NORESTART" | Out-Null
        }
        Remove-Item -LiteralPath $installRoot -Recurse -Force -ErrorAction SilentlyContinue
    }

    if (Test-Path -LiteralPath $dataRoot) {
        & icacls.exe $dataRoot /reset /T /C | Out-Null
        & icacls.exe $dataRoot /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" /T /C | Out-Null
        Remove-Item -LiteralPath $dataRoot -Recurse -Force -ErrorAction SilentlyContinue
    }

    if (Get-LocalGroup -Name $groupName -ErrorAction SilentlyContinue) {
        Remove-LocalGroup -Name $groupName -ErrorAction SilentlyContinue
    }
    Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue
}
