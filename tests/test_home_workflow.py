"""Home reconnect and batch layout regression coverage."""
from types import SimpleNamespace
import sys
import tkinter as tk
from tkinter import ttk

import pytest

from voucher_management import modern_app
from voucher_management.modern_app import ModernVoucherApp, QuickConnectDialog


class Var:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


@pytest.mark.parametrize("connected,failed", [(False, False), (True, True)])
def test_reconnect_asks_only_for_key_at_saved_endpoint(monkeypatch, connected, failed):
    calls = []
    refreshed = []
    reset = []
    fake = SimpleNamespace(
        _background_results=None, client=object() if connected else None,
        _controller_status_failed=failed,
        _controller_retrying=False,
        _reset_controller_retry_state=lambda: reset.append(True),
        refresh=lambda: refreshed.append(True),
        settings_store=SimpleNamespace(load=lambda: {"controller_api_root": "https://controller.example/api"}),
        api_root_var=Var("https://unsaved.example/api"),
        _controller_record=lambda: {"name": "Reception", "last_successful_sync_at": ""},
        _show_workspace=lambda key: pytest.fail("reconnect must not navigate away"),
    )
    monkeypatch.setattr(modern_app, "QuickConnectDialog", lambda app, **kwargs: calls.append(kwargs))
    ModernVoucherApp._home_sync_or_connect(fake)
    if connected:
        assert calls == []
        assert refreshed == [True]
        assert reset == [True]
    else:
        assert len(calls) == 1
        assert calls[0]["api_root"] == "https://controller.example/api"
        assert calls[0]["controller_name"] == "Reception"


def test_active_session_refreshes_without_prompting_for_key():
    calls = []
    fake = SimpleNamespace(
        _background_results=None, client=object(), _controller_status_failed=False,
        refresh=lambda: calls.append("refresh"),
    )
    ModernVoucherApp._home_sync_or_connect(fake)
    assert calls == ["refresh"]


def test_unconfigured_controller_opens_address_configuration():
    calls = []
    fake = SimpleNamespace(
        _background_results=None, client=None, _controller_status_failed=False,
        settings_store=SimpleNamespace(load=lambda: {}),
        _show_workspace=lambda key: calls.append(key),
        settings_notebook=SimpleNamespace(select=lambda index: calls.append(index)),
        api_root_entry=SimpleNamespace(focus_set=lambda: calls.append("address")),
    )
    ModernVoucherApp._home_sync_or_connect(fake)
    assert calls == ["settings", 1, "address"]


def test_busy_sync_does_not_open_dialog_or_start_second_operation():
    calls = []
    fake = SimpleNamespace(_background_results=object(), bell=lambda: calls.append("bell"))
    ModernVoucherApp._home_sync_or_connect(fake)
    assert calls == ["bell"]


@pytest.fixture(scope="module")
def root():
    try:
        window = tk.Tk()
    except tk.TclError:
        if sys.platform == "win32":
            raise
        pytest.skip("Tk display unavailable")
    yield window
    window.destroy()


def test_quick_connect_clears_dialog_secret_on_cancel_and_accept(root):
    root.api_root_var = tk.StringVar(root)
    root.api_key_var = tk.StringVar(root)
    root.controller_name_var = tk.StringVar(root)
    root._background_results = None
    calls = []
    def connect():
        calls.append((root.api_root_var.get(), root.api_key_var.get()))
        root.api_key_var.set("")
    root.connect = connect
    kwargs = dict(api_root="https://controller.example/api", controller_name="Reception", last_sync="Mai")
    dialog = QuickConnectDialog(root, **kwargs)
    dialog.key_var.set("synthetic-test-key")
    dialog.destroy()
    assert dialog.key_var.get() == ""
    assert calls == []
    assert root.api_key_var.get() == ""
    dialog = QuickConnectDialog(root, **kwargs)
    dialog.accept()
    assert dialog.winfo_exists()
    assert calls == []
    dialog.key_var.set("synthetic-test-key")
    dialog.accept()
    assert calls == [("https://controller.example/api", "synthetic-test-key")]
    assert dialog.key_var.get() == ""
    assert root.api_key_var.get() == ""


