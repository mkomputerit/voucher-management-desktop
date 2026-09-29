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
the flag. Existing vouchers for which that choice was never recorded initially remain
"non classificati"; the application does not infer nominal status from names.
After a successful UniFi synchronization, an operator may explicitly enrich a
voucher with a separate local recipient, free-text local notes and a
nominal/non-nominal/unclassified choice. The UniFi description, voucher code
and UniFi identifier shown in that workflow are read-only and are never changed
by the local edit. Local recipient and notes are included in the SQLite database
and therefore in application backups; operators should treat them as potentially
personal data and record only what is administratively necessary.

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

Voucher Management 5.0 also applies review-driven local retention.
Used, physically printed or PDF-generated vouchers are never retention
candidates. Old unused vouchers with no generated PDF are proposed only after
they are absent from a complete controller snapshot. Nothing is minimized
automatically. When an
operator explicitly archives a candidate, the durable historical row remains
but the reusable voucher code, recipient label, nominal assignment and free-text
notes are removed.

Pre-SQLite legacy ZIP import is an explicit historical-recovery operation.
Voucher rows backed by verified legacy PDF-generation or print evidence are
protected from ordinary retention so that imported audit evidence is not
silently disconnected from its subject. Those imported rows, including any
recipient label recovered from the legacy history, therefore remain in the
local archive unless a future explicit archive-removal workflow is used.
Importing an old ZIP can also deliberately restore clear voucher/recipient data
that had already been minimized in the current database when no durable
verifiable identity remains to prove that the ZIP record is the same minimized
voucher. The import confirmation warns about this before any live data changes.

Manual history exchange packages (`.vmhx`) are always password-protected.
They contain local audit rows and the portable history key needed to preserve
HMAC correlation across workstations, but do not contain generated PDFs,
controller settings, API keys or TLS trust. Exchange passwords are not stored.

No information is transferred to systems other than those explicitly selected/configured by the user as part of the application's requested operation.

Third-party platform/API use remains subject to the platform provider's applicable privacy terms.

### Anteprima report

I report PDF vengono generati in una cartella temporanea per la consultazione.
La chiusura dell'anteprima elimina questa copia temporanea dopo la conclusione
delle operazioni in corso; un arresto anomalo del processo può lasciare residui
nella cartella temporanea del sistema. Salva PDF conserva una copia nella posizione
scelta dall'operatore. La stampa di un report non alimenta lo storico di stampa
dei voucher. I report di dettaglio possono contenere descrizioni UniFi, destinatari locali e
operatori; le note locali non vengono esportate nei report ordinari. Descrizione
UniFi e destinatario locale restano colonne distinte e non vengono dedotti l'uno
dall'altro. Il riepilogo resta aggregate-only: il dataset passato al renderer
non conserva righe personali quando deve produrre soltanto aggregati. Le regole
di occultamento dei codici non cambiano.
