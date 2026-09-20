# Official UniFi Network voucher API analysis

Review date: 2026-09-18

## Scope and evidence rule

This analysis is intentionally based on Ubiquiti's published documentation.
It does not infer endpoint names or payload fields from the legacy web UI.

Primary sources:

- Ubiquiti Help: Getting Started with the Official UniFi API
  https://help.ui.com/hc/en-us/articles/30076656117655-Getting-Started-with-the-Official-UniFi-API
- Ubiquiti Network API developer portal
  https://developer.ui.com/network/
- Published Network v10.4.57 OpenAPI document used for schema comparison
  https://developer.ui.com/network/v10.4.57/openapi.json

Ubiquiti states that the **localized Network API documentation in
UniFi Network > Integrations is specific to the installed Network version**.
That local documentation is therefore authoritative for the controller under
test. The online v10.4.57 specification is used here to prepare the test
contract, not to assume that every installed version is identical.

## Confirmed official voucher operations

The official Network API defines the following Hotspot operations:

- GET /v1/sites/{siteId}/hotspot/vouchers
  - List Vouchers
- POST /v1/sites/{siteId}/hotspot/vouchers
  - Generate Vouchers
- GET /v1/sites/{siteId}/hotspot/vouchers/{voucherId}
  - Get Voucher Details
- DELETE /v1/sites/{siteId}/hotspot/vouchers/{voucherId}
  - Delete Voucher
- DELETE /v1/sites/{siteId}/hotspot/vouchers?filter=...
  - Delete Vouchers by filter

The public application should prefer the single-voucher DELETE when deleting
selected vouchers. It avoids constructing a destructive bulk filter and maps
directly to the application's per-voucher safety policy.

## Discovery endpoints

### Application information

GET /v1/info

The documented response includes:

- applicationVersion

The test tool calls this first so the observed Network application version is
recorded before voucher assumptions are evaluated.

### Sites

GET /v1/sites

The site ID is a UUID and is required for the voucher endpoints. The endpoint is
paginated. In v10.4.57:

- offset default: 0
- limit default: 25
- limit maximum: 200

The test tool enumerates sites from the API rather than assuming the legacy
site name "default".

## Voucher creation contract

POST /v1/sites/{siteId}/hotspot/vouchers

The v10.4.57 OpenAPI schema requires exactly these conceptual fields:

Required:

- name
  - non-empty string
  - described as the voucher note duplicated across generated vouchers
- timeLimitMinutes
  - integer
  - minimum 1
  - maximum 1,000,000
  - duration starts at authorization of the first guest
  - subsequent guests share the same expiration time

Optional:

- count
  - default 1
  - minimum 1
  - maximum 1000
- authorizedGuestLimit
  - integer
  - minimum 1
- dataUsageLimitMBytes
  - integer
  - minimum 1
  - maximum 1,048,576
- rxRateLimitKbps
  - download rate
  - minimum 2
  - maximum 100,000
- txRateLimitKbps
  - upload rate
  - minimum 2
  - maximum 100,000

The documented success status is HTTP 201. The response object contains a
"vouchers" array of created voucher-detail objects.

### Important unlimited-use point

The official schema does **not** accept authorizedGuestLimit=0; its minimum is
1 and the field is optional. Therefore the legacy convention quota=0 must not
be copied into the official payload.

Before migration, the field test must confirm that omitting
authorizedGuestLimit produces the same operational behavior that the current UI
calls "Multiuso illimitato". This is intentionally treated as a behavior to
verify on the installed controller rather than an assumption.

## Voucher detail contract

The documented voucher-detail object contains:

- id (UUID)
- createdAt (date-time)
- name
- code (secret activation code)
- authorizedGuestLimit (optional)
- authorizedGuestCount
- activatedAt (optional date-time)
- expiresAt (optional date-time)
- expired (boolean)
- timeLimitMinutes
- dataUsageLimitMBytes (optional)
- rxRateLimitKbps (optional)
- txRateLimitKbps (optional)

The test tool never prints or writes the voucher "code" value. It validates only
that the property is present when required.

## Mapping to the existing application model

| Current application meaning | Legacy field | Official field |
| --- | --- | --- |
| controller voucher ID | _id | id |
| code | code | code |
| recipient/note | note | name |
| duration | duration | timeLimitMinutes |
| created | create_time | createdAt |
| allowed guest count | quota | authorizedGuestLimit |
| usage count | used | authorizedGuestCount |
| first activation | start_time | activatedAt |
| expiration timestamp | end_time | expiresAt |
| expired state | status/status_expires | expired |
| data limit MB | bytes | dataUsageLimitMBytes |
| download limit | down | rxRateLimitKbps |
| upload limit | up | txRateLimitKbps |

