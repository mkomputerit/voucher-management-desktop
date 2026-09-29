# Changelog

## 5.1.0 - Unreleased

- Separate the two product cores explicitly: Home is now a live UniFi
  operational view whose counters stay blank until a successful controller
  snapshot in the current session, while Report is the durable local-history
  workspace and defaults to the complete SQLite archive.
- Add an explicit **Voucher nominale** flag to voucher creation. The choice is
  local reporting metadata and is never sent to UniFi; recipient text is no
  longer used to infer nominal status.
- Upgrade SQLite to schema 3 with durable voucher provenance, tri-state nominal
  classification and a monotonic ever-used fact. Existing rows migrate as
  unknown/unclassified instead of receiving guessed classifications.
- Rebuild administrative report semantics around durable facts: add generated
  by Voucher Management, generated-and-never-used, printed, nominal,
  unclassified and usage-indeterminate views; make used/never-used historical
  rather than dependent on only the latest controller counter; preserve legacy
  generation provenance only when verified migration evidence exists.
- Distinguish "mai utilizzato" from missing usage evidence, keep ever-used
  monotonic across later controller counter resets, and make deletion/retention
  fail closed when durable history says a voucher was used or its usage cannot
  be established. A confirmed UniFi create is also never reported as failed
  merely because local reporting persistence fails afterward.

- Print only the recipient value inside each cut voucher, without the
  "Destinatario:" prefix.
- Complete first-run setup with a backup step for the default folder and the
  optional shutdown proposal; expose these preferences in Settings > Backup.
- Show the last verified successful backup in Home with a manual backup action.
- Offer per-copy destination and optional password protection for manual and
  shutdown backups. Protection is selected by default; creating a readable ZIP
  requires an explicit opt-out, while .vmbk copies retain authenticated encryption.
  Closing also offers skip/cancel,
  and a one-off directory override never updates the saved default.
- Clear machine-specific backup destinations from portable settings and retain
  validated restore compatibility for both ZIP and .vmbk files.

- Reconnect from Home through a compact saved-controller summary asking only
  for the session API key, without opening Settings. Healthy active sessions
  still synchronize directly; failed sessions can enter a fresh key through
  the same TLS verification and background connection workflow.
- Give the Home table priority for a complete ten-voucher batch, compact the
  controller summary, remove redundant area shortcuts and collapse recent
  activity by default into an optional two-row panel.

- Explain in the privacy documentation and print preview that the recipient
  stays on newly generated cut vouchers handed to guests.
- Replace legacy-conflict instructions for a nonexistent in-app repair flow
  with explicit technical-assistance guidance; ambiguous imports remain blocked.
- Clarify the 5.1.0 release-candidate status and outstanding Windows visual/DPI
  acceptance checks without claiming automatic per-monitor DPI support.

- Keep Home and Voucher keyboard highlighting aligned with the actual print
  selection: arrows move focus and Space toggles the focused voucher, preserving
  hidden selections and excluding expired vouchers without selection-event loops.
- Add vertical and horizontal scrolling to both Home tables and correct recovery
  instructions to point to Settings > Manutenzione.

- Begin the operator-first desktop redesign with first-class Home, Voucher,
  Report and Settings workspaces.
- Move controller URL/API-key controls out of the main operational surface and
  into the Controller settings area.
- Add concise controller synchronization states (ready, local snapshot,
  synchronizing, unreachable, not configured) backed by the durable
  last-successful-sync timestamp.
- Add Home operational metrics/recent activity and an embedded Report overview.
- Make voucher row highlighting track the actual print selection so the
  operator sees one unambiguous selection concept.
- Add explicit first-start choices when legacy per-user data is detected:
  migrate it, restore a backup, or deliberately begin a separate new shared
  installation without deleting the old profile data.
- Make the Home synchronization action context-sensitive: synchronize an active
  session, otherwise guide the operator directly to Controller settings.
- Refine the Home into a professional operator dashboard with recent vouchers,
  controller health, last synchronization time, durable recent activity and
  quick actions.
- Split the main Settings workspace into Generali, Controller, PDF / stampa,
  Retention and Backup sections; routine operator screens no longer mix
  controller, retention and recovery concepts together.
- Surface backup recency and current retention policy in plain operator
  language while keeping recovery/migration tools in a separate maintenance
  area.
- Make startup window mapping deterministic on Windows before maximizing or
  opening first-run/retention modals, preventing a live background process with
  no visible top-level window on affected sessions.
- Add explicit green/red controller connection indicators in Home and sidebar.
- Use a high-contrast Windows-blue voucher selection in both light and dark
  themes and reorganize the Voucher workspace around the print workflow.
