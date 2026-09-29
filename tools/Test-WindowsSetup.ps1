[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$SetupPath
)

$ErrorActionPreference = "Stop"

$root = Join-Path $env:RUNNER_TEMP ("voucher-management-setup-test-" + [Guid]::NewGuid().ToString("N"))
$installRoot = Join-Path $root "ProgramFiles\VoucherManagement"
$dataRoot = Join-Path $root "ProgramData\VoucherManagement"
$groupName = "VMSetup-" + [Guid]::NewGuid().ToString("N").Substring(0, 12)
$operatorUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$uninstallKey = "HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\VoucherManagement"
$productKey = "HKLM:\Software\VoucherManagement"
$commonShortcut = Join-Path $env:ProgramData "Microsoft\Windows\Start Menu\Programs\Voucher Management.lnk"

function Invoke-CheckedProcess {
    param(
        [string]$FilePath,
        [string[]]$Arguments,
        [string]$Description
    )

    $process = Start-Process -FilePath $FilePath -ArgumentList $Arguments -PassThru -Wait
    if ($process.ExitCode -ne 0) {
        throw "$Description non riuscito. Exit code: $($process.ExitCode)"
    }
}

function Wait-PathRemoved {
    param([string]$Path)

    for ($i = 0; $i -lt 50; $i++) {
        if (-not (Test-Path -LiteralPath $Path)) {
            return
        }
        Start-Sleep -Milliseconds 200
    }
    throw "Percorso non rimosso entro il limite del test: $Path"
}

function Assert-SharedAcl {
    param([string]$Path, [string]$GroupName)

    $group = Get-LocalGroup -Name $GroupName
    $allowed = @(
        "S-1-5-18",
        "S-1-5-32-544",
        $group.SID.Value
    )

    $acl = Get-Acl -LiteralPath $Path
    if (-not $acl.AreAccessRulesProtected) {
        throw "Le ACL del DataRoot ereditano ancora dal parent."
    }

    foreach ($ace in $acl.Access) {
        if ($ace.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow) {
            continue
        }
        $sid = $ace.IdentityReference.Translate(
            [Security.Principal.SecurityIdentifier]
        ).Value
        if ($allowed -notcontains $sid) {
            throw "ACL DataRoot troppo ampia: SID $sid"
        }
    }
}

$setupArgs = @(
    "/S",
    "/INSTALLROOT=$installRoot",
    "/DATAROOT=$dataRoot",
    "/OPERATORGROUP=$groupName",
    "/OPERATORUSER=$operatorUser",
    "/SKIPSHORTCUT=1"
)

try {
    New-Item -ItemType Directory -Force -Path $root | Out-Null

    Invoke-CheckedProcess -FilePath $SetupPath -Arguments $setupArgs -Description "Prima installazione"

    $exe = Join-Path $installRoot "VoucherManagement.exe"
    $uninstaller = Join-Path $installRoot "Uninstall.exe"
    $markerPath = Join-Path $installRoot "voucher-management-deployment.json"
    foreach ($required in @($exe, $uninstaller, $markerPath)) {
        if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
            throw "File installato mancante: $required"
        }
    }

    $marker = Get-Content -LiteralPath $markerPath -Raw | ConvertFrom-Json
    if ($marker.format -ne 1 -or $marker.mode -ne "shared_programdata") {
        throw "Marker installazione non valido."
    }

    Assert-SharedAcl -Path $dataRoot -GroupName $groupName

    if (-not (Test-Path -LiteralPath $uninstallKey)) {
        throw "Voucher Management non registrato in Installed apps."
    }
    $entry = Get-ItemProperty -LiteralPath $uninstallKey
    if ($entry.DisplayName -ne "Voucher Management" -or $entry.DisplayVersion -ne "5.1.0") {
        throw "Metadati Installed apps non validi."
    }
    if ([IO.Path]::GetFullPath($entry.InstallLocation) -ne [IO.Path]::GetFullPath($installRoot)) {
        throw "InstallLocation registrata non coerente."
    }

    $sentinel = Join-Path $dataRoot "upgrade-preserves-data.txt"
    Set-Content -LiteralPath $sentinel -Value "preserve" -Encoding ascii

    Invoke-CheckedProcess -FilePath $SetupPath -Arguments $setupArgs -Description "Aggiornamento in-place"

    if (-not (Test-Path -LiteralPath $sentinel -PathType Leaf)) {
        throw "L'aggiornamento tramite Setup.exe ha cancellato ProgramData."
    }
    Assert-SharedAcl -Path $dataRoot -GroupName $groupName

    $uninstaller = Join-Path $installRoot "Uninstall.exe"
    Invoke-CheckedProcess -FilePath $uninstaller -Arguments @("/S") -Description "Disinstallazione conservativa"
    Wait-PathRemoved -Path $installRoot

    if (-not (Test-Path -LiteralPath $dataRoot -PathType Container)) {
        throw "La disinstallazione predefinita ha cancellato i dati condivisi."
    }
    if (-not (Get-LocalGroup -Name $groupName -ErrorAction SilentlyContinue)) {
        throw "La disinstallazione predefinita ha rimosso il gruppo operatori."
    }
    if (Test-Path -LiteralPath $uninstallKey) {
        throw "La voce Installed apps è rimasta dopo la disinstallazione."
    }

    Invoke-CheckedProcess -FilePath $SetupPath -Arguments $setupArgs -Description "Reinstallazione per test purge"
    if (-not (Test-Path -LiteralPath $sentinel -PathType Leaf)) {
        throw "La reinstallazione non ha conservato i dati precedenti."
    }

    $uninstaller = Join-Path $installRoot "Uninstall.exe"
    Invoke-CheckedProcess -FilePath $uninstaller -Arguments @("/S", "/PURGEDATA=1") -Description "Disinstallazione con rimozione dati"
    Wait-PathRemoved -Path $installRoot

    if (Test-Path -LiteralPath $dataRoot) {
        throw "La rimozione esplicita non ha eliminato ProgramData."
    }
    if (Get-LocalGroup -Name $groupName -ErrorAction SilentlyContinue) {
        throw "La rimozione esplicita non ha eliminato il gruppo operatori."
    }

    Write-Host "Voucher Management Setup.exe integration test OK"
}
finally {
    Remove-Item -LiteralPath $uninstallKey -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $productKey -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $commonShortcut -Force -ErrorAction SilentlyContinue

    if (Get-LocalGroup -Name $groupName -ErrorAction SilentlyContinue) {
        try {
            & (Join-Path (Split-Path -Parent $PSScriptRoot) "tools\Uninstall-VoucherManagement.ps1") `
                -InstallRoot (Join-Path $root "nonexistent") `
                -DataRoot $dataRoot `
                -OperatorGroup $groupName `
                -RemoveData `
                -SkipShortcut `
                -KeepProgramFiles
        } catch {
            Remove-LocalGroup -Name $groupName -ErrorAction SilentlyContinue
        }
    }

    Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue
}
