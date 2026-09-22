"""Managed paths and retention for generated voucher PDFs."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

from .utils import sanitize_filename_component


DEFAULT_PRINT_RETENTION_DAYS = 0
MAX_ARCHIVE_PATH_CHARS = 240
_MANAGED_PDF_RE = re.compile(
    r"^Voucher_.+_(\d{8})_(\d{6})(?:_\d+)?\.pdf$",
    re.IGNORECASE,
)


def _bounded_recipient_component(value: str, max_length: int) -> str:
    """Keep a readable recipient while preserving uniqueness when truncated."""

    cleaned = sanitize_filename_component(
        value,
        fallback="Voucher",
        max_length=0,
    )
    if len(cleaned) <= max_length:
        return cleaned

    digest = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:8]
    prefix_length = max_length - len(digest) - 1
    if prefix_length < 1:
        raise OSError(
            "Percorso Print troppo lungo per creare un nome PDF sicuro"
        )
    prefix = cleaned[:prefix_length].rstrip(" ._") or "V"
    return f"{prefix}_{digest}"


def build_voucher_pdf_path(
    print_root: Path,
    recipient: str,
    when: datetime,
    *,
    max_path_chars: int = MAX_ARCHIVE_PATH_CHARS,
) -> Path:
    """Return a unique managed PDF path bounded for conservative Windows paths."""

    folder = Path(print_root) / f"{when:%Y}" / f"{when:%m}"
    folder.mkdir(parents=True, exist_ok=True)
    timestamp = f"{when:%Y%m%d_%H%M%S}"
    absolute_folder = folder.absolute()

    index = 1
    while True:
        collision = "" if index == 1 else f"_{index}"
        fixed = f"Voucher__{timestamp}{collision}.pdf"
        available = (
            max_path_chars
            - len(str(absolute_folder))
            - 1
            - len(fixed)
        )
        if available < 12:
            raise OSError(
                "Percorso Print troppo lungo per creare un PDF entro "
                f"{max_path_chars} caratteri"
            )

        component = _bounded_recipient_component(
            recipient or "Voucher",
            available,
        )
        candidate = folder / (
            f"Voucher_{component}_{timestamp}{collision}.pdf"
        )
        if len(str(candidate.absolute())) > max_path_chars:
            raise OSError(
                "Percorso Print troppo lungo per creare un PDF sicuro"
            )
        if not candidate.exists():
            return candidate
        index += 1


def _managed_timestamp(filename: str) -> datetime | None:
    match = _MANAGED_PDF_RE.fullmatch(filename)
    if not match:
        return None
    try:
        return datetime.strptime(
            "".join(match.groups()),
            "%Y%m%d%H%M%S",
        )
    except ValueError:
        return None


def cleanup_print_archive(
    print_root: Path,
    retention_days: int,
    managed_output_names: Iterable[str],
    *,
    now: datetime | None = None,
) -> list[Path]:
    """Delete only expired PDFs both app-shaped and referenced by audit history.

    History rows are retained. Files not known to HistoryService, files outside
    Print/YYYY/MM, and names that do not match the application naming scheme are
    never removed.
    """

    days = int(retention_days)
    if days <= 0:
        return []

    root = Path(print_root)
    if not root.is_dir():
        return []

    reference_names = {
        Path(str(name)).name
        for name in managed_output_names
        if str(name).strip()
    }
    if not reference_names:
        return []

    current = now or datetime.now()
    if current.tzinfo is not None:
        current = current.replace(tzinfo=None)
    cutoff = current - timedelta(days=days)
    removed: list[Path] = []

    for candidate in root.rglob("*.pdf"):
        try:
            relative = candidate.relative_to(root)
        except ValueError:
            continue
        if len(relative.parts) != 3:
            continue

        year, month, filename = relative.parts
        if (
            len(year) != 4
            or not year.isdigit()
            or len(month) != 2
            or not month.isdigit()
            or not 1 <= int(month) <= 12
            or filename not in reference_names
        ):
            continue

        created = _managed_timestamp(filename)
        if created is None or created >= cutoff:
            continue

        try:
            candidate.unlink()
        except OSError:
            continue
        removed.append(candidate)

    # Remove only directories made empty by the cleanup. Unknown/user files
    # therefore keep their containing folders intact.
    for candidate in removed:
        for directory in (candidate.parent, candidate.parent.parent):
            try:
                directory.rmdir()
            except OSError:
                break

    return removed


def cleanup_orphan_pdf_temps(
    print_root: Path,
    *,
    older_than_hours: int = 24,
    now: datetime | None = None,
) -> list[Path]:
    """Remove only stale renderer temp files left by interrupted processes."""

    root = Path(print_root)
    if not root.is_dir():
        return []

    hours = max(1, int(older_than_hours))
    current = now or datetime.now()
    if current.tzinfo is not None:
        current = current.replace(tzinfo=None)
    cutoff = current - timedelta(hours=hours)
    removed: list[Path] = []

    for candidate in root.rglob(".Voucher_*.tmp"):
        try:
            if candidate.is_symlink() or not candidate.is_file():
                continue
            relative = candidate.relative_to(root)
        except (OSError, ValueError):
            continue

        if len(relative.parts) != 3:
            continue
        year, month, _filename = relative.parts
        if (
            len(year) != 4
            or not year.isdigit()
            or len(month) != 2
            or not month.isdigit()
            or not 1 <= int(month) <= 12
        ):
            continue

        try:
            modified = datetime.fromtimestamp(candidate.stat().st_mtime)
            if modified >= cutoff:
                continue
            candidate.unlink()
        except OSError:
            continue
        removed.append(candidate)

    for candidate in removed:
        for directory in (candidate.parent, candidate.parent.parent):
            try:
                directory.rmdir()
            except OSError:
                break

    return removed
