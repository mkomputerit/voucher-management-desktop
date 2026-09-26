"""Tests for the report dialog's Tk/thread boundary."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from voucher_management import report_ui
from voucher_management.report_policy import ReportPurpose
from voucher_management.report_ui import ReportDialog
from voucher_management.reporting import (
    ReportDataset,
    ReportKind,
    ReportTotals,
)


def _empty_dataset() -> ReportDataset:
    return ReportDataset(
        kind=ReportKind.SUMMARY,
        purpose=ReportPurpose.SUMMARY,
        title="Riepilogo voucher",
        generated_at="2026-09-26T12:00:00+00:00",
        controller_label="Controller",
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


def _variable(value):
    return SimpleNamespace(get=lambda: value)


def test_report_query_runs_before_background_renderer(monkeypatch, tmp_path: Path):
    events = []
    tasks = []
    output = tmp_path / "report.pdf"
    dataset = _empty_dataset()

    def build(database, **kwargs):
        events.append(("build", database, kwargs))
        return dataset

    def render(current_dataset, target, *, installation_name=""):
        events.append(
            ("render", current_dataset, Path(target), installation_name)
        )

    def run_background(label, worker, success, error):
        tasks.append(
            {
                "label": label,
                "worker": worker,
                "success": success,
                "error": error,
            }
        )
        return True

    app = SimpleNamespace(
        database=object(),
        active_controller_id=7,
        settings={"structure_name": "Sala Assemblee"},
        logger=SimpleNamespace(error=lambda *args, **kwargs: None),
        _run_background_task=run_background,
    )
    dialog = SimpleNamespace(
        app=app,
        kind_var=_variable("Riepilogo"),
        scope_var=_variable("Controller attivo"),
        format_var=_variable("PDF"),
        destroy=lambda: events.append(("destroy",)),
    )

    monkeypatch.setattr(report_ui, "build_report_dataset", build)
    monkeypatch.setattr(report_ui, "render_report_pdf", render)
    monkeypatch.setattr(
        report_ui.filedialog,
        "asksaveasfilename",
        lambda **kwargs: str(output),
    )

    ReportDialog._generate(dialog)

    assert events[0][0] == "build"
    assert len(tasks) == 1
    assert tasks[0]["label"] == "Generazione report…"
    assert all(event[0] != "render" for event in events)

    result = tasks[0]["worker"]()

    assert result == output
    assert events[-1] == (
        "render",
        dataset,
        output,
        "Sala Assemblee",
    )
    assert events[0][2]["controller_id"] == 7


def test_active_controller_scope_without_controller_blocks_cleanly(monkeypatch):
    messages = []
    app = SimpleNamespace(
        database=object(),
        active_controller_id=None,
        settings={},
        logger=SimpleNamespace(error=lambda *args, **kwargs: None),
    )
    dialog = SimpleNamespace(
        app=app,
        kind_var=_variable("Riepilogo"),
        scope_var=_variable("Controller attivo"),
        format_var=_variable("PDF"),
    )

    monkeypatch.setattr(
        report_ui.messagebox,
        "showinfo",
        lambda title, message, parent=None: messages.append(
            (title, message, parent)
        ),
    )

    ReportDialog._generate(dialog)

    assert len(messages) == 1
    assert messages[0][0] == "Report"
    assert "Nessun controller attivo" in messages[0][1]
