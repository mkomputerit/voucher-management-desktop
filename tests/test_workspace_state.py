from voucher_management.workspace_state import build_controller_workspace_status


def test_workspace_status_for_fresh_install_is_operator_friendly():
    status = build_controller_workspace_status(
        connected=False,
        configured=False,
    )

    assert status.key == "unconfigured"
    assert status.title == "Controller da configurare"
    assert "API" not in status.detail


def test_workspace_status_distinguishes_local_snapshot_from_live_connection():
    status = build_controller_workspace_status(
        connected=False,
        configured=True,
        controller_name="Reception",
        last_successful_sync_at="2026-09-27T18:45:00+00:00",
    )

    assert status.key == "local"
    assert status.title == "Solo dati locali"
    assert "Reception" in status.detail
    assert "27/09/2026" in status.detail


def test_workspace_status_reports_ready_when_connected():
    status = build_controller_workspace_status(
        connected=True,
        configured=True,
        controller_name="Reception",
        last_successful_sync_at="2026-09-27T18:45:00+00:00",
    )

    assert status.key == "connected"
    assert status.title == "Pronto"


def test_workspace_syncing_state_takes_precedence():
    status = build_controller_workspace_status(
        connected=True,
        configured=True,
        controller_name="Reception",
        last_successful_sync_at="2026-09-27T18:45:00+00:00",
        busy_label="Aggiornamento voucher…",
        failed=True,
    )

    assert status.key == "syncing"
    assert status.title == "Sincronizzazione in corso…"


def test_workspace_failure_keeps_last_local_snapshot_visible():
    status = build_controller_workspace_status(
        connected=False,
        configured=True,
        controller_name="Reception",
        last_successful_sync_at="2026-09-27T18:45:00+00:00",
        failed=True,
    )

    assert status.key == "error"
    assert status.title == "Controller non raggiungibile"
    assert "dati locali" in status.detail.lower()
