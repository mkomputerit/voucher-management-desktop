from __future__ import annotations

from types import SimpleNamespace

from voucher_management.app import VoucherApp
from voucher_management.unifi_api import UniFiTransportError
from voucher_management.controller_connection_ui import ControllerConnectionMixin
from voucher_management.data_maintenance_ui import DataMaintenanceMixin
from voucher_management.modern_app import (
    ModernVoucherApp,
    _controller_status_color_key,
    _controller_status_style_names,
    _main_window_minimum,
    _sidebar_icon_bitmap,
    _sidebar_icon_pixel_size,
    _theme_display_label,
    _theme_setting_value,
)
from voucher_management.operational_alerts_ui import OperationalAlertsMixin
from voucher_management.security_revocation_ui import SecurityRevocationMixin
from voucher_management.voucher_creation_ui import VoucherCreationMixin
from voucher_management.voucher_deletion_ui import VoucherDeletionMixin


def test_ui_workflows_are_composed_from_focused_mixins():
    assert issubclass(VoucherApp, VoucherCreationMixin)
    assert issubclass(ModernVoucherApp, DataMaintenanceMixin)
    assert issubclass(ModernVoucherApp, OperationalAlertsMixin)
    assert issubclass(ModernVoucherApp, SecurityRevocationMixin)
    assert issubclass(ModernVoucherApp, ControllerConnectionMixin)
    assert issubclass(ModernVoucherApp, VoucherDeletionMixin)

    assert VoucherApp.create is VoucherCreationMixin.create
    assert ModernVoucherApp.create_backup is DataMaintenanceMixin.create_backup
    assert (
        ModernVoucherApp.open_operational_alerts
        is OperationalAlertsMixin.open_operational_alerts
    )
    assert (
        ModernVoucherApp.open_security_revocation
        is SecurityRevocationMixin.open_security_revocation
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
    assert "_build_ui" not in OperationalAlertsMixin.__dict__
    assert "_build_ui" not in SecurityRevocationMixin.__dict__


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


def test_controller_status_color_keys_are_semantically_distinct():
    assert _controller_status_color_key("connected") == "green"
    assert _controller_status_color_key("local") == "orange"
    assert _controller_status_color_key("unconfigured") == "orange"
    assert _controller_status_color_key("error") == "red"
    assert _controller_status_color_key("syncing") == "blue"
    assert _controller_status_color_key("retrying") == "orange"


def test_transport_failure_schedules_finite_auto_retry_before_red():
    scheduled = []
    populated = []
    statuses = []
    errors = []
    fake = SimpleNamespace(
        client=object(),
        controller_snapshot_live=True,
        _controller_retrying=False,
        _controller_retry_attempt=0,
        _controller_retry_after=None,
        _controller_retry_dot_after="existing-animation",
        _controller_retry_dot_phase=False,
        _controller_status_failed=False,
        _controller_status_stale=False,
        logger=SimpleNamespace(
            warning=lambda *args, **kwargs: None,
            info=lambda *args, **kwargs: None,
        ),
        after=lambda delay, callback: (
            scheduled.append((delay, callback))
            or f"after-{len(scheduled)}"
        ),
        after_cancel=lambda handle: None,
        _refresh_controller_workspace_status=lambda: statuses.append(True),
        populate=lambda: populated.append(True),
        _animate_retry_dot=lambda: None,
        _stop_retry_dot_animation=lambda: None,
        _show_network_error=lambda *args, **kwargs: errors.append((args, kwargs)),
        refresh=lambda: None,
    )
    fake._cancel_controller_retry_after = (
        lambda: ModernVoucherApp._cancel_controller_retry_after(fake)
    )
    fake._reset_controller_retry_state = (
        lambda: ModernVoucherApp._reset_controller_retry_state(fake)
    )

    assert ModernVoucherApp._handle_controller_refresh_failure(
        fake,
        UniFiTransportError("offline"),
    ) is True
    assert fake.controller_snapshot_live is False
    assert fake._controller_retrying is True
    assert fake._controller_retry_attempt == 1
    assert scheduled[0][0] == 5_000
    assert errors == []

    fake._controller_retry_attempt = len(
        ModernVoucherApp._CONTROLLER_RETRY_DELAYS_MS
    )
    assert ModernVoucherApp._handle_controller_refresh_failure(
        fake,
        UniFiTransportError("still offline"),
    ) is True
    assert fake._controller_retrying is False
    assert fake._controller_status_failed is True
    assert errors
    assert "tentativi automatici" in errors[-1][1]["prefix"].lower()


def test_non_transport_failure_is_not_auto_retried():
    fake = SimpleNamespace(
        client=object(),
    )
    assert ModernVoucherApp._handle_controller_refresh_failure(
        fake,
        RuntimeError("invalid payload"),
    ) is False


def test_sidebar_icon_size_tracks_windows_tk_scaling():
    assert _sidebar_icon_pixel_size(96 / 72) == 20
    assert _sidebar_icon_pixel_size((96 / 72) * 1.25) == 25
    assert _sidebar_icon_pixel_size((96 / 72) * 1.5) == 30
    assert _sidebar_icon_pixel_size("invalid") == 20


def test_main_window_minimum_stays_inside_short_display():
    assert _main_window_minimum(1600, 755) == (1220, 655)
    assert _main_window_minimum(1093, 614) == (1013, 514)


def test_sidebar_icon_bitmap_is_rendered_at_requested_dpi_size():
    assert _sidebar_icon_bitmap("home", "#ffffff", 20).size == (20, 20)
    assert _sidebar_icon_bitmap("voucher", "#ffffff", 30).size == (30, 30)
    assert _sidebar_icon_bitmap("report", "#ffffff", 32).size == (32, 32)


def test_theme_labels_keep_operator_text_separate_from_persisted_values():
    assert _theme_display_label("system") == "Segui Windows"
    assert _theme_display_label("light") == "Chiaro"
    assert _theme_display_label("dark") == "Scuro"
    assert _theme_setting_value("Segui Windows") == "system"
    assert _theme_setting_value("Chiaro") == "light"
    assert _theme_setting_value("Scuro") == "dark"
    assert _theme_setting_value("unknown") == "system"


def test_sidebar_icons_refresh_only_when_tk_scaling_changes():
    calls = []

    class TkProxy:
        def __init__(self, scaling):
            self.scaling = scaling

        def call(self, *_args):
            return self.scaling

    fake = SimpleNamespace(
        _display_scale_after="pending",
        _last_sidebar_icon_size=20,
        tk=TkProxy((96 / 72) * 1.5),
        _refresh_sidebar_icons=lambda: calls.append("refresh"),
    )
    ModernVoucherApp._refresh_sidebar_icons_if_scale_changed(fake)
    assert fake._display_scale_after is None
    assert calls == ["refresh"]

    calls.clear()
    fake._last_sidebar_icon_size = 30
    ModernVoucherApp._refresh_sidebar_icons_if_scale_changed(fake)
    assert calls == []
