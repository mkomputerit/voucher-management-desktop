# Reporting preview for 5.1.0

This preview packages reporting implementation commit `590314e96868bba167b9381690af0ed97bc0515a`. It includes the PDF, onboarding and backup changes from the UI branch.

Download the artifact from a successful Windows workflow on `release/5.1-reporting-preview`. Extract the complete archive into a separate application folder and run `VoucherManagement.exe` from that folder. Do not copy only the executable over an older installation.

## Operator acceptance checks

- Before connecting to UniFi, Home counters must not present the local archive as current controller data. Connect and synchronize to populate the live view.
- Creating vouchers exposes the explicit **Voucher nominale** checkbox. This classification is local reporting metadata and is not sent to UniFi.
- Reports distinguish application-generated vouchers, generated and never used, nominal, unclassified and usage-indeterminate records. Missing usage evidence must not be presented as proof of never having been used.
- Existing vouchers without verified nominal classification remain unclassified.

This is a portable preview for operator validation, not a stable release or Setup.exe installer.
