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
from .reporting import ReportDataset, ReportKind


def _validate_dataset_policy(dataset: ReportDataset) -> None:
    """Reject renderer input that contradicts the central code policy."""

    decision = voucher_code_policy(
        dataset.purpose,
        include_code_requested=dataset.code_exposed,
    )
    has_clear_code = any(bool(row.code) for row in dataset.rows)
    if decision.expose_code != dataset.code_exposed:
        raise ValueError("Report dataset code policy is inconsistent")
    if has_clear_code != dataset.code_exposed:
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


def _origin_label(origin: str | None) -> str:
    return {
        "APPLICATION": "Voucher Management",
        "CONTROLLER": "Controller / esterno",
        "LEGACY": "Import storico",
        None: "Non determinata",
    }.get(origin, "Non determinata")


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
        ["Generati da Voucher Management", str(totals.generated_by_app)],
        ["Generati e mai utilizzati", str(totals.generated_never_used)],
        ["Utilizzati almeno una volta", str(totals.used_vouchers)],
        ["Utilizzi controller (ultimo valore osservato)", str(totals.total_controller_uses)],
        ["Voucher scaduti", str(totals.expired_vouchers)],
        ["Voucher stampati", str(totals.printed_vouchers)],
        ["Job di stampa", str(totals.print_jobs)],
        ["Copie fisiche", str(totals.physical_copies)],
        ["Ristampe", str(totals.reprint_jobs)],
        ["Copie da ristampa", str(totals.reprint_copies)],
        ["Stampati mai utilizzati", str(totals.printed_never_used)],
        ["Mai stampati", str(totals.never_printed)],
        ["Voucher nominali", str(totals.nominal_vouchers)],
        ["Voucher non nominali", str(totals.non_nominal_vouchers)],
        ["Nominalità non classificata", str(totals.unclassified_nominality)],
    ]


def _nominal_label(row) -> str:
    if row.is_nominal is True:
        return "Sì"
    if row.is_nominal is False:
        return "No"
    return "Non classificato"


def _historical_use_label(row) -> str:
    return "Utilizzato" if row.ever_used else "Mai osservato"


def _report_note(kind: ReportKind) -> str:
    return {
        ReportKind.SUMMARY: (
            "Riepilogo aggregato di tutto lo storico locale conservato."
        ),
        ReportKind.GENERATED: (
            "Solo voucher la cui creazione è stata attribuita con certezza "
            "a Voucher Management."
        ),
        ReportKind.GENERATED_UNUSED: (
            "Voucher creati con certezza da Voucher Management per i quali "
            "non è mai stato osservato un utilizzo positivo."
        ),
        ReportKind.USED: (
            "Voucher per i quali almeno una sincronizzazione ha osservato "
            "un utilizzo positivo."
        ),
        ReportKind.EXPIRED: (
            "Voucher scaduti secondo lo stato o la scadenza conservata "
            "nello storico locale."
        ),
        ReportKind.PRINTED: (
            "Voucher con almeno una stampa fisica registrata localmente."
        ),
        ReportKind.PRINTED_UNUSED: (
            "Voucher stampati localmente per i quali non è mai stato "
            "osservato un utilizzo positivo."
        ),
        ReportKind.NEVER_PRINTED: (
            "Voucher senza alcuna stampa fisica registrata localmente."
        ),
        ReportKind.NOMINAL: (
            "Solo voucher marcati esplicitamente come Voucher nominale "
            "durante una creazione certa."
        ),
        ReportKind.FULL_HISTORY: (
            "Dettaglio completo dei voucher conservati nello storico locale."
        ),
    }[kind]


