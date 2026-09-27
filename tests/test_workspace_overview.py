from voucher_management.database import Database
from voucher_management.workspace_overview import load_recent_workspace_activity


def _database(tmp_path):
    database = Database(tmp_path / "voucher_management.db")
    database.initialize()
    return database


def test_recent_workspace_activity_is_empty_for_new_database(tmp_path):
    database = _database(tmp_path)
    try:
        assert load_recent_workspace_activity(
            database,
            controller_id=None,
        ) == ()
    finally:
        database.close()


def test_recent_workspace_activity_combines_durable_print_and_voucher_facts(
    tmp_path,
):
    database = _database(tmp_path)
    try:
        controller_id = database.create_controller(
            name="Reception",
            api_root="https://controller.example/proxy/network/integration/v1",
            created_at="2026-09-27T08:00:00+00:00",
        )
        database.upsert_voucher(
            controller_id=controller_id,
            unifi_id="remote-1",
            code="1234567890",
            name="Camera 101",
            imported_at="2026-09-27T08:05:00+00:00",
            last_synced_at="2026-09-27T08:05:00+00:00",
        )
        database.record_print_audit(
            controller_id=controller_id,
            audit_id="audit-1",
            codes=["12345-67890"],
            output_file="voucher.pdf",
            document_copies=1,
            printed_at="2026-09-27T09:00:00+00:00",
            windows_user="TEST\\operator",
        )

        rows = load_recent_workspace_activity(
            database,
            controller_id=controller_id,
            limit=5,
        )

        assert rows[0].title == "Voucher stampato"
        assert rows[0].detail == "Camera 101"
        assert any(row.title == "Voucher rilevato" for row in rows)
    finally:
        database.close()


def test_recent_workspace_activity_respects_controller_scope(tmp_path):
    database = _database(tmp_path)
    try:
        first = database.create_controller(
            name="Reception",
            api_root="https://one.example/proxy/network/integration/v1",
            created_at="2026-09-27T08:00:00+00:00",
        )
        second = database.create_controller(
            name="Sala",
            api_root="https://two.example/proxy/network/integration/v1",
            created_at="2026-09-27T08:00:00+00:00",
        )
        for controller_id, remote_id, code, name in (
            (first, "one", "1111122222", "Reception"),
            (second, "two", "3333344444", "Sala"),
        ):
            database.upsert_voucher(
                controller_id=controller_id,
                unifi_id=remote_id,
                code=code,
                name=name,
                imported_at="2026-09-27T08:05:00+00:00",
                last_synced_at="2026-09-27T08:05:00+00:00",
            )

        rows = load_recent_workspace_activity(
            database,
            controller_id=first,
            limit=10,
        )

        assert rows
        assert all("Sala" not in row.detail for row in rows)
    finally:
        database.close()
