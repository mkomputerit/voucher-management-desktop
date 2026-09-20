# Changelog

## 4.2.0-rc.10 - 2026-09-20

- Fixed backup-test placement so the restore trust-reset, history
  fingerprint/key consistency, and oversized-manifest regression tests are
  actually collected by pytest.
- No application behavior changed from rc.9.

## 4.2.0-rc.9 - 2026-09-20

- Fixed stale settings writes that could erase the print-history key
  fingerprint and permanently fail-close lifecycle operations.
- Settings updates now merge against the latest persisted state; malformed
  settings are preserved as a diagnostic copy and surfaced to the operator.
- Re-read selected vouchers from UniFi immediately before deletion so cached
  state cannot revoke a voucher that became active since the last refresh.
- Separated successful create/delete controller mutations from subsequent list
  refresh failures so operators are not told that an already-completed action
  failed.
- Reject all HTTP redirects in the authenticated UniFi client so X-API-Key is
  never forwarded to a different request target.
- Fixed the missing-socket PinnedCertificateMismatch constructor path.
- Enforced UI/API batch quantity 1..50 and multiuse quota 2..999.
- Backup restore now verifies history fingerprint/key coherence before touching
  live data, clears restored controller endpoint/certificate trust, caps
  manifest size, and rolls back managed data directories without deleting the
  live root/log handles.

## 4.2.0-rc.8 - 2026-09-20

- Replaced the weak Windows associated-icon check with direct PE resource
  verification using pefile.
- The Windows CI now compares all generated ICO frame payloads with the
  executable RT_GROUP_ICON/RT_ICON resources.
- Removed the internal PUBLIC_RELEASE_READINESS.md engineering checklist from
  clean public snapshots and archive exports.
- Refined 16 px and 24 px artwork with a thinner outline and larger
  confirmation badge/checkmark.
- Added tests for ICO parsing, RT_GROUP_ICON parsing and readiness-checklist
  exclusion.

## 4.2.0-rc.7 - 2026-09-20

- Aligned the public-release readiness checklist with the already-created public
  repository and enabled security/reporting settings.
- Added CI verification that the bundled ICO exists and that the Windows
  executable exposes an associated icon.
- Added dedicated high-contrast 16 px and 24 px icon artwork instead of
  downscaling the 1024 px master for those Windows UI sizes.
- Added deterministic ICO and small-frame regression tests.

## 4.2.0-rc.6 - 2026-09-20

- Finalized SECURITY.md to use GitHub Private Vulnerability Reporting.
- Added original, neutral Voucher Management application artwork generated
  reproducibly from source.
- Embedded the icon in the Windows executable and bundled it for the Tk window.
- Added tests for ICO generation and bundled icon discovery.
- Excluded generated build-only icon artifacts from Git and public snapshots.

## 4.2.0-rc.5 - 2026-09-20

- Removed PRIVATE_REPOSITORY_IDENTITY from the exported public workflow.
- Added PRIVATE_REPOSITORY_IDENTITY to forbidden public-workflow tokens.
- Added a regression test simulating the public repository environment so the
  clean export remains valid when GitHub Actions runs in the public repository.

## 4.2.0-rc.4 - 2026-09-20

- Restored generic privacy detection for RFC1918 private IPv4 addresses and
  hard-coded credential assignments.
- Expanded Windows user-profile detection to both backslash and slash paths.
- Added regression tests for every generic privacy pattern.
- Added --require-markers so private CI fails when deployment markers are
  missing or empty.
- Removed private marker-secret plumbing from the exported public workflow.
- Removed hard-coded private repository identity and added an environment-driven
  export guard against accidental identity leakage.

## 4.2.0-rc.3 - 2026-09-20

- Added explicit certificate-pin rotation when a previously trusted controller
  certificate changes, showing both old and new SHA-256 fingerprints before
  replacement.
- Added optional external privacy markers for deployment-specific names and
  asset paths without committing marker plaintext.
- Added CI support for the private PUBLIC_PRIVACY_MARKERS secret.
- Removed beta engineering records and private repository identity from the
  clean public snapshot/workflow.
- Updated public TLS verification guidance and release-readiness documentation.

## 4.2.0-rc.2 - 2026-09-20

- Replaced the self-signed TLS bypass with explicit SHA-256 certificate pinning
  enforced on the same TLS socket before API credentials are sent.
- Removed brute-forceable deployment-value SHA-256 markers from the public
  privacy checker.
- Excluded internal/obsolete engineering reports from the clean public snapshot.
- Removed private prerelease plumbing from the exported public workflow and
  ensured private prereleases reuse the already-tested build.
- Redacted unexpected UI callback tracebacks from logs.
- Removed sensitive rollback copies after failed restore recovery.
- Documented multi-site and print-collation limitations.

## 4.2.0-rc.1 - 2026-09-20

- Closed the 4.2 functional field-test matrix, including real multi-client
  validation of vouchers created without an explicit guest limit.
- Renamed the Python package namespace from `unifi_voucher_tool` to
  `voucher_management`.
- Added Windows executable version/product metadata and CI verification against
  `version.txt`.
- Prepared the release-candidate branch for the final public-snapshot gate.

## 4.2.0-beta.2 - 2026-09-20

