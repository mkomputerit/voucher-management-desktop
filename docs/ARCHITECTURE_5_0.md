# Voucher Management 5.0 architecture

Status: review implementation foundation.



## Report credential exposure policy

Voucher codes are reusable network credentials and are therefore excluded by
default from every report. Summary and audit reports cannot expose a clear code
even if a caller requests it. The only permitted exception is an explicit
operational-handoff report requested by the operator.

The policy boundary already exists in `report_policy.py` and has dedicated
unit tests, but report rendering is not yet implemented in this foundation
milestone. Therefore no current PDF/CSV renderer is claimed to enforce it.
When reporting is introduced, every renderer/exporter that can emit voucher
data must call this policy module, and integration tests must prove that summary
and audit outputs cannot bypass it.

## Data ownership

Voucher Management 5.0 separates controller facts from local application facts.

- **UniFi Network is authoritative** for voucher creation metadata, activation,
  `authorizedGuestCount`, expiry and controller-side limits.
- **Voucher Management is authoritative** for PDF generation, physical printing,
  reprints, Windows operator identity and application sessions.
- A controller disappearance never deletes local historical data.
- A failed synchronization never implies that a voucher disappeared.

The local SQLite database is therefore a durable historical layer, not a cache
that can be rebuilt by deleting it.

## Database

The initial schema is implemented in
`src/voucher_management/database.py`. It uses SQLite foreign keys, WAL mode,
a busy timeout, explicit transactions and schema versioning.

Main entities:

| Entity | Purpose |
| --- | --- |
| `controllers` | Non-secret controller profiles |
| `vouchers` | Latest controller-observed voucher state plus local annotations |
| `sync_runs` | Outcome boundary for each controller synchronization |
| `voucher_sync_observations` | Meaningful controller-state changes |
| `voucher_events` | Application/operator lifecycle events |
| `print_jobs` | One Windows printer submission lifecycle |
| `voucher_prints` | Per-voucher physical-print and reprint audit |
| `application_sessions` | Last-use and Windows-session audit |
| `installation_profile` | Installation/PDF identity |
| `retention_policy` | Conservative cleanup policy |
| `backup_history` | Disaster-recovery audit |

API keys, passwords and authentication tokens have no schema field and must
remain memory-only.

Unlike the 4.x HMAC-only audit history, the 5.0 operational voucher table stores
the voucher code in plaintext. This is required for durable local operations
after controller-side deletion and is an explicit security trade-off. The
database is sensitive operational data; SECURITY.md defines the compensating
logging, backup and Windows ACL requirements.

## Print and reprint safety

A physical print is a lifecycle boundary. The first successful print creates
sequence 1. Every subsequent physical print is a reprint and must be preceded
by an operator warning explaining that another copy of the same voucher is
being produced and should only be made when the previous copy is lost or the
operator is otherwise certain a duplicate is required.

The UI should show the previous print count and latest print time. Batch
printing must show one consolidated warning when any selected voucher was
already printed.

The durable `pending_print_audit` mechanism from 4.x remains required around
the Windows printer boundary. SQLite counters must never be used to guess
whether an ambiguous OS print submission physically occurred.

Milestone A now bridges the two audit layers with the same stable print
`audit_id`. After Windows confirms submission, the HMAC history is written
idempotently while the pending marker remains durable; SQLite then records the
physical-print job and per-voucher reprint sequence; only after both stores
verify the same job is the pending marker removed. A crash at any point therefore
leaves a retryable marker rather than silently losing the SQLite print audit.
Startup and manual recovery resolve the HMAC-only marker exclusively against
voucher codes independently known from the local SQLite snapshot. No clear
voucher code is added to `pending_print_audit.json`.

After a confirmed Windows submission the printed vouchers are removed from the
current UI selection. An ambiguous Windows submission does not claim success and
still requires the existing explicit operator recovery decision.

## Retention

Default policy:

- used vouchers are protected;
- physically printed vouchers are protected;
- only never-used, never-printed vouchers are candidates for age-based cleanup;
- the default candidate age is 180 days;
- cleanup is review-driven, not silent deletion.

The first-run wizard explains that recommended retention defaults are already
configured and should be changed only when specifically required. Continue is
the primary action; advanced editing is secondary.

## Delivery milestones

The 5.0 transition is intentionally split into independent engineering gates so
a data-model regression is not confused with a Windows deployment/ACL problem.

### Milestone A — SQLite wiring, still single-user

SQLite first replaces the operational persistence path while data remains under
the existing per-user LocalAppData root. Controller synchronization, printing,
reprint decisions and reporting queries must use the real database path before
the shared-workstation deployment model is introduced.

The SQLite path must not become the automatic upgrade path for existing 4.x
users until Milestone B can migrate or preserve their existing evidence.

### Milestone B — explicit 4.x migration

