# Architecture

## Overview

Voucher Management is a Windows desktop application with four deliberately
separated responsibilities:

1. controller/API integration;
2. operator workflow and lifecycle policy;
3. PDF preview/printing;
4. persistent local audit data.

These boundaries allowed the 4.2 line to replace the legacy controller
integration with the documented Network API without rewriting PDF,
print-history or UI lifecycle policy.

## Runtime flow

```text
Operator
  |
  v
dialogs.py / ModernVoucherApp / VoucherApp
  |
  +--> workflows.py
  |      |
  |      +--> controller adapter --> voucher API
  |      +--> lifecycle policy
  |      +--> print-batch construction
  |
  +--> HistoryService --> HMAC audit rows
  |                     +--> HistoryKeyStore
  |
  +--> PDF renderer --> bundled Noto Sans --> atomic temp/replace --> Print/YYYY/MM
  |                                                      |
  |                                                      +--> retention whitelist from history
  |
  +--> PDF preview --> Windows printer
```

## UI / workflow boundary

`app.py` and `modern_app.py` remain the shared state/background coordinator and
the concrete Windows shell, but large operator workflows are composed from
focused Tk adapters rather than accumulated in those two classes:

- `voucher_creation_ui.py` owns guarded voucher creation;
- `controller_connection_ui.py` owns connection/TLS trust decisions;
- `voucher_deletion_ui.py` owns destructive delete/recovery interaction;
- `data_maintenance_ui.py` owns print-audit recovery, history exchange and
  backup/restore interaction;
- `dialogs.py` owns reusable modal input;
- `workflows.py` keeps operator-independent decisions and controller
  orchestration testable without Tk.

The mixins intentionally define no main-window layout. `ModernVoucherApp`
composes them around `VoucherApp`, so presentation, controller mutation and
maintenance responsibilities have explicit module boundaries without changing
the operator-visible workflow.

The extracted boundary now includes:

- `validate_create_params()` for voucher-creation field normalization and
  controller-limit validation;
- `verify_print_history_ready()`, `prepare_print_job()` and
  `execute_print_job()` for print preparation, deterministic archive naming,
  duplicate/reprint detection and history recording;
- `resolve_existing_pdf()` for portable/legacy archive lookup and verified
  voucher-to-PDF linkage.

Typed `ExistingPdfResolutionError.reason` values let Tk choose the appropriate
operator message without reimplementing archive policy or parsing exception
text. `CreateDialog.accept()`, `print_selected()` and
`open_existing_pdf()` therefore contain only widget access, operator dialogs
and background-task scheduling. Voucher-creation, copy-count and encrypted-data
password prompts share the consolidated `dialogs.py` implementation; password
validation delegates to the same crypto-layer policy used by backup/exchange.

## Responsive background operations

Blocking work runs on daemon worker threads through `background_tasks.py`.
Workers never access Tk widgets. They return either a value or an exception
through a thread-safe queue; one application-level coordinator in
`VoucherApp` polls that queue with `after()` and performs every UI update on
the main thread.

The coordinator is intentionally serialized: while one long operation is
active, another cannot start. The same path now covers controller connection
and voucher operations, PDF rendering/audit, 300-DPI printer rasterization and
submission, backup creation/validation/restore, and encrypted history
export/prepare-import/apply-import. The operator sees one indeterminate progress
indicator and the main action buttons are disabled during the operation.

Workflows that require an operator decision are split into phases rather than
showing Tk dialogs from a worker. For example, backup restore runs validation
in a worker, returns to Tk for confirmation, then starts a second worker for the
actual restore. History import follows the same prepare/confirm/apply pattern.

TLS certificate approval follows the same invariant. A connection worker may
return `UniFiCertificateTrustRequired` or `UniFiCertificateChanged`; only the
Tk callback displays the fingerprint confirmation dialog. If the operator
approves it, a new pinned client is created and a second worker attempt starts.
This keeps both Tk safety and the rule that the API key is never sent before
certificate trust has been established.

