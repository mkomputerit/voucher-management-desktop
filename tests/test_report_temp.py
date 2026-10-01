from pathlib import Path

from voucher_management.report_temp import (
    REPORT_TEMP_MARKER,
    REPORT_TEMP_MARKER_CONTENT,
    REPORT_TEMP_PREFIX,
    cleanup_orphan_report_temps,
    create_report_temporary_directory,
)


def test_marked_report_temp_is_removed_when_old(tmp_path):
    temporary = create_report_temporary_directory()
    root = Path(temporary.name)
    try:
        marker = root / REPORT_TEMP_MARKER
        assert root.name.startswith(REPORT_TEMP_PREFIX)
        assert marker.read_text(encoding="utf-8") == REPORT_TEMP_MARKER_CONTENT

        # Recreate the same contract under the isolated tmp_path so cleanup
        # does not scan the process-global temporary directory in this test.
        isolated = tmp_path / root.name
        isolated.mkdir()
        isolated_marker = isolated / REPORT_TEMP_MARKER
        isolated_marker.write_text(REPORT_TEMP_MARKER_CONTENT, encoding="utf-8")
        payload = isolated / "report.pdf"
        payload.write_bytes(b"%PDF-test")

        removed = cleanup_orphan_report_temps(
            temp_root=tmp_path,
            now=isolated_marker.stat().st_mtime + 25,
            min_age_seconds=20,
        )
        assert removed == (isolated,)
        assert not isolated.exists()
    finally:
        temporary.cleanup()


def test_cleanup_ignores_prefix_without_exact_marker(tmp_path):
    candidate = tmp_path / f"{REPORT_TEMP_PREFIX}foreign"
    candidate.mkdir()
    (candidate / "report.pdf").write_bytes(b"%PDF-test")
    (candidate / REPORT_TEMP_MARKER).write_text("not-our-marker\n", encoding="utf-8")

    removed = cleanup_orphan_report_temps(
        temp_root=tmp_path,
        now=10**10,
        min_age_seconds=0,
    )

    assert removed == ()
    assert candidate.exists()
