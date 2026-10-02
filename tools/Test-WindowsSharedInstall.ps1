[CmdletBinding()]
param(
    [string]$SourcePath = (Join-Path (Split-Path -Parent $PSScriptRoot) "dist\VoucherManagement")
)

$ErrorActionPreference = "Stop"

$token = [Guid]::NewGuid().ToString("N")
$root = Join-Path $env:RUNNER_TEMP ("voucher-management-install-test-" + $token)
$installRoot = Join-Path $env:ProgramFiles ("Voucher Management Test-" + $token)
$dataRoot = Join-Path $env:ProgramData ("VoucherManagementTest-" + $token)
$invalidDataParent = Join-Path $env:ProgramData ("VoucherManagementInvalid-" + $token)
$groupName = "VMTest-" + $token.Substring(0, 12)
$operatorUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$installer = Join-Path $PSScriptRoot "Install-VoucherManagement.ps1"
$uninstaller = Join-Path $PSScriptRoot "Uninstall-VoucherManagement.ps1"

function Get-AllowRightsBySid {
    param([string]$Path)

    $acl = Get-Acl -LiteralPath $Path
    $result = @{}
    foreach ($ace in $acl.Access) {
        if ($ace.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow) {
            continue
        }
        $sid = $ace.IdentityReference.Translate(
            [Security.Principal.SecurityIdentifier]
        ).Value
        if (-not $result.ContainsKey($sid)) {
            $result[$sid] = [Security.AccessControl.FileSystemRights]0
        }
        $result[$sid] = $result[$sid] -bor $ace.FileSystemRights
    }
    return @{
        Protected = $acl.AreAccessRulesProtected
        Rights = $result
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
        throw "ACL insufficiente per SID $Sid. Attuale: $($Rights[$Sid]); atteso almeno: $Expected"
    }
}

