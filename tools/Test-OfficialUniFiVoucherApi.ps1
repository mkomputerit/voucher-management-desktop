[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ApiRootUrl,

    [string]$SiteId,

    [switch]$SkipCertificateCheck,

    [switch]$AllowWriteTests,

    [switch]$FullWriteTests,

    [string]$OutputJson
)

# Official UniFi Network voucher API diagnostic.
#
# Safety properties:
# - PowerShell 7+ only: certificate bypass, when explicitly requested, is scoped
#   to these requests and does not install a global certificate callback.
# - API key is prompted as SecureString and is never accepted as a command-line
#   argument, printed, logged or written to the JSON report.
# - Default mode is read-only.
# - Voucher codes returned by the API are never printed or exported.
# - Write tests create uniquely named short-lived test vouchers and delete each
#   by its documented UUID endpoint in a finally cleanup path.

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
    throw @"
ApiRootUrl deve essere la radice ufficiale che termina in /v1.
Copiarla dalla documentazione locale: UniFi Network > Integrations.
Esempio di forma (non usare come supposizione per il proprio controller):
https://CONTROLLER/proxy/network/integration/v1
"@
}
if ($FullWriteTests -and -not $AllowWriteTests) {
    throw "-FullWriteTests richiede anche -AllowWriteTests."
}

$secureApiKey = Read-Host "UniFi API key" -AsSecureString
$keyPtr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureApiKey)
$plainApiKey = $null
$failureMessage = $null
$startedUtc = (Get-Date).ToUniversalTime().ToString("o")
$stages = [System.Collections.Generic.List[object]]::new()

$summary = [ordered]@{
    schemaVersion        = 1
    startedUtc           = $startedUtc
    powershellVersion    = $PSVersionTable.PSVersion.ToString()
    tlsVerification      = if ($SkipCertificateCheck) { "disabled-explicitly-for-test" } else { "enabled" }
    applicationVersion   = $null
    siteCount            = 0
    voucherCount         = 0
    voucherShapeValid    = $null
    writeTestsRequested  = [bool]$AllowWriteTests
    fullWriteTests       = [bool]$FullWriteTests
    writeTestsCompleted  = $false
    testVouchersCreated  = 0
    testVouchersDeleted  = 0
    stages               = $stages
}

function Add-Stage {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Result,
        [Nullable[int]]$HttpStatus = $null,
        [string]$Detail = ""
    )

    $stages.Add([PSCustomObject]@{
        name       = $Name
        result     = $Result
        httpStatus = $HttpStatus
        detail     = $Detail
    })
}

function Get-HttpHint {
    param([int]$Status)

    switch ($Status) {
        401 { return "API key non accettata per questa richiesta. Verificare tipo/ambito della chiave nella pagina Integrations della versione installata." }
        403 { return "La richiesta e autenticata ma non autorizzata per l'operazione richiesta." }
        404 { return "Endpoint non presente a questo percorso/versione. Confrontare ApiRootUrl e documentazione locale in Network > Integrations." }
        429 { return "Rate limit raggiunto. Interrompere il test e riprovare successivamente." }
        default { return "Consultare la documentazione locale della versione installata per questo stato HTTP." }
    }
}

function Invoke-OfficialApi {
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
        Uri                 = "$ApiRootUrl$Path"
        Method              = $Method
        Headers             = $headers
        TimeoutSec          = 20
        SkipHttpErrorCheck  = $true
        ErrorAction         = "Stop"
    }

    if ($SkipCertificateCheck) {
        $parameters["SkipCertificateCheck"] = $true
    }

    if ($null -ne $Body) {
        $parameters["ContentType"] = "application/json"
        $parameters["Body"] = $Body | ConvertTo-Json -Depth 10 -Compress
    }

    $response = Invoke-WebRequest @parameters
    $parsed = $null
    if (-not [string]::IsNullOrWhiteSpace([string]$response.Content)) {
        try {
            $parsed = $response.Content | ConvertFrom-Json
        }
        catch {
            # Preserve the HTTP result without echoing raw content, which could
            # contain a voucher code or other operational data.
            $parsed = $null
        }
    }

    [PSCustomObject]@{
        StatusCode  = [int]$response.StatusCode
        ContentType = [string]$response.Headers["Content-Type"]
        Json        = $parsed
    }
}

function Assert-Status {
    param(
        [Parameter(Mandatory = $true)]$Response,
        [Parameter(Mandatory = $true)][int[]]$Expected,
        [Parameter(Mandatory = $true)][string]$StageName
    )

    if ($Expected -notcontains $Response.StatusCode) {
        $hint = Get-HttpHint -Status $Response.StatusCode
        Add-Stage -Name $StageName -Result "failed" -HttpStatus $Response.StatusCode -Detail $hint
        throw "${StageName}: HTTP $($Response.StatusCode). $hint"
    }
}

