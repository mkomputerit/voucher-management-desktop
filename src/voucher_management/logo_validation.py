"""Validation for operator-supplied voucher logos."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, UnidentifiedImageError


ALLOWED_LOGO_FORMATS = ("PNG", "JPEG")
MAX_LOGO_WIDTH = 8192
MAX_LOGO_HEIGHT = 8192
MAX_LOGO_PIXELS = 40_000_000
MAX_LOGO_FILE_BYTES = 25 * 1024 * 1024


class LogoValidationError(ValueError):
    """Raised when a custom logo is not a valid PNG or JPEG image."""


class LogoLimitError(LogoValidationError):
    """Raised when an otherwise valid logo exceeds current policy limits."""


def validate_logo_image(path: Path) -> None:
    """Validate an image before it can enter the persistent logo library.

    Decoder selection is restricted explicitly to PNG/JPEG so a different
    Pillow-supported format cannot be smuggled in under a trusted extension.
    Dimensions and file size are bounded before ReportLab ever renders it.
    """

    path = Path(path)
    try:
        file_size = path.stat().st_size
    except OSError as exc:
        raise LogoValidationError("Il logo non è leggibile") from exc

    if file_size <= 0:
        raise LogoValidationError("Il logo è vuoto")

    try:
        # Image.open() is lazy: restricting formats here parses only enough of
        # the header to identify PNG/JPEG before any full image decode.
        with Image.open(path, formats=ALLOWED_LOGO_FORMATS) as image:
            width, height = image.size
            if file_size > MAX_LOGO_FILE_BYTES:
                raise LogoLimitError("Il logo supera il limite di 25 MiB")
            if width <= 0 or height <= 0:
                raise LogoValidationError("Il logo ha dimensioni non valide")
            if (
                width > MAX_LOGO_WIDTH
                or height > MAX_LOGO_HEIGHT
                or width * height > MAX_LOGO_PIXELS
            ):
                raise LogoLimitError(
                    "Il logo supera il limite di 8192×8192 pixel / 40 megapixel"
                )
            # verify() validates container structure but does not fully
            # decode JPEG pixel data. load() forces the bounded image payload to
            # be read so truncated JPEGs fail before they enter the logo library.
            image.load()
    except LogoValidationError:
        raise
    except (
        UnidentifiedImageError,
        Image.DecompressionBombError,
        OSError,
        SyntaxError,
        ValueError,
    ) as exc:
        raise LogoValidationError(
            "Il logo deve essere un file PNG o JPEG valido"
        ) from exc
