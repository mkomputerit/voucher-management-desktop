# Voucher Management

Open-source Windows desktop application for managing and printing guest-access vouchers.

The current development line is field-tested with Ubiquiti UniFi Network. **Voucher Management is an independent project and is not affiliated with or endorsed by Ubiquiti Inc.** UniFi is a trademark of Ubiquiti Inc.

## Current development state

The application currently provides:

- direct voucher listing and creation, with a durable anti-repeat barrier for
  controller-create requests whose remote outcome cannot be proven;
- serialized background execution for controller calls, PDF generation,
  high-resolution printing, backup/restore, print-audit recovery and manual
  history exchange, with visible progress while the Tk interface remains
  responsive;
- Tk-independent validation and print/PDF resolution workflows, keeping the UI
  layer focused on operator input, presentation and scheduling;
- single-use, multi-use and unlimited voucher workflows;
- task-oriented views for vouchers to print, active and expired vouchers;
- recipient search;
- A4 PDF generation with automatic pagination and bundled Unicode Noto Sans fonts;
- pre-render checks that stop with an operator warning when entered text uses
  characters not covered by the bundled font;
- atomic PDF publication through same-directory temporary files;
- managed `Print/YYYY/MM` archive with configurable PDF retention;
- embedded PDF preview and direct Windows printing;
- persistent print/PDF audit history without storing voucher codes in clear text in `history.jsonl`;
- manual idempotent recovery when a document was sent to Windows printing but
  the corresponding local print-audit event could not be recorded;
- workstation-local history and print counters with encrypted manual history
  export/import for deliberate idempotent merging between compatible stations;
  new generation events carry stable IDs so independent identical events from
  separate stations remain distinct during convergence;
- portable backup/restore of application-managed data, with optional
  password-protected authenticated `.vmbk` backups and legacy ZIP support;
- persistent custom logo library;
- Windows 11 light/dark themes.

Voucher Management 4.3.3 uses Ubiquiti's documented Network integration API with API-key authentication. The adapter has been validated against UniFi Network 10.6.106 for discovery, voucher listing/detail, creation, documented limits and single-UUID deletion. Field testing confirmed that two real guest clients can use the same voucher when `authorizedGuestLimit` is omitted; the controller reports both authorized clients through `authorizedGuestCount`.

## Current limitations

- print history, generated-document counters and physical-print counters remain
  local to each workstation and are not synchronized automatically. Operators
  can manually export/import an encrypted history package between stations that
  share the same audit identity; an empty station can explicitly adopt the
  imported identity. This is deliberate merge/transfer, not live synchronization;
- the current operator UI requires UniFi site discovery to be unambiguous; a
  controller exposing multiple sites is rejected rather than selecting one
  automatically;
- Windows printing submits each PDF page for the requested copy count; printer
  collation behavior therefore depends on the selected printer/driver;
- voucher creation is intentionally limited to batches of 50 in the operator
  UI and adapter.
- custom logos are limited to PNG or JPEG files, at most 8192 pixels per
  side, 40 megapixels and 25 MiB. A valid oversized logo found during restore
  is omitted with an operator warning; corrupt or disguised image content is
  rejected;

## Public release

Voucher Management 4.3.3:

1. use the neutral **Voucher Management** product identity;
2. use the MIT License;
3. use documented platform APIs;
4. keep credentials out of source, logs, backups and persistent settings;
5. provide reproducible GitHub Actions builds;
6. include current dependency/license notices and release checksums;
7. publish unsigned Windows binaries with SHA-256 release checksums; code signing may be introduced in the future.

See:

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

The Windows build supports two deployment modes.

**Portable mode** keeps application data under the current Windows user's local
application-data profile. Extract the complete build folder and run
`VoucherManagement.exe`; no shared-machine permissions are changed.

**Managed shared deployment** is intended for one workstation used by multiple
authorized Windows accounts. It is currently delivered as an elevated
PowerShell deployment script, not as an MSI/EXE package registered in Windows
"Installed apps". From an elevated PowerShell prompt in the extracted build
folder, run:

```powershell
.\Install-VoucherManagement.ps1
```

The managed deployment script copies the application to
`Program Files\Voucher Management`, creates the local
`Voucher Management Operators` group, authorizes the
interactive Windows user, creates `ProgramData\VoucherManagement` with
restrictive ACLs, writes the shared-deployment marker and creates a Start Menu
shortcut. If the user was newly added to the group, sign out and sign in again
before the first launch.

Installed mode uses one shared SQLite database and one machine-wide application
guard across Fast User Switching sessions. Existing per-user data is not
silently copied. Before the shared archive is used operationally, Settings
offers an explicit migration that creates and verifies an encrypted safety
backup, transfers the old LocalAppData tree through the normal restore path and
leaves the original per-user data unchanged.