### External review fixes

- Remove plaintext deployment markers from the public-tree checker; known
  private values are matched only by SHA-256 digests.
- Add an executable clean-snapshot exporter and CI validation that excludes
  private transition/review material before publication.
- Keep TLS verification enabled by default and add regression tests proving
  that unverified TLS requires the explicit per-controller compatibility mode.
- When reopening an archived PDF, correlate and audit every voucher label tied
  to that file before recording a physical print.
- Make restore rollback creation transactional: live data is untouched until a
  complete rollback snapshot exists.
- Validate a new backup ZIP before replacing an existing backup file.
- Resolve archived PDFs by literal filename so valid characters such as square
  brackets are not interpreted as glob syntax.
- Bound generated filename components to reduce Windows path-length risk.
- Make the UI expiry guard depend directly on controller state EXPIRED rather
  than an Italian display label.
- Surface unexpected Tk callback failures in console-less builds.
- Close PDFium page/document resources deterministically and remove orphan
  preview windows on PDF load failure.
- Disable the print button while a print is in progress and guard against
  reentrant submissions.
- Rasterize each PDF page once and reuse it for requested document copies.
- Debounce voucher search before rebuilding the table/history view.
- Make Python module documentation visible through __doc__ and enforce it with
  an AST-based test.
- Clarify that HMAC audit identifiers are not encryption and that backups/PDFs
  containing voucher material are sensitive operational data.
- Defer SignPath Foundation attribution until actual project acceptance.
- Document that the current Windows operator interface is Italian.

### Review evidence

- Add `docs/REVIEW_FIXES_4_2_0_BETA_2.md` with a point-by-point disposition
  of the pre-publication review.
- Add regression tests for backup failure modes, archived-PDF linkage, literal
  filename lookup, expiry state and TLS defaults.


## 4.2.0-beta.1 - 2026-09-20

### Official UniFi Network API

- Replace the legacy username/password session adapter with the documented
  Network integration API and X-API-Key authentication.
- Validate the integration against UniFi Network 10.6.106 using the
  controller's own Network > Integrations documentation.
- Add API discovery through /info and /sites.
- Map documented voucher list/detail/create/delete operations into the existing
  controller-independent operator model.
- Use single-voucher DELETE endpoints rather than destructive bulk filters.

### Authentication and TLS

- Never persist the API key; clear the visible API-key field after every
  connection attempt.
- Migrate the old controller host setting into the new API-root setting while
  dropping remembered legacy usernames from the persistent schema.
- Verify TLS certificates by default.
- Add an explicit per-controller local/self-signed compatibility option whose
  certificate bypass is scoped to the UniFi client HTTPS handler.
- Report Network version and TLS verification state in the connection status
  without logging the controller address or API key.

### Voucher semantics

- Map authorizedGuestCount to the existing usage counter.
- Derive internal lifecycle state from the documented expired flag and usage
  count.
- Treat omitted authorizedGuestLimit as the internal no-explicit-limit state;
  never send authorizedGuestLimit=0 to the official API.
- Map dataUsageLimitMBytes, rxRateLimitKbps and txRateLimitKbps using documented
  units and enforce their documented limits.
- Keep physical multi-client validation of the no-explicit-limit case as an
  explicit pending field test.

### Reviewability

- Add synthetic tests for URL normalization, connection/site discovery,
  pagination, field mapping, unlimited-field omission, documented limits,
  UUID deletion and partial-delete reporting.
- Add docs/BETA_4_2_0_REVIEW.md with field evidence, design constraints and
  rollback boundary.
- Keep the 4.1.0-beta.5 pre-API checkpoint unchanged.


## 4.1.0-beta.5 - 2026-09-18

### Identity and licensing

- Rename the public product to **Voucher Management**.
- Adopt the MIT License.
- Remove deployment-specific artwork, controller addresses and organization
  defaults from the public codebase.
- Document the project as an independent application that may interoperate with
  UniFi Network without implying affiliation or endorsement.

### Security and privacy

- Keep controller credentials out of settings, logs, backups and source.
- Remove host/controller identity from diagnostic logs.
- Store print-history correlation with HMAC identifiers rather than plaintext
  voucher codes.
- Make the history key portable with application data while retaining one-time
  migration from earlier Windows-bound data.
- Fail closed when audit history cannot be read.
- Reject unsafe backup paths and cap backup extraction resources.
- Distinguish successful Windows print submission from local audit failure to
  reduce accidental duplicate printing.

### Operator lifecycle policy

- Treat first physical print as the issue boundary.
- Block application-side deletion after printing.
- Block application-side deletion when a voucher is already used/in use.
- Keep revocation outside Voucher Management; it remains an administrator task.

### Maintainability

- Remove the obsolete duplicate UI and old runtime monkey patch.
- Centralize product identity and persistence paths.
- Separate the portable history key store from legacy DPAPI migration.
- Pin the exact dependency versions proven by Windows CI.
- Expand automated tests for backup, persistence, history and lifecycle policy.

### Distribution

- Rename the Windows artifact and executable to Voucher Management.
- Prepare GitHub Actions for a future origin-verified SignPath Foundation flow.
- Plan a clean public repository so private beta history and operational data
  are never exposed.