- Preserve automatic selection of newly created vouchers and expose the same
  audited print/reprint flow directly from Home.
- Replace text-symbol navigation with theme-aware drawn sidebar icons and allow
  operators to assign a persistent descriptive name to the controller.
- Increase the voucher logo and immediate heading text while preserving the
  established A4 and cutting geometry.
- Guide restore of pre-SQLite backups through the required second phase:
  reconnect/synchronize UniFi and migrate the verified legacy print history
  into SQLite before relying on 5.x reports.
- Add direct import of pre-SQLite ZIP backups: voucher codes are recovered
  from the archived PDFs, verified against HMAC print history, materialized
  into SQLite, and the archived PDFs are copied into the current print archive
  without overwriting current application settings.
- Move initial and manual controller snapshot persistence off the Tk thread so
  large UniFi responses cannot block the operator window after connection.
- Refresh report aggregates only when the Report workspace is opened instead
  of rebuilding them during every voucher-table population.
- Prevent Home/Voucher selection feedback from recursively generating Tk
  Treeview selection events after controller refreshes; Home selection now
  changes only from real operator row clicks.
- Add a post-connection event-loop heartbeat to field diagnostics so a future
  UI stall can be distinguished from network/database completion.
- Harden pre-SQLite backup import after engineering review: only clear PDF
  codes that resolve against HMAC history can create historical voucher rows,
  unrelated ten-digit text is reported and ignored, legacy-import rows no
  longer misuse retention's archived_at marker, individual PDFs are capped at
  64 MiB, copied paths are revalidated locally, and partial-import failures are
  explicitly documented as safe to retry.
- Show the source ZIP SHA-256 before legacy import and recommend a fresh .vmbk
  export after successful conversion because the old ZIP format is not
  externally authenticated.
- Make retention minimization irreversible across legacy reimports: a
  previously scrubbed credential/recipient is never reconstructed, and early
  5.1 rows accidentally rehydrated after retention are scrubbed again from the
  durable RETENTION_ARCHIVED audit fact.
- Treat both materialized legacy PDF-generation events and resolved pre-
  materialization legacy evidence as retention blockers, even when the current
  installation's HMAC history file contains no corresponding event.
- Preserve specific legacy-import validation errors after the pre-import safety
  backup instead of replacing them with a generic partial-import message.
- Exclude synthetic legacy-backup controllers from Home's "Voucher rilevato"
  feed so importing historical data is not presented as a new controller
  discovery in local-only mode.
- Preserve retention-minimized legacy identities across later ZIPs whose
  history has grown: stable HMAC evidence is rebound to the existing archived
  voucher without restoring its clear code or creating a duplicate, while
  conflicting code reuse fails closed before candidate creation.
- Document and surface the retention boundary for imported legacy evidence:
  verified generated/printed archive rows stay protected from ordinary
  minimization, and an explicitly imported old ZIP may restore data that the
  current database can no longer cryptographically link to a minimized row.
- Centralize the synthetic legacy-controller API-root prefix so import and Home
  filtering cannot drift onto different string literals.
- Refine the operator shell for Windows scaling and clearer status semantics:
  sidebar icons follow Tk DPI scaling, warning/local controller states use
  amber instead of error red, Settings shows the same status dot, the active
  navigation item uses the primary blue accent and the main-window minimum no
  longer assumes a 740-pixel-tall desktop.
- Move recovery/history tools out of the routine Backup tab into a dedicated
  Settings > Manutenzione page and explain that verified imported legacy
  generation/print evidence remains outside ordinary retention.
- Make the legacy-import completion counter report distinct minimized vouchers
  represented by the current ZIP only, excluding unrelated historical repairs
  and duplicate accounting across repair/resolution phases.
- Make fail-closed legacy identity conflicts actionable without silently
  skipping evidence, and render sidebar icons from supersampled geometry at the
  requested DPI size instead of enlarging a fixed 20-pixel bitmap.
- Rebalance the Home workspace for daily operation: give recent vouchers and
  durable activity more space, compact the shell for short displays, keep
  create/print/sync actions beside their actual context, and turn the former
  duplicated quick-action card into simple navigation to Voucher, Report and
  Settings.
- Refine the Voucher workspace for constrained Windows displays: separate the
  destructive delete action, let search expand naturally, and add horizontal
  scrolling so the full operational table remains reachable at higher DPI or
  narrower window sizes without changing print-selection behavior.
- Refine Report and General settings without changing their data paths: make
  report/export scope and privacy explicit, and show theme choices as
  operator-facing Italian labels while persisting the existing system/light/
  dark values for backward compatibility.
