"""Bundled Unicode font registration for voucher PDFs."""

from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path
from typing import Iterable

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont


PDF_FONT_REGULAR = "VoucherNotoSans"
PDF_FONT_BOLD = "VoucherNotoSans-Bold"
PDF_FONT_ITALIC = "VoucherNotoSans-Italic"


class UnsupportedPdfTextError(ValueError):
    """Raised before rendering when bundled fonts cannot represent input text."""


def pdf_font_directory() -> Path:
    """Return the source-tree or PyInstaller directory containing PDF fonts."""

    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        return Path(bundle_root) / "assets" / "fonts"
    return Path(__file__).resolve().parents[2] / "assets" / "fonts"


@lru_cache(maxsize=1)
def ensure_pdf_fonts_registered() -> tuple[str, str, str]:
    """Register the bundled Unicode fonts exactly once per process."""

    root = pdf_font_directory()
    required = (
        (PDF_FONT_REGULAR, root / "NotoSans-Regular.ttf"),
        (PDF_FONT_BOLD, root / "NotoSans-Bold.ttf"),
        (PDF_FONT_ITALIC, root / "NotoSans-Italic.ttf"),
    )
    missing = [path.name for _name, path in required if not path.is_file()]
    if missing:
        raise RuntimeError(
            "Font PDF Unicode mancanti: " + ", ".join(sorted(missing))
        )

    for name, path in required:
        if name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(name, str(path)))

    return PDF_FONT_REGULAR, PDF_FONT_BOLD, PDF_FONT_ITALIC


def unsupported_pdf_characters(values: Iterable[str]) -> tuple[str, ...]:
    """Return unique characters missing from the bundled regular font."""

    ensure_pdf_fonts_registered()
    font = pdfmetrics.getFont(PDF_FONT_REGULAR)
    char_to_glyph = font.face.charToGlyph
    missing: list[str] = []
    seen: set[str] = set()

    for value in values:
        for character in str(value):
            if character in "\r\n\t":
                continue
            if ord(character) in char_to_glyph or character in seen:
                continue
            seen.add(character)
            missing.append(character)

    return tuple(missing)


def validate_pdf_text_support(values: Iterable[str]) -> None:
    """Fail before PDF creation instead of silently dropping missing glyphs."""

    missing = unsupported_pdf_characters(values)
    if not missing:
        return

    labels = [
        (
            f"{character} (U+{ord(character):04X})"
            if character.isprintable() and not character.isspace()
            else f"U+{ord(character):04X}"
        )
        for character in missing[:12]
    ]
    suffix = "" if len(missing) <= 12 else f" e altri {len(missing) - 12}"
    raise UnsupportedPdfTextError(
        "Il font PDF incorporato non supporta alcuni caratteri: "
        + ", ".join(labels)
        + suffix
        + ". Modificare nome destinatario, nome struttura o titolo Wi-Fi "
        "prima di generare il PDF."
    )
