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
$installer = Join-Path $PSScriptRoot "Install-VoucherManagement.ps1"
$uninstaller = Join-Path $PSScriptRoot "Uninstall-VoucherManagement.ps1"
$builtinUsersSid = "S-1-5-32-545"

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

    # Destructive boundaries remain constrained to Program Files / ProgramData.
    $unsafeInstallRoot = Join-Path $root "unsafe-install"
    $unsafeDataRoot = Join-Path $root "unsafe-data"

    $unsafeInstallRejected = $false
    try {
        & $installer -SourcePath $SourcePath -InstallRoot $unsafeInstallRoot -DataRoot $dataRoot -SkipShortcut
    }
    catch {
        $unsafeInstallRejected = $true
    }
    if (-not $unsafeInstallRejected) {
        throw "L'installer ha accettato una cartella programma fuori da Program Files."
    }

    $unsafeDataRejected = $false
    try {
        & $installer -SourcePath $SourcePath -InstallRoot $installRoot -DataRoot $unsafeDataRoot -SkipShortcut
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
        & $uninstaller -InstallRoot $unsafeInstallRoot -DataRoot $dataRoot -SkipShortcut
    }
    catch {
        $unsafeUninstallRejected = $true
    }
    if (-not $unsafeUninstallRejected -or -not (Test-Path -LiteralPath $unsafeSentinel)) {
        throw "Il disinstaller non ha protetto una cartella programma fuori da Program Files."
    }

    # Root and nested reparse points must never be traversed by recursive ACLs.
    $junctionTarget = Join-Path $root "junction-target"
    New-Item -ItemType Directory -Force -Path $junctionTarget | Out-Null
    $junctionSentinel = Join-Path $junctionTarget "must-survive.txt"
    Set-Content -LiteralPath $junctionSentinel -Value "preserve" -Encoding ascii
    $junctionDataRoot = Join-Path $env:ProgramData ("VoucherManagementJunction-" + $token)
    New-Item -ItemType Junction -Path $junctionDataRoot -Target $junctionTarget | Out-Null

    $junctionInstallRejected = $false
    try {
        & $installer -SourcePath $SourcePath -InstallRoot $installRoot -DataRoot $junctionDataRoot -SkipShortcut
    }
    catch {
        $junctionInstallRejected = $true
    }
    if (-not $junctionInstallRejected -or -not (Test-Path -LiteralPath $junctionSentinel)) {
        throw "L'installer non ha rifiutato un DataRoot junction."
    }

    $junctionUninstallRejected = $false
    try {
        & $uninstaller -InstallRoot $installRoot -DataRoot $junctionDataRoot -RemoveData -SkipShortcut
    }
    catch {
        $junctionUninstallRejected = $true
    }
    if (-not $junctionUninstallRejected -or -not (Test-Path -LiteralPath $junctionSentinel)) {
        throw "Il disinstaller non ha rifiutato un DataRoot junction."
    }
    Remove-Item -LiteralPath $junctionDataRoot -Force

    $treeDataRoot = Join-Path $env:ProgramData ("VoucherManagementTree-" + $token)
    $treeTarget = Join-Path $root "tree-junction-target"
    New-Item -ItemType Directory -Force -Path $treeDataRoot | Out-Null
    New-Item -ItemType Directory -Force -Path $treeTarget | Out-Null
    $treeSentinel = Join-Path $treeTarget "must-survive.txt"
    Set-Content -LiteralPath $treeSentinel -Value "preserve" -Encoding ascii
    $nestedJunction = Join-Path $treeDataRoot "linked"
    New-Item -ItemType Junction -Path $nestedJunction -Target $treeTarget | Out-Null

    $treeInstallRejected = $false
    try {
        & $installer -SourcePath $SourcePath -InstallRoot $installRoot -DataRoot $treeDataRoot -SkipShortcut
    }
    catch {
        $treeInstallRejected = $true
    }
    if (-not $treeInstallRejected -or -not (Test-Path -LiteralPath $treeSentinel)) {
        throw "L'installer non ha rifiutato un reparse point interno al DataRoot."
    }

    $treeUninstallRejected = $false
    try {
        & $uninstaller -InstallRoot $installRoot -DataRoot $treeDataRoot -RemoveData -SkipShortcut
    }
    catch {
        $treeUninstallRejected = $true
    }
    if (-not $treeUninstallRejected -or -not (Test-Path -LiteralPath $treeSentinel)) {
        throw "Il disinstaller non ha rifiutato un reparse point interno al DataRoot."
    }
    Remove-Item -LiteralPath $nestedJunction -Force
    Remove-Item -LiteralPath $treeDataRoot -Recurse -Force

    # A failure before the staged swap must preserve any previous installation.
    New-Item -ItemType Directory -Force -Path $installRoot | Out-Null
    $oldInstallSentinel = Join-Path $installRoot "old-install.txt"
    Set-Content -LiteralPath $oldInstallSentinel -Value "old" -Encoding ascii
    Set-Content -LiteralPath $invalidDataParent -Value "not-a-directory" -Encoding ascii
    $invalidDataRoot = Join-Path $invalidDataParent "child"
    $failedAsExpected = $false
    try {
        & $installer -SourcePath $SourcePath -InstallRoot $installRoot -DataRoot $invalidDataRoot -SkipShortcut
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
    Remove-Item -LiteralPath $installRoot -Recurse -Force

    # Seed an existing permissive ProgramData tree. Installation must normalize
    # every descendant to SYSTEM/Admins full control + built-in Users Modify.
    New-Item -ItemType Directory -Force -Path $dataRoot | Out-Null
    $legacyFile = Join-Path $dataRoot "legacy-permissive.txt"
    Set-Content -LiteralPath $legacyFile -Value "legacy" -Encoding ascii
    & icacls.exe $dataRoot /grant "*S-1-1-0:(OI)(CI)F" /T /C | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Impossibile predisporre l'ACL permissiva di test."
    }

    & $installer -SourcePath $SourcePath -InstallRoot $installRoot -DataRoot $dataRoot -SkipShortcut

    $markerPath = Join-Path $installRoot "voucher-management-deployment.json"
    if (-not (Test-Path -LiteralPath $markerPath -PathType Leaf)) {
        throw "Marker di deployment non creato."
    }
    $marker = Get-Content -LiteralPath $markerPath -Raw | ConvertFrom-Json
    if ($marker.format -ne 1 -or $marker.mode -ne "shared_programdata") {
        throw "Marker di deployment non valido."
    }
    if ([IO.Path]::GetFullPath([string]$marker.data_root) -ine [IO.Path]::GetFullPath($dataRoot)) {
        throw "Il marker non registra il DataRoot effettivamente installato."
    }
    if ([string]$marker.access_model -ne "builtin_users_modify" -or [string]$marker.access_sid -ne $builtinUsersSid) {
        throw "Il marker non registra il modello di accesso Windows condiviso."
    }
    if ($marker.operator_group_sid -or $marker.operator_group_name) {
        throw "La nuova installazione non deve dipendere da gruppi applicativi dedicati."
    }

    $aclState = Get-AllowRightsBySid -Path $dataRoot
    if (-not $aclState.Protected) {
        throw "Le ACL ProgramData ereditano ancora permessi dal parent."
    }
    Assert-Rights -Rights $aclState.Rights -Sid "S-1-5-18" -Expected ([Security.AccessControl.FileSystemRights]::FullControl)
    Assert-Rights -Rights $aclState.Rights -Sid "S-1-5-32-544" -Expected ([Security.AccessControl.FileSystemRights]::FullControl)
    Assert-Rights -Rights $aclState.Rights -Sid $builtinUsersSid -Expected ([Security.AccessControl.FileSystemRights]::Modify)

    foreach ($forbiddenSid in @("S-1-1-0", "S-1-5-11")) {
        if ($aclState.Rights.ContainsKey($forbiddenSid)) {
            throw "ACL troppo ampia: SID vietato $forbiddenSid presente."
        }
    }

    $legacyAcl = Get-AllowRightsBySid -Path $legacyFile
    if ($legacyAcl.Rights.ContainsKey("S-1-1-0")) {
        throw "ACE esplicita Everyone sopravvissuta su un file preesistente."
    }
    Assert-Rights -Rights $legacyAcl.Rights -Sid $builtinUsersSid -Expected ([Security.AccessControl.FileSystemRights]::Modify)

    # Normal Setup upgrades must preserve a custom ProgramData child and data.
    $sentinel = Join-Path $dataRoot "upgrade-preserves-data.txt"
    Set-Content -LiteralPath $sentinel -Value "preserve" -Encoding ascii
    & $installer -SourcePath $SourcePath -InstallRoot $installRoot -SkipShortcut

    if (-not (Test-Path -LiteralPath $sentinel -PathType Leaf)) {
        throw "Un aggiornamento ha cancellato i dati condivisi."
    }
    $upgradedMarker = Get-Content -LiteralPath $markerPath -Raw | ConvertFrom-Json
    if ([IO.Path]::GetFullPath([string]$upgradedMarker.data_root) -ine [IO.Path]::GetFullPath($dataRoot)) {
        throw "Un aggiornamento senza parametri ha cambiato il DataRoot."
    }
    if ([string]$upgradedMarker.access_sid -ne $builtinUsersSid) {
        throw "Un aggiornamento ha perso il modello di accesso Users."
    }

    # Default uninstall keeps ProgramData.
    & $uninstaller -InstallRoot $installRoot -DataRoot $dataRoot -SkipShortcut
    if (Test-Path -LiteralPath $installRoot) {
        throw "La disinstallazione non ha rimosso i file programma."
    }
    if (-not (Test-Path -LiteralPath $dataRoot -PathType Container)) {
        throw "La disinstallazione predefinita ha rimosso ProgramData."
    }

    # Explicit data removal can be performed later and needs no group cleanup.
    & $uninstaller -InstallRoot $installRoot -DataRoot $dataRoot -RemoveData -SkipShortcut
    if (Test-Path -LiteralPath $dataRoot) {
        throw "-RemoveData non ha rimosso ProgramData."
    }

    Write-Host "Shared Windows installer integration test OK"
}
finally {
    Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $installRoot -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $dataRoot -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $invalidDataParent -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $treeDataRoot -Recurse -Force -ErrorAction SilentlyContinue
}
