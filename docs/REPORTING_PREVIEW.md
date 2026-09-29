# Reporting preview for 5.1.0

This document describes the current 5.1.0 pre-EXE reporting candidate. The candidate evolves on `feature/5.1-pre-exe-completion`; use the artifact from the exact successful workflow run being reviewed rather than relying on an older hard-coded commit.

Download the artifact from a successful Windows workflow for the exact candidate branch/commit under review. Extract the complete archive into a separate application folder and run `VoucherManagement.exe` from that folder. Do not copy only the executable over an older installation.

## Operator acceptance checks

- Before connecting to UniFi, Home counters must not present the local archive as current controller data. Connect and synchronize to populate the live view.
- Creating vouchers exposes the explicit **Voucher nominale** checkbox. This classification is local reporting metadata and is not sent to UniFi.
- Reports distinguish application-generated vouchers, generated and never used, nominal, unclassified and usage-indeterminate records. Missing usage evidence must not be presented as proof of never having been used.
- Existing vouchers without verified nominal classification remain unclassified.
- A report must never classify a voucher as both used and usage-indeterminate; missing usage provenance wins conservatively.
- `Stampati - nessun utilizzo rilevato` requires a real controller observation at or after the first recorded print. A later sync that only observes voucher absence does not refresh usage evidence.
- Report freshness is based on the voucher's last observed presence, not merely the last sync run touching its row.
- Print aggregates distinguish unique document jobs from physical voucher copies; one document containing many vouchers must remain one print job.
- Privacy-redacted nominality must not reappear as nominal/non-nominal even if stale database bits remain.
- Legacy backup creation/evidence timestamps must not be presented as proven controller-creation timestamps.

This is a portable preview for operator validation, not a stable release or Setup.exe installer.
