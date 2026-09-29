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
    fake = SimpleNamespace(
        _background_results=None, client=object() if connected else None,
        _controller_status_failed=failed,
        settings_store=SimpleNamespace(load=lambda: {"controller_api_root": "https://controller.example/api"}),
        api_root_var=Var("https://unsaved.example/api"),
        _controller_record=lambda: {"name": "Reception", "last_successful_sync_at": ""},
        _show_workspace=lambda key: pytest.fail("reconnect must not navigate away"),
    )
    monkeypatch.setattr(modern_app, "QuickConnectDialog", lambda app, **kwargs: calls.append(kwargs))
    ModernVoucherApp._home_sync_or_connect(fake)
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


def test_home_hides_local_snapshot_metrics_until_controller_snapshot_is_fresh(root):
    frame = ttk.Frame(root)
    frame.pack(fill="both", expand=True)
    fake = SimpleNamespace()
    for name in (
        "home_controller_name", "home_last_sync", "home_ready", "home_sync_action",
        "home_backup_summary", "home_to_print", "home_active", "home_used",
        "home_expired", "home_print_action",
    ):
        setattr(fake, name + "_var", tk.StringVar(root, value="0"))
    for name in (
        "_home_sync_or_connect", "create", "create_backup", "_home_print_selected",
        "_on_home_recent_click", "_on_voucher_selection_key",
    ):
        setattr(fake, name, lambda *args: None)
    fake._build_status_dot = lambda parent: tk.Canvas(parent, width=14, height=14)
    fake._toggle_home_activity = lambda: ModernVoucherApp._toggle_home_activity(fake)
    ModernVoucherApp._build_home_workspace(fake, frame)

    fake.vouchers = [
        SimpleNamespace(
            id="local-1",
            code_formatted="11111-22222",
            recipient="Local snapshot",
            create_time=1,
            end_time=0,
            used=0,
        )
    ]
    fake.controller_snapshot_fresh = False
    fake.checked_ids = set()
    fake._is_expired = lambda voucher: False
    fake._print_state = lambda stat: "DA STAMPARE"
    fake._refresh_home_activity = lambda: None
    fake._refresh_controller_workspace_status = lambda: None

    ModernVoucherApp._update_operator_summary(fake, {})
    root.update()

    assert fake.home_to_print_var.get() == "—"
    assert fake.home_active_var.get() == "—"
    assert fake.home_used_var.get() == "—"
    assert fake.home_expired_var.get() == "—"
    assert fake.home_recent_tree.get_children() == ()
    frame.destroy()


def test_home_displays_ten_recent_vouchers_and_collapses_activity(root):
    root.geometry("1100x760")
    frame = ttk.Frame(root)
    frame.pack(fill="both", expand=True)
    fake = SimpleNamespace()
    for name in (
        "home_controller_name", "home_last_sync", "home_ready", "home_sync_action",
        "home_backup_summary", "home_to_print", "home_active", "home_used", "home_expired", "home_print_action",
    ):
        setattr(fake, name + "_var", tk.StringVar(root, value="0"))
    for name in ("_home_sync_or_connect", "create", "create_backup", "_home_print_selected",
                 "_on_home_recent_click", "_on_voucher_selection_key"):
        setattr(fake, name, lambda *args: None)
    fake._build_status_dot = lambda parent: tk.Canvas(parent, width=14, height=14)
    fake._toggle_home_activity = lambda: ModernVoucherApp._toggle_home_activity(fake)
    ModernVoucherApp._build_home_workspace(fake, frame)
    fake.vouchers = [SimpleNamespace(
        id=str(i), code_formatted=f"{i:05d}-00000", recipient="Guest", create_time=i,
        end_time=0, used=0,
    ) for i in range(12)]
    fake.controller_snapshot_fresh = True
    fake.checked_ids = set()
    fake._is_expired = lambda voucher: False
    fake._print_state = lambda stat: "DA STAMPARE"
    fake._refresh_home_activity = lambda: None
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