def test_home_displays_ten_recent_vouchers_and_collapses_activity(root):
    root.geometry("1100x760")
    frame = ttk.Frame(root)
    frame.pack(fill="both", expand=True)
    fake = SimpleNamespace()
    for name in (
        "home_controller_name", "home_last_sync", "home_ready", "home_sync_action",
        "home_backup_summary", "home_to_print", "home_active", "home_used", "home_expired",
        "home_unprinted_alert", "home_security_alert", "home_print_action",
    ):
        setattr(fake, name + "_var", tk.StringVar(root, value="0"))
    for name in (
        "_home_sync_or_connect", "create", "create_backup", "_home_print_selected",
        "_on_home_recent_click", "_on_voucher_selection_key",
        "open_operational_alerts", "open_security_revocation",
    ):
        setattr(fake, name, lambda *args: None)
    fake._build_status_dot = lambda parent: tk.Canvas(parent, width=14, height=14)
    fake._toggle_home_activity = lambda: ModernVoucherApp._toggle_home_activity(fake)
    ModernVoucherApp._build_home_workspace(fake, frame)
    fake.vouchers = [SimpleNamespace(
        id=str(i), code_formatted=f"{i:05d}-00000", recipient="Guest", create_time=i,
        end_time=0, used=0,
    ) for i in range(12)]
    fake.checked_ids = set()
    fake.controller_snapshot_live = True
    fake._is_expired = lambda voucher: False
    fake._print_state = lambda stat: "DA STAMPARE"
    fake._workspace_print_state = lambda voucher, stat: "DA STAMPARE"
    fake._voucher_alignment_ready = lambda voucher: True
    fake._refresh_home_activity = lambda: None
    fake._refresh_home_threshold_alerts = lambda: None
    fake._refresh_controller_workspace_status = lambda: None
    ModernVoucherApp._update_operator_summary(fake, {})
    root.update()
    assert int(fake.home_recent_tree.cget("height")) == 10
    assert fake.home_recent_tree.get_children() == tuple(f"home-{i}" for i in range(11, 1, -1))
    assert fake.home_recent_tree.bbox("home-2"), "tenth voucher must be visible"
    assert not fake.home_activity_frame.winfo_ismapped()
    fake._toggle_home_activity()
    root.update()
    assert fake.home_activity_frame.winfo_ismapped()
    fake._toggle_home_activity()
    root.update()
    assert not fake.home_activity_frame.winfo_ismapped()
    assert all(str(widget.cget("text")) != "Aree" for widget in frame.winfo_children() if isinstance(widget, ttk.Labelframe))


def test_home_metrics_do_not_present_local_cache_as_live_controller_state():
    fake = SimpleNamespace(
        home_to_print_var=Var("99"),
        home_active_var=Var("99"),
        home_used_var=Var("99"),
        home_expired_var=Var("99"),
        home_unprinted_alert_var=Var("99"),
        home_security_alert_var=Var("99"),
        controller_snapshot_live=False,
        _refresh_home_activity=lambda: None,
        _refresh_controller_workspace_status=lambda: None,
    )
    ModernVoucherApp._update_operator_summary(fake, {})
    assert fake.home_to_print_var.get() == "—"
    assert fake.home_active_var.get() == "—"
    assert fake.home_used_var.get() == "—"
    assert fake.home_expired_var.get() == "—"
    assert fake.home_unprinted_alert_var.get() == "—"
    assert fake.home_security_alert_var.get() == "—"


def test_home_threshold_alerts_distinguish_unconfigured_and_candidates(monkeypatch):
    fake = SimpleNamespace(
        home_unprinted_alert_var=Var(),
        home_security_alert_var=Var(),
        controller_snapshot_live=True,
        active_controller_id=7,
        database=object(),
        logger=SimpleNamespace(warning=lambda *args, **kwargs: None),
    )
    monkeypatch.setattr(modern_app, "unprinted_warning_days", lambda _db: 10)
    monkeypatch.setattr(modern_app, "security_revoke_days", lambda _db: 30)
    monkeypatch.setattr(
        modern_app,
        "unprinted_warning_candidates",
        lambda _db, **kwargs: (object(), object()),
    )
    monkeypatch.setattr(
        modern_app,
        "security_revocation_candidates",
        lambda _db, **kwargs: (object(),),
    )

    ModernVoucherApp._refresh_home_threshold_alerts(fake)

    assert fake.home_unprinted_alert_var.get() == "2 oltre 10 gg"
    assert fake.home_security_alert_var.get() == "1 da rivedere"

    monkeypatch.setattr(modern_app, "unprinted_warning_days", lambda _db: None)
    monkeypatch.setattr(modern_app, "security_revoke_days", lambda _db: None)
    ModernVoucherApp._refresh_home_threshold_alerts(fake)
    assert fake.home_unprinted_alert_var.get() == "Soglia da configurare"
    assert fake.home_security_alert_var.get() == "Soglia da configurare"


def test_controller_failure_invalidates_live_home_metrics():
    calls = []
    fake = SimpleNamespace(
        _controller_status_failed=False,
        controller_snapshot_live=True,
        _reset_controller_retry_state=lambda: calls.append("reset"),
        _refresh_controller_workspace_status=lambda: calls.append("status"),
        populate=lambda: calls.append("populate"),
    )
    ModernVoucherApp._controller_operation_failed(fake)
    assert fake._controller_status_failed is True
    assert fake.controller_snapshot_live is False
    assert calls == ["reset", "status", "populate"]



def test_workspace_state_requires_alignment_before_voucher_is_printable():
    fake = SimpleNamespace(
        _workspace_print_state_by_unifi_id={
            "external": (False, "UNKNOWN"),
            "known-unprinted": (True, "NOT_PRINTED"),
            "known-printed": (True, "PRINTED"),
            "known-unknown": (True, "UNKNOWN"),
        }
    )
    no_history = None
    assert ModernVoucherApp._workspace_print_state(
        fake,
        SimpleNamespace(id="external"),
        no_history,
    ) == "DA ALLINEARE"
    assert ModernVoucherApp._workspace_print_state(
        fake,
        SimpleNamespace(id="known-unprinted"),
        no_history,
    ) == "DA STAMPARE"
    assert ModernVoucherApp._workspace_print_state(
        fake,
        SimpleNamespace(id="known-printed"),
        no_history,
    ) == "STAMPATO"
    assert ModernVoucherApp._workspace_print_state(
        fake,
        SimpleNamespace(id="known-unknown"),
        no_history,
    ) == "NON DETERMINABILE"
