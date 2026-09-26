from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from voucher_management.history import HistoryError, HistoryService
from voucher_management.modern_app import ModernVoucherApp
from voucher_management.pdf_preview import PdfPreview
from voucher_management.security.history_key import HistoryKeyStore
from voucher_management.settings import SettingsStore


def make_history(tmp_path):
    settings_store = SettingsStore(tmp_path / "config" / "settings.json")
    history = HistoryService(
        tmp_path / "data" / "history.jsonl",
        tmp_path / "data" / "history.lock",
        settings_store,
        secret_store=HistoryKeyStore(tmp_path / "local"),
    )
    return settings_store, history


def test_print_audit_retry_with_same_id_is_idempotent(tmp_path):
    settings_store, history = make_history(tmp_path)
    settings = settings_store.load()
    code = "12345-67890"
    output = tmp_path / "Print" / "Voucher_Test.pdf"
    audit_id = "a" * 32
    submitted_at = "2026-09-21T16:00:00+00:00"

    history.record_print(
        [code],
        output,
        2,
        settings,
        audit_id=audit_id,
        submitted_at=submitted_at,
    )
    history.record_print(
        [code],
        output,
        2,
        settings,
        audit_id=audit_id,
        submitted_at=submitted_at,
    )

    stats = history.stats_for_codes([code], settings_store.load())[code]
    assert stats.print_jobs == 1
    assert stats.printed_copies == 2


