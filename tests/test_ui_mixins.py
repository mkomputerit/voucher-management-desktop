from __future__ import annotations

from types import SimpleNamespace

from voucher_management.app import VoucherApp
from voucher_management.controller_connection_ui import ControllerConnectionMixin
from voucher_management.data_maintenance_ui import DataMaintenanceMixin
from voucher_management.modern_app import (
    ModernVoucherApp,
    _controller_status_style_names,
    _main_window_minimum,
    _sidebar_icon_pixel_size,
)
from voucher_management.retention_ui import RetentionMixin
from voucher_management.voucher_creation_ui import VoucherCreationMixin
from voucher_management.voucher_deletion_ui import VoucherDeletionMixin


def test_ui_workflows_are_composed_from_focused_mixins():
    assert issubclass(VoucherApp, VoucherCreationMixin)
    assert issubclass(ModernVoucherApp, DataMaintenanceMixin)
    assert issubclass(ModernVoucherApp, RetentionMixin)
    assert issubclass(ModernVoucherApp, ControllerConnectionMixin)
    assert issubclass(ModernVoucherApp, VoucherDeletionMixin)

    assert VoucherApp.create is VoucherCreationMixin.create
    assert ModernVoucherApp.create_backup is DataMaintenanceMixin.create_backup
    assert (
        ModernVoucherApp.open_retention_review
        is RetentionMixin.open_retention_review
    )
    assert ModernVoucherApp.request_close is DataMaintenanceMixin.request_close
    assert (
        ModernVoucherApp.recover_pending_print_audit
        is DataMaintenanceMixin.recover_pending_print_audit
    )
    assert ModernVoucherApp.connect is ControllerConnectionMixin.connect
    assert ModernVoucherApp.delete_selected is VoucherDeletionMixin.delete_selected


def test_delete_ui_revalidation_is_deferred_to_network_worker(monkeypatch):
    from voucher_management import voucher_deletion_ui as deletion_ui

    tasks = []
    selected = [SimpleNamespace(id="voucher-1")]
    client = object()
    fake = SimpleNamespace(
        client=client,
        selected=lambda: selected,
        _continue_delete_selected=lambda *args: None,
        _show_network_error=lambda *args, **kwargs: None,
        _run_network_task=lambda label, worker, success, error: (
            tasks.append(
                {
                    "label": label,
                    "worker": worker,
                    "success": success,
                    "error": error,
                }
            )
            or True
        ),
    )
    calls = []
    monkeypatch.setattr(
        deletion_ui,
        "refresh_delete_candidates",
        lambda current_client, current_selected: (
            calls.append((current_client, current_selected)) or ("fresh",)
        ),
    )

    VoucherDeletionMixin.delete_selected(fake)

    assert calls == []
    assert len(tasks) == 1
    assert tasks[0]["label"] == "Verifica stato voucher…"

    result = tasks[0]["worker"]()
    assert result == ("fresh",)
    assert calls == [(client, selected)]


def test_connection_and_maintenance_mixins_do_not_define_main_window_layout():
    assert "_build_ui" not in ControllerConnectionMixin.__dict__
    assert "_build_ui" not in DataMaintenanceMixin.__dict__
    assert "_build_ui" not in VoucherDeletionMixin.__dict__
    assert "_build_ui" not in VoucherCreationMixin.__dict__


def test_restore_closes_live_database_before_replacement():
    calls = []

    class Database:
        def close(self):
            calls.append("close")

    fake = SimpleNamespace(database=Database())

    DataMaintenanceMixin._close_database_for_restore(fake)

    assert calls == ["close"]
    assert fake.database is None


def test_failed_restore_reopens_and_verifies_database(tmp_path):
    fake = SimpleNamespace(
        database=None,
        paths=SimpleNamespace(
            database=tmp_path / "voucher_management.db",
        ),
    )

    DataMaintenanceMixin._reopen_database_after_failed_restore(fake)

    try:
        assert fake.database is not None
        fake.database.integrity_check()
    finally:
        fake.database.close()


def test_controller_status_styles_keep_warning_states_distinct_from_errors():
    local = _controller_status_style_names("local")
    unconfigured = _controller_status_style_names("unconfigured")
    error = _controller_status_style_names("error")

    assert local[0] == "Warning.Status.TLabel"
    assert local[2] == "WarningDot.TLabel"
    assert unconfigured[2] == "WarningDot.TLabel"
    assert error[0] == "Error.Status.TLabel"
    assert error[2] == "DisconnectedDot.TLabel"


def test_sidebar_icon_size_tracks_windows_tk_scaling():
    assert _sidebar_icon_pixel_size(96 / 72) == 20
    assert _sidebar_icon_pixel_size((96 / 72) * 1.25) == 25
    assert _sidebar_icon_pixel_size((96 / 72) * 1.5) == 30
    assert _sidebar_icon_pixel_size("invalid") == 20


def test_main_window_minimum_stays_inside_short_display():
    assert _main_window_minimum(1600, 755) == (1220, 655)
    assert _main_window_minimum(1093, 614) == (1013, 514)
