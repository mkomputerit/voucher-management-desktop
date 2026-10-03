[CmdletBinding()]
param(
    [string]$SourcePath = $PSScriptRoot,
    [string]$InstallRoot = (Join-Path $env:ProgramFiles "Voucher Management"),
    [string]$DataRoot,
    [switch]$SkipShortcut
)

$ErrorActionPreference = "Stop"
$DefaultDataRoot = (Join-Path $env:ProgramData "VoucherManagement")
$UninstallRegistryPath = "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\VoucherManagement"
$BuiltinUsersSid = "S-1-5-32-545"

function Assert-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "L'installazione richiede privilegi di amministratore."
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

function Assert-PathsDoNotOverlap {
    param(
        [string]$First,
        [string]$Second,
        [string]$Message
    )
    $a = [IO.Path]::GetFullPath($First).TrimEnd("\")
    $b = [IO.Path]::GetFullPath($Second).TrimEnd("\")
    $aPrefix = $a + "\"
    $bPrefix = $b + "\"
    if (
        $a -ieq $b -or
        $aPrefix.StartsWith($bPrefix, [StringComparison]::OrdinalIgnoreCase) -or
        $bPrefix.StartsWith($aPrefix, [StringComparison]::OrdinalIgnoreCase)
    ) {
        throw $Message
    }
}

function Register-WindowsUninstallEntry {
    param([string]$InstallRoot)

    $uninstaller = Join-Path $InstallRoot "VoucherManagement-Uninstall.exe"
    $application = Join-Path $InstallRoot "VoucherManagement.exe"
    if (
        -not (Test-Path -LiteralPath $uninstaller -PathType Leaf) -or
        -not (Test-Path -LiteralPath $application -PathType Leaf)
    ) {
        return
    }

    $version = [string](Get-Item -LiteralPath $application).VersionInfo.ProductVersion
    $estimatedBytes = (
        Get-ChildItem -LiteralPath $InstallRoot -File -Recurse -Force |
            Measure-Object -Property Length -Sum
    ).Sum
    $estimatedKb = [Math]::Max(1, [Math]::Ceiling([double]$estimatedBytes / 1KB))

    New-Item -Path $script:UninstallRegistryPath -Force | Out-Null
    New-ItemProperty -Path $script:UninstallRegistryPath -Name "DisplayName" -Value "Voucher Management" -PropertyType String -Force | Out-Null
    New-ItemProperty -Path $script:UninstallRegistryPath -Name "DisplayVersion" -Value $version -PropertyType String -Force | Out-Null
    New-ItemProperty -Path $script:UninstallRegistryPath -Name "Publisher" -Value "Voucher Management contributors" -PropertyType String -Force | Out-Null
    New-ItemProperty -Path $script:UninstallRegistryPath -Name "InstallLocation" -Value $InstallRoot -PropertyType String -Force | Out-Null
    New-ItemProperty -Path $script:UninstallRegistryPath -Name "DisplayIcon" -Value "$application,0" -PropertyType String -Force | Out-Null
    New-ItemProperty -Path $script:UninstallRegistryPath -Name "UninstallString" -Value ('"' + $uninstaller + '"') -PropertyType String -Force | Out-Null
    New-ItemProperty -Path $script:UninstallRegistryPath -Name "QuietUninstallString" -Value ('"' + $uninstaller + '" /quiet') -PropertyType String -Force | Out-Null
    New-ItemProperty -Path $script:UninstallRegistryPath -Name "InstallDate" -Value (Get-Date -Format "yyyyMMdd") -PropertyType String -Force | Out-Null
    New-ItemProperty -Path $script:UninstallRegistryPath -Name "EstimatedSize" -Value ([int]$estimatedKb) -PropertyType DWord -Force | Out-Null
    New-ItemProperty -Path $script:UninstallRegistryPath -Name "NoModify" -Value 1 -PropertyType DWord -Force | Out-Null
    New-ItemProperty -Path $script:UninstallRegistryPath -Name "NoRepair" -Value 1 -PropertyType DWord -Force | Out-Null
}

function Set-SharedDataAcl {
    param([string]$Path)

    New-Item -ItemType Directory -Force -Path $Path | Out-Null
    $rootItem = Get-Item -LiteralPath $Path -Force
    if (
        ($rootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0
    ) {
        throw "La cartella dati condivisa è diventata un reparse point."
    }
    Assert-NoReparsePointsInTree -Path $Path -Label "La cartella dati condivisa"

    # Shared-machine deployment follows the normal Windows split:
    # Program Files is administrator-managed; ProgramData is application data
    # shared by local users. Reset stale explicit ACEs from previous versions
    # and grant Modify to the built-in Users group by SID so localization does
    # not affect the policy.
    & icacls.exe $Path /reset /T /C | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Reset ACL ProgramData non riuscito."
    }

    # Normalize descendants to normal Windows inheritance first. The shared
    # data root is the only protected ACL boundary; files and subdirectories
    # inherit the root policy instead of carrying duplicated protected ACLs.
    & icacls.exe $Path /inheritance:e /T /C | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Ripristino ereditarietà ACL ProgramData non riuscito."
    }

    $rules = @(
        "*S-1-5-18:(OI)(CI)F",
        "*S-1-5-32-544:(OI)(CI)F",
        "*$($script:BuiltinUsersSid):(OI)(CI)M"
    )
    foreach ($rule in $rules) {
        & icacls.exe $Path /grant:r $rule /C | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Configurazione ACL ProgramData non riuscita per: $rule"
        }
    }

    # Protect only the application-data root from ProgramData's parent ACL.
    # Descendants continue to inherit SYSTEM/Admins/Users from this root.
    & icacls.exe $Path /inheritance:r /C | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Protezione ACL radice ProgramData non riuscita."
    }

    $allowedSids = @(
        "S-1-5-18",
        "S-1-5-32-544",
        $script:BuiltinUsersSid
    )
    $requiredRights = @{
        "S-1-5-18" = [Security.AccessControl.FileSystemRights]::FullControl
        "S-1-5-32-544" = [Security.AccessControl.FileSystemRights]::FullControl
        "S-1-5-32-545" = [Security.AccessControl.FileSystemRights]::Modify
    }
    $items = @((Get-Item -LiteralPath $Path)) + @(
        Get-ChildItem -LiteralPath $Path -Force -Recurse
    )
    foreach ($item in $items) {
        $acl = Get-Acl -LiteralPath $item.FullName
        $isRoot = (
            [IO.Path]::GetFullPath($item.FullName).TrimEnd("\\") -ieq
            [IO.Path]::GetFullPath($Path).TrimEnd("\\")
        )
        if ($isRoot -and -not $acl.AreAccessRulesProtected) {
            throw "ACL radice ProgramData non protetta: $($item.FullName)"
        }
        if (-not $isRoot -and $acl.AreAccessRulesProtected) {
            throw "ACL discendente ProgramData non eredita dalla radice: $($item.FullName)"
        }

        $rightsBySid = @{}
        foreach ($ace in $acl.Access) {
            if (
                $ace.AccessControlType -ne
                [Security.AccessControl.AccessControlType]::Allow
            ) {
                continue
            }
            $sid = $ace.IdentityReference.Translate(
                [Security.Principal.SecurityIdentifier]
            ).Value
            if ($allowedSids -notcontains $sid) {
                throw "ACL ProgramData contiene un principal non autorizzato: $sid"
            }
            if (-not $rightsBySid.ContainsKey($sid)) {
                $rightsBySid[$sid] = [Security.AccessControl.FileSystemRights]0
            }
            $rightsBySid[$sid] = $rightsBySid[$sid] -bor $ace.FileSystemRights
        }

        foreach ($sid in $requiredRights.Keys) {
            if (-not $rightsBySid.ContainsKey($sid)) {
                throw "ACL ProgramData mancante per SID richiesto $($sid): $($item.FullName)"
            }
            $expected = $requiredRights[$sid]
            if (($rightsBySid[$sid] -band $expected) -ne $expected) {
                throw "ACL ProgramData insufficiente per SID $($sid): $($item.FullName)"
            }
        }
    }
}

