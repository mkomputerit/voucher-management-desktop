[CmdletBinding()]
param(
    [string]$SourcePath = $PSScriptRoot,
    [string]$InstallRoot = (Join-Path $env:ProgramFiles "Voucher Management"),
    [string]$DataRoot,
    [string]$OperatorGroup,
    [string]$OperatorUser,
    [switch]$SkipShortcut
)

$ErrorActionPreference = "Stop"
$OperatorGroupDescription = "Operatori autorizzati a Voucher Management"
$DefaultOperatorGroup = "Voucher Management Operators"
$DefaultDataRoot = (Join-Path $env:ProgramData "VoucherManagement")

function Assert-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "L'installazione richiede privilegi di amministratore."
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

function Assert-OperatorGroupSafe {
    param([object]$Group)

    $sid = [string]$Group.SID.Value
    if ($sid.StartsWith("S-1-5-32-", [StringComparison]::OrdinalIgnoreCase)) {
        throw "Il gruppo operatori non può essere un gruppo Windows built-in."
    }

    $nested = @(
        Get-LocalGroupMember -Group $Group.Name -ErrorAction Stop |
            Where-Object { $_.ObjectClass -ne "User" }
    )
    if ($nested.Count -gt 0) {
        throw "Il gruppo operatori può contenere solo account utente diretti, non gruppi annidati."
    }
}

function Ensure-OperatorGroup {
    param([string]$Name, [string]$Member)

    if (-not $Name.Trim()) {
        throw "Il nome del gruppo operatori non può essere vuoto."
    }
    if (-not $Member.Trim()) {
        throw "L'account operatore non può essere vuoto."
    }

    $group = Get-LocalGroup -Name $Name -ErrorAction SilentlyContinue
    $created = $false
    if (-not $group) {
        $group = New-LocalGroup -Name $Name -Description $script:OperatorGroupDescription
        $created = $true
    }
    elseif ([string]$group.Description -ne $script:OperatorGroupDescription) {
        throw "Esiste già un gruppo con questo nome ma non appartiene a Voucher Management."
    }

    try {
        Assert-OperatorGroupSafe -Group $group
    }
    catch {
        if ($created) {
            Remove-LocalGroup -Name $Name -ErrorAction SilentlyContinue
        }
        throw
    }

    $present = Get-LocalGroupMember -Group $Name -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -ieq $Member }
    $added = $false

    try {
        if (-not $present) {
            # Membership changes are part of the same logical transaction as
            # group creation. Windows can reject some principal types before a
            # post-add validation is reached; that failure must still roll back
            # a group created by this installer attempt.
            Add-LocalGroupMember -Group $Name -Member $Member -ErrorAction Stop
            $added = $true
        }

        # Validate again so passing a group as -OperatorUser cannot silently
        # introduce nested membership and broaden access to ProgramData.
        Assert-OperatorGroupSafe -Group $group
    }
    catch {
        if ($added) {
            Remove-LocalGroupMember -Group $Name -Member $Member -ErrorAction SilentlyContinue
        }
        if ($created) {
            Remove-LocalGroup -Name $Name -ErrorAction SilentlyContinue
        }
        throw
    }

    return $group
}

function Set-SharedDataAcl {
    param(
        [string]$Path,
        [Security.Principal.SecurityIdentifier]$OperatorGroupSid
    )
    New-Item -ItemType Directory -Force -Path $Path | Out-Null
    $rootItem = Get-Item -LiteralPath $Path -Force
    if (
        ($rootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0
    ) {
        throw "La cartella dati condivisa è diventata un reparse point."
    }

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
$destination = Resolve-ManagedChildPath -Path $InstallRoot -RequiredParent $env:ProgramFiles -Label "La cartella di installazione"

# Preserve installer-owned deployment choices across upgrades when the caller
# does not explicitly replace them. This prevents a later Setup.exe from
# silently switching a custom ProgramData root or operator group back to the
# defaults and making existing data appear to have disappeared.
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
if (-not $OperatorGroup) {
    $OperatorGroup = if ($previousMarker -and $previousMarker.operator_group_name) {
        [string]$previousMarker.operator_group_name
    } else {
        $script:DefaultOperatorGroup
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

$operator = Resolve-InteractiveUser -ExplicitUser $OperatorUser
$group = Ensure-OperatorGroup -Name $OperatorGroup -Member $operator
if ($previousMarker -and $previousMarker.operator_group_sid) {
    $expectedPreviousSid = ([string]$previousMarker.operator_group_sid).Trim()
    if ($expectedPreviousSid -and [string]$group.SID.Value -ine $expectedPreviousSid) {
        throw "Il SID del gruppo operatori non corrisponde al deployment esistente."
    }
}

# Secure the shared data tree before making any installed executable advertise
# shared mode. If ACL preparation fails, the previous application installation
# remains untouched and no shared-deployment marker is written.
Set-SharedDataAcl -Path $DataRoot -OperatorGroupSid $group.SID

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
        operator_group_sid = [string]$group.SID.Value
        operator_group_name = [string]$group.Name
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
