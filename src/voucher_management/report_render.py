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
from .reporting import (
    ReportDataset,
    ReportKind,
    nominal_label,
    origin_label,
    print_state_label,
)


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
        ["Creazione Voucher Management confermata", str(totals.generated_vouchers)],
        ["Origine creazione non determinabile", str(totals.unknown_origin_vouchers)],
        ["Utilizzati almeno una volta", str(totals.used_vouchers)],
        ["Mai osservati utilizzati", str(totals.never_used_vouchers)],
        ["Utilizzo non determinabile", str(totals.usage_unknown_vouchers)],
        ["Guest autorizzati (conteggio cumulativo osservato)", str(totals.total_controller_uses)],
        ["Voucher scaduti", str(totals.expired_vouchers)],
        ["Voucher stampati", str(totals.printed_vouchers)],
        ["Mai stampati", str(totals.never_printed)],
        ["Stato stampa non determinabile", str(totals.print_unknown_vouchers)],
        ["Stampati mai osservati utilizzati", str(totals.printed_never_used)],
        ["Stampati con utilizzo non determinabile", str(totals.printed_usage_unknown)],
        ["Voucher nominali", str(totals.nominal_vouchers)],
        ["Voucher non nominali", str(totals.non_nominal_vouchers)],
        ["Nominalità non classificata", str(totals.unclassified_vouchers)],
        ["Revocati per sicurezza", str(totals.security_revoked_vouchers)],
        ["Eliminati dalla controller", str(totals.preparation_deleted_vouchers)],
        ["Nominalità rimossa per privacy", str(totals.redacted_nominality_vouchers)],
        ["Job di stampa", str(totals.print_jobs)],
        ["Copie fisiche", str(totals.physical_copies)],
        ["Ristampe", str(totals.reprint_jobs)],
        ["Copie da ristampa", str(totals.reprint_copies)],
    ]


def _usage_value(row) -> str:
    if row.ever_used:
        return "Utilizzato"
    if row.usage_observed:
        return "Mai osservato utilizzato"
    return "Uso non determinabile"


def _presence_value(row) -> str:
    return "Presente" if row.present_on_controller else "Non presente"


def _duration_value(row) -> str:
    minutes = int(row.duration_minutes or 0)
    if minutes <= 0:
        return "—"
    if minutes % 1440 == 0:
        days = minutes // 1440
        return f"{days} giorno" if days == 1 else f"{days} giorni"
    if minutes % 60 == 0:
        hours = minutes // 60
        return f"{hours} ora" if hours == 1 else f"{hours} ore"
    return f"{minutes} min"


def _quota_value(row) -> str:
    if row.authorized_guest_limit is None:
        return "—"
    return str(row.authorized_guest_limit)


def _age_days(generated_at: str, basis: str) -> str:
    generated = _display_time(generated_at)
    source = _display_time(basis)
    if generated == "—" or source == "—":
        return "—"
    try:
        generated_dt = datetime.fromisoformat(
            str(generated_at).replace("Z", "+00:00")
        )
        source_dt = datetime.fromisoformat(
            str(basis).replace("Z", "+00:00")
        )
        if generated_dt.tzinfo is None and source_dt.tzinfo is not None:
            generated_dt = generated_dt.replace(tzinfo=source_dt.tzinfo)
        if source_dt.tzinfo is None and generated_dt.tzinfo is not None:
            source_dt = source_dt.replace(tzinfo=generated_dt.tzinfo)
        return str(max(0, (generated_dt - source_dt).days))
    except (TypeError, ValueError):
        return "—"


