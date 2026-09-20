"""Collect license/notice files from pinned Python distributions.

The script runs inside the release environment after dependencies are installed.
It copies authoritative package-provided license material into the Windows
artifact instead of embedding hand-copied license text in source.
"""

from __future__ import annotations

import json
import shutil
import sys
from importlib import metadata
from pathlib import Path


DISTRIBUTIONS = (
    "reportlab",
    "Pillow",
    "pypdfium2",
    "pywin32",
    "sv-ttk",
    "darkdetect",
)

NOTICE_MARKERS = ("license", "licence", "copying", "notice", "thirdparty")


def is_notice_file(path: Path) -> bool:
    """Return True for package files that are plausibly license/notice material."""
    lowered = path.as_posix().lower()
    return any(marker in lowered for marker in NOTICE_MARKERS)


def collect(destination: Path) -> dict[str, dict]:
    """Copy package-provided notice files and return a machine-readable manifest."""
    destination.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, dict] = {}

    for distribution_name in DISTRIBUTIONS:
        dist = metadata.distribution(distribution_name)
        package_dir = destination / distribution_name
        copied: list[str] = []

        for entry in dist.files or ():
            relative = Path(str(entry))
            if not is_notice_file(relative):
                continue
            source = Path(dist.locate_file(entry))
            if not source.is_file():
                continue

            target = package_dir / relative.name
            counter = 2
            while target.exists():
                target = package_dir / (
                    f"{relative.stem}_{counter}{relative.suffix}"
                )
                counter += 1
            package_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied.append(target.name)

        manifest[distribution_name] = {
            "version": dist.version,
            "license_files": copied,
        }

    (destination / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return manifest


def main() -> int:
    if len(sys.argv) != 2:
        print(
            "Usage: python tools/collect_dependency_licenses.py "
            "<destination>",
            file=sys.stderr,
        )
        return 2

    destination = Path(sys.argv[1])
    manifest = collect(destination)
    missing = [
        name
        for name, info in manifest.items()
        if not info["license_files"]
    ]
    if missing:
        print(
            "ERROR: no packaged license files found for: "
            + ", ".join(missing),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
