"""A4 voucher PDF rendering."""

from __future__ import annotations

import os
import tempfile
import unicodedata
from functools import lru_cache
from pathlib import Path

from reportlab.lib.colors import HexColor, black, white
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

from .identity import DEFAULT_STRUCTURE_TYPE, DEFAULT_WIFI_TITLE
from .logo_validation import LogoValidationError, validate_logo_image
from .models import VoucherBatch, VoucherRecord
from .pdf_fonts import (
    PDF_FONT_BOLD,
    PDF_FONT_ITALIC,
    PDF_FONT_REGULAR,
    ensure_pdf_fonts_registered,
    validate_pdf_text_support,
)

NAVY = HexColor("#0B2A6F")
ORANGE = HexColor("#F07822")
CREAM = HexColor("#FFFEFA")
CUT_GREY = HexColor("#9A9A9A")
PRESETS = {
    "Classico": {
        "label_bg": CREAM,
        "accent": ORANGE,
        "border": ORANGE,
        "text": NAVY,
    },
    "Minimal": {
        "label_bg": white,
        "accent": NAVY,
        "border": HexColor("#BDBDBD"),
        "text": NAVY,
    },
    "Contrasto": {
        "label_bg": white,
        "accent": black,
        "border": black,
        "text": black,
    },
    "Personalizzato": {
        "label_bg": CREAM,
        "accent": ORANGE,
        "border": ORANGE,
        "text": NAVY,
    },
}

# A4 geometry: 2 columns x 5 rows = 10 vouchers/page.
# The coloured/rounded border belongs to the voucher itself. The separate
# dashed guides are therefore the unambiguous physical cutting reference.
COLUMNS = 2
ROWS = 5
VOUCHERS_PER_PAGE = COLUMNS * ROWS
RECIPIENT_STRIP = 4 * mm


def _normalize_pdf_text(value: object) -> str:
    """Normalize operator-entered text so combining marks render predictably."""

    return unicodedata.normalize("NFC", str(value))


def _truncate_to_width(
    text: str,
    font_name: str,
    font_size: float,
    max_width: float,
) -> str:
    """Return text shortened with an ellipsis so it cannot cross its column."""

    if stringWidth(text, font_name, font_size) <= max_width:
        return text

    suffix = "…"
    if stringWidth(suffix, font_name, font_size) > max_width:
        return ""

    low = 0
    high = len(text)
    while low < high:
        mid = (low + high + 1) // 2
        candidate = text[:mid].rstrip() + suffix
        if stringWidth(candidate, font_name, font_size) <= max_width:
            low = mid
        else:
            high = mid - 1
    return text[:low].rstrip() + suffix


def _fit_font(
    text: str,
    font_name: str,
    max_width: float,
    max_size: float,
    min_size: float = 5.5,
) -> float:
    size = max_size
    while size > min_size and stringWidth(
        text,
        font_name,
        size,
    ) > max_width:
        size -= 0.5
    return max(size, min_size)


@lru_cache(maxsize=64)
def _validated_logo_snapshot(
    path_value: str,
    modified_ns: int,
    size: int,
):
    """Validate and cache one ReportLab image for a filesystem snapshot."""

    del modified_ns, size
    path = Path(path_value)
    try:
        validate_logo_image(path)
        return ImageReader(str(path))
    except (LogoValidationError, OSError, ValueError):
        return None


def _draw_header_image(
    c: canvas.Canvas,
    settings: dict,
    x: float,
    y: float,
    width: float,
    height: float,
) -> None:
    custom = str(settings.get("logo_path", "")).strip()
    if not custom:
        return
    path = Path(custom)
    try:
        stat = path.stat()
        if not path.is_file():
            return
        image = _validated_logo_snapshot(
            str(path),
            stat.st_mtime_ns,
            stat.st_size,
        )
        if image is None:
            return
        c.drawImage(
            image,
            x,
            y,
            width=width,
            height=height,
            preserveAspectRatio=True,
            anchor="c",
            mask="auto",
        )
    except Exception:
        # A malformed, replaced or inaccessible optional image must not prevent
        # voucher generation.
        return


