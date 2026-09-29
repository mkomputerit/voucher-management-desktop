# Windows Setup.exe plan for 5.1

This document describes installer work prepared independently from the frozen
5.1.0 application candidate.

## Goal

Produce one user-facing `VoucherManagement-5.1.0-Setup.exe` that installs the
reviewed PyInstaller build as a managed shared Windows deployment without
weakening the existing data/ACL contract.

The application source candidate remains frozen while installer work happens on
a separate branch.

## Installer technology

The wrapper uses NSIS 3.12. The build workflow downloads the official portable
NSIS ZIP, verifies its pinned SHA-256 before extraction and invokes
`makensis.exe` only after the normal application tests, privacy checks,
PyInstaller build and shared-deployment ACL integration test have passed.

NSIS is only the user-facing packaging layer. The existing
`Install-VoucherManagement.ps1` remains responsible for the sensitive Windows
provisioning contract:

- install files below Program Files;
- shared data below ProgramData;
- create/use the local Voucher Management Operators group;
- grant Modify only to that group and Full Control to SYSTEM/Administrators;
- remove stale permissive ACL entries;
- preserve shared data on upgrades;
- refuse installation/update while VoucherManagement.exe is running.

## Expected operator experience

A normal setup:

1. requests administrator elevation;
2. shows the product/license pages;
3. installs to `Program Files\Voucher Management`;
4. provisions `ProgramData\VoucherManagement` with the reviewed ACLs;
5. creates the Start Menu shortcut;
6. registers Voucher Management in Windows Installed apps;
7. installs a normal Windows uninstaller.

The installer intentionally does not auto-launch the application. A user newly
added to the local operator group must sign out and sign in again so the new
group SID is present in the Windows logon token.

## Upgrade behavior

Running a newer Setup.exe over an existing installation replaces program files
but preserves ProgramData. The same stable Windows uninstall registry identity
is reused. The application database, generated PDFs, settings, audit history and
other managed data are not deleted by an upgrade.

## Uninstall behavior

The normal uninstall removes program files, Start Menu integration and the
Installed apps entry while preserving ProgramData and the operator group.

Interactive uninstall includes an explicit destructive option to also remove
the shared data archive and operator group. That option is unchecked by default
and requires a second confirmation.

## CI release gate

The generated Setup.exe is exercised on the Windows runner, not merely compiled.
The integration test performs:

- clean silent install into isolated test paths;
- marker, executable, uninstaller, registry and ACL verification;
- second in-place install with a ProgramData sentinel proving upgrade
  preservation;
- normal uninstall proving data/group preservation;
- reinstall over the preserved data;
- explicit purge uninstall proving data/group removal.

A stable release should publish both the Setup.exe and portable ZIP with
SHA-256 checksums. Binaries remain unsigned until the project code-signing
policy is deliberately changed.
