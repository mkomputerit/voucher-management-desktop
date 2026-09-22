import pytest
from reportlab.pdfbase import pdfmetrics

from voucher_management.pdf_fonts import (
    PDF_FONT_BOLD,
    PDF_FONT_ITALIC,
    PDF_FONT_REGULAR,
    UnsupportedPdfTextError,
    ensure_pdf_fonts_registered,
    pdf_font_directory,
    unsupported_pdf_characters,
    validate_pdf_text_support,
)


def test_bundled_unicode_fonts_exist_and_register():
    regular, bold, italic = ensure_pdf_fonts_registered()

    assert (regular, bold, italic) == (
        PDF_FONT_REGULAR,
        PDF_FONT_BOLD,
        PDF_FONT_ITALIC,
    )
    root = pdf_font_directory()
    assert (root / "NotoSans-Regular.ttf").is_file()
    assert (root / "NotoSans-Bold.ttf").is_file()
    assert (root / "NotoSans-Italic.ttf").is_file()


def test_regular_font_covers_extended_latin_greek_and_cyrillic():
    ensure_pdf_fonts_registered()
    font = pdfmetrics.getFont(PDF_FONT_REGULAR)

    for character in "ÈŁŽășΩЖ":
        assert ord(character) in font.face.charWidths



def test_unsupported_scripts_are_detected_before_rendering():
    sample = "李雷 محمد ✈"
    missing = unsupported_pdf_characters([sample])

    assert "李" in missing
    assert "雷" in missing
    assert "م" in missing
    assert "✈" in missing

    with pytest.raises(UnsupportedPdfTextError) as exc:
        validate_pdf_text_support([sample])

    message = str(exc.value)
    assert "U+674E" in message
    assert "U+0645" in message
