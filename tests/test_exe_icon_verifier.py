import struct
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.generate_app_icon import ICON_SIZES, ensure_app_icon
from tools.verify_exe_icon import parse_group_icon, read_ico_frames, verify_exe_icon


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



def _group_payload(frames, resource_ids):
    payload = struct.pack("<HHH", 0, 1, len(frames))
    for frame, resource_id in zip(frames, resource_ids):
        width = 0 if frame.width == 256 else frame.width
        height = 0 if frame.height == 256 else frame.height
        payload += struct.pack(
            "<BBBBHHIH",
            width,
            height,
            0,
            0,
            1,
            32,
            len(frame.payload),
            resource_id,
        )
    return payload


class _FakePE:
    def __init__(self, icon_payloads, group_payloads):
        self.closed = False
        self._data = {}
        offset = 1000

        icon_entries = []
        for resource_id, payload in icon_payloads.items():
            self._data[(offset, len(payload))] = payload
            language = SimpleNamespace(
                data=SimpleNamespace(
                    struct=SimpleNamespace(
                        OffsetToData=offset,
                        Size=len(payload),
                    )
                )
            )
            icon_entries.append(
                SimpleNamespace(
                    id=resource_id,
                    directory=SimpleNamespace(entries=[language]),
                )
            )
            offset += len(payload) + 17

        group_entries = []
        for payload in group_payloads:
            self._data[(offset, len(payload))] = payload
            language = SimpleNamespace(
                data=SimpleNamespace(
                    struct=SimpleNamespace(
                        OffsetToData=offset,
                        Size=len(payload),
                    )
                )
            )
            group_entries.append(
                SimpleNamespace(
                    id=len(group_entries) + 1,
                    directory=SimpleNamespace(entries=[language]),
                )
            )
            offset += len(payload) + 17

        self.DIRECTORY_ENTRY_RESOURCE = SimpleNamespace(
            entries=[
                SimpleNamespace(
                    id=3,
                    directory=SimpleNamespace(entries=icon_entries),
                ),
                SimpleNamespace(
                    id=14,
                    directory=SimpleNamespace(entries=group_entries),
                ),
            ]
        )

    def parse_data_directories(self, directories):
        assert directories == [2]

    def get_data(self, offset, size):
        return self._data[(offset, size)]

    def close(self):
        self.closed = True


def _install_fake_pefile(monkeypatch, fake_pe):
    module = SimpleNamespace(
        PE=lambda _path, fast_load=False: fake_pe,
        DIRECTORY_ENTRY={"IMAGE_DIRECTORY_ENTRY_RESOURCE": 2},
    )
    monkeypatch.setitem(sys.modules, "pefile", module)


def test_verify_exe_icon_reads_pe_and_accepts_matching_first_group(
    tmp_path,
    monkeypatch,
):
    icon = tmp_path / "VoucherManagement.ico"
    ensure_app_icon(icon)
    frames = read_ico_frames(icon)
    resource_ids = list(range(10, 10 + len(frames)))
    fake_pe = _FakePE(
        {
            resource_id: frame.payload
            for resource_id, frame in zip(resource_ids, frames)
        },
        [_group_payload(frames, resource_ids)],
    )
    _install_fake_pefile(monkeypatch, fake_pe)

    verify_exe_icon(tmp_path / "VoucherManagement.exe", icon)

    assert fake_pe.closed


def test_verify_exe_icon_rejects_bad_first_group_even_if_second_matches(
    tmp_path,
    monkeypatch,
):
    icon = tmp_path / "VoucherManagement.ico"
    ensure_app_icon(icon)
    frames = read_ico_frames(icon)
    resource_ids = list(range(20, 20 + len(frames)))
    good = _group_payload(frames, resource_ids)
    bad = _group_payload(frames[:-1], resource_ids[:-1])
    fake_pe = _FakePE(
        {
            resource_id: frame.payload
            for resource_id, frame in zip(resource_ids, frames)
        },
        [bad, good],
    )
    _install_fake_pefile(monkeypatch, fake_pe)

    with pytest.raises(ValueError, match="first executable icon group"):
        verify_exe_icon(tmp_path / "VoucherManagement.exe", icon)

    assert fake_pe.closed
