"""Tests for report PDF/CSV renderers and privacy boundaries."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from voucher_management.report_policy import ReportPurpose
from voucher_management.report_render import (
    render_report_csv,
    render_report_pdf,
)
from voucher_management.reporting import (
    ReportDataset,
    ReportKind,
    ReportRow,
    ReportTotals,
)


def _dataset(*, code="") -> ReportDataset:
    row = ReportRow(
        voucher_id=1,
        controller_name="Sala & Test <Nord>",
        code=code,
        recipient="Mario & Lucia <ospiti>",
        assigned_to="",
        created_at="2026-09-01T09:00:00+00:00",
        imported_at="2026-09-01T09:05:00+00:00",
        expires_at="2026-10-01T09:00:00+00:00",
        authorized_guest_count=2,
        print_jobs=2,
        physical_copies=3,
        reprint_jobs=1,
        reprint_copies=2,
        first_printed_at="2026-09-02T10:00:00+00:00",
        last_printed_at="2026-09-03T10:00:00+00:00",
        print_operators=("PC\\alice", "PC\\bob"),
        expired=False,
        present_on_controller=True,
        status="Utilizzato",
    )
    totals = ReportTotals(
        vouchers=1,
        used_vouchers=1,
        total_controller_uses=2,
        expired_vouchers=0,
        printed_vouchers=1,
        print_jobs=2,
        physical_copies=3,
        reprint_jobs=1,
        reprint_copies=2,
        printed_never_used=0,
        never_printed=0,
        nominal_vouchers=0,
    )
    return ReportDataset(
        kind=ReportKind.SUMMARY,
        purpose=ReportPurpose.SUMMARY,
        title="Riepilogo voucher",
        generated_at="2026-09-26T12:00:00+00:00",
        controller_label="Sala & Test <Nord>",
        rows=(row,),
        totals=totals,
        code_exposed=bool(code),
    )


def test_csv_report_omits_voucher_column_when_policy_hides_code(tmp_path: Path):
    output = tmp_path / "report.csv"

    render_report_csv(_dataset(), output)

    payload = output.read_text(encoding="utf-8-sig")
    assert "Voucher;" not in payload
    assert "12345-67890" not in payload
    assert "Mario & Lucia <ospiti>" in payload
    assert "Utilizzi controller osservati;2" in payload


def test_renderer_rejects_clear_code_for_summary_purpose(tmp_path: Path):
    output = tmp_path / "invalid.csv"

    with pytest.raises(ValueError, match="code policy"):
        render_report_csv(_dataset(code="12345-67890"), output)

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
    empty = ReportDataset(
        kind=ReportKind.EXPIRED,
        purpose=ReportPurpose.SUMMARY,
        title="Voucher scaduti",
        generated_at=base.generated_at,
        controller_label=base.controller_label,
        rows=(),
        totals=ReportTotals(
            vouchers=0,
            used_vouchers=0,
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
        ),
        code_exposed=False,
    )
    output = tmp_path / "empty.pdf"

    render_report_pdf(empty, output)

    assert output.read_bytes().startswith(b"%PDF-")


def test_csv_neutralizes_formula_like_operator_text(tmp_path: Path):
    output = tmp_path / "formula.csv"
    dataset = _dataset()
    dangerous_row = replace(
        dataset.rows[0],
        controller_name="=HYPERLINK(\"https://example.invalid\")",
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
