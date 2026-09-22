from pathlib import Path

from voucher_management.utils import (
    MAX_FILENAME_COMPONENT_LENGTH,
    find_file_by_exact_name,
    format_fingerprint,
    sanitize_filename_component,
    unique_output_path,
)


def test_unique_output_path_accepts_complete_pdf_path(tmp_path: Path):
    requested = tmp_path / "Voucher_Ospiti.pdf"
    assert unique_output_path(requested) == requested


def test_unique_output_path_increments_complete_pdf_path(tmp_path: Path):
    requested = tmp_path / "Voucher_Ospiti.pdf"
    requested.write_bytes(b"existing")
    assert unique_output_path(requested) == tmp_path / "Voucher_Ospiti_2.pdf"


def test_unique_output_path_keeps_legacy_folder_recipient_form(tmp_path: Path):
    assert unique_output_path(tmp_path, "Sala A") == tmp_path / "Voucher_Sala_A.pdf"


def test_exact_name_lookup_treats_square_brackets_literally(tmp_path: Path):
    folder = tmp_path / "Print" / "2026" / "09"
    folder.mkdir(parents=True)
    wanted = folder / "Voucher_Ospiti[VIP].pdf"
    decoy = folder / "Voucher_OspitiV.pdf"
    wanted.write_bytes(b"wanted")
    decoy.write_bytes(b"decoy")

    assert find_file_by_exact_name(
        tmp_path / "Print",
        wanted.name,
    ) == [wanted]


def test_sanitized_filename_component_is_bounded_for_windows_paths():
    result = sanitize_filename_component("A" * 500)
    assert len(result) <= MAX_FILENAME_COMPONENT_LENGTH


def test_fingerprint_formatting_is_consistent_and_idempotent():
    raw = "a1b2c3d4e5f60708"
    formatted = "A1:B2:C3:D4:E5:F6:07:08"

    assert format_fingerprint(raw) == formatted
    assert format_fingerprint(formatted) == formatted
    assert format_fingerprint(" A1 B2 C3 D4 E5 F6 07 08 ") == formatted
