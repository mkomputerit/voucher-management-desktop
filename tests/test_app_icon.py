from pathlib import Path
import sys

from PIL import Image

from tools.generate_app_icon import (
    ICON_SIZES,
    _draw_ticket_icon,
    ensure_app_icon,
)
from voucher_management.app import bundled_app_icon_path


def test_generated_icon_contains_standard_windows_sizes(tmp_path: Path):
    target = tmp_path / "VoucherManagement.ico"
    ensure_app_icon(target)

    assert target.is_file()
    with Image.open(target) as image:
        assert image.format == "ICO"
        assert set(ICON_SIZES).issubset(
            {width for width, height in image.ico.sizes() if width == height}
        )


def test_bundled_app_icon_path_uses_pyinstaller_root(tmp_path: Path, monkeypatch):
    icon = tmp_path / "assets" / "VoucherManagement.ico"
    icon.parent.mkdir(parents=True)
    icon.write_bytes(b"synthetic-icon")

    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

    assert bundled_app_icon_path() == icon


def test_bundled_app_icon_path_is_optional(monkeypatch):
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)

    assert bundled_app_icon_path() is None


def test_generated_icon_is_byte_reproducible(tmp_path: Path):
    first = tmp_path / "first.ico"
    second = tmp_path / "second.ico"

    ensure_app_icon(first)
    ensure_app_icon(second)

    assert first.read_bytes() == second.read_bytes()


def test_16_and_24_px_frames_use_dedicated_high_contrast_artwork(tmp_path: Path):
    target = tmp_path / "VoucherManagement.ico"
    ensure_app_icon(target)
    master = _draw_ticket_icon()

    with Image.open(target) as image:
        for size in (16, 24):
            frame = image.ico.getimage((size, size)).convert("RGBA")
            reduced_master = master.resize(
                (size, size),
                Image.Resampling.LANCZOS,
            )

            # Tiny frames must not silently regress to a master downscale.
            assert frame.tobytes() != reduced_master.tobytes()

            # Require visible dark-blue structure in addition to the green badge.
            pixels = list(frame.getdata())
            dark_blue_pixels = sum(
                1
                for red, green, blue, alpha in pixels
                if alpha >= 180
                and red <= 80
                and green <= 140
                and blue >= 90
            )
            green_pixels = sum(
                1
                for red, green, blue, alpha in pixels
                if alpha >= 180
                and red <= 80
                and green >= 120
                and blue <= 130
            )
            white_pixels = sum(
                1
                for red, green, blue, alpha in pixels
                if alpha >= 180
                and red >= 215
                and green >= 215
                and blue >= 215
            )
            assert dark_blue_pixels >= max(4, size // 2)
            assert green_pixels >= max(6, size // 2)
            assert white_pixels >= (2 if size == 16 else 4)
