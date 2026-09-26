"""Integration regression for the create mutation guard refresh path."""

from types import SimpleNamespace

from voucher_management import app as app_module
from voucher_management.app import VoucherApp
from voucher_management.mutation_guard import CreateMutationGuardError


def test_refresh_guard_clear_failure_warns_and_keeps_ui_usable(monkeypatch):
    warnings = []
    logs = []
    populated = []

    class Guard:
        def clear(self):
            raise CreateMutationGuardError("synthetic clear failure")

    def run_network_task(label, worker, success, error):
        assert label == "Aggiornamento voucher…"
        success([SimpleNamespace(id="voucher-1")])
        return True

    fake = SimpleNamespace(
        client=object(),
        active_controller_id=None,
        create_guard=Guard(),
        logger=SimpleNamespace(
            warning=lambda *args, **kwargs: logs.append((args, kwargs))
        ),
        populate=lambda: populated.append(True),
        _show_network_error=lambda *args, **kwargs: None,
        _run_network_task=run_network_task,
    )

    monkeypatch.setattr(
        app_module.messagebox,
        "showwarning",
        lambda title, message, parent=None: warnings.append(
            (title, message, parent)
        ),
    )

    VoucherApp.refresh(fake)

    assert [voucher.id for voucher in fake.vouchers] == ["voucher-1"]
    assert populated == [True]
    assert logs
    assert logs[0][0][0] == "create_guard_clear_failed type=%s"
    assert logs[0][0][1] == "CreateMutationGuardError"
    assert len(warnings) == 1
    title, message, parent = warnings[0]
    assert title == "Creazione ancora sospesa"
    assert "creazione resta sospesa per sicurezza" in message
    assert parent is fake
