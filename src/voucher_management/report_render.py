"""Atomic PDF/CSV rendering for privacy-safe report datasets."""

from __future__ import annotations

import csv
from datetime import datetime
import os
from pathlib import Path
import tempfile
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import mm
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from .pdf_fonts import (
    PDF_FONT_BOLD,
    PDF_FONT_REGULAR,
    ensure_pdf_fonts_registered,
    validate_pdf_text_support,
)
from .report_policy import voucher_code_policy
from .reporting import ReportDataset, ReportKind, nominal_label, origin_label


def _validate_dataset_policy(dataset: ReportDataset) -> None:
    """Reject renderer input that contradicts the central code policy."""

    decision = voucher_code_policy(
        dataset.purpose,
        include_code_requested=dataset.code_exposed,
    )
    has_clear_code = any(bool(row.code) for row in dataset.rows)
    if decision.expose_code != dataset.code_exposed:
        raise ValueError("Report dataset code policy is inconsistent")
    if has_clear_code and not dataset.code_exposed:
        raise ValueError("Report dataset contains unexpected voucher code data")


def _paragraph(value: object, style: ParagraphStyle) -> Paragraph:
    """Render untrusted/operator text as literal ReportLab paragraph content."""

    return Paragraph(escape(str(value or "—")), style)


def _csv_cell(value: object) -> str:
    """Prevent spreadsheet formula execution from operator/controller text."""

    text = str(value or "")
    if text.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