function Test-JsonObject {
    param(
        [object]$Value,
        [string[]]$RequiredProperties,
        [string]$Context
    )

    if ($null -eq $Value) {
        throw "${Context}: risposta JSON mancante o non valida."
    }

    $properties = @($Value.PSObject.Properties.Name)
    foreach ($required in $RequiredProperties) {
        if ($properties -notcontains $required) {
            throw "${Context}: proprieta documentata mancante: $required"
        }
    }
}

function Test-GuidValue {
    param(
        [object]$Value,
        [string]$Context
    )

    $parsedGuid = [Guid]::Empty
    if (-not [Guid]::TryParse([string]$Value, [ref]$parsedGuid)) {
        throw "${Context}: UUID non valido."
    }
}

function Test-VoucherShape {
    param(
        [Parameter(Mandatory = $true)]$Voucher,
        [string]$Context = "voucher"
    )

    Test-JsonObject -Value $Voucher -Context $Context -RequiredProperties @(
        "id",
        "createdAt",
        "name",
        "code",
        "authorizedGuestCount",
        "expired",
        "timeLimitMinutes"
    )
    Test-GuidValue -Value $Voucher.id -Context "$Context.id"

    if ([string]::IsNullOrWhiteSpace([string]$Voucher.code)) {
        throw "${Context}: code e presente ma vuoto."
    }

    # Intentionally do not return or display the secret code.
    [PSCustomObject]@{
        id                       = [string]$Voucher.id
        name                     = [string]$Voucher.name
        createdAt                = [string]$Voucher.createdAt
        timeLimitMinutes         = [int64]$Voucher.timeLimitMinutes
        authorizedGuestCount     = [int64]$Voucher.authorizedGuestCount
        authorizedGuestLimit     = if ($Voucher.PSObject.Properties.Name -contains "authorizedGuestLimit") { $Voucher.authorizedGuestLimit } else { $null }
        dataUsageLimitMBytes     = if ($Voucher.PSObject.Properties.Name -contains "dataUsageLimitMBytes") { $Voucher.dataUsageLimitMBytes } else { $null }
        rxRateLimitKbps          = if ($Voucher.PSObject.Properties.Name -contains "rxRateLimitKbps") { $Voucher.rxRateLimitKbps } else { $null }
        txRateLimitKbps          = if ($Voucher.PSObject.Properties.Name -contains "txRateLimitKbps") { $Voucher.txRateLimitKbps } else { $null }
        activatedAt              = if ($Voucher.PSObject.Properties.Name -contains "activatedAt") { $Voucher.activatedAt } else { $null }
        expiresAt                = if ($Voucher.PSObject.Properties.Name -contains "expiresAt") { $Voucher.expiresAt } else { $null }
        expired                  = [bool]$Voucher.expired
        codePresent              = $true
    }
}

function Get-AllSites {
    $items = @()
    $offset = 0
    $limit = 200

    while ($true) {
        $response = Invoke-OfficialApi -Method GET -Path "/sites?offset=$offset&limit=$limit"
        Assert-Status -Response $response -Expected @(200) -StageName "List sites"
        Test-JsonObject -Value $response.Json -Context "List sites" -RequiredProperties @(
            "data", "count", "totalCount", "offset", "limit"
        )

        $page = @($response.Json.data)
        $items += $page
        $total = [int64]$response.Json.totalCount

        if ($items.Count -ge $total) {
            break
        }
        if ($page.Count -eq 0) {
            throw "List sites: paginazione incoerente (pagina vuota prima di totalCount)."
        }
        $offset += $page.Count
    }

    $items
}

function Get-AllVouchers {
    param([Parameter(Mandatory = $true)][string]$SelectedSiteId)

    $items = @()
    $offset = 0
    $limit = 1000
    $encodedSite = [Uri]::EscapeDataString($SelectedSiteId)

    while ($true) {
        $response = Invoke-OfficialApi -Method GET -Path "/sites/$encodedSite/hotspot/vouchers?offset=$offset&limit=$limit"
        Assert-Status -Response $response -Expected @(200) -StageName "List vouchers"
        Test-JsonObject -Value $response.Json -Context "List vouchers" -RequiredProperties @(
            "data", "count", "totalCount", "offset", "limit"
        )

        $page = @($response.Json.data)
        $items += $page
        $total = [int64]$response.Json.totalCount

        if ($items.Count -ge $total) {
            break
        }
        if ($page.Count -eq 0) {
            throw "List vouchers: paginazione incoerente (pagina vuota prima di totalCount)."
        }
        $offset += $page.Count
    }

    $items
}