PDFium state is never shared across threads. The preview keeps only its
lightweight navigation document on Tk; each preview-raster worker opens a
separate `PdfDocument`, renders and thumbnails into a PIL image, and Tk applies
only the completed image. Stale resize/page generations are discarded. The
print worker independently opens another document before 300-DPI rasterization.

## Application workflow layer

`workflows.py` contains Tk-independent orchestration for controller refresh,
voucher creation, pre-delete revalidation, delete-policy evaluation, delete
fallback behavior and print-batch construction. Focused UI mixins collect
operator input and display results, while controller/cache decisions remain in
this layer so they can be exercised without a graphical session.

This boundary keeps controller orchestration independently testable while the
shared background coordinator handles execution policy for all long-running
operations.

## Persistent data

Program binaries are immutable and replaceable. Persistent data lives in the
per-user application-data root:

```text
VoucherManagement/
  config/settings.json
  data/voucher_management.db
  data/history.jsonl
  data/history_secret.key
  data/pending_print_audit.json      # only while a physical print awaits resolution/audit
  data/pending_create_guard          # anti-repeat barrier for an uncertain create
  data/pending_create_intent.json    # privacy-safe pre-POST recovery intent
  data/pending_create_reporting.json # confirmed create awaiting local classification
  logs/
  Loghi/
  Print/
```

Existing beta data under the legacy product directory is imported without
overwriting newer files.

The history key is portable application data so a complete backup can be
restored under another Windows account. It is not a controller credential.
Legacy DPAPI material is supported only as a one-time migration source.

## Audit history

Voucher codes are never stored in clear text in history.jsonl. HistoryService
uses HMAC-SHA-256 identifiers derived from the local portable history key.
Modern voucher-generation/print rows additionally carry an HMAC correlation
identifier derived from the verified UniFi Site UUID plus voucher UUID. This
prevents a future reused voucher code from being treated as the same modern
voucher while retaining code-HMAC compatibility for older history rows.

Generation and physical-print events are distinct:

- PDF generation records generated documents/copies;
- a Windows print submission records print jobs/physical copies;
- the first print timestamp is the lifecycle boundary used by the operator
  deletion policy.

Each physical print job also receives an internal audit ID. Before entering the
Windows printer API, HistoryService persists an atomic
`pending_print_audit.json` descriptor in the `prepared` state. After the
Windows submission returns successfully it is durably promoted to `submitted`
before the corresponding history rows are appended. The descriptor stores HMAC
voucher identifiers and print metadata, never clear voucher codes. It is removed
only after the complete print event has been read back and verified.

If the history write or verification fails, the preview exposes a **REGISTRA
STAMPA** recovery action. That action never resubmits the document to Windows:
it retries only the audit event with the same job ID and submission timestamp.
Recovery is idempotent, so rows already written before a partial failure are
preserved and only missing voucher rows are appended. Startup also attempts the
same recovery automatically. While a pending audit remains unresolved, new
physical printing, backup/restore and history exchange fail closed so a printed
voucher cannot silently reappear as unprinted state. The pending descriptor is
local recovery state and is excluded from portable backups.

## Multi-workstation boundary

Audit history, generated-document counters and physical-print counters remain
workstation-local application data. Voucher Management does not provide live
multi-workstation synchronization, locking or conflict resolution across PCs.

Operators can deliberately exchange audit state through an encrypted `.vmhx`
history package. The package contains only `history.jsonl`, the portable HMAC
identity and a small manifest; it does not contain PDFs, logos, controller
configuration, API keys or TLS trust. The package is always password-protected
with the same authenticated AES-GCM container used by encrypted backups.

Import is a merge rather than a replacement. Workstations with existing audit
rows must already use the same HMAC identity. A workstation with no audit rows
may explicitly adopt the imported identity after the UI displays that change.
A different identity behind existing local history is never merged.

