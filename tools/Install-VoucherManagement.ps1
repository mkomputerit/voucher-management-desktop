[CmdletBinding()]
param(
    [string]$SourcePath = $PSScriptRoot,
    [string]$InstallRoot = (Join-Path $env:ProgramFiles "Voucher Management"),
    [string]$DataRoot = (Join-Path $env:ProgramData "VoucherManagement"),
    [string]$OperatorGroup = "Voucher Management Operators",
    [string]$OperatorUser,
    [switch]$SkipShortcut
)

$ErrorActionPreference = "Stop"

function Assert-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "L'installazione richiede privilegi di amministratore."
    }
}

function Resolve-InteractiveUser {
    param([string]$ExplicitUser)
    if ($ExplicitUser) { return $ExplicitUser }

    # Win32_ComputerSystem.UserName identifies the console-interactive user,
    # not an arbitrary RDP/Fast User Switching session. Multi-session installs
    # should pass -OperatorUser explicitly instead of relying on inference.
    $loggedOn = (Get-CimInstance Win32_ComputerSystem).UserName
    if (-not $loggedOn) {
        throw "Impossibile determinare l'utente Windows interattivo. Usare -OperatorUser."
    }
    return $loggedOn
}

function Ensure-OperatorGroup {
    param([string]$Name, [string]$Member)
    $group = Get-LocalGroup -Name $Name -ErrorAction SilentlyContinue
    if (-not $group) {
        $group = New-LocalGroup -Name $Name -Description "Operatori autorizzati a Voucher Management"
    }
    $present = Get-LocalGroupMember -Group $Name -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -ieq $Member }
    if (-not $present) {
        Add-LocalGroupMember -Group $Name -Member $Member
    }
    return $group
}

function Set-SharedDataAcl {
    param(
        [string]$Path,
        [Security.Principal.SecurityIdentifier]$OperatorGroupSid
    )
    New-Item -ItemType Directory -Force -Path $Path | Out-Null

    # /grant:r only replaces grants for principals explicitly named in the
    # command. Reset first so stale explicit ACEs from manual/older installs
    # cannot survive an upgrade, then rebuild the complete allowed set.
    & icacls.exe $Path /reset /T /C | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Reset ACL ProgramData non riuscito."
    }

    $rules = @(
        "*S-1-5-18:(OI)(CI)F",
        "*S-1-5-32-544:(OI)(CI)F",
        "*$($OperatorGroupSid.Value):(OI)(CI)M"
    )
    & icacls.exe $Path /inheritance:r /grant:r $rules /T /C | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Configurazione ACL ProgramData non riuscita."
    }

    $allowedSids = @(
        "S-1-5-18",
        "S-1-5-32-544",
        $OperatorGroupSid.Value
    )
    $items = @((Get-Item -LiteralPath $Path)) + @(
        Get-ChildItem -LiteralPath $Path -Force -Recurse
    )
    foreach ($item in $items) {
        $acl = Get-Acl -LiteralPath $item.FullName
        if (-not $acl.AreAccessRulesProtected) {
            throw "ACL ProgramData non protetta: $($item.FullName)"
        }
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
        }
    }
}

Assert-Administrator

$source = [IO.Path]::GetFullPath($SourcePath)
$destination = [IO.Path]::GetFullPath($InstallRoot)
if (-not (Test-Path -LiteralPath (Join-Path $source "VoucherManagement.exe") -PathType Leaf)) {
    throw "VoucherManagement.exe non trovato nella cartella sorgente: $source"
}
if ($destination.StartsWith($source, [StringComparison]::OrdinalIgnoreCase)) {
    throw "La cartella di installazione non può essere contenuta nella sorgente."
}

$running = Get-Process -Name "VoucherManagement" -ErrorAction SilentlyContinue
if ($running) {
    throw "Chiudere Voucher Management prima di installare o aggiornare."
}

$operator = Resolve-InteractiveUser -ExplicitUser $OperatorUser
$group = Ensure-OperatorGroup -Name $OperatorGroup -Member $operator

if (Test-Path -LiteralPath $destination) {
    Remove-Item -LiteralPath $destination -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $destination | Out-Null
Get-ChildItem -LiteralPath $source -Force | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $destination -Recurse -Force
}

$marker = @{ format = 1; mode = "shared_programdata" } | ConvertTo-Json -Compress
$utf8NoBom = [Text.UTF8Encoding]::new($false)
[IO.File]::WriteAllText(
    (Join-Path $destination "voucher-management-deployment.json"),
    $marker,
    $utf8NoBom
)

Set-SharedDataAcl -Path $DataRoot -OperatorGroupSid $group.SID

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
Write-Host "Gruppo operatori: $OperatorGroup"
Write-Host "Utente autorizzato: $operator"
Write-Host ""
Write-Host "Se l'utente è stato appena aggiunto al gruppo, disconnettersi e accedere nuovamente a Windows prima del primo avvio."