Migration is a separate transactional workflow with fixtures for complete,
partial, ambiguous and corrupt legacy data. It validates the portable history
identity and may associate an HMAC history row with a plaintext voucher only
when that voucher code is independently known and recomputes to the exact HMAC.

The first Milestone B implementation boundary is deliberately read-only:
`legacy_migration.py` validates the history fingerprint/key pair, parses the
entire legacy audit file fail-closed, recomputes HMACs only from independently
known voucher candidates and returns an immutable migration plan. A row is
resolved only when exactly one voucher identity matches. Zero matches remain
unresolved evidence; multiple voucher identities remain ambiguous evidence.
Deterministic 5+5 display formatting may be derived from an independently known
10-character code because that is the representation 4.x used when generating
the audit HMAC.

The planner itself remains read-only: it does not write SQLite, alter
`history.jsonl`, rotate the history key or enable automatic migration at
startup.

The next Milestone B boundary persists that verified plan into schema version 2.
`migration_runs` records each apply transaction and `legacy_audit_events`
stores the original HMAC evidence with an explicit
`RESOLVED`/`AMBIGUOUS`/`UNRESOLVED` state. A resolved row receives a
foreign-key link only after the controller identity, UniFi voucher id and code
are revalidated inside the same transaction. Existing resolved evidence may
never regress to an unresolved/ambiguous state. Reapplying the same migration
identity is idempotent, while a later plan may promote previously unresolved
evidence when an independently known voucher becomes available.

Schema 1 upgrades to schema 2 inside an explicit SQLite transaction so a DDL
failure cannot leave a partial migration schema.

Resolved evidence can then be materialized idempotently into operational facts.
Legacy PDF-generation rows become `voucher_events` with source `MIGRATION`.
Resolved physical-print rows create or verify `print_jobs`/`voucher_prints`;
an existing Milestone A print job with the same audit id is verified and reused
rather than duplicated. After inserting older historical prints, print
sequences are deterministically renumbered so sequence 1 remains the earliest
known physical print. Ambiguous and unresolved evidence is never materialized.

Very old 4.x rows may predate stable `event_id`/`print_job_id` fields. Those
rows remain supported through deterministic synthetic identities derived from
their immutable legacy evidence. For print rows without `print_job_id`, job
grouping cannot be proven from the source file, so each legacy print row becomes
its own synthetic print job rather than guessing that multiple rows belonged to
one physical submission. Generate rows without `event_id` likewise receive a
deterministic synthetic event identity. Both fallbacks are covered end-to-end
and remain idempotent across retries.

A migration run is `EVIDENCE_READY` after the HMAC evidence transaction and
becomes `COMPLETED` only in the materialization transaction. A materialization
failure therefore leaves no partial operational print/event facts and can be
retried from the preserved evidence.

The core execution path requires a verified encrypted `.vmbk` safety backup
before either phase begins and performs a final SQLite integrity check.
`history.jsonl` and its key are never rewritten by migration.

Unresolved rows are preserved as legacy audit evidence. They are never guessed
into a voucher record. The Milestone B UI exposes migration only as an explicit
operator action under data maintenance: it uses voucher identities already
known to SQLite, requires the verified legacy HMAC identity and an encrypted
safety-backup password, shows resolved/ambiguous/unresolved counts before
execution, and never enables automatic startup migration.

### Milestone C — shared Windows deployment

Managed shared deployments opt into shared storage through the
deployment-script-owned `voucher-management-deployment.json` marker beside
the executable. Milestone C deliberately uses an elevated PowerShell deployment
script rather than claiming MSI/EXE installer semantics: it does not register
an Installed Apps entry or an UninstallString in Windows. Portable
and source execution remain per-user for compatibility; the marker is therefore
the explicit trust boundary that switches the runtime to
`%ProgramData%\VoucherManagement`. A malformed/unsupported marker fails
closed instead of silently falling back to another data root.

The elevated Windows deployment script copies the reviewed onedir build under
`%ProgramFiles%\Voucher Management`, creates the local
`Voucher Management Operators` group, adds the selected operator and applies
ProgramData ACLs by SID. Before granting application rights, the installer
resets stale explicit ACL state from a pre-existing data tree, removes
inheritance from broader ProgramData ACLs, and rebuilds the allowed set:
SYSTEM and BUILTIN\Administrators receive Full Control, while the dedicated
operator group receives an explicit inheritable Modify ACE. The deployment script then
enumerates the root and all existing descendants and fails closed if any
Allow ACE belongs to a principal outside those three SIDs. Ordinary users
outside that group therefore receive no application-data grant from Voucher
Management, including upgrade scenarios with previously permissive explicit
ACEs. The runtime never broadens ACLs itself.

The lifetime application guard lives directly below the active data root rather
than inside `data/`. In installed mode this means every authorized Windows
session locks the same ProgramData file, so Fast User Switching cannot create
two concurrent application writers. Keeping the guard outside managed backup
directories also prevents restore from trying to replace an open lock file.