- Regenerate sidebar icons when Tk exposes a changed display scaling value
  after root-window move/resize events, so per-monitor DPI changes do not leave
  raster navigation icons at the previous monitor's size.
- Rework Home toward a UniFi-portal-style dashboard with controller health as
  the first card, operational counters beneath it, a full-width recent-voucher
  workspace and a cleaner activity/navigation footer.
- Replace theme-dependent text glyphs for controller health with real coloured
  connection indicators: green when connected, amber for local/unconfigured,
  red for connection failure and blue only while synchronizing.
- Simplify the Voucher command bar and replace the internal-sounding
  "PREPARA STAMPA" action with the explicit operator action "Stampa
  selezionati", preserving the same audited print/reprint workflow.
- Keep the configured logo inside every physical voucher label and move the
  recipient from the sheet-only strip into the cut area, preserving the proven
  2-column × 5-row A4 geometry and dashed cutting guides.
- Align package, window title and Windows executable metadata to 5.1.0 for the
  5.1 field/release candidate instead of continuing to identify test builds as
  5.0.0.

## 5.0.0 - 2026-09-27

- Introduced the SQLite 5.0 persistence foundation with WAL mode, integrity
  checks, durable controller/voucher snapshots, local/offline startup and
  application-level audit facts.
- Added crash-safe physical-print auditing across the existing HMAC history and
  SQLite using one stable audit identity, including recovery without automatic
  print replay and explicit reprint policy.
- Added explicit 4.x history migration with verified HMAC association,
  resolved/ambiguous/unresolved evidence, idempotent materialization and
  encrypted pre-migration safety backups.
- Added installer-controlled shared Windows deployment under ProgramData with a
  dedicated operator group, machine-wide file locking, restrictive verified
  NTFS ACLs and explicit LocalAppData-to-ProgramData migration.
- Added first-run onboarding for genuinely new 5.0 installations with
  installation identity, structure/Wi-Fi branding, managed logo selection,
  ephemeral UniFi API-key verification, explicit TLS trust, a persistent
  operator-named controller profile and installation-scoped retention setup.
  Shared-mode legacy data takes precedence over onboarding and normal
  operations remain blocked until the explicit migration decision is resolved.
  Portable 4.x settings/history/PDF/logo evidence is also detected before the
  wizard so an upgrade without a pre-existing SQLite database is never treated
  as a brand-new installation.
- Added WAL-safe SQLite backup/restore using `sqlite3.Connection.backup()`,
  standalone snapshot normalization, `PRAGMA integrity_check`, schema/hash
  verification and exclusion of live WAL/SHM/journal sidecars.
- Added the 5.0 shutdown safety gate: ordinary window close creates an
  encrypted authenticated backup by default before exit, never persists the
  backup password, refuses to interrupt active background work and offers
  Retry / Close anyway / Cancel after a failed backup. Normal backup creation
  in the UI now produces only protected `.vmbk` files; legacy plaintext ZIP
  remains restore-only compatibility.
- Wired the existing `application_sessions` schema into runtime lifecycle
  auditing so a clean close records its controller and backup outcome while a
  hard crash remains distinguishable by an open session record.
- Wired the existing `backup_history` schema into the 5.0 backup lifecycle.
  Manual backups, automatic shutdown backups and verified pre-migration backups
  now record final SUCCESS/FAILED outcomes. Successful rows identify the final
  delivered backup container by SHA-256 and retain its backup format and SQLite
  schema version. Audit rows store only a destination category and filename
  basename, never a full Windows path, password or exception message. The
  shared-data migration records its verified source backup in the resulting
  ProgramData database without modifying the original per-user source.
- Added review-driven 5.0 voucher retention. The installation defaults to
  180 days and permanently protects vouchers that were used, physically
  printed or already have a generated PDF. Only old vouchers that are no
  longer present on the controller and have no use/print/PDF evidence are
  proposed as candidates.
  Retention never runs silently: the operator reviews and explicitly selects
  rows. Approved rows remain as historical records but their reusable voucher
  code, recipient, assignment and notes are removed and the action is audited.
  A later controller reappearance reactivates the same durable row.
- Added real Windows CI coverage for shared deployment ACLs, upgrade behavior,
  preserved-data uninstall and explicit `-RemoveData` cleanup.
- Hardened the stable-release trigger so a final `CHANGELOG.md` release-date
  commit is itself sufficient to run the complete Windows verification and
  explicit `[release]` publication path on `main`.
- Added SQLite-backed administrative reporting with summary, used, expired,
  printed-never-used, never-printed, nominal and full-history views, plus atomic
  printable PDF and CSV export. Summary/audit report rows are sanitized through
  the central report credential policy and never expose voucher codes in clear.