def _draw_label(
    c: canvas.Canvas,
    voucher: VoucherRecord,
    x: float,
    y: float,
    w: float,
    h: float,
    settings: dict,
) -> None:
    """Draw only content that remains on the physical cut voucher."""

    preset = PRESETS.get(
        settings.get("preset", "Classico"),
        PRESETS["Classico"],
    )
    c.setFillColor(preset["label_bg"])
    c.setStrokeColor(preset["border"])
    c.setLineWidth(0.8)
    c.roundRect(x, y, w, h, 3 * mm, fill=1, stroke=1)

    title = _normalize_pdf_text(
        (
            str(settings.get("wifi_title", DEFAULT_WIFI_TITLE)).strip()
            or DEFAULT_WIFI_TITLE
        )
    )
    subtitle = _normalize_pdf_text(
        (
            str(settings.get("structure_name", "")).strip()
            or str(
                settings.get(
                    "structure_type",
                    DEFAULT_STRUCTURE_TYPE,
                )
            ).strip()
        )
    )

    image_w = min(43 * mm, w * 0.48)
    image_h = min(16 * mm, h * 0.30)
    _draw_header_image(
        c,
        settings,
        x + (w - image_w) / 2,
        y + h - image_h - 1.5 * mm,
        image_w,
        image_h,
    )

    c.setFillColor(preset["text"])
    title_font_size = _fit_font(
        title,
        PDF_FONT_BOLD,
        w - 10 * mm,
        min(12, h / 4.2),
        8,
    )
    title = _truncate_to_width(
        title,
        PDF_FONT_BOLD,
        title_font_size,
        w - 10 * mm,
    )
    c.setFont(PDF_FONT_BOLD, title_font_size)
    c.drawCentredString(x + w / 2, y + h * 0.50, title)

    subtitle_font_size = _fit_font(
        subtitle,
        PDF_FONT_BOLD,
        w - 10 * mm,
        7.8,
        6.0,
    )
    subtitle = _truncate_to_width(
        subtitle,
        PDF_FONT_BOLD,
        subtitle_font_size,
        w - 10 * mm,
    )
    c.setFont(PDF_FONT_BOLD, subtitle_font_size)
    c.drawCentredString(x + w / 2, y + h * 0.43, subtitle)

    c.setFillColor(preset["accent"])
    c.setFont(PDF_FONT_REGULAR, 7.8)
    c.drawCentredString(
        x + w / 2,
        y + h * 0.345,
        "Voucher Wi-Fi / Wi-Fi Voucher",
    )

    box_y = y + h * 0.19
    label_x = x + 5 * mm
    c.setFillColor(preset["text"])
    c.setFont(PDF_FONT_BOLD, 6.8)
    c.drawString(
        label_x,
        box_y + 1.8 * mm,
        "Codice / Code:",
    )

    box_x = x + 34 * mm
    box_w = w - 39 * mm
    box_h = 7 * mm
    c.setStrokeColor(preset["text"])
    c.setLineWidth(0.7)
    c.roundRect(
        box_x,
        box_y,
        box_w,
        box_h,
        1.5 * mm,
        fill=0,
        stroke=1,
    )

    c.setFillColor(black)
    c.setFont(
        PDF_FONT_BOLD,
        _fit_font(
            voucher.code,
            PDF_FONT_BOLD,
            box_w - 4 * mm,
            12.5,
            9,
        ),
    )
    c.drawCentredString(
        box_x + box_w / 2,
        box_y + 1.8 * mm,
        voucher.code,
    )

    c.setFillColor(preset["text"])
    c.setFont(PDF_FONT_BOLD, 5.9)
    c.drawCentredString(
        x + w / 2,
        y + 6.1 * mm,
        "Personale - Non condividere",
    )
    c.setFont(PDF_FONT_ITALIC, 5.7)
    c.drawCentredString(
        x + w / 2,
        y + 3.5 * mm,
        "Personal - Do not share",
    )


def _draw_recipient(
    c: canvas.Canvas,
    voucher: VoucherRecord,
    x: float,
    y: float,
    w: float,
) -> None:
    """Draw recipient in the sheet-only strip below the cut voucher."""

    text = _normalize_pdf_text(f"Dest.: {voucher.recipient_label}")
    font_size = _fit_font(
        text,
        PDF_FONT_BOLD,
        w,
        6.0,
        5.0,
    )
    text = _truncate_to_width(
        text,
        PDF_FONT_BOLD,
        font_size,
        w,
    )
    c.setFillColor(black)
    c.setFont(PDF_FONT_BOLD, font_size)
    c.drawRightString(
        x + w,
        y - 2.8 * mm,
        text,
    )


