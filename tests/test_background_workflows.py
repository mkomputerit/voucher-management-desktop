from __future__ import annotations

from pathlib import Path
from voucher_management.backup_options_ui import BackupChoice
from queue import Queue
from types import SimpleNamespace

from voucher_management import app as app_module
from voucher_management import dialogs as dialogs_module
from voucher_management import modern_app
from voucher_management import data_maintenance_ui as maintenance_ui
from voucher_management import voucher_creation_ui as creation_ui
from voucher_management.app import VoucherApp
from voucher_management.background_tasks import BackgroundResult


def exchange_phrase(marker: str = "p") -> str:
    return marker * 24


def backup_artifact(path):
    return SimpleNamespace(
        path=Path(path),
        sha256="a" * 64,
        backup_format=2,
        schema_version=2,
        encrypted=True,
    )


def capture_runner(tasks):
    def run(label, worker, success, error, *, busy_scope=None):
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

    return run


def test_general_background_coordinator_serializes_all_work(monkeypatch):
    scheduled = []
    busy = []
    bells = []
    queue = object()
    fake = SimpleNamespace(
        _background_results=None,
        _background_success=None,
        _background_error=None,
        bell=lambda: bells.append(True),
        _set_background_busy=lambda state, label="": busy.append(
            (state, label)
        ),
        after=lambda delay, callback: scheduled.append((delay, callback)),
        _poll_background_task=lambda: None,
    )
    monkeypatch.setattr(
        app_module,
        "start_background_task",
        lambda worker: queue,
    )

    assert VoucherApp._run_background_task(
        fake,
        "Operazione lunga…",
        lambda: "value",
        lambda value: None,
        lambda exc: None,
    )
    assert fake._background_results is queue
    assert busy == [(True, "Operazione lunga…")]
    assert scheduled and scheduled[0][0] == 40

    assert (
        VoucherApp._run_background_task(
            fake,
            "Seconda operazione…",
            lambda: None,
            lambda value: None,
            lambda exc: None,
        )
        is False
    )
    assert bells == [True]


def test_background_completion_releases_modal_before_success_callback():
    events = []
    results = Queue()
    results.put(BackgroundResult(value="done"))
    fake = SimpleNamespace(
        _background_results=results,
        _background_success=lambda value: events.append(
            ("success", value)
        ),
        _background_error=lambda exc: events.append(("error", exc)),
        _background_scope=lambda busy: events.append(("scope", busy)),
        _set_background_busy=lambda busy, label="": events.append(
            ("app", busy)
        ),
        winfo_exists=lambda: True,
    )

    VoucherApp._poll_background_task(fake)

    assert events == [
        ("app", False),
        ("scope", False),
        ("success", "done"),
    ]
    assert fake._background_results is None
    assert fake._background_scope is None


def test_modal_busy_scope_disables_and_reenables_caller():
    attributes = []
    parent = SimpleNamespace(
        winfo_exists=lambda: True,
        attributes=lambda name, value: attributes.append((name, value)),
    )
    fake = SimpleNamespace()

    scope = modern_app.ModernVoucherApp._dialog_busy_scope(
        fake,
        parent,
    )
    scope(True)
    scope(False)

    assert attributes == [
        ("-disabled", 1),
        ("-disabled", 0),
    ]


def test_create_dialog_accept_is_thin_validation_adapter(monkeypatch):
    captured = {}
    destroyed = []
    fake = SimpleNamespace(
        name=SimpleNamespace(get=lambda: " Guest "),
        qty=SimpleNamespace(get=lambda: 2),
        mode=SimpleNamespace(get=lambda: "Multiuso"),
        quota=SimpleNamespace(get=lambda: 5),
        expire=SimpleNamespace(get=lambda: 24),
        unit=SimpleNamespace(get=lambda: "Ore"),
        data=SimpleNamespace(get=lambda: ""),
        down=SimpleNamespace(get=lambda: "50"),
        up=SimpleNamespace(get=lambda: ""),
        result=None,
        destroy=lambda: destroyed.append(True),
    )

    def validate(**kwargs):
        captured.update(kwargs)
        return {"recipient": "Guest", "quantity": 2}

    monkeypatch.setattr(
        dialogs_module,
        "validate_create_params",
        validate,
    )

    dialogs_module.CreateDialog.accept(fake)

    assert captured["recipient"] == " Guest "
    assert captured["mode"] == "Multiuso"
    assert captured["down_mbps"] == "50"
    assert fake.result == {"recipient": "Guest", "quantity": 2}
    assert destroyed == [True]


