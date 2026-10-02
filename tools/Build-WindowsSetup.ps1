[CmdletBinding()]
param(
    [string]$SourcePath = (Join-Path (Split-Path -Parent $PSScriptRoot) "dist\VoucherManagement"),
    [string]$OutputDirectory = (Join-Path (Split-Path -Parent $PSScriptRoot) "dist\installer"),
    [string]$VersionFile = (Join-Path (Split-Path -Parent $PSScriptRoot) "version.txt")
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$source = [IO.Path]::GetFullPath($SourcePath)
$outputRoot = [IO.Path]::GetFullPath($OutputDirectory)
$versionPath = [IO.Path]::GetFullPath($VersionFile)

if (-not (Test-Path -LiteralPath (Join-Path $source "VoucherManagement.exe") -PathType Leaf)) {
    throw "Payload portable non trovato: $source"
}
if (-not (Test-Path -LiteralPath (Join-Path $source "Install-VoucherManagement.ps1") -PathType Leaf)) {
    throw "Install-VoucherManagement.ps1 non presente nel payload portable."
}
if (-not (Test-Path -LiteralPath $versionPath -PathType Leaf)) {
    throw "File versione non trovato: $versionPath"
}

$version = (Get-Content -LiteralPath $versionPath -Raw).Trim()
if ($version -notmatch '^\d+\.\d+\.\d+$') {
    throw "Versione non valida per il Setup: $version"
}
$version4 = "$version.0"

$cscCandidates = @(
    (Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\csc.exe"),
    (Join-Path $env:WINDIR "Microsoft.NET\Framework\v4.0.30319\csc.exe")
)
$csc = $cscCandidates |
    Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } |
    Select-Object -First 1
if (-not $csc) {
    throw "Compilatore C# .NET Framework non trovato."
}

$frameworkDirectory = Split-Path -Parent $csc
$compression = Join-Path $frameworkDirectory "System.IO.Compression.dll"
$compressionFs = Join-Path $frameworkDirectory "System.IO.Compression.FileSystem.dll"
foreach ($reference in @($compression, $compressionFs)) {
    if (-not (Test-Path -LiteralPath $reference -PathType Leaf)) {
        throw "Assembly .NET Framework non trovato: $reference"
    }
}

$bootstrapSource = Join-Path $PSScriptRoot "SetupBootstrapper.cs"
$manifest = Join-Path $PSScriptRoot "SetupBootstrapper.manifest"
$icon = Join-Path $source "_internal\assets\VoucherManagement.ico"
foreach ($required in @($bootstrapSource, $manifest, $icon)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "File Setup richiesto non trovato: $required"
    }
}

New-Item -ItemType Directory -Force -Path $outputRoot | Out-Null
$output = Join-Path $outputRoot "VoucherManagement-Setup-$version.exe"
$tempBase = $env:RUNNER_TEMP
if (-not $tempBase) {
    $tempBase = [IO.Path]::GetTempPath()
}
$tempRoot = Join-Path $tempBase ("voucher-management-setup-build-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $tempRoot | Out-Null

try {
    $payloadZip = Join-Path $tempRoot "VoucherManagement-Payload.zip"
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [IO.Compression.ZipFile]::CreateFromDirectory(
        $source,
        $payloadZip,
        [IO.Compression.CompressionLevel]::Optimal,
        $false
    )

    $assemblyInfo = Join-Path $tempRoot "SetupAssemblyInfo.cs"
    @"
using System.Reflection;

[assembly: AssemblyTitle("Voucher Management Setup")]
[assembly: AssemblyDescription("Installer for Voucher Management")]
[assembly: AssemblyCompany("Voucher Management contributors")]
[assembly: AssemblyProduct("Voucher Management")]
[assembly: AssemblyCopyright("Copyright (c) 2026 Voucher Management contributors")]
[assembly: AssemblyVersion("$version4")]
[assembly: AssemblyFileVersion("$version4")]
[assembly: AssemblyInformationalVersion("$version")]
"@ | Set-Content -LiteralPath $assemblyInfo -Encoding UTF8

    $compilerArgs = @(
        "/nologo",
        "/target:winexe",
        "/optimize+",
        "/platform:x64",
        "/win32icon:$icon",
        "/win32manifest:$manifest",
        "/out:$output",
        "/resource:$payloadZip,VoucherManagement.Payload.zip",
        "/reference:$compression",
        "/reference:$compressionFs",
        $bootstrapSource,
        $assemblyInfo
    )
    & $csc @compilerArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Compilazione Voucher Management Setup non riuscita."
    }
    if (-not (Test-Path -LiteralPath $output -PathType Leaf)) {
        throw "Il compilatore non ha prodotto il Setup atteso."
    }

    $info = (Get-Item -LiteralPath $output).VersionInfo
    if ($info.ProductName -ne "Voucher Management") {
        throw "ProductName del Setup non valido: $($info.ProductName)"
    }
    if ($info.ProductVersion -ne $version) {
        throw "ProductVersion del Setup non valido: $($info.ProductVersion)"
    }
    if ($info.FileVersion -ne $version4) {
        throw "FileVersion del Setup non valido: $($info.FileVersion)"
    }

    $hash = (Get-FileHash -LiteralPath $output -Algorithm SHA256).Hash.ToLower()
    "$hash  $(Split-Path -Leaf $output)" |
        Out-File -LiteralPath (Join-Path $outputRoot "SHA256SUMS.txt") -Encoding ascii

    Write-Host "Setup creato: $output"
    Write-Host "SHA-256: $hash"
}
finally {
    Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
}
