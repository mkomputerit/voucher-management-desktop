"""Tests for report PDF/CSV renderers and privacy boundaries."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from voucher_management.report_policy import ReportPurpose
from voucher_management.report_render import (
    _detail_column_weights,
    _detail_headers,
    render_report_csv,
    render_report_pdf,
)
from voucher_management.reporting import (
    ReportDataset,
    ReportKind,
    ReportRow,
    ReportTotals,
)


def _dataset(*, code="", kind=ReportKind.FULL_HISTORY, purpose=ReportPurpose.AUDIT):
    row = ReportRow(
        voucher_id=1,
        controller_name="Sala & Test <Nord>",
        code=code,
        recipient="Mario & Lucia <ospiti>",
        controller_description="EMI06 & Sala <Nord>",
        created_at="2026-09-01T09:00:00+00:00",
        imported_at="2026-09-01T09:05:00+00:00",
        expires_at="2026-10-01T09:00:00+00:00",
        authorized_guest_count=2,
        ever_used=True,
        usage_observed=True,
        print_jobs=2,
        physical_copies=3,
        reprint_jobs=1,
        reprint_copies=2,
        first_printed_at="2026-09-02T10:00:00+00:00",
        last_printed_at="2026-09-03T10:00:00+00:00",
        print_operators=("PC\\alice", "PC\\bob"),
        expired=False,
        present_on_controller=True,
        archived_at="",
        status="Utilizzato",
        origin="APPLICATION",
        is_nominal=True,
        last_synced_at="2026-09-26T11:30:00+00:00",
    )
    totals = ReportTotals(
        vouchers=1,
        generated_vouchers=1,
        used_vouchers=1,
        never_used_vouchers=0,
        usage_unknown_vouchers=0,
        total_controller_uses=2,
        expired_vouchers=0,
        printed_vouchers=1,
        print_jobs=2,
        physical_copies=3,
        reprint_jobs=1,
        reprint_copies=2,
        printed_never_used=0,
        never_printed=0,
        nominal_vouchers=1,
        non_nominal_vouchers=0,
        unclassified_vouchers=0,
        unknown_origin_vouchers=0,
        redacted_nominality_vouchers=0,
    )
    return ReportDataset(
        kind=kind,
        purpose=purpose,
        title="Storico voucher",
        generated_at="2026-09-26T12:00:00+00:00",
        controller_label="Sala & Test <Nord>",
        rows=(row,),
        totals=totals,
        code_exposed=bool(code),
        data_from="2026-09-26T11:30:00+00:00",
        data_as_of="2026-09-26T11:30:00+00:00",
    )


def test_detail_csv_hides_codes_but_keeps_sanitized_administrative_detail(tmp_path: Path):
    output = tmp_path / "report.csv"

    render_report_csv(_dataset(), output)

    payload = output.read_text(encoding="utf-8-sig")
    assert "Voucher;" not in payload
    assert "12345-67890" not in payload
    assert "Mario & Lucia <ospiti>" in payload
    assert "Utilizzi/guest autorizzati (somma ultimo conteggio osservato);2" in payload
    assert "Descrizione UniFi;Destinatario locale;Origine" in payload
    assert "EMI06 & Sala <Nord>" in payload
    assert "Dato uso;Utilizzato;Guest autorizzati" in payload
    assert "Ultima presenza osservata per voucher - più recente;" in payload
    assert "26/09/2026" in payload


def test_summary_csv_is_aggregate_only_and_excludes_personal_detail(tmp_path: Path):
    output = tmp_path / "summary.csv"
    dataset = _dataset(kind=ReportKind.SUMMARY, purpose=ReportPurpose.SUMMARY)

    render_report_csv(dataset, output)

    payload = output.read_text(encoding="utf-8-sig")
    assert "Mario & Lucia" not in payload
    assert r"PC\alice" not in payload
    assert "Destinatario" not in payload
    assert "Creati con questo software;1" in payload


def test_renderer_rejects_clear_code_for_summary_purpose(tmp_path: Path):
    output = tmp_path / "invalid.csv"
    dataset = _dataset(
        code="12345-67890",
        kind=ReportKind.SUMMARY,
        purpose=ReportPurpose.SUMMARY,
    )

    with pytest.raises(ValueError, match="code policy"):
        render_report_csv(dataset, output)

    assert not output.exists()


def test_csv_can_render_explicit_operational_handoff_dataset(tmp_path: Path):
    output = tmp_path / "handoff.csv"
    dataset = replace(
        _dataset(code="12345-67890"),
        purpose=ReportPurpose.OPERATIONAL_HANDOFF,
    )

    render_report_csv(dataset, output)

    payload = output.read_text(encoding="utf-8-sig")
    assert "Voucher" in payload
    assert "12345-67890" in payload


def test_pdf_report_is_atomic_valid_pdf_and_escapes_operator_text(tmp_path: Path):
    output = tmp_path / "report.pdf"

    render_report_pdf(
        _dataset(),
        output,
        installation_name="Sala & Assemblee <Test>",
    )

    assert output.read_bytes().startswith(b"%PDF-")
    assert output.stat().st_size > 1000
    assert not list(tmp_path.glob(".report-*.pdf.tmp"))


def test_empty_pdf_report_is_still_printable(tmp_path: Path):
    base = _dataset()
    empty = replace(
        base,
        kind=ReportKind.EXPIRED,
        rows=(),
        totals=ReportTotals(
            vouchers=0,
            generated_vouchers=0,
            used_vouchers=0,
            never_used_vouchers=0,
            usage_unknown_vouchers=0,
            total_controller_uses=0,
            expired_vouchers=0,
            printed_vouchers=0,
            print_jobs=0,
            physical_copies=0,
            reprint_jobs=0,
            reprint_copies=0,
            printed_never_used=0,
            never_printed=0,
            nominal_vouchers=0,
            non_nominal_vouchers=0,
            unclassified_vouchers=0,
        ),
    )
    output = tmp_path / "empty.pdf"

    render_report_pdf(empty, output)

    assert output.read_bytes().startswith(b"%PDF-")


def test_csv_neutralizes_formula_like_operator_text(tmp_path: Path):
    output = tmp_path / "formula.csv"
    dataset = _dataset()
    dangerous_row = replace(
        dataset.rows[0],
        controller_name='=HYPERLINK("https://example.invalid")',
        recipient="+SUM(1,1)",
        print_operators=("@operator",),
    )
    dangerous = replace(
        dataset,
        controller_label="-controller",
        rows=(dangerous_row,),
    )

    render_report_csv(dangerous, output)

    payload = output.read_text(encoding="utf-8-sig")
    assert "'=HYPERLINK" in payload
    assert "'+SUM" in payload
    assert "'@operator" in payload
    assert "'-controller" in payload


def test_pdf_column_weights_follow_detail_headers():
    dataset = _dataset()
    assert len(_detail_column_weights(dataset)) == len(_detail_headers(dataset))

    handoff = replace(
        dataset,
        purpose=ReportPurpose.OPERATIONAL_HANDOFF,
        code_exposed=True,
        rows=(replace(dataset.rows[0], code="12345-67890"),),
    )
    assert len(_detail_column_weights(handoff)) == len(_detail_headers(handoff))


def test_authorized_empty_handoff_keeps_code_column_without_failing(tmp_path: Path):
    base = _dataset()
    empty = replace(
        base,
        purpose=ReportPurpose.OPERATIONAL_HANDOFF,
        code_exposed=True,
        rows=(),
        totals=replace(base.totals, vouchers=0),
    )
    output = tmp_path / "empty-handoff.csv"
    render_report_csv(empty, output)
    payload = output.read_text(encoding="utf-8-sig")
    assert output.exists()
    assert "Voucher" in payload
