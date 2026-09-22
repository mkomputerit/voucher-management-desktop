import os
from datetime import datetime, timedelta

from voucher_management.print_archive import (
    DEFAULT_PRINT_RETENTION_DAYS,
    MAX_ARCHIVE_PATH_CHARS,
    build_voucher_pdf_path,
    cleanup_orphan_pdf_temps,
    cleanup_print_archive,
)


def test_managed_output_path_is_bounded_and_unique(tmp_path):
    root = tmp_path / ("profile_" + "x" * 48) / "Print"
    when = datetime(2026, 9, 21, 8, 30, 45)
    recipient = "Very Long Recipient " + "A" * 500

    first = build_voucher_pdf_path(root, recipient, when)
    assert len(str(first.absolute())) <= MAX_ARCHIVE_PATH_CHARS
    assert first.name.startswith("Voucher_")
    assert first.name.endswith("_20260921_083045.pdf")

    first.write_bytes(b"existing")
    second = build_voucher_pdf_path(root, recipient, when)

    assert second != first
    assert second.name.endswith("_20260921_083045_2.pdf")
    assert len(str(second.absolute())) <= MAX_ARCHIVE_PATH_CHARS


def test_truncated_recipients_keep_distinct_hash_suffixes(tmp_path):
    root = tmp_path / "Print"
    when = datetime(2026, 9, 21, 8, 30, 45)
    common = "A" * 500

    first = build_voucher_pdf_path(root, common + "X", when)
    second = build_voucher_pdf_path(root, common + "Y", when)

    assert first.name != second.name


def test_path_builder_refuses_impossibly_long_parent(tmp_path):
    root = tmp_path / ("x" * 80) / "Print"

    try:
        build_voucher_pdf_path(
            root,
            "Guest",
            datetime(2026, 9, 21, 8, 30, 45),
            max_path_chars=60,
        )
    except OSError:
        pass
    else:
        raise AssertionError("Overlong parent path was accepted")


def test_retention_removes_only_old_audited_managed_pdfs(tmp_path):
    root = tmp_path / "Print"
    old_dir = root / "2025" / "01"
    recent_dir = root / "2026" / "09"
    old_dir.mkdir(parents=True)
    recent_dir.mkdir(parents=True)

    old = old_dir / "Voucher_Old_20250101_120000.pdf"
    recent = recent_dir / "Voucher_Recent_20260920_120000.pdf"
    untracked = old_dir / "Voucher_Untracked_20250102_120000.pdf"
    manual = old_dir / "manual-reference.pdf"

    for path in (old, recent, untracked, manual):
        path.write_bytes(b"PDF")

    removed = cleanup_print_archive(
        root,
        365,
        {old.name, recent.name, manual.name},
        now=datetime(2026, 9, 21, 12, 0, 0),
    )

    assert removed == [old]
    assert not old.exists()
    assert recent.exists()
    assert untracked.exists()
    assert manual.exists()


def test_zero_retention_means_keep_forever(tmp_path):
    root = tmp_path / "Print" / "2020" / "01"
    root.mkdir(parents=True)
    old = root / "Voucher_Old_20200101_120000.pdf"
    old.write_bytes(b"PDF")

    removed = cleanup_print_archive(
        tmp_path / "Print",
        0,
        {old.name},
        now=datetime(2026, 9, 21, 12, 0, 0),
    )

    assert removed == []
    assert old.exists()



def test_default_retention_is_non_destructive():
    assert DEFAULT_PRINT_RETENTION_DAYS == 0


def test_orphan_temp_cleanup_removes_only_old_managed_renderer_files(tmp_path):
    root = tmp_path / "Print"
    folder = root / "2026" / "09"
    folder.mkdir(parents=True)
    now = datetime(2026, 9, 21, 12, 0, 0)

    old = folder / ".Voucher_Old_20260920_100000-deadbeef.tmp"
    recent = folder / ".Voucher_Recent_20260921_110000-cafebabe.tmp"
    unrelated = folder / "manual.tmp"
    root_level = root / ".Voucher_Root_20260920_100000-feedface.tmp"

    for path in (old, recent, unrelated, root_level):
        path.write_bytes(b"%PDF-sensitive-test-data")

    old_time = (now - timedelta(hours=25)).timestamp()
    recent_time = (now - timedelta(hours=1)).timestamp()
    os.utime(old, (old_time, old_time))
    os.utime(unrelated, (old_time, old_time))
    os.utime(root_level, (old_time, old_time))
    os.utime(recent, (recent_time, recent_time))

    removed = cleanup_orphan_pdf_temps(
        root,
        older_than_hours=24,
        now=now,
    )

    assert removed == [old]
    assert not old.exists()
    assert recent.exists()
    assert unrelated.exists()
    assert root_level.exists()