Because this is not a Windows Installer/MSI package, removal is also performed
with the bundled administrative script. To remove the deployed program while
preserving shared data, run `Uninstall-VoucherManagement.ps1` from an elevated
PowerShell prompt. Pass
`-RemoveData` only when the shared ProgramData archive and local operator group
should also be deleted.

Removing a portable folder or the installed program does not by itself delete
retained application data.

Generated PDFs are archived below `Print/YYYY/MM`. Retention is disabled by
default (`0`) so an upgrade never removes an existing PDF unless the operator
explicitly enables a retention period in Settings. Cleanup uses audit history as
a whitelist and never deletes unrelated files. Removing an expired PDF does not
remove its audit/print history.

A hard interruption during rendering can leave a hidden `.Voucher_*.tmp`
working file containing printable voucher data. At startup, Voucher Management
removes only managed renderer temp files older than 24 hours; these scratch
files are also excluded from application backups.


Encrypted backups use a password supplied only for the active operation. The
application derives an AES-256 key with Scrypt and authenticates the complete
container with AES-GCM. Wrong passwords and modified encrypted files are
rejected before live application data is changed. Legacy unencrypted ZIP
backups remain supported for compatibility and explicit operator choice.

After restoring a backup, the saved controller API root and TLS certificate pin
are intentionally cleared. Re-enter the controller API root and independently
verify/approve the certificate fingerprint before reconnecting. This prevents a
backup from carrying controller trust to another installation.

## Manual history exchange between workstations

History exchange is designed for deliberate synchronization between
workstations; it is not live synchronization.

A typical two-workstation procedure is:

1. On workstation A, use the history export command and protect the `.vmhx`
   package with a password of at least 12 characters.
2. Transfer the package to workstation B through a trusted channel.
3. On workstation B, start history import and verify the fingerprint displayed
   by the application before approving the merge.
4. If workstation B has no useful history yet, it may explicitly adopt the
   portable audit identity contained in the package. An installation that
   already contains history with another identity is rejected instead of being
   overwritten.
5. After both workstations have produced new events independently, repeat the
   exchange in the opposite direction as needed. Imports are merges: stable
   generation-event IDs and print-job IDs prevent already imported modern
   events from being duplicated.
6. Re-importing the same merged package is safe and becomes a no-op once both
   histories contain the same events.

Export/import is blocked while a print audit is pending. Resolve the pending
print state first so an ambiguous physical-print result cannot be propagated to
another workstation.

The exchange package contains audit history and the portable history identity,
but not controller credentials, controller TLS trust, PDFs, logos or controller
settings. Treat the package and its password as operationally sensitive
because the history identity is intentionally portable.

## Code signing policy

See [CODE_SIGNING_POLICY.md](CODE_SIGNING_POLICY.md).

Published Windows binaries are currently unsigned and are distributed with
SHA-256 release checksums. Authenticode code signing may be introduced in the
future when an appropriate signing solution is available. If signing is adopted,
this policy and the public release documentation will be updated before signed
artifacts are published.

## Interface language

The current Windows operator interface is in **Italian**. Project documentation
and source-code comments are primarily in English so the codebase remains
reviewable by a wider open-source audience.

## Development

For a local Windows checkout, run the following commands from the repository
root, install the direct development requirements and add the `src` directory
to `PYTHONPATH` before running tests or the launcher:

```powershell
py -m pip install -r requirements-dev.txt
$env:PYTHONPATH = "$PWD\src"
py -m pytest -q
py launcher.py
```

For exact release-environment reproduction, use the hash-verified lock instead:

```powershell
py -m pip install --require-hashes --no-deps -r requirements-lock.txt
```

Windows builds are produced through GitHub Actions using PyInstaller. The reviewed Windows environment is fully pinned in `requirements-lock.txt`, installed in an isolated virtual environment, audited with `pip-audit`, and checked before every build. Automated tests cover the official API adapter/mapping, persistent history, backup/restore, lifecycle policy, migration and core voucher workflow behavior.


The application icon is original project artwork generated reproducibly from
`tools/generate_app_icon.py`. It contains no third-party vendor, venue or
deployment branding.

Deployment-specific names can be supplied to the privacy scanner through
`tools/check_public_tree.py --markers-file <path>`. Marker files must remain
outside the repository. The scanner checks both text content and file paths
without printing marker values. Public CI runs the repository-independent
privacy checks; maintainers can run additional deployment-specific scans before
publishing a release.

Current architecture and API behavior are documented in `docs/ARCHITECTURE.md`
and `docs/UNIFI_OFFICIAL_API_ANALYSIS.md`.

## License

Voucher Management is licensed under the [MIT License](LICENSE).

Third-party components retain their own licenses. See `THIRD_PARTY_NOTICES.md`.
