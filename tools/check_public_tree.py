"""Public snapshot privacy scanner.

Generic rules catch common operational data such as private IP addresses and
credentials. Deployment-specific names can be supplied at runtime through an
external markers file so the sensitive marker list never needs to be committed.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


DEFAULT_ROOT = Path(__file__).resolve().parents[1]

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

GENERIC_PRIVATE_PATTERNS = {
    "private IPv4 address": re.compile(
        r"(?<!\d)(?:10(?:\.\d{1,3}){3}|"
        r"192\.168(?:\.\d{1,3}){2}|"
        r"172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})(?!\d)"
    ),
    "Windows user-profile path": re.compile(
        r"(?i)\b[A-Z]:[\\/]+Users[\\/]+[^\\/\s]+"
    ),
    "hard-coded credential value": re.compile(
        r"""(?ix)
        \b(?:password|api[_-]?key|secret|access[_-]?token)\b
        \s*[:=]\s*
        ["']
        (?!\s*["'])
        [^"'\r\n]{4,}
        ["']
        """
    ),
}

FORBIDDEN_SUFFIXES = {".jsonl", ".key", ".secret", ".env"}

EXCLUDED_DIRECTORY_NAMES = {
    ".git", ".venv", "venv", "build", "dist", ".generated-assets",
    "__pycache__", ".pytest_cache",
}

TEXT_SUFFIXES = {
    ".py", ".ps1", ".md", ".txt", ".yml", ".yaml",
    ".json", ".toml", ".spec",
}


def load_markers(path: Path | None) -> tuple[str, ...]:
    """Load case-insensitive private markers from an external UTF-8 file.

    Blank lines and lines beginning with # are ignored. The marker plaintext is
    deliberately never included in scanner output.
    """

    if path is None:
        return ()
    marker_path = Path(path)
    if not marker_path.is_file():
        raise ValueError("Il file marker privacy non esiste")
    try:
        lines = marker_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError("Impossibile leggere il file marker privacy") from exc

    markers: list[str] = []
    seen: set[str] = set()
    for raw in lines:
        marker = raw.strip()
        if not marker or marker.startswith("#"):
            continue
        folded = marker.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        markers.append(marker)
    return tuple(markers)


def scan(
    root: Path,
    *,
    public_export: bool = False,
    markers: tuple[str, ...] = (),
) -> list[str]:
    """Return all privacy violations found below root."""

    root = Path(root).resolve()
    violations: list[str] = []
    folded_markers = tuple(marker.casefold() for marker in markers)

    if public_export:
        for relative in sorted(PRIVATE_ONLY_PATHS, key=str):
            if (root / relative).exists():
                violations.append(f"private-only file exported: {relative}")

    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part in EXCLUDED_DIRECTORY_NAMES for part in relative.parts):
            continue

        relative_folded = relative.as_posix().casefold()
        for index, marker in enumerate(folded_markers, start=1):
            if marker in relative_folded:
                violations.append(
                    f"external private marker #{index} matched in a path"
                )

        suffix = path.suffix.lower()
        if suffix in FORBIDDEN_SUFFIXES:
            violations.append(f"forbidden file type: {relative}")
            continue
        if suffix not in TEXT_SUFFIXES and path.name not in {
            "LICENSE",
            "requirements.txt",
            "requirements-dev.txt",
            "requirements-lock.txt",
        }:
            continue

        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue

        for label, pattern in GENERIC_PRIVATE_PATTERNS.items():
            if pattern.search(text):
                violations.append(f"{label}: {relative}")

        folded_text = text.casefold()
        for index, marker in enumerate(folded_markers, start=1):
            if marker in folded_text:
                violations.append(
                    f"external private marker #{index} matched in text content"
                )

    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--public-export", action="store_true")
    parser.add_argument(
        "--markers-file",
        type=Path,
        help="External UTF-8 file containing one private marker per line",
    )
    parser.add_argument(
        "--require-markers",
        action="store_true",
        help="Fail if the external marker file is missing or contains no markers",
    )
    args = parser.parse_args(argv)

    if args.require_markers and args.markers_file is None:
        parser.error("--require-markers richiede --markers-file")

    try:
        markers = load_markers(args.markers_file)
    except ValueError as exc:
        parser.error(str(exc))

    if args.require_markers and not markers:
        parser.error("Il file marker privacy è vuoto o contiene solo commenti")

    violations = scan(
        args.root,
        public_export=args.public_export,
        markers=markers,
    )
    if violations:
        print("Public-tree privacy check failed:", file=sys.stderr)
        for violation in violations:
            print(f" - {violation}", file=sys.stderr)
        return 1

    print("Public-tree privacy check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