function Select-Site {
    param(
        [object[]]$Sites,
        [string]$RequestedSiteId
    )

    if ($Sites.Count -eq 0) {
        throw "L'API ufficiale non ha restituito alcun sito."
    }

    foreach ($site in $Sites) {
        Test-JsonObject -Value $site -Context "site" -RequiredProperties @("id", "name")
        Test-GuidValue -Value $site.id -Context "site.id"
    }

    if (-not [string]::IsNullOrWhiteSpace($RequestedSiteId)) {
        Test-GuidValue -Value $RequestedSiteId -Context "SiteId"
        $match = @($Sites | Where-Object { [string]$_.id -eq $RequestedSiteId })
        if ($match.Count -ne 1) {
            throw "SiteId non presente nell'elenco restituito dall'API."
        }
        return $match[0]
    }

    if ($Sites.Count -eq 1) {
        return $Sites[0]
    }

    Write-Host ""
    Write-Host "Siti restituiti dall'API:" -ForegroundColor Cyan
    for ($index = 0; $index -lt $Sites.Count; $index++) {
        Write-Host ("  [{0}] {1}  ID: {2}" -f ($index + 1), $Sites[$index].name, $Sites[$index].id)
    }

    while ($true) {
        $choiceText = Read-Host "Selezionare il numero del sito"
        $choice = 0
        if ([int]::TryParse($choiceText, [ref]$choice) -and $choice -ge 1 -and $choice -le $Sites.Count) {
            return $Sites[$choice - 1]
        }
    }
}

