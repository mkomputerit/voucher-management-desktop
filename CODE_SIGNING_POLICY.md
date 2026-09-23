# Code signing policy

Voucher Management is applying for SignPath Foundation open-source code
signing.

For Foundation-signed releases: **Free code signing provided by SignPath.io,
certificate by SignPath Foundation**. Until the application is accepted and
the signing integration is active, published binaries remain unsigned and are
distributed with release SHA-256 checksums.

## Release-signing rules

- Release binaries must be produced by the declared GitHub Actions workflow in
  the public Voucher Management source repository.
- Open-source signing will use SignPath trusted-build-system and origin
  verification with GitHub-hosted runners.
- Release signing is restricted to the approved public release branch/tag
  policy.
- Every release signing request requires manual approval.
- Product name and version metadata must match the source release and
  `version.txt`.
- Unsigned development/beta artifacts must remain clearly distinguishable from
  signed public releases.
- Manually built maintainer-workstation binaries are never submitted as normal
  signed releases.
- Build workflow, dependency lock, packaging specification and signing
  integration are security-sensitive source code and receive the same review
  attention as application code.

## Project roles

Until additional maintainers are appointed, the project has one trusted
maintainer. The same person may hold the roles below; this accurately reflects
the current project rather than inventing an organizational structure.

- **Author / Committer:** GitHub user `mkomputerit`
- **Reviewer:** GitHub user `mkomputerit`; external contributions and
  security-sensitive pull requests are reviewed before merge.
- **Approver:** GitHub user `mkomputerit`; each release signing request is
  manually approved after CI/release evidence is checked.

If the maintainer group changes, this section must be updated before the next
signed release.

## Privacy

See [PRIVACY.md](PRIVACY.md).

Voucher Management does not operate telemetry, analytics, advertising or a
project-controlled cloud data-collection service. It communicates with
networked systems only when requested/configured by the operator, such as the
network controller/API endpoint selected by that operator.

## Signing provenance

The intended production chain is:

```text
public source commit/tag
        |
        v
GitHub Actions on GitHub-hosted runner
        |
        v
tests + privacy checks + deterministic dependency lock
        |
        v
PyInstaller unsigned artifact stored as GitHub workflow artifact
        |
        v
SignPath origin verification
        |
        v
manual signing approval
        |
        v
Authenticode-signed release artifact
        |
        v
signature/checksum verification before publication
```

The final SignPath project configuration, required Foundation attribution and
public repository URL will be recorded here only after Foundation acceptance.
