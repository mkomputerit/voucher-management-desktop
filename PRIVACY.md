# Privacy

This application is designed to operate directly between the user's Windows computer and the network controller/API endpoint explicitly configured by the user.

The project itself does not operate a telemetry, analytics, advertising or cloud data-collection service.

The application may process:
- controller/API endpoint configured by the user;
- operator authentication material required for the active session;
- hotspot voucher identifiers and recipient labels returned by the configured controller;
- local PDF/print audit metadata;
- generated PDFs and user-selected logo files.

Authentication secrets must not be written to application logs, settings, print history or backups.

A local `pending_create_guard` may temporarily exist when a voucher-create
request has an uncertain remote result. It contains only a fixed state marker:
no API key, controller address, voucher code, recipient or creation parameters.
It is excluded from portable backups and is removed only after a definitive
result or a successful operator-triggered controller refresh.

Print history deliberately stores the recipient label in clear text together
with audit metadata so an operator can identify who a generated voucher sheet
was prepared for. Voucher codes themselves are not stored in clear text in
`history.jsonl`; they are represented by HMAC-SHA-256 identifiers. Recipient
labels should therefore be treated as local operational data and are included
when application data is backed up.

If a physical print was submitted but its audit write has not completed, a
local `pending_print_audit.json` file temporarily stores only HMAC voucher
identifiers plus print metadata needed for idempotent recovery. It does not
contain voucher codes in clear text and is not included in portable backups.

Backup operations are also represented in the local SQLite
`backup_history` audit. Successful entries contain timestamps, a purpose
category, the backup filename basename, SHA-256 of the final backup container,
backup-format version and SQLite schema version. Failed entries retain no
unverified hash/format/schema metadata. Full Windows destination paths,
passwords and exception messages are not stored; failure detail is limited to
the exception type.

The normal backup workflow creates only password-protected `.vmbk` files.
Legacy unencrypted ZIP archives remain readable for restore compatibility but
are no longer offered as normal backup output. The encrypted form protects the
complete portable snapshot, including generated PDFs and the local history key.
Backup passwords are used only for the active create/restore operation and are
not stored in settings, logs or backup metadata. Encrypted validation/restore uses
an OS-managed anonymous/auto-delete temporary file for decrypted ZIP bytes,
rather than a named plaintext archive under the application-data directory.

Voucher Management 5.0 also applies review-driven local retention.
Used, physically printed or PDF-generated vouchers are never retention
candidates. Old unused vouchers with no generated PDF are proposed only after
they are absent from a complete controller snapshot. Nothing is minimized
automatically. When an
operator explicitly archives a candidate, the durable historical row remains
but the reusable voucher code, recipient label, nominal assignment and free-text
notes are removed.

Manual history exchange packages (`.vmhx`) are always password-protected.
They contain local audit rows and the portable history key needed to preserve
HMAC correlation across workstations, but do not contain generated PDFs,
controller settings, API keys or TLS trust. Exchange passwords are not stored.

No information is transferred to systems other than those explicitly selected/configured by the user as part of the application's requested operation.

Third-party platform/API use remains subject to the platform provider's applicable privacy terms.
