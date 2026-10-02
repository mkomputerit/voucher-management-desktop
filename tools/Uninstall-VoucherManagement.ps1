[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$InstallRoot = (Join-Path $env:ProgramFiles "Voucher Management"),
    [string]$DataRoot,
    [string]$OperatorGroup,
    [switch]$RemoveData,
    [switch]$SkipShortcut
)

$ErrorActionPreference = "Stop"
$OperatorGroupDescription = "Operatori autorizzati a Voucher Management"
$DefaultOperatorGroup = "Voucher Management Operators"
$DefaultDataRoot = (Join-Path $env:ProgramData "VoucherManagement")

function Resolve-ManagedChildPath {
    param(
        [string]$Path,
        [string]$RequiredParent,
        [string]$Label
    )
    $full = [IO.Path]::GetFullPath($Path).TrimEnd("\")
    $parent = [IO.Path]::GetFullPath($RequiredParent).TrimEnd("\")
    $prefix = $parent + "\"
    if (
        $full -ieq $parent -or
        -not $full.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)
    ) {
        throw "$Label deve essere una sottocartella di: $parent"
    }
    $directParent = [IO.Path]::GetDirectoryName($full).TrimEnd("\")
    if ($directParent -ine $parent) {
        throw "$Label deve essere una sottocartella diretta di: $parent"
    }
    if (Test-Path -LiteralPath $full) {
        $item = Get-Item -LiteralPath $full -Force
        if (
            ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0
        ) {
            throw "$Label non può essere un junction, link o altro reparse point."
        }
    }
    return $full
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "La disinstallazione richiede privilegi di amministratore."
}

$InstallRoot = Resolve-ManagedChildPath -Path $InstallRoot -RequiredParent $env:ProgramFiles -Label "La cartella di installazione"

$dataRootExplicit = $PSBoundParameters.ContainsKey("DataRoot") -and [bool]$DataRoot
$operatorGroupExplicit = $PSBoundParameters.ContainsKey("OperatorGroup") -and [bool]$OperatorGroup
$marker = $null
$markerPath = Join-Path $InstallRoot "voucher-management-deployment.json"
if (Test-Path -LiteralPath $markerPath -PathType Leaf) {
    try {
        $marker = Get-Content -LiteralPath $markerPath -Raw | ConvertFrom-Json
    }
    catch {
        if ($RemoveData) {
            throw "Marker di deployment non leggibile: rimozione dati annullata."
        }
    }
    if ($marker -and ($marker.format -ne 1 -or $marker.mode -ne "shared_programdata")) {
        if ($RemoveData) {
            throw "Marker di deployment non valido: rimozione dati annullata."
        }
        $marker = $null
    }
}

if (-not $dataRootExplicit) {
    $DataRoot = if ($marker -and $marker.data_root) {
        [string]$marker.data_root
    } else {
        $script:DefaultDataRoot
    }
}
if (-not $operatorGroupExplicit) {
    $OperatorGroup = if ($marker -and $marker.operator_group_name) {
        [string]$marker.operator_group_name
    } else {
        $script:DefaultOperatorGroup
    }
}
$DataRoot = Resolve-ManagedChildPath -Path $DataRoot -RequiredParent $env:ProgramData -Label "La cartella dati condivisa"

$managedGroup = $null
if ($RemoveData) {
    # Bind destructive cleanup to the installer-owned marker when the current
    # deployment provides one. Older format-1 markers without these optional
    # fields remain compatible and fall back to the dedicated group checks.
    $expectedGroupSid = ""
    if ($marker) {
        if ($marker.data_root) {
            $markerDataRoot = Resolve-ManagedChildPath -Path ([string]$marker.data_root) -RequiredParent $env:ProgramData -Label "Il DataRoot registrato"
            if ($markerDataRoot -ine $DataRoot) {
                throw "Il DataRoot richiesto non corrisponde al deployment installato."
            }
        }
        if ($marker.operator_group_sid) {
            $expectedGroupSid = ([string]$marker.operator_group_sid).Trim()
        }
        if (
            $marker.operator_group_name -and
            [string]$marker.operator_group_name -ine [string]$OperatorGroup
        ) {
            throw "Il gruppo operatori richiesto non corrisponde al deployment installato."
        }
    }

    $managedGroup = Get-LocalGroup -Name $OperatorGroup -ErrorAction SilentlyContinue
    if ($managedGroup) {
        $sid = [string]$managedGroup.SID.Value
        if ($sid.StartsWith("S-1-5-32-", [StringComparison]::OrdinalIgnoreCase)) {
            throw "Il gruppo operatori non può essere un gruppo Windows built-in."
        }
        if ([string]$managedGroup.Description -ne $script:OperatorGroupDescription) {
            throw "Il gruppo indicato non risulta gestito da Voucher Management."
        }
        if ($expectedGroupSid -and $sid -ine $expectedGroupSid) {
            throw "Il SID del gruppo operatori non corrisponde al deployment installato."
        }
    }
}

if (-not $SkipShortcut) {
    $shortcutPath = Join-Path $env:ProgramData "Microsoft\Windows\Start Menu\Programs\Voucher Management.lnk"
    if (Test-Path -LiteralPath $shortcutPath) {
        Remove-Item -LiteralPath $shortcutPath -Force
    }
}
$running = Get-Process -Name "VoucherManagement" -ErrorAction SilentlyContinue
if ($running) {
    throw "Chiudere Voucher Management prima della disinstallazione."
}

$installFull = [IO.Path]::GetFullPath($InstallRoot).TrimEnd("\")
$scriptFull = [IO.Path]::GetFullPath($PSCommandPath)
$selfInsideInstall = $scriptFull.StartsWith(
    $installFull + "\",
    [StringComparison]::OrdinalIgnoreCase
)

if (-not $selfInsideInstall -and (Test-Path -LiteralPath $InstallRoot)) {
    Remove-Item -LiteralPath $InstallRoot -Recurse -Force
}

if ($RemoveData) {
    if (Test-Path -LiteralPath $DataRoot) {
        # Re-check the destructive boundary immediately before icacls so a
        # replaced junction/reparse point is rejected instead of traversed.
        $DataRoot = Resolve-ManagedChildPath -Path $DataRoot -RequiredParent $env:ProgramData -Label "La cartella dati condivisa"
        # Shared mode deliberately protects every descendant with explicit,
        # non-inherited ACLs. Before destructive removal, restore an
        # administrator-deletable tree; otherwise Remove-Item can fail on
        # descendants even from an elevated uninstall process.
        & icacls.exe $DataRoot /reset /T /C | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Ripristino ACL prima della rimozione dati non riuscito."
        }
        & icacls.exe $DataRoot /grant:r `
            "*S-1-5-18:(OI)(CI)F" `
            "*S-1-5-32-544:(OI)(CI)F" `
            /T /C | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Preparazione ACL per la rimozione dati non riuscita."
        }
        Remove-Item -LiteralPath $DataRoot -Recurse -Force
    }
    if ($managedGroup) {
        Remove-LocalGroup -Name $OperatorGroup
    }
    Write-Host "Dati condivisi rimossi."
} else {
    Write-Host "Dati condivisi conservati in: $DataRoot"
}

if ($selfInsideInstall -and (Test-Path -LiteralPath $InstallRoot)) {
    $escaped = $InstallRoot.Replace('"', '""')
    $command = "timeout /t 2 /nobreak >nul & rmdir /s /q ""$escaped"""
    Start-Process -FilePath $env:ComSpec -ArgumentList "/d", "/c", $command -WindowStyle Hidden
}

Write-Host "Voucher Management disinstallato."
