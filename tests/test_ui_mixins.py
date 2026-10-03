from __future__ import annotations

from types import SimpleNamespace

import pytest

from voucher_management.app import VoucherApp
from voucher_management.database import PrintAuditSummary
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


def test_uat_golden_baseline_keeps_all_operator_workflow_entrypoints():
    required = (
        "connect",
        "refresh",
        "create",
        "print_selected",
        "open_existing_pdf",
        "delete_selected",
        "edit_selected_nominality",
        "edit_selected_notes",
        "align_pending_vouchers",
        "open_operational_alerts",
        "open_security_revocation",
        # Privacy-minimization RetentionMixin is intentionally not composed
        # into ModernVoucherApp in 5.1. The approved operational retention
        # workflows are the two methods above.
        "recover_pending_print_audit",
        "export_history_exchange",
        "import_history_exchange",
        "import_legacy_backup",
        "migrate_legacy_history",
        "create_backup",
        "restore_backup",
        "request_close",
    )

    missing = [
        name
        for name in required
        if not callable(getattr(ModernVoucherApp, name, None))
    ]

    assert missing == []


def test_unknown_print_state_requires_explicit_operator_confirmation(monkeypatch):
    from voucher_management import app as app_module

    prompts = []
    fake = SimpleNamespace(
        active_controller_id=7,
        database=SimpleNamespace(
            print_summaries_for_remote_ids=lambda **kwargs: {
                "voucher-1": PrintAuditSummary(
                    0,
                    0,
                    "",
                    "",
                    print_state="UNKNOWN",
                )
            },
            usage_state_for_remote_ids=lambda **kwargs: {
                "voucher-1": False
            },
        ),
        logger=SimpleNamespace(warning=lambda *args, **kwargs: None),
    )
    monkeypatch.setattr(
        app_module.messagebox,
        "askyesno",
        lambda *args, **kwargs: (
            prompts.append((args, kwargs)) or True
        ),
    )

    allowed = VoucherApp._confirm_physical_reprint(
        fake,
        ["11111-22222"],
        object(),
        unifi_ids=["voucher-1"],
    )

    assert allowed is True
    assert len(prompts) == 1
    assert prompts[0][0][0] == "Stampa non determinabile"
    assert "fuori da Voucher Management" in prompts[0][0][1]
    assert "stampa verificata" in prompts[0][0][1]


def test_unknown_print_state_can_be_cancelled_without_printing(monkeypatch):
    from voucher_management import app as app_module

    fake = SimpleNamespace(
        active_controller_id=7,
        database=SimpleNamespace(
            print_summaries_for_remote_ids=lambda **kwargs: {
                "voucher-1": PrintAuditSummary(
                    0,
                    0,
                    "",
                    "",
                    print_state="UNKNOWN",
                )
            },
            usage_state_for_remote_ids=lambda **kwargs: {
                "voucher-1": False
            },
        ),
        logger=SimpleNamespace(warning=lambda *args, **kwargs: None),
    )
    monkeypatch.setattr(
        app_module.messagebox,
        "askyesno",
        lambda *args, **kwargs: False,
    )

    assert VoucherApp._confirm_physical_reprint(
        fake,
        ["11111-22222"],
        object(),
        unifi_ids=["voucher-1"],
    ) is False


