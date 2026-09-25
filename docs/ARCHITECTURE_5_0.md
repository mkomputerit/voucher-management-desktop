# Voucher Management 5.0 architecture

Status: review implementation foundation.

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
`.db` file while WAL is active is not sufficient. The implementation should
use SQLite's backup API or a verified checkpoint/snapshot strategy, then verify
integrity and archive metadata before publishing the backup.

Automatic backup-on-close is enabled by default. A failed backup offers Retry,
Close anyway and Cancel; it must not trap the operator permanently.

## Reporting

Reports are calculated from durable atomic facts rather than stored aggregate
monthly counters. Required report dimensions include created/imported vouchers,
unique printed vouchers, physical copies, reprints, used vouchers, total
controller-reported uses, expired vouchers, printed-but-never-used vouchers,
never-printed vouchers, nominal assignment, controller and Windows operator.

Observation timestamps mean "the application observed this change at this
time". They must not be presented as an exact guest-use timestamp unless UniFi
explicitly supplied such a timestamp.

## Review gates

Before merging 5.0 work:

1. all existing 4.x regression tests must remain green;
2. SQLite schema/invariant tests must pass;
3. Windows build and executable checks must pass;
4. migration fixtures must cover valid, partial and ambiguous 4.x history;
5. backup/restore tests must include WAL consistency and rollback;
6. duplicate-print warning behavior must have UI-independent tests;
7. no credential field may enter persistent settings, database, logs or backup
   metadata.