def _detail_columns(dataset: ReportDataset):
    columns = [("Controller", 0.85, lambda row: row.controller_name)]
    if dataset.code_exposed:
        columns.append(("Voucher", 0.85, lambda row: row.code))

    recipient = ("Destinatario", 1.45, lambda row: row.recipient or "—")
    origin = ("Origine", 1.0, lambda row: _origin_label(row.origin))
    nominal = ("Nominale", 0.82, _nominal_label)
    created = (
        "Creazione",
        0.95,
        lambda row: _display_time(row.created_at),
    )
    imported = (
        "Prima acquisizione",
        0.95,
        lambda row: _display_time(row.imported_at),
    )
    expires = (
        "Scadenza",
        0.95,
        lambda row: _display_time(row.expires_at),
    )
    historical_use = ("Uso storico", 0.82, _historical_use_label)
    current_uses = (
        "Utilizzi (ultimo)",
        0.68,
        lambda row: str(row.authorized_guest_count),
    )
    prints = ("Stampe", 0.52, lambda row: str(row.print_jobs))
    copies = ("Copie", 0.52, lambda row: str(row.physical_copies))
    reprints = ("Ristampe", 0.58, lambda row: str(row.reprint_jobs))
    first_print = (
        "Prima stampa",
        0.95,
        lambda row: _display_time(row.first_printed_at),
    )
    last_print = (
        "Ultima stampa",
        0.95,
        lambda row: _display_time(row.last_printed_at),
    )
    operators = (
        "Operatori",
        1.05,
        lambda row: ", ".join(row.print_operators) or "—",
    )
    status = ("Stato", 0.75, lambda row: row.status)

    by_kind = {
        ReportKind.SUMMARY: (),
        ReportKind.GENERATED: (
            recipient, nominal, created, expires,
            historical_use, prints, status,
        ),
        ReportKind.GENERATED_UNUSED: (
            recipient, nominal, created, expires, prints, status,
        ),
        ReportKind.USED: (
            recipient, origin, nominal, created, expires,
            historical_use, current_uses, prints, status,
        ),
        ReportKind.EXPIRED: (
            recipient, origin, nominal, expires,
            historical_use, prints, status,
        ),
        ReportKind.PRINTED: (
            recipient, origin, nominal, first_print, last_print,
            copies, reprints, operators, historical_use, status,
        ),
        ReportKind.PRINTED_UNUSED: (
            recipient, origin, nominal, first_print, last_print,
            copies, reprints, operators, status,
        ),
        ReportKind.NEVER_PRINTED: (
            recipient, origin, nominal, created, expires,
            historical_use, status,
        ),
        ReportKind.NOMINAL: (
            recipient, origin, created, expires,
            historical_use, current_uses, prints, status,
        ),
        ReportKind.FULL_HISTORY: (
            recipient, origin, nominal, created, imported, expires,
            historical_use, current_uses, prints, copies,
            reprints, operators, status,
        ),
    }

    selected = by_kind[dataset.kind]
    # Operational-handoff policy tests can construct a code-bearing SUMMARY
    # dataset even though the normal UI never does. Keep such datasets
    # renderable without changing the ordinary aggregate-only summary.
    if dataset.kind is ReportKind.SUMMARY and dataset.code_exposed:
        selected = by_kind[ReportKind.FULL_HISTORY]
    columns.extend(selected)
    return tuple(columns)


def _detail_headers(dataset: ReportDataset) -> list[str]:
    return [header for header, _weight, _getter in _detail_columns(dataset)]


def _detail_row(dataset: ReportDataset, row) -> list[str]:
    return [
        str(getter(row))
        for _header, _weight, getter in _detail_columns(dataset)
    ]


def _detail_weights(dataset: ReportDataset) -> list[float]:
    return [weight for _header, weight, _getter in _detail_columns(dataset)]

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
                [_csv_cell("Criterio"), _csv_cell(_report_note(dataset.kind))]
            )
            writer.writerow([])
            writer.writerow([_csv_cell("Riepilogo"), _csv_cell("Valore")])
            for summary_row in _summary_rows(dataset):
                writer.writerow([_csv_cell(value) for value in summary_row])
            headers = _detail_headers(dataset)
        if headers:
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
                weights = _detail_weights(dataset)
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
            if dataset.code_exposed:
                weights = [
                    0.8, 0.8, 1.3, 0.9, 0.8, 0.8, 0.8, 0.8,
                    0.72, 0.62, 0.5, 0.5, 0.5, 0.9, 0.7,
                ]
            else:
                weights = [
                    0.8, 1.35, 0.9, 0.8, 0.8, 0.8, 0.8,
                    0.72, 0.62, 0.5, 0.5, 0.5, 0.9, 0.7,
                ]
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
                "Nota: il numero di utilizzi è il totale osservato dal controller. "
                "Non rappresenta l'ora esatta in cui un ospite ha utilizzato il voucher.",
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
