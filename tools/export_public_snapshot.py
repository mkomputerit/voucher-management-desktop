"""Create the reviewed clean source snapshot used for public publication."""

from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

PRIVATE_ONLY_PATHS = {
    Path("docs/PRIVATE_TO_PUBLIC_TRANSITION.md"),
    Path("docs/PUBLIC_RELEASE_READINESS.md"),
    Path("docs/CODE_REVIEW_REPORT_PRIVATE.md"),
    Path("docs/REVIEW_FIXES_4_2_0_BETA_2.md"),
    Path("docs/PUBLICATION_PLAN.md"),
    Path("docs/CODE_REVIEW_REPORT.md"),
    Path("docs/UBIQUITI_COMPLIANCE_REVIEW.md"),
    Path("docs/BETA_4_2_0_REVIEW.md"),
    Path("docs/BETA_4_2_0_TEST_PLAN.md"),
    Path(".github/workflows/cleanup-actions-storage.yml"),
}

EXCLUDED_DIRECTORY_NAMES = {
    ".git", ".venv", "venv", "build", "dist", ".generated-assets",
    "__pycache__", ".pytest_cache",
}

PRIVATE_MARKERS_START = "      - name: Prepare private privacy markers\n"
PRIVATE_MARKERS_END = "      - name: Validate PowerShell diagnostics\n"
PRIVATE_WORKFLOW_START = "      - name: Package private engineering prerelease\n"
PRIVATE_WORKFLOW_END = "      - name: Upload portable build\n"


def _remove_block(text: str, start_marker: str, end_marker: str, *, label: str) -> str:
    """Remove one workflow block delimited by stable step-name markers."""

    start = text.find(start_marker)
    end = text.find(end_marker)

    # The exporter also runs in the already-sanitized public repository CI.
    # If the start marker is absent, the private block has already been
    # removed; the public workflow intentionally keeps the following public
    # step that also serves as the end marker. A surviving start marker without
    # a valid end marker is malformed and must fail closed.
    if start == -1:
        return text
    if end == -1 or end <= start:
        raise RuntimeError(f"Impossibile isolare {label}")
    return text[:start] + text[end:]


def _assert_private_identity_absent(destination: Path, identity: str) -> None:
    """Fail if the caller-supplied private repository identity survives export."""

    needle = identity.strip()
    if not needle:
        return
    folded = needle.casefold()
    for path in destination.rglob("*"):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if folded in text.casefold():
            raise RuntimeError(
                f"Identità repository privato ancora presente in: "
                f"{path.relative_to(destination)}"
            )


def _sanitize_public_workflow(destination: Path) -> None:
    """Remove private-only CI plumbing from the public workflow."""

    workflow = destination / ".github" / "workflows" / "build-windows.yml"
    text = workflow.read_text(encoding="utf-8")

    text = _remove_block(
        text,
        PRIVATE_MARKERS_START,
        PRIVATE_MARKERS_END,
        label="il passaggio dei marker privacy privati",
    )
    text = _remove_block(
        text,
        PRIVATE_WORKFLOW_START,
        PRIVATE_WORKFLOW_END,
        label="i passaggi prerelease privati",
    )
    text = text.replace(
        "  test-and-build:\n    permissions:\n      contents: write\n",
        "  test-and-build:\n",
    )
    text = text.replace(
        "        env:\n"
        "          PRIVATE_REPOSITORY_IDENTITY: ${{ github.repository }}\n",
        "",
    )
    text = text.replace(
        ' --markers-file "$env:PRIVACY_MARKERS_FILE" --require-markers',
        "",
    )
    base_publish_condition = (
        "if: github.event_name == 'workflow_dispatch' || "
        "github.ref == 'refs/heads/main'"
    )
    public_publish_condition = (
        base_publish_condition
        + " || startsWith(github.ref, 'refs/heads/release/')"
    )
    if public_publish_condition not in text:
        if base_publish_condition not in text:
            raise RuntimeError(
                "Impossibile individuare la condizione di pubblicazione pubblica"
            )
        text = text.replace(
            base_publish_condition,
            public_publish_condition,
            1,
        )

    forbidden_public_workflow_tokens = (
        "PUBLIC_PRIVACY_MARKERS",
        "PRIVACY_MARKERS_FILE",
        "PRIVATE_REPOSITORY_IDENTITY",
        "--require-markers",
        "Package private engineering prerelease",
        "Publish private engineering prerelease",
    )
    for token in forbidden_public_workflow_tokens:
        if token in text:
            raise RuntimeError(
                f"Il workflow pubblico contiene ancora plumbing privato: {token}"
            )

    workflow.write_text(text, encoding="utf-8")



def export_snapshot(destination: Path) -> Path:
    """Copy the source tree while excluding private engineering-only files."""

    source = ROOT.resolve()
    destination = Path(destination).resolve()

    if destination == source:
        raise ValueError("La destinazione export non può essere il sorgente")

    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)

    for item in source.rglob("*"):
        relative = item.relative_to(source)
        if any(part in EXCLUDED_DIRECTORY_NAMES for part in relative.parts):
            continue
        if relative in PRIVATE_ONLY_PATHS:
            continue
        if item.is_symlink():
            continue

        target = destination / relative
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif item.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)

    for relative in PRIVATE_ONLY_PATHS:
        if (destination / relative).exists():
            raise RuntimeError(f"File privato esportato per errore: {relative}")

    _sanitize_public_workflow(destination)
    _assert_private_identity_absent(
        destination,
        os.environ.get("PRIVATE_REPOSITORY_IDENTITY", ""),
    )
    return destination


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    print(export_snapshot(args.destination))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
