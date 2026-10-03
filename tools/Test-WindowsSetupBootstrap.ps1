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

$token = [Guid]::NewGuid().ToString("N")
$root = Join-Path $env:RUNNER_TEMP ("voucher-management-setup-test-" + $token)
$installRoot = Join-Path $env:ProgramFiles ("Voucher Management Setup Test-" + $token)
$dataRoot = Join-Path $env:ProgramData ("VoucherManagementSetupTest-" + $token)
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

try {
    New-Item -ItemType Directory -Force -Path $root | Out-Null

    $logPath = Join-Path $root "setup-diagnostic.log"
    $arguments = @(
        '"/quiet"',
        ('"/InstallRoot={0}"' -f $installRoot),
        ('"/DataRoot={0}"' -f $dataRoot),
        ('"/LogPath={0}"' -f $logPath),
        '"/SkipShortcut"'
    ) -join " "
    $process = Start-Process -FilePath $setup -ArgumentList $arguments -Wait -PassThru
    if ($process.ExitCode -ne 0) {
        $diagnostic = (
            Get-Content -LiteralPath $logPath -Raw -ErrorAction SilentlyContinue
        )
        if (-not $diagnostic) {
            $diagnostic = "Nessun log diagnostico prodotto dal Setup."
        }
        throw (
            "Setup bootstrapper terminato con codice $($process.ExitCode). " +
            $diagnostic
        )
    }

    $installedExe = Join-Path $installRoot "VoucherManagement.exe"
    if (-not (Test-Path -LiteralPath $installedExe -PathType Leaf)) {
        throw "Il Setup non ha installato VoucherManagement.exe."
    }

    $markerPath = Join-Path $installRoot "voucher-management-deployment.json"
    if (-not (Test-Path -LiteralPath $markerPath -PathType Leaf)) {
        throw "Il Setup non ha creato il marker shared mode."
    }
    $marker = Get-Content -LiteralPath $markerPath -Raw | ConvertFrom-Json
    if ($marker.format -ne 1 -or $marker.mode -ne "shared_programdata") {
        throw "Marker shared mode non valido dopo Setup.exe."
    }
    if ([IO.Path]::GetFullPath([string]$marker.data_root) -ine [IO.Path]::GetFullPath($dataRoot)) {
        throw "Il Setup non ha registrato il DataRoot installato nel marker."
    }
    if ([string]$marker.access_model -ne "builtin_users_modify" -or [string]$marker.access_sid -ne $builtinUsersSid) {
        throw "Il Setup non ha registrato il modello di accesso condiviso Windows."
    }
    if ($marker.operator_group_sid -or $marker.operator_group_name) {
        throw "Il Setup non deve creare o registrare gruppi applicativi dedicati."
    }

    $uninstallExe = Join-Path $installRoot "VoucherManagement-Uninstall.exe"
    if (-not (Test-Path -LiteralPath $uninstallExe -PathType Leaf)) {
        throw "Il Setup non ha installato l'uninstaller elevato."
    }
    $uninstallInfo = (Get-Item -LiteralPath $uninstallExe).VersionInfo
    if ($uninstallInfo.ProductName -ne "Voucher Management") {
        throw "ProductName dell'uninstaller installato non valido."
    }

    $uninstallRegistryPath = "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\VoucherManagement"
    if (-not (Test-Path -LiteralPath $uninstallRegistryPath)) {
        throw "Il Setup non ha registrato Voucher Management in App installate."
    }
    $uninstallEntry = Get-ItemProperty -LiteralPath $uninstallRegistryPath
    if ([IO.Path]::GetFullPath([string]$uninstallEntry.InstallLocation) -ine [IO.Path]::GetFullPath($installRoot)) {
        throw "InstallLocation della voce di disinstallazione non valido."
    }
    if ([string]$uninstallEntry.DisplayName -ne "Voucher Management") {
        throw "DisplayName della voce di disinstallazione non valido."
    }
    if ([string]$uninstallEntry.UninstallString -notlike "*VoucherManagement-Uninstall.exe*") {
        throw "UninstallString non punta al bootstrapper elevato."
    }

    $sumPath = Join-Path $installRoot "SHA256SUMS.txt"
    if (-not (Test-Path -LiteralPath $sumPath -PathType Leaf)) {
        throw "Checksum applicazione non installato."
    }
    $sumLine = (Get-Content -LiteralPath $sumPath | Select-Object -First 1).Trim()
    $expectedExeHash = ($sumLine -split "\s+")[0].ToLowerInvariant()
    $actualExeHash = (Get-FileHash -LiteralPath $installedExe -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($expectedExeHash -ne $actualExeHash) {
        throw "VoucherManagement.exe installato non corrisponde al checksum del payload."
    }

    $aclState = Get-AllowRightsBySid -Path $dataRoot
    if (-not $aclState.Protected) {
        throw "ACL ProgramData non protetta dopo Setup.exe."
    }
    foreach ($forbiddenSid in @("S-1-1-0", "S-1-5-11")) {
        if ($aclState.Rights.ContainsKey($forbiddenSid)) {
            throw "Setup.exe ha lasciato un SID vietato nelle ACL: $forbiddenSid"
        }
    }
    if (-not $aclState.Rights.ContainsKey($builtinUsersSid)) {
        throw "Gruppo built-in Users assente dalle ACL dopo Setup.exe."
    }
    $modify = [Security.AccessControl.FileSystemRights]::Modify
    if (($aclState.Rights[$builtinUsersSid] -band $modify) -ne $modify) {
        throw "Permessi Users insufficienti dopo Setup.exe."
    }

    # Re-run the real Setup without repeating DataRoot. The marker must preserve
    # the custom ProgramData child and the standard built-in Users access model.
    $upgradeSentinel = Join-Path $dataRoot "setup-upgrade-preserves-data.txt"
    Set-Content -LiteralPath $upgradeSentinel -Value "preserve" -Encoding ascii
    $upgradeLogPath = Join-Path $root "setup-upgrade-diagnostic.log"
    $upgradeArguments = @(
        '"/quiet"',
        ('"/InstallRoot={0}"' -f $installRoot),
        ('"/LogPath={0}"' -f $upgradeLogPath),
        '"/SkipShortcut"'
    ) -join " "
    $upgradeProcess = Start-Process -FilePath $setup -ArgumentList $upgradeArguments -Wait -PassThru
    if ($upgradeProcess.ExitCode -ne 0) {
        $diagnostic = (
            Get-Content -LiteralPath $upgradeLogPath -Raw -ErrorAction SilentlyContinue
        )
        throw "Setup upgrade senza parametri deployment fallito. $diagnostic"
    }
    if (-not (Test-Path -LiteralPath $upgradeSentinel -PathType Leaf)) {
        throw "Il Setup upgrade ha cambiato DataRoot o perso dati esistenti."
    }
    $upgradedMarker = Get-Content -LiteralPath $markerPath -Raw | ConvertFrom-Json
    if ([IO.Path]::GetFullPath([string]$upgradedMarker.data_root) -ine [IO.Path]::GetFullPath($dataRoot)) {
        throw "Il Setup upgrade ha cambiato il DataRoot installato."
    }
    if ([string]$upgradedMarker.access_sid -ne $builtinUsersSid) {
        throw "Il Setup upgrade ha perso il modello di accesso Users."
    }

    $uninstaller = Join-Path $installRoot "Uninstall-VoucherManagement.ps1"
    if (-not (Test-Path -LiteralPath $uninstaller -PathType Leaf)) {
        throw "Disinstaller PowerShell non installato."
    }

    $uninstallProcess = Start-Process -FilePath $uninstallExe -ArgumentList '"/quiet"', '"/RemoveData"' -Wait -PassThru
    if ($uninstallProcess.ExitCode -ne 0) {
        throw "Bootstrapper di disinstallazione terminato con codice $($uninstallProcess.ExitCode)."
    }

    $deadline = [DateTime]::UtcNow.AddSeconds(8)
    while ((Test-Path -LiteralPath $installRoot) -and [DateTime]::UtcNow -lt $deadline) {
        Start-Sleep -Milliseconds 250
    }
    if (Test-Path -LiteralPath $dataRoot) {
        throw "La pulizia di test non ha rimosso ProgramData."
    }
    if (Test-Path -LiteralPath $uninstallRegistryPath) {
        throw "La disinstallazione non ha rimosso la voce da App installate."
    }

    Write-Host "Windows Setup bootstrapper integration test OK"
}
finally {
    Remove-Item -LiteralPath "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\VoucherManagement" -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $installRoot -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $dataRoot -Recurse -Force -ErrorAction SilentlyContinue
}