def test_create_dialog_validation_error_stays_in_ui(monkeypatch):
    shown = []
    destroyed = []
    var = SimpleNamespace(get=lambda: "value")
    fake = SimpleNamespace(
        name=var,
        qty=var,
        mode=var,
        quota=var,
        expire=var,
        unit=var,
        data=var,
        down=var,
        up=var,
        result=None,
        destroy=lambda: destroyed.append(True),
    )
    monkeypatch.setattr(
        dialogs_module,
        "validate_create_params",
        lambda **kwargs: (_ for _ in ()).throw(
            ValueError("invalid")
        ),
    )
    monkeypatch.setattr(
        dialogs_module.messagebox,
        "showerror",
        lambda *args, **kwargs: shown.append((args, kwargs)),
    )

    dialogs_module.CreateDialog.accept(fake)

    assert fake.result is None
    assert destroyed == []
    assert shown
    assert shown[0][0][0] == "Voucher"


def test_uncertain_create_keeps_guard_and_blocks_second_create(monkeypatch):
    tasks = []
    shown = []

    class Guard:
        def __init__(self):
            self.pending = False

        def begin(self):
            assert self.pending is False
            self.pending = True

        def clear(self):
            self.pending = False
            return True

    guard = Guard()
    filter_var = SimpleNamespace(set=lambda value: None)
    fake = SimpleNamespace(
        client=object(),
        create_guard=guard,
        vouchers=[],
        checked_ids={"old"},
        filter_var=filter_var,
        populate=lambda: None,
        logger=SimpleNamespace(warning=lambda *args: None),
        _run_network_task=capture_runner(tasks),
        _show_network_error=lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        creation_ui,
        "CreateDialog",
        lambda parent: SimpleNamespace(
            result={"recipient": "Guest", "quantity": 1}
        ),
    )
    outcome = SimpleNamespace(
        created=(),
        vouchers=("fresh",),
        refresh_error=None,
        uncertain_error=RuntimeError("uncertain"),
    )
    monkeypatch.setattr(
        creation_ui,
        "create_vouchers_and_refresh",
        lambda *args, **kwargs: outcome,
    )
    monkeypatch.setattr(
        creation_ui.messagebox,
        "showwarning",
        lambda *args, **kwargs: shown.append((args, kwargs)),
    )

    VoucherApp.create(fake)

    assert guard.pending is True
    assert len(tasks) == 1
    result = tasks[0]["worker"]()
    tasks[0]["success"](result)

    assert guard.pending is True
    assert fake.vouchers == ["fresh"]
    assert fake.checked_ids == set()
    assert shown
    assert "Non ripetere" in shown[-1][0][1]

    VoucherApp.create(fake)

    assert len(tasks) == 1
    assert "esito da verificare" in shown[-1][0][1]


def test_manual_refresh_clears_pending_create_guard(monkeypatch):
    tasks = []

    class Guard:
        pending = True

        def clear(self):
            self.pending = False
            return True

    guard = Guard()
    client = object()
    fake = SimpleNamespace(
        client=client,
        create_guard=guard,
        vouchers=[],
        logger=SimpleNamespace(warning=lambda *args: None),
        populate=lambda: None,
        _run_network_task=capture_runner(tasks),
        _show_network_error=lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        app_module,
        "refresh_vouchers",
        lambda current: ["fresh"],
    )
    monkeypatch.setattr(
        app_module.messagebox,
        "showwarning",
        lambda *args, **kwargs: None,
    )

    VoucherApp.refresh(fake)

    assert guard.pending is True
    result = tasks[0]["worker"]()
    tasks[0]["success"](result)

    assert guard.pending is False
    assert fake.vouchers == ["fresh"]


