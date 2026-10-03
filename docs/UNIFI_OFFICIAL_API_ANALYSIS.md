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

The read-only live contract tool calls this first so the observed Network
application version is recorded before voucher assumptions are evaluated.

### Sites

GET /v1/sites

The site ID is a UUID and is required for the voucher endpoints. The endpoint is
paginated. In v10.4.57:

- offset default: 0
- limit default: 25
- limit maximum: 200

The application and the read-only live contract tool enumerate Sites from the
API rather than assuming the legacy site name "default". Voucher Management now
persists the verified Site UUID with the controller identity and reuses that
UUID on later sessions; an unknown first association is still required to be
unambiguous.

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

Field validation subsequently confirmed the operational behavior used by the
UI label "Multiuso illimitato": omitting authorizedGuestLimit allowed two real
guest clients to authorize with the same voucher, and UniFi reported both
through authorizedGuestCount. The production adapter therefore continues to
represent the omitted field internally as quota=0 while never transmitting zero
to UniFi.

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

Engineering contract checks never print or write the voucher "code" value.
The current Python smoke tool reports only version, a shortened one-way Site
fingerprint and aggregate voucher shape/state counts; it performs at most one
read-only voucher-detail lookup to verify list/detail consistency.

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

## TLS policy

Certificate validation is ON by default.

The production adapter supports local/self-signed controllers only through
explicit SHA-256 certificate pinning. The fingerprint is shown to the operator
before the application stores the non-secret pin, and a changed fingerprint is
blocked before the API key is sent until the new value is independently
approved. No process-wide certificate verification bypass is used.

The read-only Python contract smoke tool follows the same adapter behavior and
accepts only an explicitly supplied, already verified SHA-256 pin when one is
needed.

## Current contract-test sequence

`tools/verify_unifi_contract_live.py` is the current privacy-safe, read-only
smoke check for a real controller:

1. GET /info;
2. enumerate and resolve the Site through the same production adapter;
3. GET all voucher pages with the production pagination validator;
4. map all returned voucher objects through the production mapper;
5. when at least one voucher exists, GET that voucher again by UUID and require
   list/detail identity consistency;
6. print only aggregate counts plus Network version and a shortened one-way Site
   fingerprint.

The API key is entered with a hidden prompt and remains process-memory-only.
Codes, names and raw voucher/Site UUIDs are not emitted.

Mutation behavior remains covered by the automated adapter/workflow tests and by
controlled field testing. The production software itself never uses a test
mutation path to probe a live installation.

## Field-test contract retained for release validation

Before a new Network application version is considered field-validated, the
release process should re-check:

- the exact local Integration API root documented by that installation;
- /info and Site discovery through the production adapter;
- voucher list/detail response shape and pagination;
- authorizedGuestCount semantics for single-, multi- and unlimited-use
  vouchers;
- activatedAt/expiresAt/expired lifecycle behavior, including that validity
  starts with first authorization rather than voucher creation;
- data/rx/tx limit round-tripping when those options are used;
- single-UUID DELETE behavior during a controlled operator-approved mutation
  test.

Voucher Management deliberately does not maintain an arbitrary version
allowlist. Compatibility is established by the documented API contract,
automated parser/workflow tests and an explicit read-only smoke against the
target installation; mutation field tests are performed separately when a new
controller line needs release validation.


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

### Subsequent unlimited-use field observation

A later real-client test completed the remaining operational observation: two
guest clients successfully used the same voucher created with
authorizedGuestLimit omitted, and the controller reflected the increasing
authorizedGuestCount. This matches the application's "Multiuso illimitato"
mapping and removes the earlier pending observation.
