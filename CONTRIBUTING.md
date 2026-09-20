# Contributing

## Security first

Do not commit:
- real voucher codes or production voucher PDFs;
- passwords, API keys, session cookies or CSRF tokens;
- real controller addresses, customer/site identifiers or private paths;
- production logs/history/backups;
- copyrighted third-party artwork without redistribution permission.

Use synthetic fixtures only.

## Changes

Keep changes focused and documented. Non-obvious security, lifecycle and compatibility decisions require comments/docstrings explaining why the rule exists.

Changes to authentication, API request construction, deletion behavior, audit integrity, backup/restore, PDF geometry or the release workflow require tests and reviewer attention.

## Pull requests

Before requesting review:
1. run the complete test suite;
2. confirm no credentials or operational data are present;
3. update CHANGELOG/docs when behavior changes;
4. keep release/build configuration changes visible and separately reviewable.

External contributions require review by a project maintainer before merge.