- Final release review completed with no release blocker. Package metadata,
  Windows executable metadata and the public release archive are aligned to
  5.0.0; publication is performed only by the verified `[release]` workflow
  from this reviewed `main` commit.

## 4.3.3 - 2026-09-23

- Harden protected backup and history-exchange decryption with a bounded-memory
  two-pass AES-256-GCM flow: the complete ciphertext is authenticated before
  any plaintext is written to the temporary restore target.
- Require every UniFi POST request to declare its uncertain-operation context
  explicitly. Voucher creation keeps the existing conservative no-replay
  behavior, while future POST operations cannot inherit a misleading
  voucher-creation error message accidentally.
- Add regression coverage proving failed authentication never writes plaintext
  to the target and that POST requests cannot reach the network without an
  explicit uncertainty description.
- Align package, Windows executable metadata, security documentation and
  code-signing policy with the 4.3.3 release prepared for SignPath Foundation
  application.
- Ensure changes to Windows version-resource metadata trigger the full Windows
  CI/build workflow before release.

## 4.3.2 - 2026-09-22

- Removed the full voucher-table rebuild from ordinary checkbox selection.
  Single-row and select-all changes now update checkbox marks, the selected
  count and the print-action label in place, eliminating the visible refresh
  that was especially noticeable over Remote Desktop sessions.
- Added regression tests proving that checkbox selection does not call the
  expensive full-table populate path and that expired rows remain
  non-selectable during bulk selection.
- Aligned direct local-development/runtime requirements with the
  hash-verified Windows release lock, including Pillow 12.3.0 and the explicit
  cryptography dependency used by encrypted backup/history features.
- Added dependency-consistency tests so direct requirement versions cannot
  silently diverge from the reviewed release lock.

## 4.3.1 - 2026-09-22

- Consolidated the Tk architecture without changing operator behavior:
  guarded creation, controller/TLS connection, deletion/recovery and
  backup/history maintenance now live in focused UI adapters composed by
  `VoucherApp` / `ModernVoucherApp`; the main shell no longer accumulates
  those unrelated workflows.
- Added a durable anti-repeat guard around voucher creation. The marker is
  persisted before the non-idempotent controller POST, survives process
  interruption and contains no API key, voucher code, controller address or
  operator-entered creation data.
- Classify transport failures, server-side 408/5xx responses and malformed or
  incomplete successful create responses as an uncertain mutation. Voucher
  Management never replays the POST automatically; it performs only a safe
  controller refresh and blocks further creation until the operator completes
  a successful manual synchronization.
- Block backup creation and restore while a voucher-create outcome is unresolved
  so portable state changes cannot erase the anti-repeat boundary.
- Added stable random `event_id` values to new PDF-generation history rows.
  Manual multi-workstation history exchange now merges these events by stable
  identity, preventing independent but otherwise identical generation events
  from collapsing during convergence while preserving legacy multiset behavior.
- Serialize history-export snapshots with the same cross-process lock used by
  audit writers so exported packages cannot observe a partially appended local
  history.
- Moved definitive PDF-preview rasterization and thumbnailing off the Tk main
  thread. Each worker opens its own PDFium document; stale resize/page results
  and stale worker failures are discarded before Tk image updates.
- Completed the durable physical-print lifecycle introduced during review:
  print intent is persisted before entering the Windows printer API, successful
  submission is marked explicitly, and ambiguous prepared jobs require an
  operator decision without automatically reprinting the document.
- Preserved compatibility for callers that omit `record_print(audit_id=...)`
  by generating a valid random print-job ID internally.
- Removed the Pillow `Image.getdata()` deprecation from icon verification
  tests.
- Expanded Windows regression coverage for uncertain controller mutations,
  crash-persistent create guards, backup/restore blocking, asynchronous PDF
  preview, physical-print recovery and modern/legacy workstation convergence.

## 4.3.0 - 2026-09-21

- Hardened settings loading against malformed numeric retention values. PDF
  retention accepts only integer 0..3650, log retention only integer 1..3650,
  booleans/strings/out-of-range values fall back to safe defaults, and startup
  cleanup failures are contained rather than terminating the console-less app.
- Added a fatal startup dialog so unexpected initialization failures are visible
  to the operator instead of closing the executable silently.
- Fully decode bounded PNG/JPEG logos during validation so truncated JPEG pixel
  data is rejected before rendering.
- Truncate oversized structure names inside the PDF label, matching the existing
  title and recipient width protections.
