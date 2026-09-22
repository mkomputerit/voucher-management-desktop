# Security

## Security goals

Voucher Management is designed to avoid turning operational network credentials
or guest voucher data into repository, log or backup secrets.

The project follows these rules:

- no passwords or API keys in source control;
- no authentication secrets in settings, logs, print history or backups;
- voucher codes are not stored in clear text in `history.jsonl`;
- no controller address in diagnostic logs;
- non-idempotent voucher creation is protected by a crash-persistent
  anti-repeat marker; uncertain POST outcomes are never replayed automatically;
- destructive voucher deletion requires explicit confirmation;
- vouchers are not deletable by the application after their first recorded
  physical print;
- backup extraction rejects traversal paths and unsupported archive content;
- custom logos are decoded only as PNG/JPEG and are bounded by file size,
  dimensions and pixel count before PDF rendering;
- release binaries are built automatically rather than from a maintainer
  workstation;
- the reviewed Windows dependency set is version-pinned and SHA-256 pinned;
  CI installs it with pip `--require-hashes --no-deps` before testing/building.

## Authentication

Voucher Management 4.3.1 uses the documented UniFi Network API and X-API-Key
authentication.

The API key is accepted only for the active connection. It is never persisted
to settings, logs, backups or source control, and the visible UI field is
cleared immediately after each connection attempt.

Legacy saved usernames/password workflow is not part of the current settings
schema.

## TLS

The official API client verifies TLS certificates by default.

For local/self-signed controller deployments, Voucher Management never sends
the API key over a permanently unverified connection. When normal certificate
validation fails, the application obtains the peer certificate without sending
HTTP credentials, displays its SHA-256 fingerprint for explicit operator
verification, and stores that fingerprint only after approval.

Subsequent authenticated connections require an exact fingerprint match before
the API key is sent. If the certificate changes, the application obtains the
new peer fingerprint without sending HTTP credentials and shows both the
previous and new SHA-256 fingerprints. The stored pin is replaced only after
explicit operator approval; declining leaves the previous pin unchanged.

## Voucher audit privacy

`history.jsonl` does **not** store voucher codes in clear text. It stores
HMAC-SHA-256 identifiers derived from the voucher code and the local history
key in `data/history_secret.key`.

This HMAC design prevents casual disclosure from `history.jsonl` alone; it is
not encryption of the voucher code. The voucher code space is small enough to
be enumerable, and a backup intentionally carries the history key so audit
correlation survives restore. Anyone who obtains both history and key should
therefore be treated as having sensitive audit material.

Generated files under `Print/` contain the actual printable voucher codes.
Application backups can include those PDFs together with history/key data, so a
backup must be protected as sensitive operational data.

After Windows accepts a physical print job, the application persists a local
`pending_print_audit.json` descriptor before updating `history.jsonl`. The
descriptor contains HMAC voucher identifiers rather than clear voucher codes.
It supports idempotent recovery after interruption and is deleted only after the
print event verifies successfully. While it exists, new physical prints,
backup/restore and history exchange are blocked to prevent lifecycle state from
moving backwards. This descriptor is excluded from portable backups.

## Diagnostic logs

Application logs contain timestamps, severity, OS family/architecture and
operation identifiers. Unexpected UI callbacks record only the exception type,
not the traceback or exception text. Logs must not contain:

- hostnames or usernames;
- controller addresses;
- voucher codes;
- recipients unless explicitly required for an error report;
- passwords, API keys, cookies or CSRF tokens.

## Custom logo validation

Custom logos are treated as untrusted image input. Voucher Management accepts
only PNG and JPEG content and does not trust the filename extension to select a
decoder. A file renamed from another image format is therefore rejected.

The current limits are 8192 pixels per side, 40 megapixels and 25 MiB. Logo
content is validated when selected, when legacy settings are migrated, while a
backup is still in restore staging, and immediately before ReportLab renders
the image into a PDF. Render-time validation and the reusable ReportLab image
object are cached using file path, modification timestamp and size so repeated
labels do not repeatedly validate/decode the same unchanged logo. A configured
logo rejected during startup migration is cleared with an explicit operator
warning rather than disappearing silently.

For compatibility with older releases, a syntactically valid PNG/JPEG found in
a backup but exceeding the current limits does not block restoration of
settings, audit history or the portable history key. That logo is omitted and
the operator is warned. Corrupt, unsupported or disguised image content still
causes the restore to fail closed.

## Backup/restore

Backups contain application-managed settings, audit data, the portable history
key, generated PDFs and custom logos. They can therefore contain recipient
labels and voucher codes in the generated PDFs.

Voucher Management can create an optional password-protected `.vmbk` container.
The logical ZIP snapshot is streamed directly into AES-256-GCM rather than
being written to a plaintext intermediate archive. The 256-bit AES key is derived
from the operator password with Scrypt (random 16-byte salt, N=131072, r=8,
p=1). The container header is authenticated as additional data. A wrong
password or modified encrypted file fails authentication before restore staging
or rollback creation begins. The password is never persisted.

Unencrypted ZIP backups remain supported for backward compatibility and
explicit operator choice; they must still be protected as sensitive operational
data. Encrypted backup validation and restore decrypt into an OS-managed
anonymous/auto-delete seekable temporary file because ZIP validation needs
random access; no named decrypted ZIP is created below the application-data
tree. Authentication/validation complete before live application data or the
rollback state is changed.

Backups never intentionally contain controller passwords or API keys. The
transient `pending_create_guard` contains no controller/voucher data and is
excluded from backups; backup creation and restore fail closed while that marker
exists so an unresolved controller mutation cannot be forgotten by moving local
state backwards.

Manual multi-workstation history exchange uses a separate encrypted `.vmhx`
package. It carries audit rows plus the portable HMAC history identity so another
workstation can verify or, only when it has no audit rows, explicitly adopt that
identity. The package never carries generated PDFs, logos, controller settings,
API keys or TLS certificate trust. Import refuses identity mismatch behind
existing history and blocks conflicting modern print-job identities.

On restore, Voucher Management clears the saved controller API root and
certificate fingerprint before the restored data becomes active. The operator
must re-enter the API root and independently approve any self-signed certificate
again; controller trust is never imported from a backup.

## Repository policy

Never commit real:

- controller addresses or site identifiers;
- voucher codes or voucher PDFs;
- production settings/history/logs/backups;
- passwords, API keys, cookies, tokens or private certificates;
- third-party artwork without redistribution permission.

Fixtures must be synthetic.

The privacy scanner also accepts an external marker list through
`--markers-file`. Deployment-specific marker lists must stay outside the
repository and should be supplied only during maintainer-side release checks.
The public workflow performs generic repository checks without requiring any
deployment-specific secret.

## Vulnerability reporting

Do not publish credentials, voucher data or operational details in a public
issue.

Use GitHub Private Vulnerability Reporting for security issues:

1. open the public repository's **Security** tab;
2. choose **Advisories**;
3. choose **Report a vulnerability**;
4. include the affected version, reproduction steps and impact, while omitting
   real credentials or production voucher data.

Security reports submitted through that channel remain private to the
maintainers while they are investigated.
