"""A4 voucher PDF rendering."""

from __future__ import annotations

from pathlib import Path

from reportlab.lib.colors import HexColor, black, white
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

from .identity import DEFAULT_STRUCTURE_TYPE, DEFAULT_WIFI_TITLE
from .models import VoucherBatch, VoucherRecord

NAVY = HexColor("#0B2A6F")
ORANGE = HexColor("#F07822")
CREAM = HexColor("#FFFEFA")
CUT_GREY = HexColor("#9A9A9A")
PRESETS = {
    "Classico": {"label_bg": CREAM, "accent": ORANGE, "border": ORANGE, "text": NAVY},
    "Minimal": {"label_bg": white, "accent": NAVY, "border": HexColor("#BDBDBD"), "text": NAVY},
    "Contrasto": {"label_bg": white, "accent": black, "border": black, "text": black},
    "Personalizzato": {"label_bg": CREAM, "accent": ORANGE, "border": ORANGE, "text": NAVY},
}

# Beta 2.3 A4 geometry: 2 columns x 5 rows = 10 vouchers/page.
# The coloured/rounded border belongs to the voucher itself.  The separate
# dashed guides are therefore the unambiguous physical cutting reference.
COLUMNS = 2
ROWS = 5
VOUCHERS_PER_PAGE = COLUMNS * ROWS
RECIPIENT_STRIP = 4 * mm


def _fit_font(text: str, font_name: str, max_width: float, max_size: float, min_size: float = 5.5) -> float:
    size = max_size
    while size > min_size and stringWidth(text, font_name, size) > max_width:
        size -= 0.5
    return max(size, min_size)


def _draw_header_image(c: canvas.Canvas, settings: dict, x: float, y: float, width: float, height: float) -> None:
    custom = str(settings.get("logo_path", "")).strip()
    if not custom:
        return
    path = Path(custom)
    if not path.is_file():
        return
    try:
        c.drawImage(str(path), x, y, width=width, height=height, preserveAspectRatio=True, anchor="c", mask="auto")
    except Exception:
        # A malformed optional image must not prevent voucher generation.
        return


def _draw_label(c: canvas.Canvas, voucher: VoucherRecord, x: float, y: float, w: float, h: float, settings: dict) -> None:
    """Draw only content that remains on the physical cut voucher."""
    preset = PRESETS.get(settings.get("preset", "Classico"), PRESETS["Classico"])
    c.setFillColor(preset["label_bg"]); c.setStrokeColor(preset["border"]); c.setLineWidth(0.8)
    c.roundRect(x, y, w, h, 3 * mm, fill=1, stroke=1)
    title = str(settings.get("wifi_title", DEFAULT_WIFI_TITLE)).strip() or DEFAULT_WIFI_TITLE
    subtitle = str(settings.get("structure_name", "")).strip() or str(settings.get("structure_type", DEFAULT_STRUCTURE_TYPE)).strip()
    image_w = min(43 * mm, w * 0.48); image_h = min(16 * mm, h * 0.30)
    _draw_header_image(c, settings, x + (w - image_w) / 2, y + h - image_h - 1.5 * mm, image_w, image_h)
    c.setFillColor(preset["text"])
    c.setFont("Helvetica-Bold", _fit_font(title, "Helvetica-Bold", w - 10 * mm, min(12, h / 4.2), 8)); c.drawCentredString(x + w / 2, y + h * 0.50, title)
    c.setFont("Helvetica-Bold", _fit_font(subtitle, "Helvetica-Bold", w - 10 * mm, 7.8, 6.0)); c.drawCentredString(x + w / 2, y + h * 0.43, subtitle)
    c.setFillColor(preset["accent"]); c.setFont("Helvetica", 7.8); c.drawCentredString(x + w / 2, y + h * 0.345, "Voucher Wi-Fi / Wi-Fi Voucher")
    box_y = y + h * 0.19; label_x = x + 5 * mm
    c.setFillColor(preset["text"]); c.setFont("Helvetica-Bold", 6.8); c.drawString(label_x, box_y + 1.8 * mm, "Codice / Code:")
    box_x = x + 34 * mm; box_w = w - 39 * mm; box_h = 7 * mm
    c.setStrokeColor(preset["text"]); c.setLineWidth(0.7); c.roundRect(box_x, box_y, box_w, box_h, 1.5 * mm, fill=0, stroke=1)
    c.setFillColor(black); c.setFont("Helvetica-Bold", _fit_font(voucher.code, "Helvetica-Bold", box_w - 4 * mm, 12.5, 9)); c.drawCentredString(box_x + box_w / 2, box_y + 1.8 * mm, voucher.code)
    c.setFillColor(preset["text"]); c.setFont("Helvetica-Bold", 5.9); c.drawCentredString(x + w / 2, y + 6.1 * mm, "Personale - Non condividere")
    c.setFont("Helvetica-Oblique", 5.7); c.drawCentredString(x + w / 2, y + 3.5 * mm, "Personal - Do not share")