def test_print_selected_delegates_preparation_and_defers_execution(monkeypatch):
    calls = []
    tasks = []
    voucher = SimpleNamespace(
        code_formatted="11111-22222",
        quota=1,
    )
    job = SimpleNamespace(
        output=Path("Print") / "Voucher_Test.pdf",
    )
    outcome = SimpleNamespace(
        output=job.output,
        codes=("11111-22222",),
    )
    history = SimpleNamespace()
    fake = SimpleNamespace(
        selected=lambda: [voucher],
        history=history,
        settings={"structure_name": "Test"},
        paths=SimpleNamespace(prints=Path("Print")),
        _run_background_task=capture_runner(tasks),
        logger=SimpleNamespace(
            warning=lambda *args: None,
            error=lambda *args: None,
        ),
        last_pdf=None,
        populate=lambda: calls.append(("populate", None)),
        _preview=lambda path, codes: calls.append(
            ("preview", path, list(codes))
        ),
    )

    monkeypatch.setattr(
        app_module,
        "verify_print_history_ready",
        lambda selected, *, history, settings: calls.append(
            ("history-ready", tuple(selected))
        ),
    )
    monkeypatch.setattr(
        app_module,
        "prepare_print_job",
        lambda selected, prints_root, *, unlimited_copies, now: (
            calls.append(("prepare", unlimited_copies, prints_root))
            or job
        ),
    )
    monkeypatch.setattr(
        app_module,
        "execute_print_job",
        lambda current_job, **kwargs: (
            calls.append(("execute", current_job))
            or outcome
        ),
    )

    VoucherApp.print_selected(fake)

    assert [entry[0] for entry in calls] == [
        "history-ready",
        "prepare",
    ]
    assert tasks[0]["label"] == "Generazione PDF…"
    assert not any(entry[0] == "execute" for entry in calls)

    result = tasks[0]["worker"]()
    assert result is outcome
    assert calls[-1] == ("execute", job)

    tasks[0]["success"](result)
    assert fake.last_pdf == job.output
    assert calls[-2][0] == "populate"
    assert calls[-1] == (
        "preview",
        job.output,
        ["11111-22222"],
    )


def test_open_existing_pdf_delegates_resolution_and_opens_verified_codes(
    monkeypatch,
):
    current = SimpleNamespace(code_formatted="11111-22222")
    other = SimpleNamespace(code_formatted="33333-44444")
    resolved = SimpleNamespace(
        path=Path("Print") / "Voucher_Group.pdf",
        linked_codes=("11111-22222", "33333-44444"),
    )
    captured = {}
    previews = []
    fake = SimpleNamespace(
        selected=lambda: [current],
        vouchers=[current, other],
        history=object(),
        settings={"structure_name": "Test"},
        paths=SimpleNamespace(prints=Path("Print")),
        _preview=lambda path, codes: previews.append(
            (path, list(codes))
        ),
        logger=SimpleNamespace(error=lambda *args: None),
    )

    def resolve(voucher, all_vouchers, **kwargs):
        captured["voucher"] = voucher
        captured["all_vouchers"] = list(all_vouchers)
        captured.update(kwargs)
        return resolved

    monkeypatch.setattr(
        app_module,
        "resolve_existing_pdf",
        resolve,
    )

    VoucherApp.open_existing_pdf(fake)

    assert captured["voucher"] is current
    assert captured["all_vouchers"] == [current, other]
    assert captured["history"] is fake.history
    assert previews == [
        (
            resolved.path,
            ["11111-22222", "33333-44444"],
        )
    ]


def test_open_existing_pdf_maps_typed_linkage_failure_to_ui(monkeypatch):
    shown = []
    current = SimpleNamespace(code_formatted="11111-22222")
    fake = SimpleNamespace(
        selected=lambda: [current],
        vouchers=[current],
        history=object(),
        settings={},
        paths=SimpleNamespace(prints=Path("Print")),
        _preview=lambda *args: None,
        logger=SimpleNamespace(error=lambda *args: None),
    )
    monkeypatch.setattr(
        app_module,
        "resolve_existing_pdf",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            app_module.ExistingPdfResolutionError(
                "linkage_mismatch"
            )
        ),
    )
    monkeypatch.setattr(
        app_module.messagebox,
        "showerror",
        lambda *args, **kwargs: shown.append((args, kwargs)),
    )

    VoucherApp.open_existing_pdf(fake)

    assert shown
    assert "non è verificabile" in shown[0][0][1]