def test_positive_not_printed_state_does_not_add_unknown_warning(monkeypatch):
    from voucher_management import app as app_module

    fake = SimpleNamespace(
        active_controller_id=7,
        database=SimpleNamespace(
            print_summaries_for_remote_ids=lambda **kwargs: {
                "voucher-1": PrintAuditSummary(
                    0,
                    0,
                    "",
                    "",
                    print_state="NOT_PRINTED",
                )
            },
            usage_state_for_remote_ids=lambda **kwargs: {
                "voucher-1": False
            },
        ),
        logger=SimpleNamespace(warning=lambda *args, **kwargs: None),
    )
    monkeypatch.setattr(
        app_module.messagebox,
        "askyesno",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("unexpected unknown-print prompt")
        ),
    )

    assert VoucherApp._confirm_physical_reprint(
        fake,
        ["11111-22222"],
        object(),
        unifi_ids=["voucher-1"],
    ) is True


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
    fake._request_delete_vouchers = (
        lambda current: VoucherDeletionMixin._request_delete_vouchers(
            fake,
            current,
        )
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


def test_ordinary_delete_rechecks_live_usage_after_operator_confirmation(monkeypatch):
    from voucher_management import voucher_deletion_ui as deletion_ui

    client = object()
    initial = [
        SimpleNamespace(
            id="voucher-1",
            code_formatted="11111-22222",
            used=0,
            status="VALID_ONE",
        )
    ]
    became_used = [
        SimpleNamespace(
            id="voucher-1",
            code_formatted="11111-22222",
            used=1,
            status="USED_MULTIPLE",
        )
    ]
    tasks = []
    mutations = []
    fact = SimpleNamespace(
        print_state="NOT_PRINTED",
        alignment_completed=True,
        invalid_external_cleanup_allowed=False,
    )
    fake = SimpleNamespace(
        active_controller_id=7,
        database=SimpleNamespace(
            historically_used_remote_ids=lambda **kwargs: frozenset(),
        ),
        paths=SimpleNamespace(database="test.sqlite"),
        vouchers=list(initial),
        checked_ids=set(),
        logger=SimpleNamespace(warning=lambda *args, **kwargs: None),
        _history_stats_for=lambda current: {},
        _windows_operator_identity=lambda: r"PC\\operator",
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

    monkeypatch.setattr(
        deletion_ui,
        "preparation_delete_facts",
        lambda *args, **kwargs: {"voucher-1": fact},
    )
    monkeypatch.setattr(
        deletion_ui.simpledialog,
        "askstring",
        lambda *args, **kwargs: "Errore di preparazione",
    )
    monkeypatch.setattr(
        deletion_ui.messagebox,
        "askyesno",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        deletion_ui,
        "refresh_delete_candidates",
        lambda current_client, current: list(became_used),
    )
    monkeypatch.setattr(
        deletion_ui,
        "record_preparation_delete_requests_to_path",
        lambda *args, **kwargs: mutations.append("audit"),
    )
    monkeypatch.setattr(
        deletion_ui,
        "delete_vouchers_and_refresh",
        lambda *args, **kwargs: mutations.append("delete"),
    )

    VoucherDeletionMixin._continue_delete_selected(fake, client, initial)

    assert len(tasks) == 1
    assert tasks[0]["label"] == "Eliminazione voucher…"
    with pytest.raises(RuntimeError, match="no longer satisfies"):
        tasks[0]["worker"]()
    assert mutations == []


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
        _CONTROLLER_RETRY_DELAYS_MS=ModernVoucherApp._CONTROLLER_RETRY_DELAYS_MS,
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


def test_successful_voucher_operation_clears_selection_and_refreshes_every_projection():
    calls = []
    fake = SimpleNamespace(
        checked_ids={"one", "two"},
        populate=lambda: calls.append("populate"),
        _refresh_threshold_summary=lambda: calls.append("thresholds"),
        _refresh_report_summary=lambda: calls.append("report"),
        logger=SimpleNamespace(warning=lambda *args, **kwargs: None),
    )

    result = VoucherApp._finalize_voucher_operation_ui(
        fake,
        operation="alignment",
    )

    assert result is True
    assert fake.checked_ids == set()
    assert calls == ["populate", "thresholds", "report"]


def test_threshold_only_operation_skips_report_rebuild():
    calls = []
    fake = SimpleNamespace(
        checked_ids={"one"},
        populate=lambda: calls.append("populate"),
        _refresh_threshold_summary=lambda: calls.append("thresholds"),
        _refresh_report_summary=lambda: calls.append("report"),
        logger=SimpleNamespace(warning=lambda *args, **kwargs: None),
    )

    result = VoucherApp._finalize_voucher_operation_ui(
        fake,
        operation="threshold_update",
        refresh_reports=False,
    )

    assert result is True
    assert fake.checked_ids == set()
    assert calls == ["populate", "thresholds"]


def test_successful_voucher_operation_keeps_selection_cleared_if_refresh_fails(
    monkeypatch,
):
    warnings = []
    sync_calls = []
    fake = SimpleNamespace(
        checked_ids={"one"},
        populate=lambda: (_ for _ in ()).throw(RuntimeError("layout")),
        _refresh_report_summary=lambda: None,
        _sync_selection_ui=lambda: sync_calls.append("selection"),
        logger=SimpleNamespace(warning=lambda *args, **kwargs: None),
    )
    monkeypatch.setattr(
        "voucher_management.app.messagebox.showwarning",
        lambda *args, **kwargs: warnings.append((args, kwargs)),
    )

    result = VoucherApp._finalize_voucher_operation_ui(
        fake,
        operation="nominality",
    )

    assert result is False
    assert fake.checked_ids == set()
    assert sync_calls == ["selection"]
    assert len(warnings) == 1


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