def _detail_headers(dataset: ReportDataset) -> list[str]:
    kind = dataset.kind
    if kind is ReportKind.FULL_HISTORY:
        headers = ["Controller", "ID UniFi"]
        if dataset.code_exposed:
            headers.append("Voucher")
        headers.extend(
            [
                "Destinatario",
                "Note locali",
                "Origine",
                "Nominale",
                "Creazione controller",
                "Prima acquisizione locale",
                "Prima attivazione",
                "Scadenza",
                "Ultima osservazione controller",
                "Ultima sincronizzazione locale",
                "Dato uso",
                "Utilizzato",
                "Guest autorizzati cumulativi",
                "Quota guest",
                "Durata",
                "Stato stampa",
                "Prima stampa",
                "Ultima stampa",
                "Stampe",
                "Copie",
                "Ristampe",
                "Operatori stampa",
                "Data cancellazione",
                "Origine cancellazione",
                "Motivo cancellazione",
                "Stato",
            ]
        )
        return headers

    if kind in {ReportKind.USED, ReportKind.EXPIRED}:
        return [
            "Destinatario",
            "Creazione",
            "Prima attivazione",
            "Guest autorizzati cumulativi",
            "Quota guest",
            "Durata",
            "Scadenza",
            "Prima stampa",
            "Ultima stampa",
            "Copie",
            "Stato",
        ]

    if kind in {ReportKind.PRINTED, ReportKind.PRINTED_UNUSED}:
        return [
            "Destinatario",
            "Creazione",
            "Prima stampa",
            "Ultima stampa",
            "Copie",
            "Ristampe",
            "Uso",
            "Guest autorizzati cumulativi",
            "Scadenza",
            "Presenza controller",
            "Stato",
        ]

    if kind is ReportKind.SECURITY_REVIEW:
        return [
            "Destinatario",
            "Ultima stampa",
            "Giorni dall'ultima stampa",
            "Copie",
            "Uso",
            "Guest autorizzati cumulativi",
            "Presenza controller",
            "Stato",
        ]

    if kind in {ReportKind.NEVER_PRINTED, ReportKind.UNPRINTED_WARNING}:
        return [
            "Destinatario",
            "Creazione",
            "Giorni dalla creazione",
            "Nominale",
            "Uso",
            "Presenza controller",
            "Stato",
        ]

    if kind is ReportKind.PREPARATION_DELETED:
        return [
            "Destinatario",
            "Creazione",
            "Data cancellazione",
            "Origine cancellazione",
            "Motivo cancellazione",
            "Uso",
            "Stato stampa",
            "Operatore stampa",
            "Stato",
        ]

    if kind is ReportKind.SECURITY_REVOKED:
        return [
            "Destinatario",
            "Creazione",
            "Data revoca",
            "Ultima stampa",
            "Uso",
            "Guest autorizzati cumulativi",
            "Stato",
        ]

    if kind in {
        ReportKind.PRINT_UNKNOWN,
        ReportKind.USAGE_UNKNOWN,
        ReportKind.ORIGIN_UNKNOWN,
        ReportKind.UNCLASSIFIED,
    }:
        return [
            "Destinatario",
            "Creazione",
            "Origine",
            "Nominale",
            "Uso",
            "Stato stampa",
            "Ultima osservazione controller",
            "Stato",
        ]

    return [
        "Destinatario",
        "Creazione",
        "Nominale",
        "Uso",
        "Guest autorizzati cumulativi",
        "Stato stampa",
        "Presenza controller",
        "Stato",
    ]