def test_create_backup_defers_encrypted_archive_work(monkeypatch):
    tasks = []
    calls = []
    password = exchange_phrase("b")
    service = SimpleNamespace(
        create_verified=lambda target, password=None: calls.append(
            ("create", Path(target), password)
        )
        or backup_artifact(target)
    )
    fake = SimpleNamespace(
        paths=SimpleNamespace(automatic_backups=Path("C:/Temp")),
        _backup_service=lambda: service,
        _backup_audit_started_at=lambda: "2026-09-27T07:00:00+00:00",
        _record_backup_audit=lambda **kwargs: calls.append(
            ("audit", kwargs)
        )
        or True,
        _dialog_busy_scope=lambda parent: None,
        _run_background_task=capture_runner(tasks),
    )
    monkeypatch.setattr(
        maintenance_ui,
        "ask_backup_options",
        lambda parent, **kwargs: BackupChoice(Path("C:/Temp/test-backup.vmbk"), password),
    )
    monkeypatch.setattr(
        maintenance_ui.filedialog,
        "asksaveasfilename",
        lambda **kwargs: "C:/Temp/test-backup.vmbk",
    )
    monkeypatch.setattr(
        maintenance_ui.messagebox,
        "showinfo",
        lambda *args, **kwargs: calls.append(("info", None)),
    )
    monkeypatch.setattr(
        maintenance_ui.messagebox,
        "showerror",
        lambda *args, **kwargs: calls.append(("error", None)),
    )

    modern_app.ModernVoucherApp.create_backup(fake)

    assert calls == []
    assert tasks[0]["label"] == "Creazione backup…"
    result = tasks[0]["worker"]()
    assert calls[0] == (
        "create",
        Path("C:/Temp/test-backup.vmbk"),
        password,
    )
    tasks[0]["success"](result)
    assert calls[-2][0] == "audit"
    assert calls[-2][1]["destination"] == "MANUAL"
    assert calls[-2][1]["artifact"].sha256 == "a" * 64
    assert calls[-1][0] == "info"

def test_create_encrypted_backup_uses_per_copy_options(monkeypatch):
    tasks = []
    calls = []
    password = exchange_phrase("s")
    service = SimpleNamespace(
        create_verified=lambda target, password=None: calls.append(
            ("create", Path(target), password)
        )
        or backup_artifact(target)
    )
    fake = SimpleNamespace(
        paths=SimpleNamespace(automatic_backups=Path("C:/Temp")),
        _backup_service=lambda: service,
        _backup_audit_started_at=lambda: "2026-09-27T07:00:00+00:00",
        _record_backup_audit=lambda **kwargs: calls.append(
            ("audit", kwargs)
        )
        or True,
        _dialog_busy_scope=lambda parent: None,
        _run_background_task=capture_runner(tasks),
    )
    prompts = []
    monkeypatch.setattr(
        maintenance_ui.messagebox,
        "askyesnocancel",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        maintenance_ui,
        "ask_backup_options",
        lambda parent, **kwargs: (
            prompts.append(kwargs) or BackupChoice(Path("C:/Temp/test-backup.vmbk"), password)
        ),
    )
    monkeypatch.setattr(
        maintenance_ui.filedialog,
        "asksaveasfilename",
        lambda **kwargs: "C:/Temp/test-backup.vmbk",
    )
    monkeypatch.setattr(
        maintenance_ui.messagebox,
        "showinfo",
        lambda *args, **kwargs: calls.append(("info", None)),
    )
    monkeypatch.setattr(
        maintenance_ui.messagebox,
        "showerror",
        lambda *args, **kwargs: calls.append(("error", None)),
    )

    modern_app.ModernVoucherApp.create_backup(fake)

    assert prompts and prompts[0]["closing"] is False
    assert calls == []
    assert tasks[0]["label"] == "Creazione backup…"

    result = tasks[0]["worker"]()
    assert calls[0] == (
        "create",
        Path("C:/Temp/test-backup.vmbk"),
        password,
    )
    tasks[0]["success"](result)
    assert calls[-1][0] == "info"


def test_history_export_uses_shared_password_prompt(monkeypatch):
    tasks = []
    calls = []
    password = exchange_phrase("h")
    service = SimpleNamespace(
        export=lambda target, supplied: calls.append(
            ("export", Path(target), supplied)
        )
        or Path(target)
    )
    fake = SimpleNamespace(
        history=SimpleNamespace(
            identity_state=lambda: SimpleNamespace(ready=True)
        ),
        _history_exchange_service=lambda: service,
        _dialog_busy_scope=lambda parent: None,
        _run_background_task=capture_runner(tasks),
    )
    prompts = []
    monkeypatch.setattr(
        maintenance_ui,
        "ask_password",
        lambda parent, **kwargs: (
            prompts.append(kwargs) or password
        ),
    )
    monkeypatch.setattr(
        maintenance_ui.filedialog,
        "asksaveasfilename",
        lambda **kwargs: "C:/Temp/history.vmhx",
    )
    monkeypatch.setattr(
        maintenance_ui.messagebox,
        "showinfo",
        lambda *args, **kwargs: calls.append(("info", None)),
    )
    monkeypatch.setattr(
        maintenance_ui.messagebox,
        "showerror",
        lambda *args, **kwargs: calls.append(("error", None)),
    )

    modern_app.ModernVoucherApp.export_history_exchange(fake)

    assert prompts and prompts[0]["confirm"] is True
    assert calls == []
    assert tasks[0]["label"] == "Esportazione cronologia…"

    result = tasks[0]["worker"]()
    assert calls[0] == (
        "export",
        Path("C:/Temp/history.vmhx"),
        password,
    )
    tasks[0]["success"](result)
    assert calls[-1][0] == "info"