- Strengthened executable icon verification to require the first
  RT_GROUP_ICON/RT_ICON group to match the generated project icon and added
  direct PE-verifier regression tests.
- Corrected the bundled Noto Sans Italic copyright year to 2015-2022.

- Replaced ReportLab's standard Helvetica text with bundled Noto Sans
  Regular/Bold/Italic fonts so voucher PDFs render extended Latin, Greek,
  Cyrillic and Vietnamese text consistently on every workstation.
- Added pre-render glyph checks for operator-entered PDF text. Unsupported
  scripts or symbols now stop generation with an explicit Unicode warning
  instead of disappearing silently from the document.
- Normalize title, structure and recipient text to NFC before validation and
  rendering so decomposed accents pasted from macOS/web sources render
  predictably.
- Made PDF publication atomic: render to a same-directory temporary file,
  verify the PDF signature and replace the final archive path only after a
  complete render. Failed rendering leaves an existing final PDF untouched.
- Added conservative full-path handling for Windows: generated PDF paths are
  bounded to 240 characters, long recipient filename components use a stable
  hash suffix, and long visible recipient/Wi-Fi title text is truncated with an
  ellipsis rather than crossing the label boundary.
- Added configurable PDF archive retention under `Print/YYYY/MM`. Retention is
  disabled by default (`0`) so upgrades from 4.2.x never delete archived PDFs
  automatically; cleanup only removes expired files that also appear in valid
  audit history.
- Added startup cleanup for managed `.Voucher_*.tmp` renderer files older than
  24 hours and exclude those scratch files from backups, preventing sensitive
  crash leftovers from becoming permanent archive data.
- Bundled the Noto Sans OFL notice, documented the font copyright attribution,
  and added CI checks that the fonts and license material are present in the
  Windows distribution.
- Synchronized package and Windows executable metadata to 4.3.0.

## 4.2.1 - 2026-09-21

- Moved create, delete and print orchestration into the Tk-independent
  `workflows.py` application layer and added regression coverage for
  successful controller mutations followed by refresh failures.
- Hardened the public release workflow: stable publication now accepts only
  `X.Y.Z` versions, release notes follow the archive version, release write
  permissions are isolated to the publication job, and the Windows ZIP checksum
  is re-verified immediately before publishing.
- Updated Pillow from 11.3.0 to 12.3.0 and added a pinned `pip-audit` gate to
  the Windows CI. The Windows build also instantiates `ImageWin.Dib` so the
  Pillow integration used by native printing is exercised, not merely imported.
- Restricted custom-logo decoding to PNG/JPEG and validate logo content during
  selection, legacy migration, backup restore and PDF rendering. Render-time
  validation is cached by path, modification timestamp and file size.
- Set custom-logo limits to 8192 pixels per side, 40 megapixels and 25 MiB.
  These limits preserve legitimate large logos accepted by 4.2.0 while still
  bounding decompression and rendering cost. A valid restored logo above the
  current limits is omitted with an operator warning; corrupt or disguised
  image content still rejects the backup.
- Added documentation for the custom-logo security boundary and synchronized
  package/Windows version metadata for the 4.2.1 release.

## 4.2.0 - 2026-09-20

- First public stable release.
- Application behavior is unchanged from 4.2.0-rc.13; this release promotes
  the reviewed candidate, aligns stable version metadata and adds the
  public-release CI path that publishes the verified Windows ZIP together with
  `SHA256SUMS.txt`.

## 4.2.0-rc.13 - 2026-09-20

- Preserved only one forensic `settings.json.corrupt-*` copy per distinct
  malformed settings payload and excluded those forensic copies from backups.
- Validated `history.jsonl` parsing in restore staging before live data is
  changed.
- Added contextual 404 messages for vouchers that have already disappeared
  from the controller instead of suggesting that the API root is wrong.
- Normalized legacy Windows/POSIX paths portably when sanitizing backup logo and
  PDF filenames.
- Removed private-engineering CI/secret details and a private-only broken link
  from public README/SECURITY documentation.
- Documented that restore intentionally clears controller endpoint/certificate
  trust and that voucher creation is limited to batches of 50.
- Corrected the exporter idempotence comment and added a regression test that
  public docs remain free of private-CI plumbing.
- Documented the current WinAnsi PDF character-set limitation; full Unicode
  font embedding remains a planned 4.2.x improvement.

## 4.2.0-rc.12 - 2026-09-20

- Made public snapshot export idempotent when run inside an already-sanitized
  public repository.
- Prevented repeated export from duplicating the public release-branch workflow
  condition.
- Added a regression test requiring three consecutive public exports to be
  byte-for-byte identical.
- No application behavior changed from rc.10.

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
