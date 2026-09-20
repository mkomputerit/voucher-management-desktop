"""Filesystem utility helpers used by PDF archive generation."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path


INVALID_WINDOWS_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
MAX_FILENAME_COMPONENT_LENGTH = 96


def sanitize_filename_component(
    value: str,
    fallback: str = "Voucher",
    *,
    max_length: int = MAX_FILENAME_COMPONENT_LENGTH,
) -> str:
    """Return a Windows-safe, bounded filename component.

    Square brackets are valid Windows filename characters and are intentionally
    preserved. Archive lookup must therefore compare Path.name literally
    rather than passing the filename back into glob/rglob as a pattern.
    """
    value = INVALID_WINDOWS_CHARS.sub("_", value.strip())
    value = re.sub(r"\s+", "_", value)
    value = value.rstrip(". ")
    if max_length > 0:
        value = value[:max_length].rstrip(". ")
    return value or fallback[:max_length] or "Voucher"


def find_file_by_exact_name(root: Path, filename: str) -> list[Path]:
    """Find files recursively by literal filename, never glob semantics."""

    wanted = Path(filename).name
    if not wanted or not Path(root).is_dir():
        return []
    matches: list[Path] = []
    for candidate in Path(root).rglob("*"):
        try:
            if candidate.is_file() and candidate.name == wanted:
                matches.append(candidate)
        except OSError:
            # A transient/inaccessible entry must not make another valid
            # archived PDF undiscoverable.
            continue
    return matches


def unique_output_path(folder: Path, recipient: str | None = None) -> Path:
    """Return a non-existing PDF path.

    Revision 2 beta.2 accidentally called this helper with a complete candidate
    path instead of the historical ``(folder, recipient)`` pair.  Accept both
    forms so the print workflow is robust and backwards compatible.
    """
    folder = Path(folder)
    if recipient is None:
        candidate = folder
        if candidate.suffix.lower() != ".pdf":
            candidate = candidate.with_suffix(".pdf")
        parent = candidate.parent
        stem = candidate.stem
    else:
        parent = folder
        stem = f"Voucher_{sanitize_filename_component(recipient, 'SenzaNome')}"
        candidate = parent / f"{stem}.pdf"

    if not candidate.exists():
        return candidate

    index = 2
    while True:
        candidate = parent / f"{stem}_{index}.pdf"
        if not candidate.exists():
            return candidate
        index += 1


def open_with_default_app(path: Path) -> None:
    path = path.resolve()
    if os.name == "nt":
        os.startfile(str(path))  # type: ignore[attr-defined]
        return
    if os.name == "posix":
        subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    raise RuntimeError("Apertura file non supportata su questo sistema operativo")
