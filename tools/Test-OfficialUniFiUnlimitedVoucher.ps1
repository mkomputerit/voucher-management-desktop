[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ApiRootUrl,

    [Parameter(Mandatory = $true)]
    [ValidateSet("Prepare", "Inspect", "Cleanup")]
    [string]$Mode,

    [string]$VoucherId,

    [string]$SiteId,

    [switch]$SkipCertificateCheck,

    [string]$OutputJson = ".\unifi-unlimited-field-test.json"
)

# Controlled operational test for the documented UniFi Network voucher API.
#
# Purpose:
# - verify the real behavior of a voucher created WITHOUT authorizedGuestLimit;
# - keep the voucher alive between Prepare and Inspect so two real clients can
#   authenticate with it;
# - never print or write the secret voucher code to the JSON report.
#
# Safety:
# - PowerShell 7+ only;
# - API key is prompted as SecureString and exists in plaintext only in memory;
# - TLS validation is enabled unless -SkipCertificateCheck is explicitly used;
# - Prepare creates exactly one 15-minute voucher;
# - Cleanup deletes only the exact voucher UUID supplied by the operator.

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($PSVersionTable.PSVersion.Major -lt 7) {
    throw "PowerShell 7 o successivo e richiesto per questo test."
}

$ApiRootUrl = $ApiRootUrl.Trim().TrimEnd("/")
if (-not $ApiRootUrl.StartsWith("https://", [StringComparison]::OrdinalIgnoreCase)) {
    throw "ApiRootUrl deve usare HTTPS."
}
if (-not $ApiRootUrl.EndsWith("/v1", [StringComparison]::OrdinalIgnoreCase)) {
    throw "ApiRootUrl deve essere la radice ufficiale UniFi Network API che termina in /v1."
}

if ($Mode -in @("Inspect", "Cleanup")) {
    $parsedGuid = [Guid]::Empty
    if (-not [Guid]::TryParse([string]$VoucherId, [ref]$parsedGuid)) {
        throw "-VoucherId deve essere l'UUID restituito dalla fase Prepare."
    }
}

$secureApiKey = Read-Host "UniFi API key" -AsSecureString
$keyPtr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureApiKey)
$plainApiKey = $null

function Invoke-Api {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet("GET", "POST", "DELETE")]
        [string]$Method,
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [object]$Body = $null
    )

    $headers = @{
        "Accept"    = "application/json"
        "X-API-Key" = $plainApiKey
    }

    $parameters = @{
        Uri                = "$ApiRootUrl$Path"
        Method             = $Method
        Headers            = $headers
        TimeoutSec         = 20
        SkipHttpErrorCheck = $true
        ErrorAction        = "Stop"
    }

    if ($SkipCertificateCheck) {
        $parameters["SkipCertificateCheck"] = $true
    }

    if ($null -ne $Body) {
        $parameters["ContentType"] = "application/json"
        $parameters["Body"] = $Body | ConvertTo-Json -Depth 10 -Compress
    }

    $response = Invoke-WebRequest @parameters
    $json = $null
    if (-not [string]::IsNullOrWhiteSpace([string]$response.Content)) {
        try {
            $json = $response.Content | ConvertFrom-Json
        }
        catch {
            # Raw content is intentionally not echoed because voucher responses
            # can contain the activation code.
            $json = $null
        }
    }

    [PSCustomObject]@{
        StatusCode = [int]$response.StatusCode
        Json       = $json
    }
}

function Require-Status {
    param(
        [Parameter(Mandatory = $true)]$Response,
        [Parameter(Mandatory = $true)][int[]]$Expected,
        [Parameter(Mandatory = $true)][string]$Context
    )

    if ($Expected -notcontains $Response.StatusCode) {
        throw "${Context}: HTTP $($Response.StatusCode)."
    }
}

function Get-Sites {
    $response = Invoke-Api -Method GET -Path "/sites?offset=0&limit=200"
    Require-Status -Response $response -Expected @(200) -Context "List sites"
    if ($null -eq $response.Json -or $null -eq $response.Json.data) {
        throw "List sites: risposta JSON non valida."
    }
    @($response.Json.data)
}

function Resolve-Site {
    param([object[]]$Sites)

    if ($SiteId) {
        $selected = @($Sites | Where-Object { [string]$_.id -eq $SiteId })
        if ($selected.Count -ne 1) {
            throw "SiteId non trovato."
        }
        return $selected[0]
    }

    if ($Sites.Count -eq 1) {
        return $Sites[0]
    }

    throw "Sono presenti piu siti. Rieseguire specificando -SiteId."
}

function Get-SafeVoucher {
    param([Parameter(Mandatory = $true)]$Voucher)

    foreach ($name in @("id", "name", "authorizedGuestCount", "expired", "timeLimitMinutes", "code")) {
        if ($Voucher.PSObject.Properties.Name -notcontains $name) {
            throw "Voucher: proprieta documentata mancante: $name"
        }
    }

    $id = [Guid]::Empty
    if (-not [Guid]::TryParse([string]$Voucher.id, [ref]$id)) {
        throw "Voucher: UUID non valido."
    }

    [PSCustomObject]@{
        id                   = [string]$Voucher.id
        name                 = [string]$Voucher.name
        authorizedGuestCount = [int64]$Voucher.authorizedGuestCount
        authorizedGuestLimit = if ($Voucher.PSObject.Properties.Name -contains "authorizedGuestLimit") { $Voucher.authorizedGuestLimit } else { $null }
        activatedAt          = if ($Voucher.PSObject.Properties.Name -contains "activatedAt") { $Voucher.activatedAt } else { $null }
        expiresAt            = if ($Voucher.PSObject.Properties.Name -contains "expiresAt") { $Voucher.expiresAt } else { $null }
        expired              = [bool]$Voucher.expired
        timeLimitMinutes     = [int64]$Voucher.timeLimitMinutes
        codePresent          = -not [string]::IsNullOrWhiteSpace([string]$Voucher.code)
    }
}