function Invoke-TestVoucherCase {
    param(
        [Parameter(Mandatory = $true)][string]$SelectedSiteId,
        [Parameter(Mandatory = $true)][string]$CaseName,
        [Parameter(Mandatory = $true)][hashtable]$Payload
    )

    $encodedSite = [Uri]::EscapeDataString($SelectedSiteId)
    $createdId = $null
    $deleted = $false

    try {
        $createResponse = Invoke-OfficialApi -Method POST -Path "/sites/$encodedSite/hotspot/vouchers" -Body $Payload
        Assert-Status -Response $createResponse -Expected @(201) -StageName "Create test voucher ($CaseName)"
        Test-JsonObject -Value $createResponse.Json -Context "Create test voucher ($CaseName)" -RequiredProperties @("vouchers")

        $created = @($createResponse.Json.vouchers)
        if ($created.Count -ne 1) {
            throw "Create test voucher ($CaseName): atteso esattamente un voucher, ricevuti $($created.Count)."
        }

        $safeCreated = Test-VoucherShape -Voucher $created[0] -Context "created voucher ($CaseName)"
        $createdId = $safeCreated.id
        $summary.testVouchersCreated++

        if ($safeCreated.name -ne [string]$Payload.name) {
            throw "Create test voucher ($CaseName): name non corrisponde al payload."
        }
        if ($safeCreated.timeLimitMinutes -ne [int64]$Payload.timeLimitMinutes) {
            throw "Create test voucher ($CaseName): timeLimitMinutes non corrisponde al payload."
        }

        if ($Payload.ContainsKey("authorizedGuestLimit")) {
            if ([int64]$safeCreated.authorizedGuestLimit -ne [int64]$Payload.authorizedGuestLimit) {
                throw "Create test voucher ($CaseName): authorizedGuestLimit non corrisponde."
            }
        }
        elseif ($null -ne $safeCreated.authorizedGuestLimit) {
            throw "Create test voucher ($CaseName): il controller ha restituito un authorizedGuestLimit non richiesto."
        }

        foreach ($field in @("dataUsageLimitMBytes", "rxRateLimitKbps", "txRateLimitKbps")) {
            if ($Payload.ContainsKey($field)) {
                if ([int64]$safeCreated.$field -ne [int64]$Payload[$field]) {
                    throw "Create test voucher ($CaseName): $field non corrisponde."
                }
            }
        }

        Add-Stage -Name "Create test voucher ($CaseName)" -Result "passed" -HttpStatus 201 -Detail "Schema e round-trip campi validi; code redatto."

        $encodedVoucher = [Uri]::EscapeDataString($createdId)
        $detailResponse = Invoke-OfficialApi -Method GET -Path "/sites/$encodedSite/hotspot/vouchers/$encodedVoucher"
        Assert-Status -Response $detailResponse -Expected @(200) -StageName "Get test voucher ($CaseName)"
        $null = Test-VoucherShape -Voucher $detailResponse.Json -Context "voucher detail ($CaseName)"
        Add-Stage -Name "Get test voucher ($CaseName)" -Result "passed" -HttpStatus 200 -Detail "Dettaglio UUID valido; code redatto."

        $deleteResponse = Invoke-OfficialApi -Method DELETE -Path "/sites/$encodedSite/hotspot/vouchers/$encodedVoucher"
        Assert-Status -Response $deleteResponse -Expected @(200) -StageName "Delete test voucher ($CaseName)"
        Test-JsonObject -Value $deleteResponse.Json -Context "Delete test voucher ($CaseName)" -RequiredProperties @("vouchersDeleted")

        if ([int64]$deleteResponse.Json.vouchersDeleted -lt 1) {
            throw "Delete test voucher ($CaseName): vouchersDeleted non conferma la rimozione."
        }

        $deleted = $true
        $summary.testVouchersDeleted++
        Add-Stage -Name "Delete test voucher ($CaseName)" -Result "passed" -HttpStatus 200 -Detail "Cancellazione UUID confermata."
    }
    finally {
        if ($createdId -and -not $deleted) {
            try {
                $encodedVoucher = [Uri]::EscapeDataString($createdId)
                $cleanup = Invoke-OfficialApi -Method DELETE -Path "/sites/$encodedSite/hotspot/vouchers/$encodedVoucher"
                if ($cleanup.StatusCode -eq 200) {
                    $deleted = $true
                    $summary.testVouchersDeleted++
                    Add-Stage -Name "Emergency cleanup ($CaseName)" -Result "passed" -HttpStatus 200 -Detail "Voucher di test rimosso dopo errore intermedio."
                }
                else {
                    Add-Stage -Name "Emergency cleanup ($CaseName)" -Result "failed" -HttpStatus $cleanup.StatusCode -Detail "Verificare manualmente il voucher di test tramite controller."
                }
            }
            catch {
                Add-Stage -Name "Emergency cleanup ($CaseName)" -Result "failed" -Detail "Cleanup automatico non riuscito; verificare manualmente il controller."
            }
        }
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

    Write-Host "[1] Lettura versione Network tramite API ufficiale..." -ForegroundColor Cyan
    $infoResponse = Invoke-OfficialApi -Method GET -Path "/info"
    Assert-Status -Response $infoResponse -Expected @(200) -StageName "Application info"
    Test-JsonObject -Value $infoResponse.Json -Context "Application info" -RequiredProperties @("applicationVersion")
    $summary.applicationVersion = [string]$infoResponse.Json.applicationVersion
    Add-Stage -Name "Application info" -Result "passed" -HttpStatus 200 -Detail "applicationVersion rilevata."
    Write-Host "    Network applicationVersion: $($summary.applicationVersion)" -ForegroundColor Green

    Write-Host "[2] Enumerazione siti..." -ForegroundColor Cyan
    $sites = @(Get-AllSites)
    $summary.siteCount = $sites.Count
    Add-Stage -Name "List sites" -Result "passed" -HttpStatus 200 -Detail "$($sites.Count) siti restituiti."
    $selectedSite = Select-Site -Sites $sites -RequestedSiteId $SiteId
    $selectedSiteId = [string]$selectedSite.id
    Write-Host "    Sito selezionato: $($selectedSite.name)" -ForegroundColor Green

    Write-Host "[3] Lettura voucher con paginazione..." -ForegroundColor Cyan
    $vouchers = @(Get-AllVouchers -SelectedSiteId $selectedSiteId)
    $summary.voucherCount = $vouchers.Count

    $validShapes = 0
    foreach ($voucher in $vouchers) {
        $null = Test-VoucherShape -Voucher $voucher -Context "voucher list item"
        $validShapes++
    }
    $summary.voucherShapeValid = $true
    Add-Stage -Name "List vouchers" -Result "passed" -HttpStatus 200 -Detail "$validShapes voucher validati; codici non mostrati."
    Write-Host "    Voucher restituiti: $($vouchers.Count) (codici non mostrati)" -ForegroundColor Green

    if ($vouchers.Count -gt 0) {
        Write-Host "[4] Lettura dettaglio di un voucher esistente..." -ForegroundColor Cyan
        $firstSafe = Test-VoucherShape -Voucher $vouchers[0] -Context "first voucher"
        $encodedSite = [Uri]::EscapeDataString($selectedSiteId)
        $encodedVoucher = [Uri]::EscapeDataString($firstSafe.id)
        $detailResponse = Invoke-OfficialApi -Method GET -Path "/sites/$encodedSite/hotspot/vouchers/$encodedVoucher"
        Assert-Status -Response $detailResponse -Expected @(200) -StageName "Get voucher detail"
        $null = Test-VoucherShape -Voucher $detailResponse.Json -Context "voucher detail"
        Add-Stage -Name "Get voucher detail" -Result "passed" -HttpStatus 200 -Detail "Dettaglio schema valido; code redatto."
        Write-Host "    Dettaglio valido." -ForegroundColor Green
    }
    else {
        Add-Stage -Name "Get voucher detail" -Result "skipped" -Detail "Nessun voucher esistente."
        Write-Host "[4] Nessun voucher esistente: dettaglio saltato." -ForegroundColor DarkGray
    }

    if ($AllowWriteTests) {
        Write-Host ""
        Write-Host "WRITE TEST richiesto: verranno creati voucher di test brevi e poi eliminati." -ForegroundColor Yellow
        $confirmation = Read-Host "Digitare esattamente TEST-VOUCHER per procedere"
        if ($confirmation -ne "TEST-VOUCHER") {
            Add-Stage -Name "Write tests" -Result "skipped" -Detail "Conferma test di scrittura non fornita."
            Write-Host "Test di scrittura annullato." -ForegroundColor Yellow
        }
        else {
            $stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMdd-HHmmss")

            $basicPayload = @{
                count                = 1
                name                 = "VoucherManagement-API-Test-Single-$stamp"
                authorizedGuestLimit = 1
                timeLimitMinutes     = 5
            }
            Invoke-TestVoucherCase -SelectedSiteId $selectedSiteId -CaseName "single-use" -Payload $basicPayload

            if ($FullWriteTests) {
                $limitedPayload = @{
                    count                  = 1
                    name                   = "VoucherManagement-API-Test-Limited-$stamp"
                    authorizedGuestLimit   = 3
                    timeLimitMinutes       = 5
                    dataUsageLimitMBytes   = 20
                    rxRateLimitKbps        = 5000
                    txRateLimitKbps        = 2000
                }
                Invoke-TestVoucherCase -SelectedSiteId $selectedSiteId -CaseName "multi-use-with-limits" -Payload $limitedPayload

                # The official schema makes authorizedGuestLimit optional and
                # rejects zero. This verifies only that omission is accepted and
                # round-trips as no explicit limit. Actual multi-guest behavior
                # still requires a later controlled field-use test.
                $noExplicitLimitPayload = @{
                    count            = 1
                    name             = "VoucherManagement-API-Test-NoLimit-$stamp"
                    timeLimitMinutes = 5
                }
                Invoke-TestVoucherCase -SelectedSiteId $selectedSiteId -CaseName "no-explicit-guest-limit" -Payload $noExplicitLimitPayload
            }

            $after = @(Get-AllVouchers -SelectedSiteId $selectedSiteId)
            Add-Stage -Name "Post-write list verification" -Result "passed" -HttpStatus 200 -Detail "Elenco voucher nuovamente leggibile dopo cleanup."
            $summary.writeTestsCompleted = $true
        }
    }
    else {
        Add-Stage -Name "Write tests" -Result "skipped" -Detail "Modalita read-only predefinita."
    }
}
catch {
    $failureMessage = $_.Exception.Message
}
finally {
    $plainApiKey = $null
    if ($keyPtr -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($keyPtr)
    }

    $summary["completedUtc"] = (Get-Date).ToUniversalTime().ToString("o")
    $summary["success"] = [string]::IsNullOrWhiteSpace($failureMessage)

    if (-not [string]::IsNullOrWhiteSpace($OutputJson)) {
        $outputPath = [IO.Path]::GetFullPath($OutputJson)
        $parent = Split-Path -Parent $outputPath
        if ($parent -and -not (Test-Path $parent)) {
            New-Item -ItemType Directory -Force $parent | Out-Null
        }
        $summary | ConvertTo-Json -Depth 8 | Set-Content -Encoding utf8 $outputPath
        Write-Host "Report JSON sanitizzato: $outputPath"
    }
}

if ($failureMessage) {
    Write-Host ""
    Write-Host "TEST INTERROTTO: $failureMessage" -ForegroundColor Red
    Write-Host "Nessuna API key o codice voucher e stato scritto nel report." -ForegroundColor DarkGray
    exit 1
}

Write-Host ""
Write-Host "TEST COMPLETATO." -ForegroundColor Green
Write-Host "Network version: $($summary.applicationVersion)"
Write-Host "Siti: $($summary.siteCount)   Voucher: $($summary.voucherCount)"
if ($AllowWriteTests) {
    Write-Host "Voucher di test creati: $($summary.testVouchersCreated)   eliminati: $($summary.testVouchersDeleted)"
}
exit 0
