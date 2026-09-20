# Voucher Management

Open-source Windows desktop application for managing and printing guest-access vouchers.

The current development line is field-tested with Ubiquiti UniFi Network. **Voucher Management is an independent project and is not affiliated with or endorsed by Ubiquiti Inc.** UniFi is a trademark of Ubiquiti Inc.

## Current development state

The application currently provides:

- direct voucher listing and creation;
- single-use, multi-use and unlimited voucher workflows;
- task-oriented views for vouchers to print, active and expired vouchers;
- recipient search;
- A4 PDF generation with automatic pagination;
- embedded PDF preview and direct Windows printing;
- persistent print/PDF audit history without storing voucher codes in clear text in `history.jsonl`;
- portable backup/restore of application-managed data;
- persistent custom logo library;
- Windows 11 light/dark themes.

The 4.2 beta line uses Ubiquiti's documented Network integration API with API-key authentication. The adapter has been validated against UniFi Network 10.6.106 for discovery, voucher listing/detail, creation, documented limits and single-UUID deletion. Field testing confirmed that two real guest clients can use the same voucher when `authorizedGuestLimit` is omitted; the controller reports both authorized clients through `authorizedGuestCount`.

## Current limitations

- the current operator UI requires UniFi site discovery to be unambiguous; a
  controller exposing multiple sites is rejected rather than selecting one
  automatically;
- Windows printing submits each PDF page for the requested copy count; printer
  collation behavior therefore depends on the selected printer/driver.

## Public release goals

The public release will:

1. use the neutral **Voucher Management** product identity;
2. use the MIT License;
3. use documented platform APIs;
4. keep credentials out of source, logs, backups and persistent settings;
5. provide reproducible GitHub Actions builds;
6. include current dependency/license notices and release checksums;
7. prepare releases for SignPath Foundation origin-verified code signing.

See:

- `docs/PUBLIC_RELEASE_READINESS.md`
- `docs/UNIFI_OFFICIAL_API_ANALYSIS.md`
- `SECURITY.md`
- `PRIVACY.md`
- `CODE_SIGNING_POLICY.md`
- `CONTRIBUTING.md`

## Security model

Authentication secrets are used only for the active connection and must not be persisted to settings, diagnostic logs, print history or backup archives.

Print history uses HMAC-SHA-256 identifiers rather than clear-text voucher
codes in `history.jsonl`. This is an audit-correlation mechanism, not encryption:
the portable history key travels with backups, and generated PDFs contain the
printable voucher codes. Backups and the `Print/` archive must therefore be
protected as sensitive operational data.

Real voucher codes, customer/controller configuration, production PDFs, API keys, credentials and operational logs must never be committed to the repository.

### Self-signed controller certificates

TLS certificate validation is enabled by default. If a local controller uses a
self-signed or otherwise untrusted certificate, Voucher Management displays its
SHA-256 fingerprint before storing a certificate pin.

Verify that fingerprint independently before approving it, for example by
comparing it with the certificate shown by the controller's own management
interface or with the certificate inspected directly from a trusted management
workstation. Do not approve a new fingerprint only because the application
prompt appeared.

If a previously pinned certificate changes, Voucher Management blocks the API
connection before sending the API key and displays both the old and new
fingerprints. The stored pin is replaced only after explicit approval of the
new fingerprint.

## Installation and removal

The current Windows distribution is portable:

1. extract the complete release folder;
2. run `VoucherManagement.exe`;
3. keep all files in the extracted program folder together.

To remove the portable application, close Voucher Management and delete the
extracted program folder.

Persistent user data is intentionally separate from the executable under the
user application-data profile. Removing the portable program folder does not
delete settings, audit history, generated PDFs or custom logos. Users who also
want to remove their local data can delete the Voucher Management application
data directory after making any desired backup.

## Code signing policy

See [CODE_SIGNING_POLICY.md](CODE_SIGNING_POLICY.md).

The project intends to apply for SignPath Foundation open-source code signing.
Any Foundation attribution required by the program will be added only after the
project has been accepted.

## Interface language

The current Windows operator interface is in **Italian**. Project documentation
and source-code comments are primarily in English so the codebase remains
reviewable by a wider open-source audience.

## Development


Windows builds are produced through GitHub Actions using PyInstaller. The reviewed Windows environment is fully pinned in `requirements-lock.txt`, installed in an isolated virtual environment, and checked before every build. Automated tests cover the official API adapter/mapping, persistent history, backup/restore, lifecycle policy, migration and core voucher workflow behavior.


The application icon is original project artwork generated reproducibly from
`tools/generate_app_icon.py`. It contains no third-party vendor, venue or
deployment branding.

Deployment-specific names can be supplied to the privacy scanner through
`tools/check_public_tree.py --markers-file <path>`. The marker file must remain
outside the repository. The private engineering CI requires the
`PUBLIC_PRIVACY_MARKERS` Actions secret, one marker per line, and fails if it is
missing or empty. The scanner checks both text content and file paths without
printing the marker values. The clean public workflow does not reference this
private secret.

Current architecture and API behavior are documented in `docs/ARCHITECTURE.md`
and `docs/UNIFI_OFFICIAL_API_ANALYSIS.md`.

## License

Voucher Management is licensed under the [MIT License](LICENSE).

Third-party components retain their own licenses. See `THIRD_PARTY_NOTICES.md`.