The official API does not expose the legacy status strings used by the current
adapter. A future adapter should derive only the minimum compatibility state
required by the current UI, and the mapping must be covered by tests.

## List contract

GET /v1/sites/{siteId}/hotspot/vouchers

The response is a page object containing:

- offset
- limit
- count
- totalCount
- data[]

In v10.4.57 the list endpoint has:

- offset default 0
- limit default 100
- limit maximum 1000

The analysis tool paginates locally and never relies on a single default page.

## Delete contract

Single voucher:

DELETE /v1/sites/{siteId}/hotspot/vouchers/{voucherId}

The documented success status is HTTP 200. The response uses the
"Voucher deletion results" schema with a vouchersDeleted integer.

Bulk deletion also exists and requires a filter query. Voucher Management does
not need bulk-filter deletion for its normal workflow and the test tool does
not use it.

## Authentication and base URL

Ubiquiti documents API-key authentication for the official APIs. The API key is
sent in the X-API-Key header.

For local Network API use, Ubiquiti explicitly directs administrators to the
localized API documentation in **Network > Integrations**. Because URL routing
and API availability can vary with the installed application/control-plane
version, the analysis tool does not invent the local base URL.

The operator must copy the API root ending in /v1 from the installed
Integrations documentation. A common local form is:

https://CONTROLLER/proxy/network/integration/v1

but the tool treats the value supplied from the installed documentation as
authoritative.

## TLS policy for testing

Certificate validation is ON by default.

The test tool offers -SkipCertificateCheck only as an explicit diagnostic
switch for controllers using a local/self-signed certificate. This switch is
not a proposed production security design.

The public application's official adapter must use verified TLS by default and,
if local self-signed certificates must be supported, use a deliberate scoped
trust mechanism.

## Test sequence

The accompanying Test-OfficialUniFiVoucherApi.ps1 performs:

Read-only default:

1. GET /info
2. GET all /sites pages
3. select/validate one site UUID
4. GET all voucher pages
5. validate the documented voucher response shape without displaying codes
6. GET one voucher detail when a voucher exists

Optional mutation test, requiring -AllowWriteTests and an additional typed
confirmation:

1. POST exactly one five-minute single-use test voucher
2. require HTTP 201 and one voucher object
3. validate its documented fields without exposing the code
4. GET the new voucher by UUID
5. DELETE that exact voucher by UUID
6. require HTTP 200 and inspect vouchersDeleted
7. list vouchers again and verify the test UUID is absent from returned pages

A cleanup DELETE is attempted in a finally block if a test voucher was created
and an intermediate validation step fails.

## What the field test must answer before migration

- Does the installed Network version expose all five documented voucher
  operations?
- Which exact local API root does its own Integrations page document?
- Does the API key accepted by that installation authorize both reads and
  voucher writes?
- Does omitting authorizedGuestLimit behave as unlimited multi-use?
- Are createdAt/activatedAt/expiresAt values sufficient for the current table
  and lifecycle display?
- Does authorizedGuestCount match the usage semantics expected by the current
  UI?
- Do data/rx/tx limits round-trip with the documented units?
- Does the single-voucher DELETE behave consistently for unused vouchers?

Only after those observations should the production UniFi adapter be changed.


## Field validation result — Network 10.6.106

Controlled validation was completed on 2026-09-19/20 against an installed
**UniFi Network 10.6.106** instance using its own Network > Integrations
documentation.

Observed local request format:

`https://CONTROLLER/proxy/network/integration/v1`

with the `X-API-Key` request header.

### Read-only result

The diagnostic completed successfully:

- GET /info -> HTTP 200, applicationVersion 10.6.106;
- GET /sites -> HTTP 200;
- voucher list with pagination -> HTTP 200;
- voucher detail by UUID -> HTTP 200;
- existing voucher shapes validated.

No voucher code or API key was written to the sanitized report.

### Controlled write result

The write test completed successfully:

- single-use voucher POST -> HTTP 201;
- detail read -> HTTP 200;
- exact UUID DELETE -> HTTP 200;
- post-delete list remained readable.

The full write test additionally validated:

- authorizedGuestLimit=3;
- dataUsageLimitMBytes=20;
- rxRateLimitKbps=5000;
- txRateLimitKbps=2000;
- creation with authorizedGuestLimit omitted.

All created test vouchers were removed.

### Remaining operational observation

The controller accepts and returns a voucher with no explicit
authorizedGuestLimit. A real multi-client authorization test is still pending
because the operator was not physically at the guest network during this review.

That pending observation does not block implementation of the documented API
mapping, but it remains a release/field-test item for the UI label
"Multiuso illimitato".
