from unittest.mock import patch

import pypdfium2 as pdfium
import pytest
from PIL import Image

from reportlab.pdfbase.pdfmetrics import stringWidth

from voucher_management.models import VoucherBatch, VoucherRecord
from voucher_management.pdf_fonts import (
    PDF_FONT_BOLD,
    UnsupportedPdfTextError,
    ensure_pdf_fonts_registered,
)
from voucher_management.pdf_render import (
    _draw_header_image,
    _normalize_pdf_text,
    _truncate_to_width,
    _validated_logo_snapshot,
    render_batch_pdf,
)


def test_render_creates_paginated_pdf_without_packaged_branding(tmp_path):
    vouchers = []
    for index in range(11):
        recipient = "Reception" if index == 0 else "Guests"
        vouchers.append(
            VoucherRecord(
                code=f"{10000 + index:05d}-{20000 + index:05d}",
                duration_minutes=1440,
                recipient=recipient,
            )
        )

    batch = VoucherBatch(
        source_path=tmp_path / "CONTROLLER_API",
        recipient="Reception_Guests",
        vouchers=vouchers,
    )
    output = tmp_path / "Voucher_Reception_Guests.pdf"

    render_batch_pdf(
        batch,
        output,
        {
            "preset": "Classico",
            "wifi_title": "Guest Wi-Fi",
            "structure_type": "Personalizzata",
            "structure_name": "Example Venue",
            "location": "",
            "logo_path": "",
        },
    )

    assert output.exists()
    assert output.stat().st_size > 1000
    assert output.read_bytes().startswith(b"%PDF")


class _ImageCanvas:
    def __init__(self):
        self.draw_calls = 0

    def drawImage(self, *_args, **_kwargs):
        self.draw_calls += 1


def test_header_image_rejects_disguised_gif_at_render_time(tmp_path):
    logo = tmp_path / "renamed.png"
    Image.new("RGB", (16, 16), "white").save(logo, format="GIF")
    drawing = _ImageCanvas()

    _draw_header_image(
        drawing,
        {"logo_path": str(logo)},
        0,
        0,
        100,
        50,
    )

    assert drawing.draw_calls == 0


def test_header_image_validation_is_cached_per_file_snapshot(tmp_path):
    logo = tmp_path / "logo.png"
    Image.new("RGB", (16, 16), "white").save(logo, format="PNG")
    drawing = _ImageCanvas()
    _validated_logo_snapshot.cache_clear()

    with patch(
        "voucher_management.pdf_render.validate_logo_image",
    ) as validate:
        for _ in range(3):
            _draw_header_image(
                drawing,
                {"logo_path": str(logo)},
                0,
                0,
                100,
                50,
            )

    assert validate.call_count == 1
    assert drawing.draw_calls == 3


def test_render_preserves_extended_unicode_text(tmp_path):
    sample = "Łódź Žilina Brașov ă Ω Ж"
    batch = VoucherBatch(
        source_path=tmp_path / "CONTROLLER_API",
        recipient=sample,
        vouchers=[
            VoucherRecord(
                code="12345-67890",
                duration_minutes=60,
                recipient=sample,
            )
        ],
    )
    output = tmp_path / "unicode.pdf"

    render_batch_pdf(
        batch,
        output,
        {
            "preset": "Classico",
            "wifi_title": f"Guest {sample}",
            "structure_type": "Personalizzata",
            "structure_name": sample,
            "logo_path": "",
        },
    )

    document = pdfium.PdfDocument(str(output))
    try:
        page = document[0]
        try:
            text_page = page.get_textpage()
            try:
                extracted = text_page.get_text_bounded()
            finally:
                text_page.close()
        finally:
            page.close()
    finally:
        document.close()

    assert sample in extracted


def test_failed_render_never_replaces_existing_final_pdf(tmp_path):
    batch = VoucherBatch(
        source_path=tmp_path / "CONTROLLER_API",
        recipient="Guest",
        vouchers=[
            VoucherRecord(
                code="12345-67890",
                duration_minutes=60,
                recipient="Guest",
            )
        ],
    )
    output = tmp_path / "Voucher_Guest.pdf"
    original = b"known-good-existing-pdf"
    output.write_bytes(original)

    with patch(
        "voucher_management.pdf_render._render_pdf_file",
        side_effect=RuntimeError("synthetic render failure"),
    ):
        with pytest.raises(RuntimeError, match="synthetic render failure"):
            render_batch_pdf(batch, output, {"logo_path": ""})

    assert output.read_bytes() == original
    assert not [
        item
        for item in tmp_path.iterdir()
        if item.suffix == ".tmp"
    ]


