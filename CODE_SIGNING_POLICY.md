# Code signing policy

Voucher Management currently distributes unsigned Windows binaries with
SHA-256 release checksums. The project does not currently use a production
code-signing certificate or signing service.

Authenticode code signing may be introduced in the future when an appropriate
signing solution is available. Any future signing integration must preserve the
public build provenance and release controls described below.

## Current release integrity

- Release binaries are produced by the declared GitHub Actions workflow in the
  public Voucher Management source repository.
- The reviewed Windows environment is pinned through the project dependency
  lock and checked by the public CI workflow before packaging.
- Published release artifacts include SHA-256 checksums so users can verify
  file integrity independently.
- Unsigned binaries are not represented as signed or trusted by a third-party
  certificate authority.

## Rules for future signed releases

- Release binaries must continue to be produced by the declared GitHub Actions
  workflow from the public source repository.
- Signing must be restricted to the approved public release branch/tag policy.
- A maintainer must verify CI and release evidence before publication of a
  signed artifact.
- Product name and version metadata must match the source release and
  `version.txt`.
- Unsigned development/beta artifacts must remain clearly distinguishable from
  signed public releases.
- Manually built maintainer-workstation binaries must not be published as
  normal signed releases.
- Build workflow, dependency lock, packaging specification and any future
  signing integration are security-sensitive source code and receive the same
  review attention as application code.
- The selected signing provider, certificate ownership model and verification
  procedure must be documented here before the first signed public release.

## Project roles

Until additional maintainers are appointed, the project has one trusted
maintainer. This accurately reflects the current project rather than inventing
an organizational structure.

- **Author / Committer:** GitHub user `mkomputerit`
- **Reviewer:** GitHub user `mkomputerit`; external contributions and
  security-sensitive pull requests are reviewed before merge.
- **Release approver:** GitHub user `mkomputerit`; release evidence is checked
  before publication. If code signing is introduced, signing approval remains
  part of this release review.

If the maintainer group changes, this section must be updated before the next
release that depends on those roles.

## Privacy

See [PRIVACY.md](PRIVACY.md).

Voucher Management does not operate telemetry, analytics, advertising or a
project-controlled cloud data-collection service. It communicates with
networked systems only when requested/configured by the operator, such as the
network controller/API endpoint selected by that operator.

## Release provenance

The current production chain is:

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
PyInstaller unsigned release artifact
        |
        v
SHA-256 checksum generation and verification
        |
        v
publication
```

If Authenticode code signing is introduced, a controlled signing stage will be
inserted between artifact creation and publication. The provider, certificate
model, approval flow and public verification instructions will be documented
here before that pipeline is used for a release.