try {
    New-Item -ItemType Directory -Force -Path $root | Out-Null

    # Elevated install/uninstall paths are destructive boundaries. Reject
    # caller-controlled roots outside Program Files / ProgramData before any
    # ACL reset, application swap or recursive deletion can happen.
    $unsafeInstallRoot = Join-Path $root "unsafe-install"
    $unsafeDataRoot = Join-Path $root "unsafe-data"

    $unsafeInstallRejected = $false
    try {
        $unsafeInstallArgs = @{
            SourcePath = $SourcePath
            InstallRoot = $unsafeInstallRoot
            DataRoot = $dataRoot
            OperatorGroup = $groupName
            OperatorUser = $operatorUser
            SkipShortcut = $true
        }
        & $installer @unsafeInstallArgs
    }
    catch {
        $unsafeInstallRejected = $true
    }
    if (-not $unsafeInstallRejected) {
        throw "L'installer ha accettato una cartella programma fuori da Program Files."
    }

    $unsafeDataRejected = $false
    try {
        $unsafeDataArgs = @{
            SourcePath = $SourcePath
            InstallRoot = $installRoot
            DataRoot = $unsafeDataRoot
            OperatorGroup = $groupName
            OperatorUser = $operatorUser
            SkipShortcut = $true
        }
        & $installer @unsafeDataArgs
    }
    catch {
        $unsafeDataRejected = $true
    }
    if (-not $unsafeDataRejected) {
        throw "L'installer ha accettato una cartella dati fuori da ProgramData."
    }

    New-Item -ItemType Directory -Force -Path $unsafeInstallRoot | Out-Null
    $unsafeSentinel = Join-Path $unsafeInstallRoot "must-survive.txt"
    Set-Content -LiteralPath $unsafeSentinel -Value "preserve" -Encoding ascii
    $unsafeUninstallRejected = $false
    try {
        $unsafeUninstallArgs = @{
            InstallRoot = $unsafeInstallRoot
            DataRoot = $dataRoot
            OperatorGroup = $groupName
            SkipShortcut = $true
        }
        & $uninstaller @unsafeUninstallArgs
    }
    catch {
        $unsafeUninstallRejected = $true
    }
    if (-not $unsafeUninstallRejected -or -not (Test-Path -LiteralPath $unsafeSentinel)) {
        throw "Il disinstaller non ha protetto una cartella programma fuori da Program Files."
    }

    $junctionTarget = Join-Path $root "junction-target"
    New-Item -ItemType Directory -Force -Path $junctionTarget | Out-Null
    $junctionSentinel = Join-Path $junctionTarget "must-survive.txt"
    Set-Content -LiteralPath $junctionSentinel -Value "preserve" -Encoding ascii
    $junctionDataRoot = Join-Path $env:ProgramData ("VoucherManagementJunction-" + $token)
    New-Item -ItemType Junction -Path $junctionDataRoot -Target $junctionTarget | Out-Null

    $junctionInstallRejected = $false
    try {
        $junctionInstallArgs = @{
            SourcePath = $SourcePath
            InstallRoot = $installRoot
            DataRoot = $junctionDataRoot
            OperatorGroup = $groupName
            OperatorUser = $operatorUser
            SkipShortcut = $true
        }
        & $installer @junctionInstallArgs
    }
    catch {
        $junctionInstallRejected = $true
    }
    if (-not $junctionInstallRejected -or -not (Test-Path -LiteralPath $junctionSentinel)) {
        throw "L'installer non ha rifiutato un DataRoot junction."
    }

    $junctionUninstallRejected = $false
    try {
        $junctionUninstallArgs = @{
            InstallRoot = $installRoot
            DataRoot = $junctionDataRoot
            OperatorGroup = $groupName
            RemoveData = $true
            SkipShortcut = $true
        }
        & $uninstaller @junctionUninstallArgs
    }
    catch {
        $junctionUninstallRejected = $true
    }
    if (-not $junctionUninstallRejected -or -not (Test-Path -LiteralPath $junctionSentinel)) {
        throw "Il disinstaller non ha rifiutato un DataRoot junction."
    }
    Remove-Item -LiteralPath $junctionDataRoot -Force

    # A failure before the staged application swap must never destroy the
    # previously installed version or leave a shared-deployment marker behind.
    New-Item -ItemType Directory -Force -Path $installRoot | Out-Null
    $oldInstallSentinel = Join-Path $installRoot "old-install.txt"
    Set-Content -LiteralPath $oldInstallSentinel -Value "old" -Encoding ascii
    Set-Content -LiteralPath $invalidDataParent -Value "not-a-directory" -Encoding ascii
    $invalidDataRoot = Join-Path $invalidDataParent "child"
    $failedAsExpected = $false
    try {
        $failedArgs = @{
            SourcePath = $SourcePath
            InstallRoot = $installRoot
            DataRoot = $invalidDataRoot
            OperatorGroup = $groupName
            OperatorUser = $operatorUser
            SkipShortcut = $true
        }
        & $installer @failedArgs
    }
    catch {
        $failedAsExpected = $true
    }
    if (-not $failedAsExpected) {
        throw "Il test di installazione fallita non ha prodotto un errore."
    }
    if (-not (Test-Path -LiteralPath $oldInstallSentinel -PathType Leaf)) {
        throw "Un'installazione fallita ha distrutto la versione precedente."
    }
    if (Test-Path -LiteralPath (Join-Path $installRoot "voucher-management-deployment.json")) {
        throw "Un'installazione fallita ha scritto il marker shared mode."
    }

    New-Item -ItemType Directory -Force -Path $dataRoot | Out-Null
    $legacyFile = Join-Path $dataRoot "legacy-permissive.txt"
    Set-Content -LiteralPath $legacyFile -Value "legacy" -Encoding ascii

    # Seed the exact upgrade hazard under review: explicit third-party grants
    # on an already-existing ProgramData tree. The installer must remove them.
    & icacls.exe $dataRoot /grant "*S-1-1-0:(OI)(CI)F" /T /C | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Impossibile predisporre l'ACL permissiva di test."
    }

    $installArgs = @{
        SourcePath = $SourcePath
        InstallRoot = $installRoot
        DataRoot = $dataRoot
        OperatorGroup = $groupName
        OperatorUser = $operatorUser
        SkipShortcut = $true
    }
    & $installer @installArgs

    $markerPath = Join-Path $installRoot "voucher-management-deployment.json"
    if (-not (Test-Path -LiteralPath $markerPath -PathType Leaf)) {
        throw "Marker di deployment non creato."
    }
    $marker = Get-Content -LiteralPath $markerPath -Raw | ConvertFrom-Json
    if ($marker.format -ne 1 -or $marker.mode -ne "shared_programdata") {
        throw "Marker di deployment non valido."
    }

    $group = Get-LocalGroup -Name $groupName
    $aclState = Get-AllowRightsBySid -Path $dataRoot
    if (-not $aclState.Protected) {
        throw "Le ACL ProgramData ereditano ancora permessi dal parent."
    }

    Assert-Rights -Rights $aclState.Rights -Sid "S-1-5-18" -Expected ([Security.AccessControl.FileSystemRights]::FullControl)
    Assert-Rights -Rights $aclState.Rights -Sid "S-1-5-32-544" -Expected ([Security.AccessControl.FileSystemRights]::FullControl)
    Assert-Rights -Rights $aclState.Rights -Sid $group.SID.Value -Expected ([Security.AccessControl.FileSystemRights]::Modify)

    foreach ($forbiddenSid in @("S-1-1-0", "S-1-5-11")) {
        if ($aclState.Rights.ContainsKey($forbiddenSid)) {
            throw "ACL troppo ampia: SID vietato $forbiddenSid presente."
        }
    }

    $legacyAcl = Get-AllowRightsBySid -Path $legacyFile
    if ($legacyAcl.Rights.ContainsKey("S-1-1-0")) {
        throw "ACE esplicita Everyone sopravvissuta su un file preesistente."
    }

    $sentinel = Join-Path $dataRoot "upgrade-preserves-data.txt"
    Set-Content -LiteralPath $sentinel -Value "preserve" -Encoding ascii

    & $installer @installArgs

    if (-not (Test-Path -LiteralPath $sentinel -PathType Leaf)) {
        throw "Un aggiornamento ha cancellato i dati condivisi."
    }

    $uninstallArgs = @{
        InstallRoot = $installRoot
        DataRoot = $dataRoot
        OperatorGroup = $groupName
        SkipShortcut = $true
    }
    & $uninstaller @uninstallArgs

    if (Test-Path -LiteralPath $installRoot) {
        throw "La disinstallazione non ha rimosso i file programma."
    }
    if (-not (Test-Path -LiteralPath $dataRoot -PathType Container)) {
        throw "La disinstallazione predefinita ha rimosso ProgramData."
    }
    if (-not (Get-LocalGroup -Name $groupName -ErrorAction SilentlyContinue)) {
        throw "La disinstallazione predefinita ha rimosso il gruppo operatori."
    }

    $uninstallArgs.Remove("SkipShortcut")
    $uninstallArgs["SkipShortcut"] = $true
    $uninstallArgs["RemoveData"] = $true
    & $uninstaller @uninstallArgs

    if (Test-Path -LiteralPath $dataRoot) {
        throw "-RemoveData non ha rimosso ProgramData."
    }
    if (Get-LocalGroup -Name $groupName -ErrorAction SilentlyContinue) {
        throw "-RemoveData non ha rimosso il gruppo operatori."
    }

    Write-Host "Shared Windows installer integration test OK"
}
finally {
    Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $installRoot -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $dataRoot -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $invalidDataParent -Force -ErrorAction SilentlyContinue
    if (Get-LocalGroup -Name $groupName -ErrorAction SilentlyContinue) {
        Remove-LocalGroup -Name $groupName -ErrorAction SilentlyContinue
    }
}