Migration from the current user's old LocalAppData tree is explicit, never
automatic. It is offered only in shared mode and only while the ProgramData
archive is still operationally pristine. The source tree is held against both
old and new instance-lock locations, its history lock is held while snapshotting,
and the transfer is performed through an authenticated encrypted `.vmbk`
backup. The shared SQLite handle is closed before restore; the existing
WAL-safe restore path supplies rollback. The original per-user tree is left
unchanged and the application restarts after a successful migration.

The deployment script does not auto-launch after adding a user to the local operator
group because Windows group membership is reflected in a newly created logon
token. A newly added operator may therefore need to sign out and sign in before
the first shared-data launch.

### Milestone A.1 — per-user single-instance gate

Before shared deployment work begins, the current LocalAppData application must
hold one OS-backed application guard for the lifetime of the Tk root window.

Exit criteria are executable tests, not only documentation:

- a concurrent second process is rejected immediately;
- normal shutdown releases ownership for the next launch;
- abrupt owner termination releases ownership through the operating system;
- the guard remains scoped to the current user's LocalAppData root.

This guard is intentionally distinct from the short history-write lock. Milestone
C will replace its per-user scope with machine-wide ownership across Windows
sessions.

## Shared Windows workstation

5.0 targets one Windows workstation with multiple Windows accounts. Installed
program files belong under Program Files; shared mutable application data will
belong under ProgramData. All authorized operators use one SQLite database.

The Windows shell must enforce one application writer per machine/shared
database, including Fast User Switching sessions. SQLite is not to be placed on
an SMB/network share.

Diagnostic logs remain privacy-conscious and do not gain voucher codes,
controller addresses or Windows usernames merely because the application audit
database records operator identity.

## Migration boundary

4.3.3 data is currently per-user under LocalAppData. Migration to 5.0 must be
explicit and transactional. It must preserve valid print history, its HMAC
identity, settings, managed logos and known PDF archive data without inventing
facts that are absent from legacy history.

Legacy `history.jsonl` identifies voucher codes by HMAC. Migration may only
associate a historical row with a plaintext voucher when the existing history
identity is valid and the code is independently known. Ambiguous legacy data
must remain preserved as legacy audit evidence rather than being guessed into a
new voucher record.

4.x protected backups remain an import source. A 5.0 restore/import workflow
must validate the old archive before changing live data and create a safety
backup before replacing a live 5.0 database.

## Backup

SQLite backups must be transactionally consistent; copying only the main
`.db` file while WAL is active is not sufficient. Milestone A uses
`sqlite3.Connection.backup()` to build a standalone committed snapshot,
verifies `PRAGMA integrity_check`, records the snapshot SHA-256 and schema
version in the manifest, and excludes live `-wal`/`-shm`/`-journal`
sidecars. Validation repeats the integrity/hash/schema checks before restore.
The pre-restore rollback snapshot uses the same SQLite-safe mechanism.

Automatic backup-on-close is enabled by default. Because the 5.0 SQLite
database contains clear voucher codes, the normal 5.0 backup path is encrypted
and password-protected. Unencrypted ZIP import remains supported for legacy
compatibility, but the 5.0 UI must not present plaintext backup as the default
or recommended choice. A failed backup offers Retry, Close anyway and Cancel;
it must not trap the operator permanently.

## Reporting

Reports are calculated from durable atomic facts rather than stored aggregate
monthly counters. Required report dimensions include created/imported vouchers,
unique printed vouchers, physical copies, reprints, used vouchers, total
controller-reported uses, expired vouchers, printed-but-never-used vouchers,
never-printed vouchers, nominal assignment, controller and Windows operator.

Observation timestamps mean "the application observed this change at this
time". They must not be presented as an exact guest-use timestamp unless UniFi
explicitly supplied such a timestamp.

## Locking transition

The 4.x history lock no longer recovers a supposedly stale lock by checking a
timestamp and unlinking the path. That pattern had a TOCTOU window in which a
new owner's lock could be deleted. The lock now uses an OS advisory lock whose
ownership is released by the operating system on process exit; the lock path is
kept persistent to avoid creating independently lockable filesystem objects.

This audit-write lock is not itself the future application single-instance
guard. Milestone A introduces a per-user application guard; Milestone C later
elevates that concept to a machine-wide guard across Windows sessions.

## Review gates

Before merging 5.0 work:

1. all existing 4.x regression tests must remain green;
2. SQLite schema/invariant tests must pass;
3. Windows build and executable checks must pass;
4. migration fixtures must cover valid, partial and ambiguous 4.x history;
5. backup/restore tests must include WAL consistency and rollback;
6. duplicate-print warning behavior must have UI-independent tests;
7. no credential field may enter persistent settings, database, logs or backup
   metadata;
8. the Milestone A.1 single-instance tests must cover concurrent launch, clean
   shutdown and abrupt owner termination.