def _draw_recipient(c: canvas.Canvas, voucher: VoucherRecord, x: float, y: float, w: float) -> None:
    """Draw recipient in the sheet-only strip below the cut voucher."""
    text = f"Dest.: {voucher.recipient_label}"
    c.setFillColor(black); c.setFont("Helvetica-Bold", _fit_font(text, "Helvetica-Bold", w, 6.0, 5.0))
    c.drawRightString(x + w, y - 2.8 * mm, text)


def _draw_cut_guides(c: canvas.Canvas, x: float, y: float, w: float, h: float) -> None:
    """Draw dashed cut references around the voucher, not around recipient text.

    The rounded coloured border remains inside the delivered voucher.  Guides
    are offset slightly outside that border so staff can cut without removing
    the graphic frame.  Shared vertical/horizontal segments form a clear grid.
    """
    offset = 0.8 * mm
    c.saveState()
    c.setStrokeColor(CUT_GREY)
    c.setLineWidth(0.45)
    c.setDash(2.2 * mm, 1.5 * mm)
    c.line(x - offset, y - offset, x + w + offset, y - offset)
    c.line(x - offset, y + h + offset, x + w + offset, y + h + offset)
    c.line(x - offset, y - offset, x - offset, y + h + offset)
    c.line(x + w + offset, y - offset, x + w + offset, y + h + offset)
    c.restoreState()


def render_batch_pdf(batch: VoucherBatch, output_path: Path, settings: dict) -> None:
    """Render vouchers to A4, automatically paginating in groups of ten."""
    if not batch.vouchers:
        raise ValueError("Nessun voucher da generare")
    if any(not v.recipient.strip() for v in batch.vouchers):
        raise ValueError("Destinatario mancante per uno o più voucher")

    output_path = Path(output_path); output_path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(output_path), pagesize=A4, pageCompression=1); page_w, page_h = A4
    margin_x = 7 * mm; margin_y = 7 * mm; gap_x = 2.5 * mm; row_gap = 1.5 * mm
    label_w = (page_w - 2 * margin_x - gap_x) / COLUMNS
    slot_h = (page_h - 2 * margin_y - (ROWS - 1) * row_gap) / ROWS
    label_h = slot_h - RECIPIENT_STRIP

    # Copy count is deliberately not capped here.  The caller may replicate a
    # single unlimited-use voucher as many times as requested; this renderer
    # only decides how those physical copies are distributed across pages.
    for page_start in range(0, len(batch.vouchers), VOUCHERS_PER_PAGE):
        page_items = batch.vouchers[page_start:page_start + VOUCHERS_PER_PAGE]
        c.setFillColor(white); c.rect(0, 0, page_w, page_h, fill=1, stroke=0)
        for idx, voucher in enumerate(page_items):
            row, col = divmod(idx, COLUMNS)
            x = margin_x + col * (label_w + gap_x)
            slot_top = page_h - margin_y - row * (slot_h + row_gap)
            y = slot_top - label_h
            _draw_label(c, voucher, x, y, label_w, label_h, settings)
            _draw_cut_guides(c, x, y, label_w, label_h)
            _draw_recipient(c, voucher, x, y, label_w)
        c.showPage()
    c.save()