def _detail_row(dataset: ReportDataset, row) -> list[str]:
    kind = dataset.kind
    usage = _usage_value(row)

    if kind is ReportKind.FULL_HISTORY:
        values = [row.controller_name, row.unifi_id]
        if dataset.code_exposed:
            values.append(row.code)
        values.extend(
            [
                row.recipient or "—",
                row.local_notes or "—",
                origin_label(row.origin),
                nominal_label(
                    row.is_nominal,
                    redacted=row.nominality_redacted,
                ),
                _display_time(row.created_at),
                _display_time(row.imported_at),
                _display_time(row.activated_at),
                _display_time(row.expires_at),
                _display_time(row.last_seen_at),
                _display_time(row.last_synced_at),
                "Osservato" if row.usage_observed else "Non disponibile",
                "Sì" if row.ever_used else ("No" if row.usage_observed else "—"),
                str(row.authorized_guest_count) if row.usage_observed else "—",
                _quota_value(row),
                _duration_value(row),
                print_state_label(row),
                _display_time(row.first_printed_at),
                _display_time(row.last_printed_at),
                str(row.print_jobs),
                str(row.physical_copies),
                str(row.reprint_jobs),
                ", ".join(row.print_operators) or "—",
                _display_time(row.preparation_deleted_at),
                row.controller_deletion_source or "—",
                row.preparation_delete_reason or "—",
                row.status,
            ]
        )
        return values

    if kind in {ReportKind.USED, ReportKind.EXPIRED}:
        return [
            row.recipient or "—",
            _display_time(row.created_at),
            _display_time(row.activated_at),
            str(row.authorized_guest_count),
            _quota_value(row),
            _duration_value(row),
            _display_time(row.expires_at),
            _display_time(row.first_printed_at),
            _display_time(row.last_printed_at),
            str(row.physical_copies),
            row.status,
        ]

    if kind in {ReportKind.PRINTED, ReportKind.PRINTED_UNUSED}:
        return [
            row.recipient or "—",
            _display_time(row.created_at),
            _display_time(row.first_printed_at),
            _display_time(row.last_printed_at),
            str(row.physical_copies),
            str(row.reprint_jobs),
            usage,
            str(row.authorized_guest_count) if row.usage_observed else "—",
            _display_time(row.expires_at),
            _presence_value(row),
            row.status,
        ]

    if kind is ReportKind.SECURITY_REVIEW:
        return [
            row.recipient or "—",
            _display_time(row.last_printed_at),
            (
                _age_days(dataset.generated_at, row.last_printed_at)
                if row.last_printed_at
                else "Revisione immediata"
            ),
            str(row.physical_copies),
            usage,
            str(row.authorized_guest_count) if row.usage_observed else "—",
            _presence_value(row),
            row.status,
        ]

    if kind in {ReportKind.NEVER_PRINTED, ReportKind.UNPRINTED_WARNING}:
        return [
            row.recipient or "—",
            _display_time(row.created_at),
            _age_days(dataset.generated_at, row.created_at),
            nominal_label(
                row.is_nominal,
                redacted=row.nominality_redacted,
            ),
            usage,
            _presence_value(row),
            row.status,
        ]

    if kind is ReportKind.PREPARATION_DELETED:
        return [
            row.recipient or "—",
            _display_time(row.created_at),
            _display_time(row.preparation_deleted_at),
            row.controller_deletion_source or "—",
            row.preparation_delete_reason or "—",
            usage,
            print_state_label(row),
            ", ".join(row.print_operators) or "—",
            row.status,
        ]

    if kind is ReportKind.SECURITY_REVOKED:
        return [
            row.recipient or "—",
            _display_time(row.created_at),
            _display_time(row.security_revoked_at),
            _display_time(row.last_printed_at),
            usage,
            str(row.authorized_guest_count) if row.usage_observed else "—",
            row.status,
        ]

    if kind in {
        ReportKind.PRINT_UNKNOWN,
        ReportKind.USAGE_UNKNOWN,
        ReportKind.ORIGIN_UNKNOWN,
        ReportKind.UNCLASSIFIED,
    }:
        return [
            row.recipient or "—",
            _display_time(row.created_at),
            origin_label(row.origin),
            nominal_label(
                row.is_nominal,
                redacted=row.nominality_redacted,
            ),
            usage,
            print_state_label(row),
            _display_time(row.last_seen_at),
            row.status,
        ]

    return [
        row.recipient or "—",
        _display_time(row.created_at),
        nominal_label(
            row.is_nominal,
            redacted=row.nominality_redacted,
        ),
        usage,
        str(row.authorized_guest_count) if row.usage_observed else "—",
        print_state_label(row),
        _presence_value(row),
        row.status,
    ]


def _detail_weights(dataset: ReportDataset) -> list[float]:
    wide = {
        "Destinatario": 1.9,
        "Note locali": 5.0,
        "Motivo cancellazione": 3.0,
        "Origine": 1.15,
        "Origine cancellazione": 1.15,
        "ID UniFi": 1.25,
        "Voucher": 0.85,
        "Operatori stampa": 1.15,
        "Operatore stampa": 1.15,
        "Ultima osservazione controller": 1.05,
        "Ultima sincronizzazione locale": 1.05,
    }
    return [wide.get(header, 0.85) for header in _detail_headers(dataset)]


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
                [_csv_cell("Dati controller dal"), _csv_cell(_display_time(dataset.data_from))]
            )
            writer.writerow(
                [
                    _csv_cell("Dati controller aggiornati fino a"),
                    _csv_cell(_display_time(dataset.data_as_of)),
                ]
            )
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
                row.unifi_id,
                row.recipient,
                row.local_notes,
                row.controller_deletion_source,
                row.preparation_delete_reason,
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
                    f"  |  Dati controller fino a: {_display_time(dataset.data_as_of)}"
                ),
                subtitle_style,
            )
        )
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
                weights = _detail_weights(dataset)
                if len(weights) != len(headers):
                    raise RuntimeError(
                        "report PDF column geometry does not match export fields"
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
                "Nota: “Mai osservato utilizzato” significa soltanto che Voucher "
                "Management non ha mai osservato un conteggio guest autorizzati positivo "
                "fino all'ultima osservazione controller indicata. Se manca questa "
                "evidenza, il report mostra “uso non determinabile”. Il conteggio guest "
                "autorizzati è il massimo conteggio cumulativo positivo osservato dal "
                "controller; non rappresenta un timestamp d'uso.",
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
