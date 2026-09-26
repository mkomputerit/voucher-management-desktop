[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$InstallRoot = (Join-Path $env:ProgramFiles "Voucher Management"),
    [string]$DataRoot = (Join-Path $env:ProgramData "VoucherManagement"),
    [string]$OperatorGroup = "Voucher Management Operators",
    [switch]$RemoveData,
    [switch]$SkipShortcut
)

$ErrorActionPreference = "Stop"

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "La disinstallazione richiede privilegi di amministratore."
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
    $group = Get-LocalGroup -Name $OperatorGroup -ErrorAction SilentlyContinue
    if ($group) {
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
