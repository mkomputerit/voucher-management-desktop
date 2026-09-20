import struct
from pathlib import Path

from tools.generate_app_icon import ICON_SIZES, ensure_app_icon
from tools.verify_exe_icon import parse_group_icon, read_ico_frames


def test_read_ico_frames_returns_all_expected_sizes(tmp_path: Path):
    icon = tmp_path / "VoucherManagement.ico"
    ensure_app_icon(icon)

    frames = read_ico_frames(icon)

    assert {(frame.width, frame.height) for frame in frames} == {
        (size, size) for size in ICON_SIZES
    }
    assert all(frame.payload for frame in frames)


def test_parse_group_icon_reads_resource_ids_and_256_encoding():
    payload = struct.pack("<HHH", 0, 1, 2)
    payload += struct.pack(
        "<BBBBHHIH",
        16,
        16,
        0,
        0,
        1,
        32,
        1234,
        7,
    )
    payload += struct.pack(
        "<BBBBHHIH",
        0,
        0,
        0,
        0,
        1,
        32,
        5678,
        9,
    )

    assert parse_group_icon(payload) == (
        (16, 16, 1234, 7),
        (256, 256, 5678, 9),
    )


def test_parse_group_icon_rejects_truncated_payload():
    payload = struct.pack("<HHH", 0, 1, 1)

    try:
        parse_group_icon(payload)
    except ValueError as exc:
        assert "truncated" in str(exc).lower()
    else:
        raise AssertionError("truncated RT_GROUP_ICON was accepted")