try {
    $plainApiKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($keyPtr)
    if ([string]::IsNullOrWhiteSpace($plainApiKey)) {
        throw "API key vuota."
    }

    if ($SkipCertificateCheck) {
        Write-Host "ATTENZIONE: verifica certificato TLS disabilitata solo per questo test." -ForegroundColor Yellow
    }

    $sites = @(Get-Sites)
    $site = Resolve-Site -Sites $sites
    $resolvedSiteId = [string]$site.id
    $encodedSite = [Uri]::EscapeDataString($resolvedSiteId)

    $report = [ordered]@{
        schemaVersion   = 1
        mode            = $Mode
        startedUtc      = (Get-Date).ToUniversalTime().ToString("o")
        siteId          = $resolvedSiteId
        tlsVerification = if ($SkipCertificateCheck) { "disabled-explicitly-for-test" } else { "enabled" }
        voucher         = $null
        success         = $false
    }

    switch ($Mode) {
        "Prepare" {
            $stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMdd-HHmmss")
            $payload = @{
                count            = 1
                name             = "VoucherManagement-Unlimited-FieldTest-$stamp"
                timeLimitMinutes = 15
            }

            Write-Host "Creo 1 voucher di test da 15 minuti SENZA authorizedGuestLimit..." -ForegroundColor Cyan
            $response = Invoke-Api -Method POST -Path "/sites/$encodedSite/hotspot/vouchers" -Body $payload
            Require-Status -Response $response -Expected @(201) -Context "Create field-test voucher"

            $created = @($response.Json.vouchers)
            if ($created.Count -ne 1) {
                throw "Create field-test voucher: atteso esattamente un voucher."
            }

            $safe = Get-SafeVoucher -Voucher $created[0]
            if ($null -ne $safe.authorizedGuestLimit) {
                throw "Il controller ha restituito un authorizedGuestLimit non richiesto."
            }
            if (-not $safe.codePresent) {
                throw "Il controller non ha restituito un codice voucher valido."
            }

            $report.voucher = $safe
            $report.success = $true

            Write-Host ""
            Write-Host "VOUCHER DI TEST CREATO." -ForegroundColor Green
            Write-Host "Nome: $($safe.name)"
            Write-Host "UUID: $($safe.id)"
            Write-Host "authorizedGuestLimit: assente"
            Write-Host ""
            Write-Host "Il CODICE non viene mostrato da questo script." -ForegroundColor Yellow
            Write-Host "Apri UniFi > Hotspot/Voucher, cerca il nome sopra e usa il codice mostrato da UniFi." -ForegroundColor Yellow
            Write-Host "NON eliminare ancora il voucher."
        }

        "Inspect" {
            $encodedVoucher = [Uri]::EscapeDataString($VoucherId)
            $response = Invoke-Api -Method GET -Path "/sites/$encodedSite/hotspot/vouchers/$encodedVoucher"
            Require-Status -Response $response -Expected @(200) -Context "Inspect field-test voucher"
            $safe = Get-SafeVoucher -Voucher $response.Json

            $report.voucher = $safe
            $report.success = $true

            Write-Host "Voucher di test letto." -ForegroundColor Green
            Write-Host "Nome: $($safe.name)"
            Write-Host "authorizedGuestCount: $($safe.authorizedGuestCount)"
            Write-Host "authorizedGuestLimit: $(if ($null -eq $safe.authorizedGuestLimit) { 'assente' } else { $safe.authorizedGuestLimit })"
            Write-Host "activatedAt: $($safe.activatedAt)"
            Write-Host "expiresAt: $($safe.expiresAt)"
            Write-Host "expired: $($safe.expired)"
        }

        "Cleanup" {
            $encodedVoucher = [Uri]::EscapeDataString($VoucherId)
            $response = Invoke-Api -Method DELETE -Path "/sites/$encodedSite/hotspot/vouchers/$encodedVoucher"
            Require-Status -Response $response -Expected @(200) -Context "Delete field-test voucher"

            if ($null -eq $response.Json -or $response.Json.PSObject.Properties.Name -notcontains "vouchersDeleted") {
                throw "Delete field-test voucher: risposta inattesa."
            }
            if ([int64]$response.Json.vouchersDeleted -lt 1) {
                throw "Delete field-test voucher: il controller non conferma la rimozione."
            }

            $report.voucher = [ordered]@{ id = $VoucherId }
            $report.success = $true
            Write-Host "Voucher di test eliminato." -ForegroundColor Green
        }
    }

    $outputPath = [IO.Path]::GetFullPath($OutputJson)
    $report | ConvertTo-Json -Depth 8 | Set-Content -Encoding utf8 $outputPath
    Write-Host "Report sanitizzato: $outputPath"
}
finally {
    $plainApiKey = $null
    if ($keyPtr -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($keyPtr)
    }
}