def _draw_cut_guides(
    c: canvas.Canvas,
    x: float,
    y: float,
    w: float,
    h: float,
) -> None:
    """Draw dashed cut references around the voucher."""

    offset = 0.8 * mm
    c.saveState()
    c.setStrokeColor(CUT_GREY)
    c.setLineWidth(0.45)
    c.setDash(2.2 * mm, 1.5 * mm)
    c.line(
        x - offset,
        y - offset,
        x + w + offset,
        y - offset,
    )
    c.line(
        x - offset,
        y + h + offset,
        x + w + offset,
        y + h + offset,
    )
    c.line(
        x - offset,
        y - offset,
        x - offset,
        y + h + offset,
    )
    c.line(
        x + w + offset,
        y - offset,
        x + w + offset,
        y + h + offset,
    )
    c.restoreState()


def _render_pdf_file(
    batch: VoucherBatch,
    path: Path,
    settings: dict,
) -> None:
    """Render a complete PDF to the supplied working path."""

    ensure_pdf_fonts_registered()
    c = canvas.Canvas(
        str(path),
        pagesize=A4,
        pageCompression=1,
    )
    page_w, page_h = A4
    margin_x = 7 * mm
    margin_y = 7 * mm
    gap_x = 2.5 * mm
    row_gap = 1.5 * mm
    label_w = (
        page_w
        - 2 * margin_x
        - gap_x
    ) / COLUMNS
    slot_h = (
        page_h
        - 2 * margin_y
        - (ROWS - 1) * row_gap
    ) / ROWS
    label_h = slot_h - RECIPIENT_STRIP

    # Copy count is deliberately not capped here. The caller may replicate a
    # single unlimited-use voucher as many times as requested.
    for page_start in range(
        0,
        len(batch.vouchers),
        VOUCHERS_PER_PAGE,
    ):
        page_items = batch.vouchers[
            page_start:page_start + VOUCHERS_PER_PAGE
        ]
        c.setFillColor(white)
        c.rect(
            0,
            0,
            page_w,
            page_h,
            fill=1,
            stroke=0,
        )
        for idx, voucher in enumerate(page_items):
            row, col = divmod(idx, COLUMNS)
            x = (
                margin_x
                + col * (label_w + gap_x)
            )
            slot_top = (
                page_h
                - margin_y
                - row * (slot_h + row_gap)
            )
            y = slot_top - label_h
            _draw_label(
                c,
                voucher,
                x,
                y,
                label_w,
                label_h,
                settings,
            )
            _draw_cut_guides(
                c,
                x,
                y,
                label_w,
                label_h,
            )
            _draw_recipient(
                c,
                voucher,
                x,
                y,
                label_w,
            )
        c.showPage()

    c.save()


def render_batch_pdf(
    batch: VoucherBatch,
    output_path: Path,
    settings: dict,
) -> None:
    """Render atomically so the archive never exposes a partial final PDF."""

    if not batch.vouchers:
        raise ValueError("Nessun voucher da generare")
    if any(
        not voucher.recipient.strip()
        for voucher in batch.vouchers
    ):
        raise ValueError(
            "Destinatario mancante per uno o più voucher"
        )

    title = _normalize_pdf_text(
        (
            str(settings.get("wifi_title", DEFAULT_WIFI_TITLE)).strip()
            or DEFAULT_WIFI_TITLE
        )
    )
    subtitle = _normalize_pdf_text(
        (
            str(settings.get("structure_name", "")).strip()
            or str(
                settings.get(
                    "structure_type",
                    DEFAULT_STRUCTURE_TYPE,
                )
            ).strip()
        )
    )
    validate_pdf_text_support(
        [
            title,
            subtitle,
            *(
                _normalize_pdf_text(voucher.recipient_label)
                for voucher in batch.vouchers
            ),
        ]
    )

    output_path = Path(output_path)
    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    handle, temp_name = tempfile.mkstemp(
        dir=output_path.parent,
        prefix=f".{output_path.stem}-",
        suffix=".tmp",
    )
    os.close(handle)
    temp_path = Path(temp_name)

    try:
        _render_pdf_file(
            batch,
            temp_path,
            settings,
        )
        with temp_path.open("rb") as generated:
            if generated.read(5) != b"%PDF-":
                raise RuntimeError(
                    "Il PDF generato non è valido"
                )
        os.replace(
            temp_path,
            output_path,
        )
    finally:
        temp_path.unlink(missing_ok=True)
