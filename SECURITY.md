# Security

## Security goals

Voucher Management is designed to avoid turning operational network credentials
or guest voucher data into repository, log or backup secrets.

The project follows these rules:

- no passwords or API keys in source control;
- no authentication secrets in settings, logs, print history or backups;
- voucher codes are not stored in clear text in `history.jsonl`;
- no controller address in diagnostic logs;
- destructive voucher deletion requires explicit confirmation;
- vouchers are not deletable by the application after their first recorded
  physical print;
- backup extraction rejects traversal paths and unsupported archive content;
- release binaries are built automatically rather than from a maintainer
  workstation.

## Authentication

The 4.2 development line uses the documented UniFi Network API and X-API-Key
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

## Diagnostic logs

Application logs contain timestamps, severity, OS family/architecture and
operation identifiers. Unexpected UI callbacks record only the exception type,
not the traceback or exception text. Logs must not contain:

- hostnames or usernames;
- controller addresses;
- voucher codes;
- recipients unless explicitly required for an error report;
- passwords, API keys, cookies or CSRF tokens.

## Backup/restore

Backups contain application-managed settings, audit data, the portable history
key, generated PDFs and custom logos. They can therefore contain recipient
labels and voucher codes in the generated PDFs. Backups must be protected as
sensitive operational data.

Backups never intentionally contain controller passwords or API keys.

## Repository policy

Never commit real:

- controller addresses or site identifiers;
- voucher codes or voucher PDFs;
- production settings/history/logs/backups;
- passwords, API keys, cookies, tokens or private certificates;
- third-party artwork without redistribution permission.

Fixtures must be synthetic.

The CI privacy scanner also accepts an external marker list through
`--markers-file`. Deployment-specific names belong in the private
`PUBLIC_PRIVACY_MARKERS` Actions secret, one marker per line, rather than in
repository source or ordinary SHA-256 digests. Private engineering CI invokes
the scanner with `--require-markers`, so a missing or empty marker list is a
hard failure. The exported public workflow does not consume that secret.

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
