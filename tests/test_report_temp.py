"""Regression tests for privacy-safe administrative report temp files."""

from __future__ import annotations

import os
import time
from pathlib import Path

from voucher_management.report_temp import (
    REPORT_TEMP_MARKER,
    REPORT_TEMP_MARKER_CONTENT,
    REPORT_TEMP_PREFIX,
    cleanup_orphan_report_temps,
    create_report_temporary_directory,
)


def test_created_report_temp_is_marked_for_crash_cleanup():
    temporary = create_report_temporary_directory()
    try:
        root = Path(temporary.name)
        assert root.name.startswith(REPORT_TEMP_PREFIX)
        assert (root / REPORT_TEMP_MARKER).read_text(
            encoding="utf-8"
        ) == REPORT_TEMP_MARKER_CONTENT
    finally:
        temporary.cleanup()


def test_cleanup_removes_only_old_exactly_marked_report_dirs(tmp_path):
    now = time.time()
    old = tmp_path / f"{REPORT_TEMP_PREFIX}old"
    recent = tmp_path / f"{REPORT_TEMP_PREFIX}recent"
    unmarked = tmp_path / f"{REPORT_TEMP_PREFIX}unmarked"
    for root in (old, recent, unmarked):
        root.mkdir()
        (root / "report.pdf").write_bytes(b"%PDF-test")

    for root in (old, recent):
        marker = root / REPORT_TEMP_MARKER
        marker.write_text(REPORT_TEMP_MARKER_CONTENT, encoding="utf-8")

    os.utime(old / REPORT_TEMP_MARKER, (now - 7200, now - 7200))
    os.utime(recent / REPORT_TEMP_MARKER, (now, now))

    removed = cleanup_orphan_report_temps(
        temp_root=tmp_path,
        now=now,
        min_age_seconds=3600,
    )

    assert removed == (old,)
    assert not old.exists()
    assert recent.exists()
    assert unmarked.exists()
