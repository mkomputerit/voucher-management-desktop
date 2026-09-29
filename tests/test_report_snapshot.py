"""Snapshot-consistency tests for multi-query report construction."""

from voucher_management.database import Database
from voucher_management.reporting import ReportKind, build_report_dataset


NOW = "2026-09-29T18:00:00+00:00"


def _seed(path):
    database = Database(path)
    database.initialize()
    controller_id = database.create_controller(
        name="Reception",
        api_root="https://unifi.example/proxy/network/integration/v1",
        created_at=NOW,
    )
    voucher_id = database.upsert_voucher(
        controller_id=controller_id,
        unifi_id="voucher-1",
        code="1234567890",
        name="Descrizione UniFi",
        imported_at=NOW,
        last_synced_at=NOW,
        created_at="2026-09-29T10:00:00+00:00",
        duration_minutes=1440,
        authorized_guest_limit=1,
        authorized_guest_count=0,
        expires_at="2026-09-30T10:00:00+00:00",
    )
    with database.transaction() as connection:
        connection.execute(
            "UPDATE vouchers SET assigned_to=? WHERE id=?",
            ("Destinatario prima", voucher_id),
        )
    return database, controller_id, voucher_id


def test_report_detail_uses_same_snapshot_as_filtering(tmp_path, monkeypatch):
    path = tmp_path / "voucher-management.db"
    reader, controller_id, voucher_id = _seed(path)
    writer = Database(path)
    writer.initialize()

    original_details = reader.report_voucher_personal_details
    write_committed = {"value": False}

    def details_after_concurrent_commit(*, voucher_ids):
        # The first-pass facts query has already established the reader's
        # snapshot. WAL allows this second connection to commit a newer value.
        with writer.transaction() as connection:
            connection.execute(
                "UPDATE vouchers SET assigned_to=? WHERE id=?",
                ("Destinatario dopo", voucher_id),
            )
        write_committed["value"] = True
        return original_details(voucher_ids=voucher_ids)

    monkeypatch.setattr(
        reader,
        "report_voucher_personal_details",
        details_after_concurrent_commit,
    )

    try:
        dataset = build_report_dataset(
            reader,
            kind=ReportKind.FULL_HISTORY,
            generated_at=NOW,
            controller_id=controller_id,
        )
        assert write_committed["value"] is True
        assert dataset.rows[0].recipient == "Destinatario prima"

        # The concurrent write did commit; a fresh read sees the newer state.
        fresh = writer.connection.execute(
            "SELECT assigned_to FROM vouchers WHERE id=?",
            (voucher_id,),
        ).fetchone()
        assert fresh["assigned_to"] == "Destinatario dopo"
    finally:
        writer.close()
        reader.close()


def test_read_snapshot_releases_transaction_after_report(tmp_path):
    path = tmp_path / "voucher-management.db"
    reader, controller_id, _voucher_id = _seed(path)
    try:
        assert reader.connection.in_transaction is False
        build_report_dataset(
            reader,
            kind=ReportKind.SUMMARY,
            generated_at=NOW,
            controller_id=controller_id,
        )
        assert reader.connection.in_transaction is False
    finally:
        reader.close()
