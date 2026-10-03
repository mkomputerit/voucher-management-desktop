[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$InstallRoot = (Join-Path $env:ProgramFiles "Voucher Management"),
    [string]$DataRoot,
    [switch]$RemoveData,
    [switch]$SkipShortcut
)

$ErrorActionPreference = "Stop"
$DefaultDataRoot = (Join-Path $env:ProgramData "VoucherManagement")
$UninstallRegistryPath = "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\VoucherManagement"

function Remove-WindowsUninstallEntry {
    param([string]$InstallRoot)

    if (-not (Test-Path -LiteralPath $script:UninstallRegistryPath)) {
        return
    }
    try {
        $entry = Get-ItemProperty -LiteralPath $script:UninstallRegistryPath -ErrorAction Stop
        $registered = [string]$entry.InstallLocation
        if (
            $registered -and
            [IO.Path]::GetFullPath($registered).TrimEnd("\") -ieq
            [IO.Path]::GetFullPath($InstallRoot).TrimEnd("\")
        ) {
            Remove-Item -LiteralPath $script:UninstallRegistryPath -Recurse -Force
        }
    }
    catch {
        throw "Impossibile rimuovere la registrazione Windows della disinstallazione."
    }
}

function Assert-NoReparsePointsInTree {
    param(
        [string]$Path,
        [string]$Label
    )
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        return
    }

    $pending = New-Object System.Collections.Stack
    $pending.Push([IO.Path]::GetFullPath($Path))
    while ($pending.Count -gt 0) {
        $current = $pending.Pop()
        foreach ($item in Get-ChildItem -LiteralPath $current -Force) {
            if (
                ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0
            ) {
                throw "$Label contiene un junction, link o altro reparse point: $($item.FullName)"
            }
            if ($item.PSIsContainer) {
                $pending.Push($item.FullName)
            }
        }
    }
}

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
$DataRoot = Resolve-ManagedChildPath -Path $DataRoot -RequiredParent $env:ProgramData -Label "La cartella dati condivisa"

if ($RemoveData -and $marker -and $marker.data_root) {
    $markerDataRoot = Resolve-ManagedChildPath -Path ([string]$marker.data_root) -RequiredParent $env:ProgramData -Label "Il DataRoot registrato"
    if ($markerDataRoot -ine $DataRoot) {
        throw "Il DataRoot richiesto non corrisponde al deployment installato."
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

# Complete optional destructive data cleanup before removing the installed
# program. If data cleanup fails, the application remains installed and the
# operator can retry instead of ending in a half-uninstalled state.
if ($RemoveData) {
    if (Test-Path -LiteralPath $DataRoot) {
        $DataRoot = Resolve-ManagedChildPath -Path $DataRoot -RequiredParent $env:ProgramData -Label "La cartella dati condivisa"
        Assert-NoReparsePointsInTree -Path $DataRoot -Label "La cartella dati condivisa"

        # Shared mode uses explicit protected ACLs. Restore an
        # administrator-deletable tree before recursive removal.
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
    Write-Host "Dati condivisi rimossi."
} else {
    Write-Host "Dati condivisi conservati in: $DataRoot"
}

if (-not $SkipShortcut) {
    $shortcutPath = Join-Path $env:ProgramData "Microsoft\Windows\Start Menu\Programs\Voucher Management.lnk"
    if (Test-Path -LiteralPath $shortcutPath) {
        Remove-Item -LiteralPath $shortcutPath -Force
    }
}

Remove-WindowsUninstallEntry -InstallRoot $InstallRoot

if (-not $selfInsideInstall -and (Test-Path -LiteralPath $InstallRoot)) {
    Remove-Item -LiteralPath $InstallRoot -Recurse -Force
}
elseif ($selfInsideInstall -and (Test-Path -LiteralPath $InstallRoot)) {
    $escaped = $InstallRoot.Replace('"', '""')
    $command = "timeout /t 2 /nobreak >nul & rmdir /s /q ""$escaped"""
    Start-Process -FilePath $env:ComSpec -ArgumentList "/d", "/c", $command -WindowStyle Hidden
}

Write-Host "Voucher Management disinstallato."