def test_manual_pending_print_recovery_runs_on_background_worker(monkeypatch):
    tasks = []
    calls = []
    fake = SimpleNamespace(
        history=SimpleNamespace(
            pending_print_state=lambda: "",
            recover_pending_print_audit=lambda: (
                calls.append(("recover", None)) or True
            ),
        ),
        _dialog_busy_scope=lambda parent: None,
        _run_background_task=capture_runner(tasks),
        populate=lambda: calls.append(("populate", None)),
    )
    monkeypatch.setattr(
        maintenance_ui.messagebox,
        "showinfo",
        lambda *args, **kwargs: calls.append(("info", args[0])),
    )
    monkeypatch.setattr(
        maintenance_ui.messagebox,
        "showerror",
        lambda *args, **kwargs: calls.append(("error", args[0])),
    )

    modern_app.ModernVoucherApp.recover_pending_print_audit(fake)

    assert calls == []
    assert tasks[0]["label"] == "Recupero stampa pendente…"

    result = tasks[0]["worker"]()
    assert result is True
    assert calls == [("recover", None)]

    tasks[0]["success"](result)
    assert calls[-2:] == [
        ("populate", None),
        ("info", "Registrazione stampa"),
    ]


def test_restore_validation_and_apply_are_two_background_phases(monkeypatch):
    tasks = []
    calls = []

    class Service:
        def is_encrypted_backup(self, source):
            return False

        def validate(self, source):
            calls.append(("validate", Path(source)))
            return {"created_utc": "2026-09-21T18:00:00+00:00"}

        def restore(self, source, password=None):
            calls.append(("restore", Path(source), password))
            return Path("rollback")

        def consume_restore_warnings(self):
            return ()

    fake = SimpleNamespace(
        _backup_service=lambda: Service(),
        _dialog_busy_scope=lambda parent: None,
        _run_background_task=capture_runner(tasks),
        destroy=lambda: calls.append(("destroy", None)),
    )
    monkeypatch.setattr(
        maintenance_ui.filedialog,
        "askopenfilename",
        lambda **kwargs: "C:/Temp/test-backup.zip",
    )
    monkeypatch.setattr(
        maintenance_ui.messagebox,
        "askyesno",
        lambda *args, **kwargs: calls.append(("confirm", None)) or True,
    )
    monkeypatch.setattr(
        maintenance_ui.messagebox,
        "showinfo",
        lambda *args, **kwargs: calls.append(("info", None)),
    )
    monkeypatch.setattr(
        maintenance_ui.messagebox,
        "showerror",
        lambda *args, **kwargs: calls.append(("error", None)),
    )

    modern_app.ModernVoucherApp.restore_backup(fake)

    assert calls == []
    assert len(tasks) == 1
    assert tasks[0]["label"] == "Verifica backup…"

    manifest = tasks[0]["worker"]()
    assert calls == [("validate", Path("C:/Temp/test-backup.zip"))]
    tasks[0]["success"](manifest)

    assert calls[-1][0] == "confirm"
    assert len(tasks) == 2
    assert tasks[1]["label"] == "Ripristino backup…"
    assert not any(entry[0] == "restore" for entry in calls)

    rollback = tasks[1]["worker"]()
    assert calls[-1][0] == "restore"
    tasks[1]["success"](rollback)
    assert calls[-2][0] == "info"
    assert calls[-1][0] == "destroy"