def test_successful_render_leaves_no_temporary_pdf(tmp_path):
    batch = VoucherBatch(
        source_path=tmp_path / "CONTROLLER_API",
        recipient="Guest",
        vouchers=[
            VoucherRecord(
                code="12345-67890",
                duration_minutes=60,
                recipient="Guest",
            )
        ],
    )
    output = tmp_path / "Voucher_Guest.pdf"

    render_batch_pdf(batch, output, {"logo_path": ""})

    assert output.read_bytes().startswith(b"%PDF-")
    assert not [
        item
        for item in tmp_path.iterdir()
        if item.suffix == ".tmp"
    ]



def test_render_rejects_unsupported_text_before_creating_pdf(tmp_path):
    sample = "李雷 محمد ✈"
    batch = VoucherBatch(
        source_path=tmp_path / "CONTROLLER_API",
        recipient=sample,
        vouchers=[
            VoucherRecord(
                code="12345-67890",
                duration_minutes=60,
                recipient=sample,
            )
        ],
    )
    output = tmp_path / "unsupported.pdf"

    with pytest.raises(UnsupportedPdfTextError):
        render_batch_pdf(
            batch,
            output,
            {
                "preset": "Classico",
                "wifi_title": "Guest Wi-Fi",
                "structure_type": "Personalizzata",
                "structure_name": "Example Venue",
                "logo_path": "",
            },
        )

    assert not output.exists()
    assert not list(tmp_path.glob("*.tmp"))


def test_long_recipient_is_truncated_to_column_width():
    ensure_pdf_fonts_registered()
    text = "Dest.: " + ("Very long recipient name " * 12)
    width = 120

    shortened = _truncate_to_width(
        text,
        PDF_FONT_BOLD,
        5.0,
        width,
    )

    assert shortened.endswith("…")
    assert len(shortened) < len(text)
    assert stringWidth(shortened, PDF_FONT_BOLD, 5.0) <= width



def _extract_pdf_text(path):
    document = pdfium.PdfDocument(str(path))
    try:
        chunks = []
        for index in range(len(document)):
            page = document[index]
            try:
                text_page = page.get_textpage()
                try:
                    chunks.append(text_page.get_text_bounded())
                finally:
                    text_page.close()
            finally:
                page.close()
        return "\n".join(chunks)
    finally:
        document.close()


def test_nfd_operator_text_is_normalized_to_nfc_before_render(tmp_path):
    decomposed = "Jose\u0301 Zoe\u0308"
    composed = "José Zoë"
    assert _normalize_pdf_text(decomposed) == composed

    batch = VoucherBatch(
        source_path=tmp_path / "CONTROLLER_API",
        recipient=decomposed,
        vouchers=[
            VoucherRecord(
                code="12345-67890",
                duration_minutes=60,
                recipient=decomposed,
            )
        ],
    )
    output = tmp_path / "nfc.pdf"

    render_batch_pdf(
        batch,
        output,
        {
            "preset": "Classico",
            "wifi_title": "Guest Jose\u0301",
            "structure_type": "Personalizzata",
            "structure_name": "Zoe\u0308",
            "logo_path": "",
        },
    )

    extracted = _extract_pdf_text(output)

    assert "Guest José" in extracted
    assert "Zoë" in extracted
    assert "Dest.: José Zoë" in extracted
    assert "\u0301" not in extracted
    assert "\u0308" not in extracted


def test_very_long_wifi_title_is_truncated_in_rendered_pdf(tmp_path):
    title = "Guest Wi-Fi " + ("A" * 120)
    batch = VoucherBatch(
        source_path=tmp_path / "CONTROLLER_API",
        recipient="Guest",
        vouchers=[
            VoucherRecord(
                code="12345-67890",
                duration_minutes=60,
                recipient="Guest",
            )
        ],
    )
    output = tmp_path / "long-title.pdf"

    render_batch_pdf(
        batch,
        output,
        {
            "preset": "Classico",
            "wifi_title": title,
            "structure_type": "Personalizzata",
            "structure_name": "Example Venue",
            "logo_path": "",
        },
    )

    extracted = _extract_pdf_text(output)

    assert title not in extracted
    assert "Guest Wi-Fi " in extracted
    assert "…" in extracted



def test_very_long_structure_name_is_truncated_in_rendered_pdf(tmp_path):
    structure = "Assembly Hall " + ("B" * 140)
    batch = VoucherBatch(
        source_path=tmp_path / "CONTROLLER_API",
        recipient="Guest",
        vouchers=[
            VoucherRecord(
                code="12345-67890",
                duration_minutes=60,
                recipient="Guest",
            )
        ],
    )
    output = tmp_path / "long-structure.pdf"

    render_batch_pdf(
        batch,
        output,
        {
            "preset": "Classico",
            "wifi_title": "Guest Wi-Fi",
            "structure_type": "Personalizzata",
            "structure_name": structure,
            "logo_path": "",
        },
    )

    extracted = _extract_pdf_text(output)

    assert structure not in extracted
    assert "Assembly Hall " in extracted
    assert "…" in extracted
