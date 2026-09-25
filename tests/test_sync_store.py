"""Tests for complete-snapshot persistence and UniFi observation history."""

from __future__ import annotations

from voucher_management.database import Database
from voucher_management.sync_store import persist_successful_snapshot
from voucher_management.unifi_api import ApiVoucher


def voucher(remote_id, *, used=0, status="VALID_MULTI"):
    return ApiVoucher(
        id=remote_id,
        code=f"CODE-{remote_id}",
        recipient="",
        duration_minutes=60,
        create_time=1_700_000_000,
        quota=5,
        used=used,
        status=status,
        start_time=1_700_000_100 if used else 0,
        end_time=1_700_003_600 if status == "EXPIRED" else 0,
    )


def test_snapshot_records_usage_change_without_inventing_use_timestamp(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    db.initialize()
    controller = db.create_controller(name="A", api_root="https://a.example", created_at="t")
    try:
        persist_successful_snapshot(
            db, controller_id=controller, vouchers=[voucher("1")],
            observed_at="2026-09-25T10:00:00+00:00", sync_uuid="sync-1",
        )
        persist_successful_snapshot(
            db, controller_id=controller, vouchers=[voucher("1", used=2)],
            observed_at="2026-09-25T11:00:00+00:00", sync_uuid="sync-2",
        )
        rows = db.connection.execute(
            """SELECT field_name, previous_value, new_value, observed_at
               FROM voucher_sync_observations ORDER BY id"""
        ).fetchall()
        usage = [row for row in rows if row["field_name"] == "authorized_guest_count"]
        assert len(usage) == 1
        assert usage[0]["previous_value"] == "0"
        assert usage[0]["new_value"] == "2"
        assert usage[0]["observed_at"] == "2026-09-25T11:00:00+00:00"
    finally:
        db.close()


def test_complete_snapshot_marks_missing_voucher_absent_but_keeps_history(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    db.initialize()
    controller = db.create_controller(name="A", api_root="https://a.example", created_at="t")
    try:
        persist_successful_snapshot(
            db, controller_id=controller, vouchers=[voucher("1")],
            observed_at="2026-09-25T10:00:00+00:00", sync_uuid="sync-1",
        )
        persist_successful_snapshot(
            db, controller_id=controller, vouchers=[],
            observed_at="2026-09-25T12:00:00+00:00", sync_uuid="sync-2",
        )
        row = db.connection.execute("SELECT * FROM vouchers").fetchone()
        assert row is not None
        assert row["present_on_controller"] == 0
        observation = db.connection.execute(
            """SELECT * FROM voucher_sync_observations
               WHERE field_name='present_on_controller'"""
        ).fetchone()
        assert observation["previous_value"] == "1"
        assert observation["new_value"] == "0"
    finally:
        db.close()


def test_new_voucher_does_not_create_fake_change_history(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    db.initialize()
    controller = db.create_controller(name="A", api_root="https://a.example", created_at="t")
    try:
        persist_successful_snapshot(
            db, controller_id=controller, vouchers=[voucher("1", used=3)],
            observed_at="2026-09-25T10:00:00+00:00", sync_uuid="sync-1",
        )
        assert db.connection.execute(
            "SELECT COUNT(*) FROM voucher_sync_observations"
        ).fetchone()[0] == 0
    finally:
        db.close()