def test_print_audit_retry_repairs_only_missing_rows(tmp_path):
    settings_store, history = make_history(tmp_path)
    settings = settings_store.load()
    codes = ["11111-22222", "33333-44444"]
    output = tmp_path / "Print" / "Voucher_Group.pdf"
    audit_id = "b" * 32
    submitted_at = "2026-09-21T16:05:00+00:00"

    history.record_print(
        codes,
        output,
        1,
        settings,
        audit_id=audit_id,
        submitted_at=submitted_at,
    )

    rows = [
        json.loads(line)
        for line in history.history_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    first_print = next(
        row
        for row in rows
        if row.get("event") == "print"
        and row.get("print_job_id") == audit_id
    )
    history.history_path.write_text(
        json.dumps(first_print, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    history.record_print(
        codes,
        output,
        1,
        settings_store.load(),
        audit_id=audit_id,
        submitted_at=submitted_at,
    )

    stats = history.stats_for_codes(codes, settings_store.load())
    assert stats[codes[0]].print_jobs == 1
    assert stats[codes[1]].print_jobs == 1


def test_print_audit_id_cannot_be_reused_with_different_job_data(tmp_path):
    settings_store, history = make_history(tmp_path)
    settings = settings_store.load()
    output = tmp_path / "Print" / "Voucher_Test.pdf"
    audit_id = "c" * 32
    submitted_at = "2026-09-21T16:10:00+00:00"

    history.record_print(
        ["12345-67890"],
        output,
        1,
        settings,
        audit_id=audit_id,
        submitted_at=submitted_at,
    )

    with pytest.raises(HistoryError, match="dati di stampa diversi"):
        history.record_print(
            ["12345-67890"],
            output,
            2,
            settings_store.load(),
            audit_id=audit_id,
            submitted_at=submitted_at,
        )


class _Var:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value


class _Button:
    def __init__(self):
        self.states = []
        self.visible = False

    def state(self, value):
        self.states.append(tuple(value))

    def grid(self):
        self.visible = True

    def grid_remove(self):
        self.visible = False


def test_failed_audit_exposes_manual_registration_without_reprinting(monkeypatch):
    printed = []
    audited = []
    refreshed = []
    shown = []
    captured = {}
    print_button = _Button()
    register_button = _Button()

    class FailingHistory:
        def assert_no_pending_print_audit(self):
            return None

        def prepare_print_audit(
            self,
            codes,
            pdf_path,
            copies,
            settings,
            *,
            audit_id,
            submitted_at,
        ):
            return None

        def mark_print_submitted(self, audit_id):
            return None

        def record_print(
            self,
            codes,
            pdf_path,
            copies,
            settings,
            *,
            audit_id,
            submitted_at,
        ):
            audited.append(
                (list(codes), Path(pdf_path), copies, dict(settings))
            )
            raise HistoryError("synthetic audit failure")

    app = SimpleNamespace(
        _run_background_task=lambda label, worker, success, error: (
            captured.update(
                label=label,
                worker=worker,
                success=success,
                error=error,
            )
            or True
        )
    )
    fake = SimpleNamespace(
        app=app,
        _printing=False,
        printer_var=_Var("Test printer"),
        copies_var=_Var(2),
        print_button=print_button,
        register_print_button=register_button,
        _print_windows=lambda printer, copies: printed.append(
            (printer, copies)
        ),
        history=FailingHistory(),
        codes=["12345-67890"],
        pdf_path=Path("Voucher_Test.pdf"),
        settings={"structure_name": "Test"},
        _pending_print_audit=None,
        on_print=lambda: refreshed.append(True),
        winfo_exists=lambda: True,
    )

    monkeypatch.setattr(
        "voucher_management.pdf_preview.messagebox.showwarning",
        lambda *args, **kwargs: shown.append((args, kwargs)),
    )
    monkeypatch.setattr(
        "voucher_management.pdf_preview.messagebox.showerror",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "voucher_management.pdf_preview.messagebox.showinfo",
        lambda *args, **kwargs: None,
    )

    PdfPreview.print_document(fake)

    assert printed == []
    assert audited == []
    assert captured["label"] == "Rasterizzazione e invio alla stampante…"

    result = captured["worker"]()
    assert printed == [("Test printer", 2)]
    assert len(audited) == 1

    captured["success"](result)
    assert fake._pending_print_audit is not None
    assert register_button.visible is True
    assert refreshed == [True]
    assert shown
    assert "REGISTRA STAMPA" in shown[0][0][1]


def test_pending_audit_blocks_physical_print_before_printer_submission(
    monkeypatch,
):
    printed = []
    captured = {}
    print_button = _Button()
    register_button = _Button()

    class BlockingHistory:
        def assert_no_pending_print_audit(self):
            raise HistoryError("pending audit exists")

        def record_print(self, *args, **kwargs):
            pytest.fail("record_print must not run when pending blocks print")

    app = SimpleNamespace(
        _run_background_task=lambda label, worker, success, error: (
            captured.update(
                label=label,
                worker=worker,
                success=success,
                error=error,
            )
            or True
        )
    )
    fake = SimpleNamespace(
        app=app,
        _printing=False,
        printer_var=_Var("Test printer"),
        copies_var=_Var(1),
        print_button=print_button,
        register_print_button=register_button,
        _print_windows=lambda *args: printed.append(args),
        history=BlockingHistory(),
        codes=["12345-67890"],
        pdf_path=Path("Voucher_Test.pdf"),
        settings={},
        _pending_print_audit=None,
        on_print=None,
        winfo_exists=lambda: True,
    )
    shown = []
    monkeypatch.setattr(
        "voucher_management.pdf_preview.messagebox.showerror",
        lambda *args, **kwargs: shown.append((args, kwargs)),
    )

    PdfPreview.print_document(fake)

    with pytest.raises(HistoryError, match="pending audit"):
        captured["worker"]()

    assert printed == []
    captured["error"](HistoryError("pending audit exists"))
    assert shown
    assert "Impossibile inviare" in shown[0][0][1]


def test_manual_registration_retries_audit_only_on_worker(monkeypatch):
    printed = []
    audited = []
    refreshed = []
    shown = []
    captured = {}
    pending = {
        "copies": 3,
        "audit_id": "d" * 32,
        "submitted_at": "2026-09-21T16:15:00+00:00",
    }
    print_button = _Button()
    register_button = _Button()
    register_button.visible = True

    class History:
        def record_print(
            self,
            codes,
            pdf_path,
            copies,
            settings,
            *,
            audit_id,
            submitted_at,
        ):
            audited.append(
                {
                    "codes": list(codes),
                    "pdf_path": Path(pdf_path),
                    "copies": copies,
                    "settings": dict(settings),
                    "audit_id": audit_id,
                    "submitted_at": submitted_at,
                }
            )

    app = SimpleNamespace(
        _run_background_task=lambda label, worker, success, error: (
            captured.update(
                label=label,
                worker=worker,
                success=success,
                error=error,
            )
            or True
        )
    )
    fake = SimpleNamespace(
        app=app,
        _printing=False,
        print_button=print_button,
        register_print_button=register_button,
        _pending_print_audit=dict(pending),
        history=History(),
        codes=["12345-67890"],
        pdf_path=Path("Voucher_Test.pdf"),
        settings={"structure_name": "Test"},
        _print_windows=lambda *args: printed.append(args),
        on_print=lambda: refreshed.append(True),
        winfo_exists=lambda: True,
    )

    monkeypatch.setattr(
        "voucher_management.pdf_preview.messagebox.showinfo",
        lambda *args, **kwargs: shown.append((args, kwargs)),
    )
    monkeypatch.setattr(
        "voucher_management.pdf_preview.messagebox.showerror",
        lambda *args, **kwargs: None,
    )

    PdfPreview.register_print_audit(fake)

    assert printed == []
    assert audited == []
    assert captured["label"] == "Registrazione stampa…"
    assert fake._pending_print_audit == pending

    result = captured["worker"]()
    assert result is None
    assert printed == []
    assert len(audited) == 1
    assert audited[0]["audit_id"] == pending["audit_id"]
    assert audited[0]["submitted_at"] == pending["submitted_at"]
    assert audited[0]["copies"] == 3

    captured["success"](result)
    assert fake._pending_print_audit is None
    assert register_button.visible is False
    assert refreshed == [True]
    assert shown
    assert "registrata correttamente" in shown[0][0][1]


def test_manual_registration_failure_keeps_pending_job(monkeypatch):
    captured = {}
    pending = {
        "copies": 1,
        "audit_id": "e" * 32,
        "submitted_at": "2026-09-21T16:20:00+00:00",
    }
    print_button = _Button()
    register_button = _Button()
    register_button.visible = True

    class History:
        def record_print(self, *args, **kwargs):
            raise HistoryError("synthetic retry failure")

    app = SimpleNamespace(
        _run_background_task=lambda label, worker, success, error: (
            captured.update(
                label=label,
                worker=worker,
                success=success,
                error=error,
            )
            or True
        )
    )
    fake = SimpleNamespace(
        app=app,
        _printing=False,
        print_button=print_button,
        register_print_button=register_button,
        _pending_print_audit=dict(pending),
        history=History(),
        codes=["12345-67890"],
        pdf_path=Path("Voucher_Test.pdf"),
        settings={},
        on_print=None,
        winfo_exists=lambda: True,
    )
    shown = []
    monkeypatch.setattr(
        "voucher_management.pdf_preview.messagebox.showerror",
        lambda *args, **kwargs: shown.append((args, kwargs)),
    )

    PdfPreview.register_print_audit(fake)
    with pytest.raises(HistoryError):
        captured["worker"]()

    captured["error"](HistoryError("synthetic retry failure"))
    assert fake._pending_print_audit == pending
    assert register_button.visible is True
    assert shown
    assert "non ristampare" in shown[0][0][1]


def test_print_flow_persists_intent_before_windows_submission(monkeypatch):
    order = []
    captured = {}
    confirmed = []
    print_button = _Button()
    register_button = _Button()

    class History:
        def assert_no_pending_print_audit(self):
            order.append("assert")

        def prepare_print_audit(
            self,
            codes,
            pdf_path,
            copies,
            settings,
            *,
            audit_id,
            submitted_at,
        ):
            order.append(("prepare", audit_id, submitted_at, copies))

        def mark_print_submitted(self, audit_id):
            order.append(("submitted", audit_id))

        def record_print(
            self,
            codes,
            pdf_path,
            copies,
            settings,
            *,
            audit_id,
            submitted_at,
        ):
            order.append(("record", audit_id, submitted_at, copies))

    app = SimpleNamespace(
        _run_background_task=lambda label, worker, success, error: (
            captured.update(
                label=label,
                worker=worker,
                success=success,
                error=error,
            )
            or True
        )
    )
    fake = SimpleNamespace(
        app=app,
        _printing=False,
        printer_var=_Var("Test printer"),
        copies_var=_Var(2),
        print_button=print_button,
        register_print_button=register_button,
        _print_windows=lambda printer, copies: order.append(
            ("windows", printer, copies)
        ),
        history=History(),
        codes=["12345-67890"],
        pdf_path=Path("Voucher_Test.pdf"),
        settings={},
        _pending_print_audit=None,
        on_print=None,
        on_submitted=lambda: confirmed.append(True),
        winfo_exists=lambda: True,
    )

    monkeypatch.setattr(
        "voucher_management.pdf_preview.messagebox.showinfo",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "voucher_management.pdf_preview.messagebox.showwarning",
        lambda *args, **kwargs: None,
    )

    PdfPreview.print_document(fake)
    result = captured["worker"]()

    assert order[0] == "assert"
    assert order[1][0] == "prepare"
    assert order[2] == ("windows", "Test printer", 2)
    assert order[3][0] == "submitted"
    assert order[4][0] == "record"
    assert order[1][1] == order[3][1] == order[4][1]
    assert order[1][2] == order[4][2]
    assert result[1] is None

    captured["success"](result)
    assert confirmed == [True]


def test_windows_failure_leaves_prepared_job_for_operator_resolution(monkeypatch):
    captured = {}
    warnings = []
    print_button = _Button()
    register_button = _Button()

    class History:
        state = ""

        def assert_no_pending_print_audit(self):
            return None

        def prepare_print_audit(self, *args, **kwargs):
            self.state = "prepared"

        def mark_print_submitted(self, *args, **kwargs):
            pytest.fail("submission must not be marked after Windows failure")

        def record_print(self, *args, **kwargs):
            pytest.fail("audit must not be recorded after Windows failure")

        def pending_print_state(self):
            return self.state

    app = SimpleNamespace(
        _run_background_task=lambda label, worker, success, error: (
            captured.update(
                label=label,
                worker=worker,
                success=success,
                error=error,
            )
            or True
        )
    )
    fake = SimpleNamespace(
        app=app,
        _printing=False,
        printer_var=_Var("Test printer"),
        copies_var=_Var(1),
        print_button=print_button,
        register_print_button=register_button,
        _print_windows=lambda *args: (_ for _ in ()).throw(
            RuntimeError("synthetic Windows failure")
        ),
        history=History(),
        codes=["12345-67890"],
        pdf_path=Path("Voucher_Test.pdf"),
        settings={},
        _pending_print_audit=None,
        on_print=None,
        winfo_exists=lambda: True,
    )

    monkeypatch.setattr(
        "voucher_management.pdf_preview.messagebox.showwarning",
        lambda *args, **kwargs: warnings.append((args, kwargs)),
    )
    monkeypatch.setattr(
        "voucher_management.pdf_preview.messagebox.showerror",
        lambda *args, **kwargs: None,
    )

    PdfPreview.print_document(fake)
    with pytest.raises(RuntimeError, match="synthetic Windows failure"):
        captured["worker"]()

    assert fake.history.pending_print_state() == "prepared"
    captured["error"](RuntimeError("synthetic Windows failure"))
    assert warnings
    assert "Non ristampare" in warnings[0][0][1]


def test_prepared_recovery_ui_confirms_print_without_resubmitting(monkeypatch):
    captured = {}
    calls = []

    class History:
        def pending_print_state(self):
            return "prepared"

        def recover_pending_print_audit(self, *, assume_submitted=False):
            calls.append(("recover", assume_submitted))
            return True

        def discard_prepared_print_audit(self):
            calls.append(("discard",))
            return True

    fake = SimpleNamespace(
        history=History(),
        populate=lambda: calls.append(("populate",)),
        _dialog_busy_scope=lambda parent: None,
        _run_background_task=lambda label, worker, success, error, **kwargs: (
            captured.update(label=label, worker=worker, success=success, error=error)
            or True
        ),
    )
    monkeypatch.setattr(
        "voucher_management.data_maintenance_ui.messagebox.askyesnocancel",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        "voucher_management.data_maintenance_ui.messagebox.showinfo",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "voucher_management.data_maintenance_ui.messagebox.showerror",
        lambda *args, **kwargs: None,
    )

    ModernVoucherApp.recover_pending_print_audit(fake, parent=object())
    assert captured["label"] == "Recupero stampa pendente…"
    assert captured["worker"]() is True
    assert calls == [("recover", True)]


def test_prepared_recovery_ui_can_discard_not_printed_job(monkeypatch):
    captured = {}
    calls = []

    class History:
        def pending_print_state(self):
            return "prepared"

        def recover_pending_print_audit(self, *, assume_submitted=False):
            pytest.fail("recovery must not run when operator says not printed")

        def discard_prepared_print_audit(self):
            calls.append(("discard",))
            return True

    fake = SimpleNamespace(
        history=History(),
        populate=lambda: calls.append(("populate",)),
        _dialog_busy_scope=lambda parent: None,
        _run_background_task=lambda label, worker, success, error, **kwargs: (
            captured.update(label=label, worker=worker, success=success, error=error)
            or True
        ),
    )
    monkeypatch.setattr(
        "voucher_management.data_maintenance_ui.messagebox.askyesnocancel",
        lambda *args, **kwargs: False,
    )
    monkeypatch.setattr(
        "voucher_management.data_maintenance_ui.messagebox.showinfo",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "voucher_management.data_maintenance_ui.messagebox.showerror",
        lambda *args, **kwargs: None,
    )

    ModernVoucherApp.recover_pending_print_audit(fake, parent=object())
    assert captured["label"] == "Annullamento stampa pendente…"
    assert captured["worker"]() is True
    assert calls == [("discard",)]


def test_sqlite_audit_keeps_marker_until_secondary_commit(monkeypatch):
    captured = {}
    order = []
    print_button = _Button()
    register_button = _Button()

    class History:
        def assert_no_pending_print_audit(self):
            order.append("assert")

        def prepare_print_audit(self, *args, **kwargs):
            order.append("prepare")

        def mark_print_submitted(self, audit_id):
            order.append(("submitted", audit_id))

        def record_print(self, *args, **kwargs):
            order.append(("history", kwargs.get("clear_pending", True)))

        def finalize_pending_print_audit(self, audit_id):
            order.append(("finalize", audit_id))

    app = SimpleNamespace(
        _run_background_task=lambda label, worker, success, error: (
            captured.update(worker=worker, success=success, error=error) or True
        )
    )
    fake = SimpleNamespace(
        app=app,
        _printing=False,
        printer_var=_Var("Test printer"),
        copies_var=_Var(1),
        print_button=print_button,
        register_print_button=register_button,
        _print_windows=lambda *args: order.append("windows"),
        history=History(),
        codes=["12345-67890"],
        pdf_path=Path("Voucher_Test.pdf"),
        settings={},
        _pending_print_audit=None,
        on_print=None,
        on_submitted=None,
        on_audit=lambda pending, codes, path: order.append(
            ("sqlite", pending["audit_id"], tuple(codes), path.name)
        ),
        winfo_exists=lambda: True,
    )

    monkeypatch.setattr(
        "voucher_management.pdf_preview.messagebox.showinfo",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "voucher_management.pdf_preview.messagebox.showwarning",
        lambda *args, **kwargs: None,
    )

    PdfPreview.print_document(fake)
    result = captured["worker"]()
    assert ("history", False) in order
    assert not any(
        isinstance(item, tuple) and item[0] == "finalize"
        for item in order
    )

    captured["success"](result)
    sqlite_index = next(i for i, item in enumerate(order) if isinstance(item, tuple) and item[0] == "sqlite")
    finalize_index = next(i for i, item in enumerate(order) if isinstance(item, tuple) and item[0] == "finalize")
    assert sqlite_index < finalize_index
