from __future__ import annotations

from types import SimpleNamespace

from voucher_management.app import VoucherApp
from voucher_management.controller_connection_ui import ControllerConnectionMixin
from voucher_management.data_maintenance_ui import DataMaintenanceMixin
from voucher_management.modern_app import ModernVoucherApp
from voucher_management.voucher_creation_ui import VoucherCreationMixin
from voucher_management.voucher_deletion_ui import VoucherDeletionMixin


def test_ui_workflows_are_composed_from_focused_mixins():
    assert issubclass(VoucherApp, VoucherCreationMixin)
    assert issubclass(ModernVoucherApp, DataMaintenanceMixin)
    assert issubclass(ModernVoucherApp, ControllerConnectionMixin)
    assert issubclass(ModernVoucherApp, VoucherDeletionMixin)

    assert VoucherApp.create is VoucherCreationMixin.create
    assert ModernVoucherApp.create_backup is DataMaintenanceMixin.create_backup
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