def _display_time(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return "—"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone()
        return parsed.strftime("%d/%m/%Y %H:%M")
    except (TypeError, ValueError):
        return text


def _atomic_target(output_path: Path, suffix: str) -> tuple[int, Path]:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(
        dir=output_path.parent,
        prefix=f".{output_path.stem}-",
        suffix=suffix,
    )
    return handle, Path(name)


def _summary_rows(dataset: ReportDataset) -> list[list[str]]:
    totals = dataset.totals
    return [
        ["Voucher nel report", str(totals.vouchers)],
        ["Creati con questo software", str(totals.generated_vouchers)],
        ["Origine creazione non determinabile", str(totals.unknown_origin_vouchers)],
        ["Utilizzati almeno una volta", str(totals.used_vouchers)],
        ["Nessun utilizzo rilevato", str(totals.never_used_vouchers)],
        ["Utilizzo non determinabile", str(totals.usage_unknown_vouchers)],
        ["Guest autorizzati (somma ultimo conteggio osservato)", str(totals.total_controller_uses)],
        ["Voucher scaduti", str(totals.expired_vouchers)],
        ["Voucher stampati", str(totals.printed_vouchers)],
        ["Senza stampe registrate", str(totals.never_printed)],
        ["Stampati - nessun utilizzo rilevato", str(totals.printed_never_used)],
        ["Voucher nominali", str(totals.nominal_vouchers)],
        ["Voucher non nominali", str(totals.non_nominal_vouchers)],
        ["Nominalità non classificata", str(totals.unclassified_vouchers)],
        ["Nominalità rimossa per privacy", str(totals.redacted_nominality_vouchers)],
        ["Job di stampa unici", str(totals.print_jobs)],
        ["Copie fisiche dei voucher", str(totals.physical_copies)],
        ["Job con almeno una ristampa", str(totals.reprint_jobs)],
        ["Copie di voucher ristampate", str(totals.reprint_copies)],
    ]


def _detail_headers(dataset: ReportDataset) -> list[str]:
    headers = ["Controller"]
    if dataset.code_exposed:
        headers.append("Voucher")
    headers.extend(
        [
            "Descrizione UniFi",
            "Destinatario locale",
            "Origine",
            "Nominale",
            "Data creazione / evidenza",
            "Prima acquisizione",
            "Scadenza",
            "Ultima presenza osservata",
            "Dato uso",
            "Utilizzato",
            "Guest autorizzati",
            "Job stampa",
            "Copie voucher",
            "Job ristampa",
            "Operatori",
            "Stato",
        ]
    )
    return headers


_DETAIL_COLUMN_WEIGHTS = {
    "Controller": 0.72,
    "Voucher": 0.72,
    "Descrizione UniFi": 0.90,
    "Destinatario locale": 1.00,
    "Origine": 0.88,
    "Nominale": 0.55,
    "Data creazione / evidenza": 0.68,
    "Prima acquisizione": 0.68,
    "Scadenza": 0.68,
    "Ultima presenza osservata": 0.72,
    "Dato uso": 0.66,
    "Utilizzato": 0.46,
    "Guest autorizzati": 0.40,
    "Job stampa": 0.44,
    "Copie voucher": 0.46,
    "Job ristampa": 0.46,
    "Operatori": 0.72,
    "Stato": 0.56,
}


def _detail_column_weights(dataset: ReportDataset) -> list[float]:
    """Keep PDF widths structurally aligned with the generated headers."""

    headers = _detail_headers(dataset)
    return [_DETAIL_COLUMN_WEIGHTS[header] for header in headers]


def _detail_row(dataset: ReportDataset, row) -> list[str]:
    values = [row.controller_name]
    if dataset.code_exposed:
        values.append(row.code)
    values.extend(
        [
            row.controller_description or "—",
            row.recipient or "—",
            origin_label(row.origin),
            nominal_label(
                row.is_nominal,
                redacted=row.nominality_redacted,
            ),
            _display_time(row.created_at),
            _display_time(row.imported_at),
            _display_time(row.expires_at),
            _display_time(row.last_seen_at),
            "Osservato" if row.usage_observed else "Non disponibile",
            (
                "Sì"
                if row.usage_observed and row.ever_used
                else ("No" if row.usage_observed else "—")
            ),
            str(row.authorized_guest_count) if row.usage_observed else "—",
            str(row.print_jobs),
            str(row.physical_copies),
            str(row.reprint_jobs),
            ", ".join(row.print_operators) or "—",
            row.status,
        ]
    )
    return values


def render_report_csv(dataset: ReportDataset, output_path: Path) -> None:
    """Write an Excel-friendly CSV atomically from a sanitized dataset."""

    _validate_dataset_policy(dataset)
    output_path = Path(output_path)
    handle, temp_path = _atomic_target(output_path, ".csv.tmp")
    os.close(handle)
    try:
        with temp_path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream, delimiter=";")
            writer.writerow(
                [_csv_cell("Report"), _csv_cell(dataset.title)]
            )
            writer.writerow(
                [_csv_cell("Generato"), _csv_cell(_display_time(dataset.generated_at))]
            )
            writer.writerow(
                [_csv_cell("Ambito"), _csv_cell(dataset.controller_label)]
            )
            writer.writerow(
                [
                    _csv_cell("Ultima presenza osservata per voucher - più vecchia"),
                    _csv_cell(_display_time(dataset.data_from)),
                ]
            )
            writer.writerow(
                [
                    _csv_cell("Ultima presenza osservata per voucher - più recente"),
                    _csv_cell(_display_time(dataset.data_as_of)),
                ]
            )
            writer.writerow([_csv_cell("Copertura dati"), _csv_cell(dataset.coverage_note)])
            writer.writerow([])
            writer.writerow([_csv_cell("Riepilogo"), _csv_cell("Valore")])
            for summary_row in _summary_rows(dataset):
                writer.writerow([_csv_cell(value) for value in summary_row])
            if dataset.kind is not ReportKind.SUMMARY:
                writer.writerow([])
                writer.writerow(
                    [_csv_cell(value) for value in _detail_headers(dataset)]
                )
                for row in dataset.rows:
                    writer.writerow(
                        [_csv_cell(value) for value in _detail_row(dataset, row)]
                    )
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, output_path)
    finally:
        temp_path.unlink(missing_ok=True)


