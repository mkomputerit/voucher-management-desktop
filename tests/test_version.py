from pathlib import Path

from voucher_management import __version__


def test_package_version_matches_release_version_file():
    root = Path(__file__).resolve().parents[1]
    release_version = (
        root / "version.txt"
    ).read_text(encoding="utf-8").strip()

    assert release_version
    assert __version__ == release_version
