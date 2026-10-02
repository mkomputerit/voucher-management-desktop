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

Voucher creation uses two privacy-safe crash barriers before a non-idempotent
POST can be sent. `pending_create_guard` is only a fixed anti-repeat marker.
`pending_create_intent.json` contains the verified Site identity, the baseline
voucher UUID set and non-secret creation parameters needed for later
correlation; the recipient is represented only by an HMAC. Neither file stores
the API key, controller address, voucher code or recipient plaintext. If UniFi
confirmed the create but local reporting persistence is incomplete,
`pending_create_reporting.json` stores only confirmed voucher UUIDs and the
local classification needed for idempotent recovery. Backup/restore is blocked
while an unresolved create intent exists so moving local state backwards cannot
erase an ambiguous remote mutation.

Print history deliberately stores the recipient label in clear text together
with audit metadata so an operator can identify who a generated voucher sheet
was prepared for. Voucher codes themselves are not stored in clear text in
`history.jsonl`; they are represented by HMAC-SHA-256 identifiers. Recipient
labels should therefore be treated as local operational data and are included
when application data is backed up.

Starting with 5.1, each generated voucher label includes its recipient inside
the cutting border, next to the access code. The recipient therefore remains
on the physical voucher handed to the guest, as well as in the archived PDF.
This includes any name or room reference entered as the recipient. The print
preview explains this before physical printing. Operators should use only the
recipient information they intend to hand to the guest. Existing archived PDFs
retain their original layout; this change does not rewrite them.

Starting with the 5.1 reporting revision, voucher creation also offers an
explicit local **Voucher nominale** classification. The flag is stored in the
local SQLite archive for administrative reporting and is not transmitted to
UniFi. It is independent from the recipient text: a room/reference label can be
non-nominal and a person's name can be nominal only when the operator selects
the flag. Existing vouchers for which that choice was never recorded remain
"non classificati"; the application does not infer nominal status from names.


Administrative report exports use two privacy levels. **Riepilogo storico** is
aggregate-only and does not include per-voucher recipient data, UniFi
descriptions, local notes or Windows print-operator identities. Detailed PDF/CSV
reports do not expose the clear voucher code for routine administrative
purposes, but may include the immutable UniFi voucher UUID, controller name,
controller-owned description/destinatario, local notes, timestamps, nominal
classification and Windows operator identities associated with printing.
Voucher Management does not maintain a second editable local recipient field.
These detailed exports are therefore sensitive local operational data and
should be stored and shared with the same access controls used for the
application database and backups.

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

From 5.1, operators choose an unencrypted ZIP or a password-protected `.vmbk`
for each normal manual or shutdown backup. Password protection is selected by
default, so producing a readable ZIP requires an explicit opt-out. The dialog explicitly identifies
unencrypted copies: anyone with file access can read the database, voucher
codes, recipients and PDFs. Selecting password protection encrypts and
authenticates the complete portable snapshot, including the local history key.
The default backup folder is stored as an operator preference. A folder chosen
in the backup dialog applies only to that copy and never changes the default.
Machine-specific backup destinations are cleared in exported settings so a
restore cannot silently reuse another workstation's destination.
Backup passwords are used only for the active create/restore operation and are
not stored in settings, logs or backup metadata. Encrypted validation/restore uses
an OS-managed anonymous/auto-delete temporary file for decrypted ZIP bytes,
rather than a named plaintext archive under the application-data directory.

Voucher Management 5.1 keeps local retention review separate from privacy
minimization. The operator must explicitly choose the age threshold used to
identify old records for review. Used, physically printed or PDF-generated
vouchers remain protected by the conservative candidate rules. In this release
privacy minimization is disabled: retention review does not replace voucher
codes, remove recipient labels, clear nominal classification or erase local
notes.

Security revocation is a separate controller-side operation. A printed voucher
that remains without positive-use evidence beyond an operator-selected threshold
may be proposed for revocation. Immediately before deletion Voucher Management
reads that voucher again from UniFi and refuses the operation if the live state
is no longer eligible. After revocation the complete local historical record,
including the voucher code and local metadata, remains available for audit and
reporting. A DELETE confirmed by UniFi is persisted immediately. If the DELETE
outcome is uncertain, later reconciliation does not treat a missing list row as
proof: absence is accepted only after the direct voucher-UUID lookup returns the
typed not-found result.

Pre-SQLite legacy ZIP import is an explicit historical-recovery operation.
Verified legacy PDF-generation or print evidence is retained as document/print
history. A legacy `generate` event proves that Voucher Management generated a
document for the voucher; it does not prove that the application originally
created that voucher on the UniFi controller. Imported recipient and audit data
remain in the local archive; no current-release retention action minimizes them.

Manual history exchange packages (`.vmhx`) are always password-protected.
They contain local audit rows and the portable history key needed to preserve
HMAC correlation across workstations, but do not contain generated PDFs,
controller settings, API keys or TLS trust. Exchange passwords are not stored.

No information is transferred to systems other than those explicitly selected/configured by the user as part of the application's requested operation.

Third-party platform/API use remains subject to the platform provider's applicable privacy terms.