New PDF-generation rows carry a random stable `event_id`; modern print rows
are reconciled by `print_job_id + voucher_id`. Matching modern identities are
idempotent and different data under the same identity is a blocking conflict.
Pre-4.3.1 generate rows and legacy print rows have no stable event ID and remain
reconciled as a multiset of complete canonical rows. This preserves backward
compatibility and intentional repeated labels while ensuring independent but
otherwise identical events created on separate workstations do not collapse.

The exchange workflow is manual by design. It improves deliberate transfer and
merging between operator stations without presenting itself as automatic
synchronization. A complete application backup remains the mechanism for moving
PDFs, logos, settings and the rest of application-managed state.

## PDF generation and archive

PDF text uses the bundled Noto Sans Regular, Bold and Italic TrueType fonts
rather than ReportLab's WinAnsi Helvetica fonts. The fonts are registered from
`assets/fonts` both in source runs and from the PyInstaller bundle, so extended
Latin, Greek and Cyrillic operator text does not depend on fonts installed on
the workstation. Before any temporary PDF is created, dynamic operator text is
checked against the font's `charToGlyph` map; unsupported scripts or symbols
stop generation with an explicit operator warning instead of disappearing from
the document.

`render_batch_pdf()` never writes directly to the final archive filename. It
renders into a same-directory temporary file, verifies the PDF signature and
publishes it with `os.replace()`. If rendering fails, an existing final PDF is
left untouched and the temporary file is removed.

`print_archive.py` creates the `Print/YYYY/MM` path and bounds the complete
absolute pathname to a conservative 240 characters. Long recipient components
are shortened with a stable hash suffix so distinct long names do not collapse
onto the same filename.

Custom-logo validation and the ReportLab `ImageReader` are cached together by
path, modification timestamp and file size. Repeated labels therefore reuse the
same validated image object, while replacing/changing the file invalidates the
cache automatically.

PDF retention defaults to 0 (keep forever) and is configurable up to 3650 days.
This non-destructive default preserves archived PDFs when upgrading from 4.2.x.
Cleanup requires both the managed filename/date layout and an output filename
present in verified audit history. The history rows themselves are never
deleted by PDF retention, and unrelated files under `Print/` are ignored.

A hard process termination can leave the same-directory renderer scratch file
behind after voucher data has already been written. On startup,
`cleanup_orphan_pdf_temps()` removes only hidden managed `.Voucher_*.tmp`
files under `Print/YYYY/MM` when they are older than 24 hours. Backup creation
also excludes those renderer scratch files.

## Deletion and revocation policy

Ordinary deletion is deliberately narrow: it is a preparation-error correction,
not a general revocation command. A voucher must be positively unused and
positively not printed, with complete local alignment, before the application
offers ordinary deletion. The operator must supply a reason and every selected
voucher is re-read from UniFi immediately before the destructive operation.

One controlled exception exists for an unusable controller-created voucher
whose UniFi description is empty: if usage has been positively observed as zero,
no verified print exists, alignment is incomplete and print state remains
unknown, the operator may remove it with a mandatory reason rather than invent
recipient or print history.

Printed vouchers are handled by the separate security-revocation workflow.
Printed vouchers left without positive-use evidence beyond the configured
threshold are proposed for review, never revoked automatically. Each candidate
is read directly by UUID immediately before DELETE. The local voucher code,
recipient/description, nominality, notes and complete audit are preserved after
revocation.

A list omission is never sufficient by itself to prove that a voucher has been
deleted. The first omission is recorded only as an internal suspicion; after a
later complete omission Voucher Management performs a direct UUID read. Only a
definitive voucher-not-found result is accepted as external deletion evidence.
A confirmed DELETE response, on the other hand, is itself positive mutation
evidence and is persisted immediately without waiting for read-after-write list
consistency.

## Backup

Backup format 2 stores config, data, generated PDFs and custom logos in one ZIP.
The portable history key is included naturally inside data/. Restore validates
archive paths and size limits before extraction and creates a rollback copy.

