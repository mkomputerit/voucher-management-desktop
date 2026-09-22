"""Verify that a Windows executable embeds the expected project icon.

The check compares the raw image payloads referenced by RT_GROUP_ICON/RT_ICON
resources with every frame in the generated .ico file. A generic/default
Windows or PyInstaller icon therefore cannot satisfy the check.
"""

from __future__ import annotations

import argparse
import struct
from dataclasses import dataclass
from pathlib import Path


RT_ICON = 3
RT_GROUP_ICON = 14


@dataclass(frozen=True)
class IcoFrame:
    width: int
    height: int
    payload: bytes


def read_ico_frames(path: Path) -> tuple[IcoFrame, ...]:
    """Read raw image payloads from a Windows ICO container."""

    data = Path(path).read_bytes()
    if len(data) < 6:
        raise ValueError("ICO header is truncated")

    reserved, icon_type, count = struct.unpack_from("<HHH", data, 0)
    if reserved != 0 or icon_type != 1 or count < 1:
        raise ValueError("Invalid ICO header")

    directory_end = 6 + count * 16
    if len(data) < directory_end:
        raise ValueError("ICO directory is truncated")

    frames: list[IcoFrame] = []
    for index in range(count):
        offset = 6 + index * 16
        (
            width_raw,
            height_raw,
            _color_count,
            _reserved,
            _planes,
            _bit_count,
            byte_count,
            image_offset,
        ) = struct.unpack_from("<BBBBHHII", data, offset)

        width = 256 if width_raw == 0 else width_raw
        height = 256 if height_raw == 0 else height_raw
        end = image_offset + byte_count
        if image_offset < directory_end or end > len(data):
            raise ValueError("ICO frame points outside the file")

        frames.append(
            IcoFrame(
                width=width,
                height=height,
                payload=data[image_offset:end],
            )
        )

    return tuple(frames)


def parse_group_icon(payload: bytes) -> tuple[tuple[int, int, int, int], ...]:
    """Return width, height, byte-count and RT_ICON resource id entries."""

    if len(payload) < 6:
        raise ValueError("RT_GROUP_ICON header is truncated")

    reserved, icon_type, count = struct.unpack_from("<HHH", payload, 0)
    if reserved != 0 or icon_type != 1 or count < 1:
        raise ValueError("Invalid RT_GROUP_ICON header")

    expected_length = 6 + count * 14
    if len(payload) < expected_length:
        raise ValueError("RT_GROUP_ICON directory is truncated")

    entries: list[tuple[int, int, int, int]] = []
    for index in range(count):
        offset = 6 + index * 14
        (
            width_raw,
            height_raw,
            _color_count,
            _reserved,
            _planes,
            _bit_count,
            byte_count,
            resource_id,
        ) = struct.unpack_from("<BBBBHHIH", payload, offset)
        entries.append(
            (
                256 if width_raw == 0 else width_raw,
                256 if height_raw == 0 else height_raw,
                byte_count,
                resource_id,
            )
        )
    return tuple(entries)


def _resource_payload(pe, language_entry) -> bytes:
    data = language_entry.data.struct
    return pe.get_data(data.OffsetToData, data.Size)


def _resource_type_entry(pe, type_id: int):
    root = getattr(pe, "DIRECTORY_ENTRY_RESOURCE", None)
    if root is None:
        return None
    for entry in root.entries:
        if entry.id == type_id:
            return entry
    return None


def _icon_payloads_by_id(pe) -> dict[int, tuple[bytes, ...]]:
    icon_type = _resource_type_entry(pe, RT_ICON)
    if icon_type is None:
        return {}

    result: dict[int, tuple[bytes, ...]] = {}
    for icon_entry in icon_type.directory.entries:
        if icon_entry.id is None or not hasattr(icon_entry, "directory"):
            continue
        payloads = tuple(
            _resource_payload(pe, language_entry)
            for language_entry in icon_entry.directory.entries
            if hasattr(language_entry, "data")
        )
        if payloads:
            result[int(icon_entry.id)] = payloads
    return result


def _first_group_payload(pe) -> bytes | None:
    """Return the first RT_GROUP_ICON payload Windows will consider."""

    group_type = _resource_type_entry(pe, RT_GROUP_ICON)
    if group_type is None:
        return None

    for group_entry in group_type.directory.entries:
        if not hasattr(group_entry, "directory"):
            continue
        for language_entry in group_entry.directory.entries:
            if hasattr(language_entry, "data"):
                return _resource_payload(pe, language_entry)
    return None


def verify_exe_icon(exe_path: Path, ico_path: Path) -> None:
    """Require one executable icon group to match every expected ICO frame."""

    try:
        import pefile
    except ImportError as exc:
        raise RuntimeError("pefile is required for executable icon verification") from exc

    expected_frames = read_ico_frames(ico_path)
    expected_by_size = {
        (frame.width, frame.height): frame.payload for frame in expected_frames
    }
    if len(expected_by_size) != len(expected_frames):
        raise ValueError("Expected ICO contains duplicate frame sizes")

    pe = pefile.PE(str(exe_path), fast_load=False)
    try:
        pe.parse_data_directories(
            directories=[
                pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_RESOURCE"]
            ]
        )
        icon_payloads = _icon_payloads_by_id(pe)
        group_payload = _first_group_payload(pe)
        if group_payload is None:
            raise ValueError("Executable does not contain an RT_GROUP_ICON resource")

        entries = parse_group_icon(group_payload)
        group_sizes = {(width, height) for width, height, _, _ in entries}
        if group_sizes != set(expected_by_size):
            raise ValueError(
                "The first executable icon group does not expose the expected sizes"
            )
        if len(entries) != len(expected_by_size):
            raise ValueError(
                "The first executable icon group contains duplicate frame sizes"
            )

        for width, height, byte_count, resource_id in entries:
            expected = expected_by_size[(width, height)]
            if byte_count != len(expected):
                raise ValueError(
                    "The first executable icon group has an unexpected payload size"
                )
            candidates = icon_payloads.get(resource_id, ())
            if expected not in candidates:
                raise ValueError(
                    "The first executable icon group does not match "
                    "the generated VoucherManagement.ico"
                )

        return
    finally:
        pe.close()



def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--ico", type=Path, required=True)
    args = parser.parse_args()

    verify_exe_icon(args.exe, args.ico)
    print("Windows executable icon resources match the generated ICO.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
