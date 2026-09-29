"""Report print submission is independent of the voucher audit lifecycle."""
from types import SimpleNamespace
from voucher_management.report_preview import ReportPreview


def test_report_print_submits_without_voucher_history(monkeypatch):
    from voucher_management import report_preview
    submitted = []
    states = []
    tasks = []
    view = SimpleNamespace(
        _printing=False,
        printer_var=SimpleNamespace(get=lambda: "Test printer"),
        copies_var=SimpleNamespace(get=lambda: 2),
        print_button=SimpleNamespace(state=lambda value: states.append(value)),
        _print_windows=lambda printer, copies: submitted.append((printer, copies)),
        app=SimpleNamespace(_run_background_task=lambda *args: tasks.append(args) or True),
    )
    monkeypatch.setattr(report_preview.messagebox, "showinfo", lambda *args, **kwargs: None)
    ReportPreview.print_document(view)
    assert submitted == []
    assert view._printing
    tasks[0][1]()
    tasks[0][2](None)
    assert submitted == [("Test printer", 2)]
    assert not view._printing
    assert states[-1] == ["!disabled"]


def test_report_busy_close_preserves_pdf_until_print_finishes():
    calls = []
    view = SimpleNamespace(_printing=True, bell=lambda: calls.append("busy"))
    ReportPreview.destroy(view)
    assert calls == ["busy"]
