[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$InstallRoot = (Join-Path $env:ProgramFiles "Voucher Management"),
    [string]$DataRoot = (Join-Path $env:ProgramData "VoucherManagement"),
    [string]$OperatorGroup = "Voucher Management Operators",
    [switch]$RemoveData
)

$ErrorActionPreference = "Stop"

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "La disinstallazione richiede privilegi di amministratore."
}

$shortcutPath = Join-Path $env:ProgramData "Microsoft\Windows\Start Menu\Programs\Voucher Management.lnk"
if (Test-Path -LiteralPath $shortcutPath) {
    Remove-Item -LiteralPath $shortcutPath -Force
}
if (Test-Path -LiteralPath $InstallRoot) {
    Remove-Item -LiteralPath $InstallRoot -Recurse -Force
}

if ($RemoveData) {
    if (Test-Path -LiteralPath $DataRoot) {
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

Write-Host "Voucher Management disinstallato."
