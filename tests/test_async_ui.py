from __future__ import annotations

from types import SimpleNamespace

from voucher_management import app as app_module
from voucher_management import modern_app
from voucher_management import controller_connection_ui as connection_ui
from voucher_management.app import VoucherApp
from voucher_management.unifi_api import UniFiCertificateTrustRequired


class _Var:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class _Widget:
    def __init__(self):
        self.states = []

    def state(self, value):
        self.states.append(tuple(value))


class _Progress(_Widget):
    def __init__(self):
        super().__init__()
        self.visible = False
        self.started = []
        self.stopped = 0

    def grid(self):
        self.visible = True

    def grid_remove(self):
        self.visible = False

    def start(self, interval):
        self.started.append(interval)

    def stop(self):
        self.stopped += 1


class _Label:
    def __init__(self):
        self.visible = False

    def grid(self):
        self.visible = True

    def grid_remove(self):
        self.visible = False


def test_connect_clears_visible_api_key_and_only_schedules_network(monkeypatch):
    scheduled = []
    failed = []

    class FakeClient:
        def __init__(self, api_root, *, trusted_cert_sha256=None):
            self.base_url = api_root
            self.trusted_cert_sha256 = trusted_cert_sha256 or ""

        def connect(self, _key):
            raise AssertionError("network called from Tk connect()")

    fake = SimpleNamespace(
        _background_results=None,
        api_root_var=_Var("controller.example.invalid"),
        api_key_var=_Var("secret-key"),
        connection_var=_Var(),
        settings={},
        settings_store=SimpleNamespace(
            load=lambda: {
                "controller_api_root": "",
                "controller_cert_sha256": "",
            }
        ),
        bell=lambda: None,
        _connection_failed=lambda exc: failed.append(exc),
        _start_connect_attempt=lambda client, key: scheduled.append(
            (client, key)
        ),
    )
    monkeypatch.setattr(connection_ui, "UniFiClient", FakeClient)

    modern_app.ModernVoucherApp.connect(fake)

    assert not failed
    assert fake.api_key_var.get() == ""
    assert fake.connection_var.get() == "Connessione in corso…"
    assert len(scheduled) == 1
    assert scheduled[0][1] == "secret-key"


def test_connect_worker_contains_network_and_tls_prompt_is_callback_only():
    calls = []
    captured = {}

    class FakeClient:
        base_url = "https://controller.invalid/v1"

        def connect(self, key):
            calls.append(("connect", key))
            return {"applicationVersion": "10.6.106", "siteName": "Default"}

        def list_vouchers(self):
            calls.append(("list", None))
            return ["voucher"]

    fake = SimpleNamespace(
        _run_network_task=lambda label, worker, success, error: captured.update(
            label=label,
            worker=worker,
            success=success,
            error=error,
        ),
        _finish_connection=lambda *args: calls.append(("finish", args)),
        _confirm_changed_certificate=lambda *args: calls.append(
            ("changed-prompt", args)
        ),
        _confirm_untrusted_certificate=lambda *args: calls.append(
            ("trust-prompt", args)
        ),
        _connection_failed=lambda exc: calls.append(("failed", exc)),
    )
    client = FakeClient()

    modern_app.ModernVoucherApp._start_connect_attempt(
        fake,
        client,
        "secret-key",
    )

    assert calls == []
    result = captured["worker"]()
    assert calls == [("connect", "secret-key"), ("list", None)]

    captured["success"](result)
    assert calls[-1][0] == "finish"

    trust = UniFiCertificateTrustRequired("a" * 64)
    captured["error"](trust)
    assert calls[-1][0] == "trust-prompt"


def test_network_busy_state_drives_progress_and_network_buttons():
    widgets = [_Widget() for _ in range(6)]
    progress = _Progress()
    label = _Label()
    fake = SimpleNamespace(
        connect_button=widgets[0],
        create_button=widgets[1],
        refresh_button=widgets[2],
        delete_button=widgets[3],
        print_button=widgets[4],
        open_pdf_button=widgets[5],
        background_operation_var=_Var(),
        background_progress=progress,
        background_operation_label=label,
    )

    modern_app.ModernVoucherApp._set_background_busy(
        fake,
        True,
        "Aggiornamento…",
    )

    assert all(widget.states[-1] == ("disabled",) for widget in widgets)
    assert fake.background_operation_var.get() == "Aggiornamento…"
    assert progress.visible is True
    assert progress.started == [12]
    assert label.visible is True

    modern_app.ModernVoucherApp._set_background_busy(fake, False)

    assert all(widget.states[-1] == ("!disabled",) for widget in widgets)
    assert fake.background_operation_var.get() == ""
    assert progress.visible is False
    assert progress.stopped == 1
    assert label.visible is False


def test_refresh_defers_controller_access_to_worker(monkeypatch):
    captured = {}
    calls = []
    client = SimpleNamespace()
    fake = SimpleNamespace(
        client=client,
        bell=lambda: None,
        vouchers=[],
        create_guard=SimpleNamespace(clear=lambda: False),
        populate=lambda: calls.append(("populate", None)),
        _run_network_task=lambda label, worker, success, error: captured.update(
            label=label,
            worker=worker,
            success=success,
            error=error,
        ),
    )
    monkeypatch.setattr(
        app_module,
        "refresh_vouchers",
        lambda current: calls.append(("network", current)) or ["fresh"],
    )

    VoucherApp.refresh(fake)

    assert captured["label"] == "Aggiornamento voucher…"
    assert calls == []

    result = captured["worker"]()
    assert calls == [("network", client)]

    captured["success"](result)
    assert fake.vouchers == ["fresh"]
    assert calls[-1] == ("populate", None)



def test_unexpected_background_error_is_redacted(monkeypatch):
    shown = []
    logged = []
    fake = SimpleNamespace(
        logger=SimpleNamespace(
            error=lambda *args: logged.append(args)
        ),
    )
    monkeypatch.setattr(
        app_module.messagebox,
        "showerror",
        lambda *args, **kwargs: shown.append((args, kwargs)),
    )

    VoucherApp._show_network_error(
        fake,
        "Sincronizzazione",
        RuntimeError("sensitive synthetic detail"),
    )

    assert logged
    assert shown
    assert "sensitive synthetic detail" not in shown[0][0][1]
    assert "Errore imprevisto" in shown[0][0][1]
