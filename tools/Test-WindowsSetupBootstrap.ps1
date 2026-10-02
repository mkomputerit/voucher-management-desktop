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
$installRoot = Join-Path $root "ProgramFiles\Voucher Management"
$dataRoot = Join-Path $root "ProgramData\VoucherManagement"
$groupName = "VMSetup-" + [Guid]::NewGuid().ToString("N").Substring(0, 12)
$operatorUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name

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
        ('"/OperatorGroup={0}"' -f $groupName),
        ('"/OperatorUser={0}"' -f $operatorUser),
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

    $group = Get-LocalGroup -Name $groupName
    $aclState = Get-AllowRightsBySid -Path $dataRoot
    if (-not $aclState.Protected) {
        throw "ACL ProgramData non protetta dopo Setup.exe."
    }
    foreach ($forbiddenSid in @("S-1-1-0", "S-1-5-11")) {
        if ($aclState.Rights.ContainsKey($forbiddenSid)) {
            throw "Setup.exe ha lasciato un SID vietato nelle ACL: $forbiddenSid"
        }
    }
    if (-not $aclState.Rights.ContainsKey($group.SID.Value)) {
        throw "Gruppo operatori assente dalle ACL dopo Setup.exe."
    }
    $modify = [Security.AccessControl.FileSystemRights]::Modify
    if (($aclState.Rights[$group.SID.Value] -band $modify) -ne $modify) {
        throw "Permessi gruppo operatori insufficienti dopo Setup.exe."
    }

    $uninstaller = Join-Path $installRoot "Uninstall-VoucherManagement.ps1"
    if (-not (Test-Path -LiteralPath $uninstaller -PathType Leaf)) {
        throw "Disinstaller PowerShell non installato."
    }
    $uninstallArgs = @{
        InstallRoot = $installRoot
        DataRoot = $dataRoot
        OperatorGroup = $groupName
        RemoveData = $true
        SkipShortcut = $true
    }
    & $uninstaller @uninstallArgs

    $deadline = [DateTime]::UtcNow.AddSeconds(8)
    while ((Test-Path -LiteralPath $installRoot) -and [DateTime]::UtcNow -lt $deadline) {
        Start-Sleep -Milliseconds 250
    }
    if (Test-Path -LiteralPath $dataRoot) {
        throw "La pulizia di test non ha rimosso ProgramData."
    }
    if (Get-LocalGroup -Name $groupName -ErrorAction SilentlyContinue) {
        throw "La pulizia di test non ha rimosso il gruppo operatori."
    }

    Write-Host "Windows Setup bootstrapper integration test OK"
}
finally {
    Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue
    if (Get-LocalGroup -Name $groupName -ErrorAction SilentlyContinue) {
        Remove-LocalGroup -Name $groupName -ErrorAction SilentlyContinue
    }
}