from pathlib import Path

import pytest
from PIL import Image

from voucher_management.logo_validation import (
    LogoLimitError,
    LogoValidationError,
    validate_logo_image,
)


def test_accepts_valid_png(tmp_path: Path):
    logo = tmp_path / "logo.png"
    Image.new("RGB", (64, 32), "white").save(logo, format="PNG")

    validate_logo_image(logo)


def test_accepts_valid_jpeg(tmp_path: Path):
    logo = tmp_path / "logo.jpg"
    Image.new("RGB", (64, 32), "white").save(logo, format="JPEG")

    validate_logo_image(logo)


def test_rejects_other_decoder_even_with_png_extension(tmp_path: Path):
    logo = tmp_path / "renamed.png"
    logo.write_bytes(b"8BPS\x00\x01synthetic-psd-payload")

    with pytest.raises(LogoValidationError, match="PNG o JPEG"):
        validate_logo_image(logo)


def test_accepts_legacy_24_megapixel_png(tmp_path: Path):
    logo = tmp_path / "legacy-large.png"
    Image.new("1", (6000, 4000), 1).save(logo, format="PNG")

    validate_logo_image(logo)


def test_rejects_oversized_dimensions(tmp_path: Path):
    logo = tmp_path / "wide.png"
    Image.new("1", (8193, 1), 1).save(logo, format="PNG")

    with pytest.raises(LogoLimitError, match="8192"):
        validate_logo_image(logo)


def test_rejects_more_than_40_megapixels(tmp_path: Path):
    logo = tmp_path / "too-many-pixels.png"
    Image.new("1", (7000, 6000), 1).save(logo, format="PNG")

    with pytest.raises(LogoLimitError, match="40 megapixel"):
        validate_logo_image(logo)



def test_rejects_truncated_jpeg_payload(tmp_path: Path):
    logo = tmp_path / "truncated.jpg"
    source = tmp_path / "source.jpg"
    Image.new("RGB", (256, 256), "white").save(source, format="JPEG", quality=90)
    payload = source.read_bytes()
    logo.write_bytes(payload[:-128])

    with pytest.raises(LogoValidationError, match="PNG o JPEG valido"):
        validate_logo_image(logo)
