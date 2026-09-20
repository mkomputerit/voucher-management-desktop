"""Checks for Windows executable metadata source consistency."""

from pathlib import Path


def test_windows_version_resource_matches_release_version():
    root = Path(__file__).resolve().parents[1]
    version = (root / "version.txt").read_text(encoding="utf-8").strip()
    resource = (root / "windows_version_info.txt").read_text(encoding="utf-8")

    numeric = version.split("-", 1)[0]
    major, minor, patch = (int(part) for part in numeric.split("."))

    assert f"filevers=({major}, {minor}, {patch}, 0)" in resource
    assert f"prodvers=({major}, {minor}, {patch}, 0)" in resource
    assert "StringStruct('ProductName', 'Voucher Management')" in resource
    assert "StringStruct('FileDescription', 'Voucher Management')" in resource
    assert "StringStruct('OriginalFilename', 'VoucherManagement.exe')" in resource
    assert f"StringStruct('FileVersion', '{version}')" in resource
    assert f"StringStruct('ProductVersion', '{version}')" in resource