def render_report_pdf(
    dataset: ReportDataset,
    output_path: Path,
    *,
    installation_name: str = "",
) -> None:
    """Render a printable A4 landscape report atomically.

    The renderer accepts only ReportDataset, whose voucher-code field has
    already crossed report_policy. It has no database/API access and therefore
    cannot bypass the reporting credential boundary.
    """

    _validate_dataset_policy(dataset)
    ensure_pdf_fonts_registered()
    output_path = Path(output_path)

    detail_text = (
        []
        if dataset.kind is ReportKind.SUMMARY
        else [
            value
            for row in dataset.rows
            for value in (
                row.controller_name,
                row.controller_description,
                row.recipient,
                row.status,
                ", ".join(row.print_operators),
                row.code,
            )
        ]
    )
    text_values = [
        dataset.title,
        dataset.controller_label,
        installation_name,
        *detail_text,
    ]
    validate_pdf_text_support(text_values)

    handle, temp_path = _atomic_target(output_path, ".pdf.tmp")
    os.close(handle)

    page_width, _page_height = landscape(A4)
    regular = ParagraphStyle(
        "ReportRegular",
        fontName=PDF_FONT_REGULAR,
        fontSize=7,
        leading=9,
        textColor=colors.black,
    )
    small = ParagraphStyle(
        "ReportSmall",
        parent=regular,
        fontSize=6.2,
        leading=7.6,
    )
    title_style = ParagraphStyle(
        "ReportTitle",
        parent=regular,
        fontName=PDF_FONT_BOLD,
        fontSize=16,
        leading=19,
        spaceAfter=4 * mm,
    )
    subtitle_style = ParagraphStyle(
        "ReportSubtitle",
        parent=regular,
        fontSize=8,
        leading=10,
        textColor=colors.HexColor("#555555"),
    )

    try:
        doc = SimpleDocTemplate(
            str(temp_path),
            pagesize=landscape(A4),
            leftMargin=10 * mm,
            rightMargin=10 * mm,
            topMargin=10 * mm,
            bottomMargin=10 * mm,
            title=dataset.title,
            author="Voucher Management",
        )

        story = []
        if installation_name.strip():
            story.append(
                _paragraph(
                    installation_name.strip(),
                    subtitle_style,
                )
            )
        story.append(_paragraph(dataset.title, title_style))
        story.append(
            _paragraph(
                (
                    f"Generato: {_display_time(dataset.generated_at)}"
                    f"  |  Ambito: {dataset.controller_label}"
                    f"  |  Ultime presenze osservate per voucher: {_display_time(dataset.data_from)} - {_display_time(dataset.data_as_of)}"
                ),
                subtitle_style,
            )
        )
        if dataset.coverage_note:
            story.append(_paragraph(dataset.coverage_note, regular))
        story.append(Spacer(1, 4 * mm))

        summary = [
            [
                _paragraph(label, small),
                _paragraph(value, small),
            ]
            for label, value in _summary_rows(dataset)
        ]
        summary_table = Table(
            summary,
            colWidths=[58 * mm, 22 * mm],
            hAlign="LEFT",
        )
        summary_table.setStyle(
            TableStyle(
                [
                    ("FONTNAME", (0, 0), (-1, -1), PDF_FONT_REGULAR),
                    ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#F2F2F2")),
                    ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#CCCCCC")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 4),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                    ("TOPPADDING", (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ]
            )
        )
        story.append(summary_table)
        story.append(Spacer(1, 5 * mm))

        if dataset.kind is not ReportKind.SUMMARY:
            headers = _detail_headers(dataset)
            rows = [
                [_paragraph(value, small) for value in headers]
            ]
            for row in dataset.rows:
                rows.append(
                    [
                        _paragraph(value, small)
                        for value in _detail_row(dataset, row)
                    ]
                )

            if len(rows) == 1:
                story.append(
                    _paragraph(
                        "Nessun voucher corrisponde ai criteri del report.",
                        regular,
                    )
                )
            else:
                usable = page_width - 20 * mm
                weights = _detail_column_weights(dataset)
                if len(weights) != len(headers):
                    raise RuntimeError(
                        "La configurazione colonne del report non è coerente."
                    )
                scale = usable / sum(weights)
                detail_table = Table(
                    rows,
                    colWidths=[weight * scale for weight in weights],
                    repeatRows=1,
                    hAlign="LEFT",
                )
                detail_table.setStyle(
                    TableStyle(
                        [
                            ("FONTNAME", (0, 0), (-1, -1), PDF_FONT_REGULAR),
                            ("FONTNAME", (0, 0), (-1, 0), PDF_FONT_BOLD),
                            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8E8E8")),
                            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#CCCCCC")),
                            ("VALIGN", (0, 0), (-1, -1), "TOP"),
                            ("LEFTPADDING", (0, 0), (-1, -1), 2.5),
                            ("RIGHTPADDING", (0, 0), (-1, -1), 2.5),
                            ("TOPPADDING", (0, 0), (-1, -1), 2.5),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
                        ]
                    )
                )
                story.append(detail_table)

        story.append(Spacer(1, 4 * mm))
        story.append(
            _paragraph(
                "Nota: “Nessun utilizzo rilevato” significa soltanto che Voucher "
                "Management non ha mai osservato un conteggio guest autorizzati positivo "
                "fino all'ultima presenza del voucher effettivamente osservata su UniFi. "
                "Se manca questa evidenza, il report mostra “uso non determinabile”. "
                "Il conteggio guest "
                "autorizzati è l'ultimo valore conservato e non è un contatore cumulativo "
                "di accessi né un timestamp d'uso.",
                small,
            )
        )

        doc.build(story)

        with temp_path.open("rb") as generated:
            if generated.read(5) != b"%PDF-":
                raise RuntimeError("Il report PDF generato non è valido")
        os.replace(temp_path, output_path)
    finally:
        temp_path.unlink(missing_ok=True)