Operators may alternatively wrap the same logical format-2 snapshot in the
authenticated `.vmbk` container. A 256-bit AES-GCM key is derived from the
operator password with Scrypt (16-byte random salt, N=131072, r=8, p=1); each
backup also receives a random 12-byte GCM nonce. The fixed container header is
authenticated as additional data. Passwords are never stored.

Encrypted creation streams the logical ZIP directly into AES-GCM, so no
plaintext archive is written during backup creation. Validation/restore decrypt
into an OS-managed anonymous/auto-delete seekable temporary file rather than a
named ZIP below the application-data tree. This provides the random access
required by `zipfile` without leaving a recoverable
`voucher-management-decrypted-*.zip` pathname after a process interruption.
GCM authentication and ZIP validation both complete before rollback/live-data
replacement begins. Wrong passwords and modified containers therefore fail
before application state changes.

Legacy unencrypted format-1 and format-2 ZIP backups remain readable.

## Network adapter

The 5.1 adapter uses the documented UniFi Network integration API with an
X-API-Key header.

Connection flow:

1. validate the supplied API root and session-only API key with GET /info;
2. enumerate sites through GET /sites;
3. on first association select automatically only when discovery is unambiguous;
4. persist the verified Site UUID as non-secret controller identity;
5. on later connections require that exact Site UUID when the same API root
   exposes multiple Sites;
6. paginate the official hotspot voucher list with strict count/offset/total
   consistency checks and duplicate-UUID detection;
7. map official fields into the controller-independent ApiVoucher model.

The durable controller identity is therefore API root plus Site UUID. A legacy
profile with no Site UUID may adopt the first uniquely verified Site in place,
preserving its local history; a different non-empty Site UUID at the same URL is
never silently merged into that archive.

The API key exists only in the connected client instance. TLS certificate
verification is enabled by default. Local/self-signed compatibility uses
explicit SHA-256 certificate pinning before any authenticated request. It is an
explicit per-client setting and does not modify process-wide SSL defaults. A
backup restore clears the saved live API root, Site UUID and certificate pin so
restored state never pre-authorizes a controller target.

Voucher creation uses only documented request fields. Internal quota=0 means
"authorizedGuestLimit omitted" and is never transmitted as zero. Because create
is non-idempotent, an anti-repeat guard and a privacy-safe recovery intent are
written before the POST. The recovery intent contains no voucher code, recipient
plaintext, API key or controller URL. Transport failures, ambiguous server
errors and malformed successful responses are classified as an uncertain
mutation and are never replayed automatically. After a later authoritative
snapshot, compatible new vouchers are only proposed to the operator: even an
exact candidate set is never associated automatically.

DELETE requests use one documented UUID endpoint per voucher. Transport/server
ambiguity is typed as an uncertain mutation and is not blindly replayed.
Confirmed DELETE responses are persisted immediately; uncertain results remain
pending until later positive presence or direct UUID absence resolves them.

Retryable read transport loss during an established session enters a finite
automatic reconnect state. Home becomes non-live and the status indicator is
orange while retries are scheduled; after the retry budget is exhausted the
state becomes red and further reconnection requires an explicit operator action.

## Packaging

The application payload is a PyInstaller onedir build whose executable is
`VoucherManagement.exe`. Managed deployment additionally produces a
self-contained elevated `VoucherManagement-Setup-<version>.exe` bootstrapper.
The bootstrapper embeds the verified onedir payload and invokes the reviewed
shared-install PowerShell path from a protected Program Files staging area.
Windows CI tests the portable payload, shared ACLs, Setup compilation, Setup
icon/metadata and an actual silent install/uninstall cycle before artifacts are
retained. Public release artifacts remain unsigned unless the code-signing
policy is explicitly updated.


## Current site-selection boundary

A previously unknown controller still requires unambiguous Site discovery; the
application does not ask an operator to guess between arbitrary Sites. Once a
Site has been verified, its UUID is stored with the controller identity and
future sessions reconnect only to that Site. If that stored Site UUID is no
longer returned by the same API root, connection fails closed rather than
silently switching the archive to another Site.

Interactive first-association selection among multiple unknown Sites remains
outside the current release scope.