Assert-Administrator

$source = [IO.Path]::GetFullPath($SourcePath)
$destination = Resolve-ManagedChildPath -Path $InstallRoot -RequiredParent $env:ProgramFiles -Label "La cartella di installazione"

# Preserve the installed ProgramData location across upgrades. Older format-1
# markers may still contain operator-group fields; they are intentionally
# ignored because current shared deployments use the Windows built-in Users
# group and require no application-specific account/group management.
$previousMarker = $null
$previousMarkerPath = Join-Path $destination "voucher-management-deployment.json"
if (Test-Path -LiteralPath $previousMarkerPath -PathType Leaf) {
    try {
        $previousMarker = Get-Content -LiteralPath $previousMarkerPath -Raw | ConvertFrom-Json
    }
    catch {
        throw "Marker di deployment esistente non leggibile: aggiornamento annullato."
    }
    if ($previousMarker.format -ne 1 -or $previousMarker.mode -ne "shared_programdata") {
        throw "Marker di deployment esistente non valido: aggiornamento annullato."
    }
}

if (-not $DataRoot) {
    $DataRoot = if ($previousMarker -and $previousMarker.data_root) {
        [string]$previousMarker.data_root
    } else {
        $script:DefaultDataRoot
    }
}

$dataDestination = Resolve-ManagedChildPath -Path $DataRoot -RequiredParent $env:ProgramData -Label "La cartella dati condivisa"
Assert-PathsDoNotOverlap -First $destination -Second $dataDestination -Message "Programma e dati condivisi non possono usare cartelle sovrapposte."
Assert-PathsDoNotOverlap -First $source -Second $dataDestination -Message "La sorgente di installazione e i dati condivisi non possono sovrapporsi."
$InstallRoot = $destination
$DataRoot = $dataDestination
if (-not (Test-Path -LiteralPath (Join-Path $source "VoucherManagement.exe") -PathType Leaf)) {
    throw "VoucherManagement.exe non trovato nella cartella sorgente: $source"
}
$sourceWithSeparator = $source.TrimEnd("\") + "\"
$destinationWithSeparator = $destination.TrimEnd("\") + "\"
if (
    $destination -ieq $source -or
    $destinationWithSeparator.StartsWith(
        $sourceWithSeparator,
        [StringComparison]::OrdinalIgnoreCase
    ) -or
    $sourceWithSeparator.StartsWith(
        $destinationWithSeparator,
        [StringComparison]::OrdinalIgnoreCase
    )
) {
    throw "La sorgente e la cartella di installazione non possono sovrapporsi."
}

$running = Get-Process -Name "VoucherManagement" -ErrorAction SilentlyContinue
if ($running) {
    throw "Chiudere Voucher Management prima di installare o aggiornare."
}

# Secure shared application data before publishing a deployment marker. If ACL
# preparation fails, the previous application installation remains untouched.
Set-SharedDataAcl -Path $DataRoot

$destinationParent = Split-Path -Parent $destination
New-Item -ItemType Directory -Force -Path $destinationParent | Out-Null
$staging = $destination + ".staging-" + [Guid]::NewGuid().ToString("N")
$previous = $destination + ".previous-" + [Guid]::NewGuid().ToString("N")
$previousMoved = $false

try {
    New-Item -ItemType Directory -Force -Path $staging | Out-Null
    Get-ChildItem -LiteralPath $source -Force | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination $staging -Recurse -Force
    }

    $marker = @{
        format = 1
        mode = "shared_programdata"
        data_root = $DataRoot
        access_model = "builtin_users_modify"
        access_sid = $script:BuiltinUsersSid
    } | ConvertTo-Json -Compress
    $utf8NoBom = [Text.UTF8Encoding]::new($false)
    [IO.File]::WriteAllText(
        (Join-Path $staging "voucher-management-deployment.json"),
        $marker,
        $utf8NoBom
    )

    if (Test-Path -LiteralPath $destination) {
        Move-Item -LiteralPath $destination -Destination $previous
        $previousMoved = $true
    }

    try {
        Move-Item -LiteralPath $staging -Destination $destination
    }
    catch {
        if (
            $previousMoved -and
            (Test-Path -LiteralPath $previous) -and
            -not (Test-Path -LiteralPath $destination)
        ) {
            Move-Item -LiteralPath $previous -Destination $destination
            $previousMoved = $false
        }
        throw
    }

    if ($previousMoved -and (Test-Path -LiteralPath $previous)) {
        Remove-Item -LiteralPath $previous -Recurse -Force
        $previousMoved = $false
    }
}
finally {
    if (Test-Path -LiteralPath $staging) {
        Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue
    }
    if (
        $previousMoved -and
        (Test-Path -LiteralPath $previous) -and
        -not (Test-Path -LiteralPath $destination)
    ) {
        Move-Item -LiteralPath $previous -Destination $destination -ErrorAction SilentlyContinue
    }
}

Register-WindowsUninstallEntry -InstallRoot $destination

if (-not $SkipShortcut) {
    $startMenu = Join-Path $env:ProgramData "Microsoft\Windows\Start Menu\Programs"
    $shortcutPath = Join-Path $startMenu "Voucher Management.lnk"
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = Join-Path $destination "VoucherManagement.exe"
    $shortcut.WorkingDirectory = $destination
    $shortcut.Description = "Voucher Management"
    $shortcut.Save()
}

Write-Host "Voucher Management installato in: $destination"
Write-Host "Dati condivisi: $DataRoot"
Write-Host "Accesso dati: utenti locali Windows (gruppo built-in Users)"