def test_history_import_prepare_confirm_apply_are_split_across_workers(
    monkeypatch,
):
    tasks = []
    calls = []
    plan = SimpleNamespace(
        incoming_fingerprint="ab" * 8,
        incoming_events=4,
        new_events=2,
        duplicate_events=2,
        conflict_events=0,
        can_adopt_identity=False,
    )

    class Service:
        def prepare_import(self, source, phrase):
            calls.append(("prepare", Path(source), phrase))
            return plan

        def apply_import(self, current, *, adopt_identity):
            calls.append(("apply", current, adopt_identity))
            return 2

    fake = SimpleNamespace(
        _history_exchange_service=lambda: Service(),
        _history_exchange_fingerprint=lambda value: value.upper(),
        _dialog_busy_scope=lambda parent: None,
        _run_background_task=capture_runner(tasks),
        settings_store=SimpleNamespace(load=lambda: {"ok": True}),
        settings={},
        _history_error_shown=True,
        populate=lambda: calls.append(("populate", None)),
    )
    monkeypatch.setattr(
        maintenance_ui.filedialog,
        "askopenfilename",
        lambda **kwargs: "C:/Temp/history.vmhx",
    )
    monkeypatch.setattr(
        maintenance_ui,
        "ask_password",
        lambda *args, **kwargs: exchange_phrase(),
    )
    monkeypatch.setattr(
        maintenance_ui.messagebox,
        "askyesno",
        lambda *args, **kwargs: calls.append(("confirm", None)) or True,
    )
    monkeypatch.setattr(
        maintenance_ui.messagebox,
        "showinfo",
        lambda *args, **kwargs: calls.append(("info", None)),
    )
    monkeypatch.setattr(
        maintenance_ui.messagebox,
        "showerror",
        lambda *args, **kwargs: calls.append(("error", None)),
    )

    modern_app.ModernVoucherApp.import_history_exchange(fake)

    assert calls == []
    assert len(tasks) == 1
    assert tasks[0]["label"] == "Verifica pacchetto cronologia…"

    prepared = tasks[0]["worker"]()
    assert calls[0][0] == "prepare"
    tasks[0]["success"](prepared)

    assert calls[-1][0] == "confirm"
    assert len(tasks) == 2
    assert tasks[1]["label"] == "Merge cronologia…"
    assert not any(entry[0] == "apply" for entry in calls)

    added = tasks[1]["worker"]()
    assert calls[-1][0] == "apply"
    tasks[1]["success"](added)
    assert fake.settings == {"ok": True}
    assert fake._history_error_shown is False
    assert calls[-2][0] == "populate"
    assert calls[-1][0] == "info"



def test_request_close_without_backup_enabled_closes_with_disabled_status(monkeypatch):
    closed = []
    fake = SimpleNamespace(
        settings={"backup_on_close": False},
        _background_results=None,
        _finish_close=lambda **kwargs: closed.append(kwargs),
    )
    monkeypatch.setattr(
        maintenance_ui,
        "ask_backup_options",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("password prompt must not open")
        ),
    )

    modern_app.ModernVoucherApp.request_close(fake)

    assert closed == [
        {"close_status": "CLOSED", "backup_status": "DISABLED"}
    ]


def test_request_close_refuses_to_interrupt_background_operation(monkeypatch):
    shown = []
    destroyed = []
    fake = SimpleNamespace(
        settings={"backup_on_close": True},
        _background_results=object(),
        destroy=lambda: destroyed.append(True),
    )
    monkeypatch.setattr(
        maintenance_ui.messagebox,
        "showinfo",
        lambda *args, **kwargs: shown.append((args, kwargs)),
    )

    modern_app.ModernVoucherApp.request_close(fake)

    assert shown
    assert destroyed == []


def test_request_close_runs_encrypted_backup_before_destroy(monkeypatch, tmp_path):
    tasks = []
    calls = []
    password = exchange_phrase("c")
    service = SimpleNamespace(
        create_verified=lambda target, password=None: calls.append(
            ("create", Path(target), password)
        )
        or backup_artifact(target)
    )
    fake = SimpleNamespace(
        settings={"backup_on_close": True},
        _background_results=None,
        paths=SimpleNamespace(automatic_backups=tmp_path / "backups"),
        _backup_service=lambda: service,
        _backup_audit_started_at=lambda: "2026-09-27T07:00:00+00:00",
        _record_backup_audit=lambda **kwargs: calls.append(
            ("audit", kwargs)
        )
        or True,
        _run_background_task=capture_runner(tasks),
        _finish_close=lambda **kwargs: calls.append(("close", kwargs)),
        logger=SimpleNamespace(warning=lambda *args: calls.append(("warning", args))),
    )
    fake._start_close_backup = (
        lambda target, password: modern_app.ModernVoucherApp._start_close_backup(
            fake,
            target,
            password,
        )
    )
    monkeypatch.setattr(
        maintenance_ui,
        "ask_backup_options",
        lambda parent, **kwargs: BackupChoice(Path(kwargs["default_directory"]) / "test.vmbk", password),
    )

    modern_app.ModernVoucherApp.request_close(fake)

    assert len(tasks) == 1
    assert tasks[0]["label"] == "Backup di chiusura…"
    result = tasks[0]["worker"]()
    assert calls[0][0] == "create"
    assert calls[0][1].parent == tmp_path / "backups"
    assert calls[0][1].suffix == ".vmbk"
    assert calls[0][2] == password
    tasks[0]["success"](result)
    assert calls[-1] == (
        "close",
        {"close_status": "CLOSED", "backup_status": "SUCCESS"},
    )


