"""Tests for the report dialog's Tk/thread boundary."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from voucher_management import report_ui
from voucher_management.report_policy import ReportPurpose
from voucher_management.report_ui import (
    ReportDialog,
    ReportGuideDialog,
    report_label_for_kind,
)
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


def test_pdf_report_is_rendered_to_temp_and_opens_preview_before_save(
    monkeypatch,
    tmp_path: Path,
):
    events = []
    tasks = []
    previews = []
    output = tmp_path / "report-preview.pdf"
    dataset = _empty_dataset()
    database_path = tmp_path / "voucher_management.db"

    class Temporary:
        name = str(output)

        def close(self):
            events.append(("temp_closed",))

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
        include_codes_var=_variable(False),
        _busy=True,
        destroy=lambda: events.append(("destroy",)),
        _set_busy=lambda busy: events.append(("busy", busy)),
    )

    monkeypatch.setattr(report_ui, "build_report_dataset_from_path", build)
    monkeypatch.setattr(report_ui, "render_report_pdf", render)
    monkeypatch.setattr(
        report_ui.tempfile,
        "NamedTemporaryFile",
        lambda **kwargs: Temporary(),
    )
    monkeypatch.setattr(
        report_ui.filedialog,
        "asksaveasfilename",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("PDF must preview before asking where to save")
        ),
    )
    monkeypatch.setattr(
        report_ui,
        "PdfPreview",
        lambda *args, **kwargs: previews.append((args, kwargs)),
    )

    ReportDialog._generate(dialog)

    assert events == [("temp_closed",)]
    assert len(tasks) == 1
    assert tasks[0]["label"] == "Generazione report…"
    assert tasks[0]["busy_scope"] is dialog._set_busy

    result = tasks[0]["worker"]()

    assert result == output
    assert events[1][0] == "build"
    assert events[1][1] == database_path
    assert events[1][2]["controller_id"] == 7
    assert events[2] == (
        "render",
        dataset,
        output,
        "Sala Assemblee",
    )

    tasks[0]["success"](output)

    assert ("destroy",) in events
    assert len(previews) == 1
    args, kwargs = previews[0]
    assert args[0] is app
    assert args[1] == output
    assert kwargs["report_mode"] is True
    assert kwargs["allow_save_copy"] is True
    assert kwargs["delete_on_close"] is True
    assert kwargs["preview_title"] == "Anteprima report"


def test_csv_report_still_uses_direct_save_dialog(monkeypatch, tmp_path: Path):
    tasks = []
    target = tmp_path / "report.csv"
    app = SimpleNamespace(
        active_controller_id=None,
        paths=SimpleNamespace(database=tmp_path / "voucher_management.db"),
        settings={},
        logger=SimpleNamespace(error=lambda *args, **kwargs: None),
        _run_background_task=lambda label, worker, success, error, busy_scope=None: (
            tasks.append((worker, success)) or True
        ),
    )
    dialog = SimpleNamespace(
        app=app,
        kind_var=_variable("Riepilogo storico"),
        scope_var=_variable("Tutto lo storico locale"),
        format_var=_variable("CSV"),
        include_codes_var=_variable(False),
        _set_busy=lambda busy: None,
        destroy=lambda: None,
    )
    monkeypatch.setattr(
        report_ui.filedialog,
        "asksaveasfilename",
        lambda **kwargs: str(target),
    )
    monkeypatch.setattr(
        report_ui,
        "build_report_dataset_from_path",
        lambda *args, **kwargs: _empty_dataset(),
    )
    monkeypatch.setattr(
        report_ui,
        "render_report_csv",
        lambda dataset, output: output.write_text("ok", encoding="utf-8"),
    )

    ReportDialog._generate(dialog)

    assert len(tasks) == 1
    result = tasks[0][0]()
    assert result == target
    assert target.read_text(encoding="utf-8") == "ok"


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
    assert "Eliminati dalla controller" in labels



def test_operational_report_selector_does_not_mix_in_technical_audit():
    operational_kinds = {
        kind for _label, kind in report_ui.REPORT_OPERATIONAL_CHOICES
    }
    technical_kinds = {
        kind for _label, kind in report_ui.REPORT_TECHNICAL_CHOICES
    }

    assert ReportKind.SUMMARY in operational_kinds
    assert ReportKind.USED in operational_kinds
    assert ReportKind.EXPIRED in operational_kinds
    assert ReportKind.SECURITY_REVIEW in operational_kinds
    assert ReportKind.UNCLASSIFIED in operational_kinds

    assert ReportKind.FULL_HISTORY in technical_kinds
    assert ReportKind.PRINT_UNKNOWN in technical_kinds
    assert ReportKind.USAGE_UNKNOWN in technical_kinds
    assert ReportKind.ORIGIN_UNKNOWN in technical_kinds

    assert operational_kinds.isdisjoint(technical_kinds)
    assert operational_kinds | technical_kinds == {
        kind for _label, kind in report_ui.REPORT_CHOICES
    }


def test_report_level_follows_requested_initial_kind():
    assert (
        report_ui.report_level_for_kind(ReportKind.USED)
        == report_ui.REPORT_LEVEL_OPERATIONAL
    )
    assert (
        report_ui.report_level_for_kind(ReportKind.FULL_HISTORY)
        == report_ui.REPORT_LEVEL_TECHNICAL
    )


def test_report_guide_covers_core_operator_questions():
    kinds = {
        kind for _label, kind, _description in report_ui.REPORT_GUIDE_CHOICES
    }

    assert ReportKind.SUMMARY in kinds
    assert ReportKind.GENERATED_UNUSED in kinds
    assert ReportKind.PRINTED_UNUSED in kinds
    assert ReportKind.UNPRINTED_WARNING in kinds
    assert ReportKind.SECURITY_REVIEW in kinds
    assert ReportKind.NOMINAL in kinds
    assert ReportKind.PREPARATION_DELETED in kinds
    assert ReportKind.UNCLASSIFIED in kinds
    assert ReportKind.FULL_HISTORY in kinds
    assert all(
        description.strip()
        for _label, _kind, description in report_ui.REPORT_GUIDE_CHOICES
    )


def test_report_guide_keeps_preparation_deletion_separate_from_security_revocation():
    guide_by_kind = {
        kind: (label, description)
        for label, kind, description in report_ui.REPORT_GUIDE_CHOICES
    }

    nominal_label, nominal_description = guide_by_kind[ReportKind.NOMINAL]
    deleted_label, deleted_description = guide_by_kind[
        ReportKind.PREPARATION_DELETED
    ]

    assert "nominali" in nominal_label.lower()
    assert "titolare" in nominal_description.lower()
    assert "errore di preparazione" in deleted_label.lower()
    assert "revoche di sicurezza" in deleted_description.lower()
    assert ReportKind.SECURITY_REVOKED is not ReportKind.PREPARATION_DELETED
    assert (
        report_ui.report_level_for_kind(ReportKind.NOMINAL)
        == report_ui.REPORT_LEVEL_OPERATIONAL
    )
    assert (
        report_ui.report_level_for_kind(ReportKind.PREPARATION_DELETED)
        == report_ui.REPORT_LEVEL_OPERATIONAL
    )


def test_report_label_for_kind_selects_requested_report():
    assert (
        report_label_for_kind(ReportKind.PRINTED_UNUSED)
        == "Stampati senza uso positivo osservato"
    )
    assert report_label_for_kind(ReportKind.FULL_HISTORY) == "Storico completo"


def test_report_guide_opens_generator_with_recommended_kind(monkeypatch):
    opened = []
    destroyed = []
    fake = SimpleNamespace(
        app=object(),
        choice_var=SimpleNamespace(
            get=lambda: "Voucher stampati ma senza uso osservato"
        ),
        destroy=lambda: destroyed.append(True),
    )
    monkeypatch.setattr(
        report_ui,
        "ReportDialog",
        lambda *args, **kwargs: opened.append((args, kwargs)),
    )

    ReportGuideDialog._open_report(fake)

    assert destroyed == [True]
    assert len(opened) == 1
    assert opened[0][1]["initial_kind"] is ReportKind.PRINTED_UNUSED
