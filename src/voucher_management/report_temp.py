"""Privacy-safe lifecycle for temporary administrative report previews."""

from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path


REPORT_TEMP_PREFIX = "voucher-management-report-"
REPORT_TEMP_MARKER = ".voucher-management-report-temp"
REPORT_TEMP_MARKER_CONTENT = "voucher-management-report-temp-v1\n"
DEFAULT_ORPHAN_AGE_SECONDS = 24 * 60 * 60


def create_report_temporary_directory() -> tempfile.TemporaryDirectory:
    """Create a marked per-user temporary directory for one report preview."""

    temporary = tempfile.TemporaryDirectory(prefix=REPORT_TEMP_PREFIX)
    root = Path(temporary.name)
    marker = root / REPORT_TEMP_MARKER
    marker.write_text(REPORT_TEMP_MARKER_CONTENT, encoding="utf-8")
    return temporary


def cleanup_orphan_report_temps(
    *,
    temp_root: Path | None = None,
    now: float | None = None,
    min_age_seconds: int = DEFAULT_ORPHAN_AGE_SECONDS,
) -> tuple[Path, ...]:
    """Remove only old report temp directories carrying our exact marker.

    Prefix matching alone is deliberately insufficient: a directory must also
    contain the marker written by create_report_temporary_directory(). Symlinks
    are ignored. Failures are skipped so startup never becomes unavailable just
    because Windows still has a stale preview file open.
    """

    if min_age_seconds < 0:
        raise ValueError("min_age_seconds must be non-negative")
    root = Path(temp_root) if temp_root is not None else Path(tempfile.gettempdir())
    current = time.time() if now is None else float(now)
    removed: list[Path] = []

    try:
        candidates = tuple(root.glob(f"{REPORT_TEMP_PREFIX}*"))
    except OSError:
        return ()

    for candidate in candidates:
        try:
            if candidate.is_symlink() or not candidate.is_dir():
                continue
            marker = candidate / REPORT_TEMP_MARKER
            if (
                not marker.is_file()
                or marker.is_symlink()
                or marker.read_text(encoding="utf-8")
                != REPORT_TEMP_MARKER_CONTENT
            ):
                continue
            age = current - marker.stat().st_mtime
            if age < min_age_seconds:
                continue
            shutil.rmtree(candidate)
            removed.append(candidate)
        except (OSError, UnicodeError):
            continue

    return tuple(removed)