def test_shutdown_backup_audit_failure_is_visible_but_does_not_trap_close(
    monkeypatch,
    tmp_path,
):
    tasks = []
    events = []
    password = exchange_phrase("a")
    fake = SimpleNamespace(
        settings={"backup_on_close": True},
        _background_results=None,
        paths=SimpleNamespace(automatic_backups=tmp_path / "backups"),
        _backup_service=lambda: SimpleNamespace(
            create_verified=lambda target, password=None: backup_artifact(target)
        ),
        _backup_audit_started_at=lambda: "2026-09-27T07:00:00+00:00",
        _record_backup_audit=lambda **kwargs: False,
        _run_background_task=capture_runner(tasks),
        _finish_close=lambda **kwargs: events.append(("close", kwargs)),
        logger=SimpleNamespace(warning=lambda *args: None),
    )
    fake._start_close_backup = (
        lambda target, password: modern_app.ModernVoucherApp._start_close_backup(
            fake,
            target,
            password,
        )
    )
    monkeypatch.setattr(
        maintenance_ui,
        "ask_backup_options",
        lambda parent, **kwargs: BackupChoice(Path(kwargs["default_directory"]) / "test.vmbk", password),
    )
    monkeypatch.setattr(
        maintenance_ui.messagebox,
        "showwarning",
        lambda *args, **kwargs: events.append(("warning", args)),
    )

    modern_app.ModernVoucherApp.request_close(fake)
    result = tasks[0]["worker"]()
    tasks[0]["success"](result)

    assert events[0][0] == "warning"
    assert events[-1] == (
        "close",
        {
            "close_status": "CLOSED",
            "backup_status": "SUCCESS_AUDIT_FAILED",
        },
    )


def test_failed_close_backup_can_retry_without_losing_password(
    monkeypatch,
    tmp_path,
):
    tasks = []
    password = exchange_phrase("r")
    fake = SimpleNamespace(
        settings={"backup_on_close": True},
        _background_results=None,
        paths=SimpleNamespace(automatic_backups=tmp_path / "backups"),
        _backup_service=lambda: SimpleNamespace(
            create_verified=lambda *args, **kwargs: backup_artifact(args[0])
        ),
        _backup_audit_started_at=lambda: "2026-09-27T07:00:00+00:00",
        _record_backup_audit=lambda **kwargs: True,
        _run_background_task=capture_runner(tasks),
        _finish_close=lambda **kwargs: None,
        logger=SimpleNamespace(warning=lambda *args: None),
    )
    fake._start_close_backup = (
        lambda target, password: modern_app.ModernVoucherApp._start_close_backup(
            fake,
            target,
            password,
        )
    )
    monkeypatch.setattr(
        maintenance_ui,
        "ask_backup_options",
        lambda parent, **kwargs: BackupChoice(Path(kwargs["default_directory"]) / "test.vmbk", password),
    )
    monkeypatch.setattr(
        maintenance_ui.messagebox,
        "askyesnocancel",
        lambda *args, **kwargs: True,
    )

    modern_app.ModernVoucherApp.request_close(fake)
    first_worker = tasks[0]["worker"]
    tasks[0]["error"](maintenance_ui.BackupError("synthetic failure"))

    assert len(tasks) == 2
    # Both attempts retain the same already-validated in-memory password.
    assert first_worker.__closure__ is not None
    tasks[1]["worker"]()


