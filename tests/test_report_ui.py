"""Tests for the report dialog's Tk/thread boundary."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from voucher_management import report_ui
from voucher_management.report_policy import ReportPurpose
from voucher_management.report_ui import ReportDialog
from voucher_management.reporting import ReportDataset, ReportKind, ReportTotals


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
        code_exposed=False,
    )


def _variable(value):
    return SimpleNamespace(get=lambda: value)


def test_report_query_and_renderer_both_run_inside_background_worker(
    monkeypatch,
    tmp_path: Path,
):
    events = []
    tasks = []
    output = tmp_path / "report.pdf"
    dataset = _empty_dataset()
    database_path = tmp_path / "voucher_management.db"

    def build(path, **kwargs):
        events.append(("build", Path(path), kwargs))
        return dataset

    def render(current_dataset, target, *, installation_name=""):
        events.append(
            ("render", current_dataset, Path(target), installation_name)
        )

    def run_background(label, worker, success, error, busy_scope=None):
        tasks.append(
            {
                "label": label,
                "worker": worker,
                "success": success,
                "error": error,
                "busy_scope": busy_scope,
            }
        )
        return True

    app = SimpleNamespace(
        active_controller_id=7,
        paths=SimpleNamespace(database=database_path),
        settings={"structure_name": "Sala Assemblee"},
        logger=SimpleNamespace(error=lambda *args, **kwargs: None),
        _run_background_task=run_background,
    )
    dialog = SimpleNamespace(
        app=app,
        kind_var=_variable("Riepilogo storico"),
        scope_var=_variable("Controller attivo"),
        format_var=_variable("PDF"),
        destroy=lambda: events.append(("destroy",)),
        _set_busy=lambda busy: events.append(("busy", busy)),
    )

    monkeypatch.setattr(report_ui, "build_report_dataset_from_path", build)
    monkeypatch.setattr(report_ui, "render_report_pdf", render)
    monkeypatch.setattr(
        report_ui.filedialog,
        "asksaveasfilename",
        lambda **kwargs: str(output),
    )

    ReportDialog._generate(dialog)

    assert events == []
    assert len(tasks) == 1
    assert tasks[0]["label"] == "Generazione report…"
    assert tasks[0]["busy_scope"] is dialog._set_busy

    result = tasks[0]["worker"]()

    assert result == output
    assert events[0][0] == "build"
    assert events[0][1] == database_path
    assert events[0][2]["controller_id"] == 7
    assert events[1] == (
        "render",
        dataset,
        output,
        "Sala Assemblee",
    )


def test_active_controller_scope_without_controller_blocks_cleanly(monkeypatch):
    messages = []
    app = SimpleNamespace(
        active_controller_id=None,
        settings={},
        logger=SimpleNamespace(error=lambda *args, **kwargs: None),
    )
    dialog = SimpleNamespace(
        app=app,
        kind_var=_variable("Riepilogo storico"),
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


def test_report_choices_cover_historical_core_and_data_quality_views():
    labels = [label for label, _kind in report_ui.REPORT_CHOICES]
    assert "Creazione VM confermata" in labels
    assert "Creazione VM confermata • mai osservati usati" in labels
    assert "Stampati" in labels
    assert "Nominali" in labels
    assert "Non nominali" in labels
    assert "Non classificati" in labels
    assert "Uso non determinabile" in labels
    assert "Origine creazione non determinabile" in labels
    assert "Nominalità rimossa per privacy" in labels
    assert "Stato stampa non determinabile" in labels
    assert "Creati ma non stampati oltre soglia" in labels
    assert "Da revocare per sicurezza" in labels
    assert "Revocati per sicurezza" in labels
