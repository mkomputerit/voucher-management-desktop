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
  data/history.jsonl
  data/history_secret.key
  data/pending_print_audit.json  # only while a physical print awaits resolution/audit
  data/pending_create_guard       # fixed marker while a create outcome is uncertain
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

## Deletion policy

Voucher Management is not a revocation console. A voucher may be deleted by the
application only before the first physical print recorded by the application.
After printing, lifecycle administration belongs to the controller
administrator.

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

The 4.2 adapter uses the documented UniFi Network integration API with an
X-API-Key header.

Connection flow:

1. validate the supplied API root and API key with GET /info;
2. enumerate sites through GET /sites;
3. automatically select the site only when the choice is unambiguous;
4. paginate the official hotspot voucher list;
5. map official fields into the controller-independent ApiVoucher model.

The API key exists only in the connected client instance. TLS certificate
verification is enabled by default. Local/self-signed compatibility uses
explicit SHA-256 certificate pinning before any authenticated request. It is an
explicit per-client setting and does not modify process-wide SSL defaults.

Voucher creation uses only documented request fields. Internal quota=0 means
"authorizedGuestLimit omitted" and is never transmitted as zero. Because create
is non-idempotent, a fixed-content durable marker is written before the POST.
Transport failures, ambiguous server errors and malformed successful responses
are classified as an uncertain mutation and are never replayed automatically.
The application may perform a safe GET reconciliation, but further creation
remains blocked until an operator-triggered refresh succeeds. Deletion uses one
documented UUID DELETE request per voucher rather than bulk filters.

## Packaging

The Windows build is PyInstaller onedir. The public executable name is
VoucherManagement.exe. Release builds are intended to be generated by GitHub
Actions and later submitted to SignPath Foundation using origin verification.


## Current site-selection boundary

The public 4.2 release line automatically selects a UniFi site only when site
discovery is unambiguous. Controllers exposing multiple sites are intentionally
rejected rather than guessed. Interactive multi-site selection is planned for a
later release.