def test_failed_close_backup_can_close_anyway(monkeypatch, tmp_path):
    tasks = []
    closed = []
    fake = SimpleNamespace(
        settings={"backup_on_close": True},
        _background_results=None,
        paths=SimpleNamespace(automatic_backups=tmp_path / "backups"),
        _backup_service=lambda: SimpleNamespace(
            create_verified=lambda *args, **kwargs: backup_artifact(args[0])
        ),
        _backup_audit_started_at=lambda: "2026-09-27T07:00:00+00:00",
        _record_backup_audit=lambda **kwargs: True,
        _run_background_task=capture_runner(tasks),
        _finish_close=lambda **kwargs: closed.append(kwargs),
        logger=SimpleNamespace(warning=lambda *args: None),
    )
    fake._start_close_backup = (
        lambda target, password: modern_app.ModernVoucherApp._start_close_backup(
            fake,
            target,
            password,
        )
    )
    monkeypatch.setattr(
        maintenance_ui,
        "ask_backup_options",
        lambda parent, **kwargs: BackupChoice(Path(kwargs["default_directory"]) / "test.vmbk", exchange_phrase("x")),
    )
    monkeypatch.setattr(
        maintenance_ui.messagebox,
        "askyesnocancel",
        lambda *args, **kwargs: False,
    )

    modern_app.ModernVoucherApp.request_close(fake)
    tasks[0]["error"](maintenance_ui.BackupError("synthetic failure"))

    assert closed == [
        {
            "close_status": "CLOSED_WITHOUT_BACKUP",
            "backup_status": "FAILED",
        }
    ]


def test_cancelling_close_backup_notifies_modal_caller(
    monkeypatch,
    tmp_path,
):
    aborted = []
    fake = SimpleNamespace(
        settings={"backup_on_close": True},
        _background_results=None,
        paths=SimpleNamespace(automatic_backups=tmp_path / "backups"),
    )
    monkeypatch.setattr(
        maintenance_ui,
        "ask_backup_options",
        lambda parent, **kwargs: None,
    )

    modern_app.ModernVoucherApp.request_close(
        fake,
        on_abort=lambda: aborted.append(True),
    )

    assert aborted == [True]


def test_cancelling_close_backup_password_keeps_application_open(
    monkeypatch,
    tmp_path,
):
    tasks = []
    destroyed = []
    fake = SimpleNamespace(
        settings={"backup_on_close": True},
        _background_results=None,
        paths=SimpleNamespace(automatic_backups=tmp_path / "backups"),
        _run_background_task=capture_runner(tasks),
        destroy=lambda: destroyed.append(True),
    )
    monkeypatch.setattr(
        maintenance_ui,
        "ask_backup_options",
        lambda parent, **kwargs: None,
    )

    modern_app.ModernVoucherApp.request_close(fake)

    assert tasks == []
    assert destroyed == []



def test_finish_close_records_session_before_destroy():
    events = []

    class SessionDatabase:
        def close_application_session(self, **kwargs):
            events.append(("audit", kwargs))

    fake = SimpleNamespace(
        database=SessionDatabase(),
        session_uuid="session-1",
        active_controller_id=7,
        logger=SimpleNamespace(warning=lambda *args: events.append(("warning", args))),
        destroy=lambda: events.append(("destroy", None)),
    )

    modern_app.ModernVoucherApp._finish_close(
        fake,
        close_status="CLOSED",
        backup_status="SUCCESS",
    )

    assert events[0][0] == "audit"
    assert events[0][1]["session_uuid"] == "session-1"
    assert events[0][1]["controller_id"] == 7
    assert events[0][1]["close_status"] == "CLOSED"
    assert events[0][1]["backup_status"] == "SUCCESS"
    assert events[-1] == ("destroy", None)



def test_backup_audit_writer_uses_basename_and_verified_metadata():
    calls = []

    class AuditDatabase:
        def record_backup_history(self, **kwargs):
            calls.append(kwargs)

    fake = SimpleNamespace(
        database=AuditDatabase(),
        logger=SimpleNamespace(warning=lambda *args: None),
    )

    result = modern_app.ModernVoucherApp._record_backup_audit(
        fake,
        started_at="2026-09-27T07:00:00+00:00",
        destination="MANUAL",
        target=Path("C:/Temp/audit-backup.vmbk"),
        artifact=backup_artifact("C:/Temp/audit-backup.vmbk"),
    )

    assert result is True
    assert calls[0]["filename"] == "audit-backup.vmbk"
    assert calls[0]["destination"] == "MANUAL"
    assert calls[0]["status"] == "SUCCESS"
    assert calls[0]["sha256"] == "a" * 64
    assert calls[0]["error_summary"] is None
