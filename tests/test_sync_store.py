"""Tests for complete-snapshot persistence and UniFi observation history."""

from __future__ import annotations

from voucher_management.database import Database
from voucher_management.sync_store import (
    load_local_vouchers,
    persist_connection_snapshot_to_path,
    persist_creation_result_to_path,
    persist_refresh_snapshot_to_path,
    persist_successful_snapshot,
)
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


def test_ever_used_is_sticky_after_later_zero_controller_count(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    db.initialize()
    controller = db.create_controller(
        name="A",
        api_root="https://a.example",
        created_at="t",
    )
    try:
        persist_successful_snapshot(
            db,
            controller_id=controller,
            vouchers=[voucher("1", used=2)],
            observed_at="2026-09-25T10:00:00+00:00",
            sync_uuid="sync-used",
        )
        persist_successful_snapshot(
            db,
            controller_id=controller,
            vouchers=[voucher("1", used=0)],
            observed_at="2026-09-25T11:00:00+00:00",
            sync_uuid="sync-zero",
        )
        row = db.connection.execute(
            "SELECT authorized_guest_count, ever_used FROM vouchers"
        ).fetchone()
        assert row["authorized_guest_count"] == 0
        assert row["ever_used"] == 1
    finally:
        db.close()


def test_creation_result_persists_nominal_classification_only_for_definite_created_rows(tmp_path):
    path = tmp_path / "creation.sqlite"
    db = Database(path)
    db.initialize()
    controller = db.create_controller(
        name="A",
        api_root="https://a.example",
        created_at="t",
    )
    db.close()

    created = voucher("created")
    external = voucher("external")
    persist_creation_result_to_path(
        path,
        controller_id=controller,
        vouchers=[created, external],
        created=[created],
        observed_at="2026-09-29T08:00:00+00:00",
        is_nominal=True,
        snapshot_complete=True,
    )

    check = Database(path)
    try:
        check.initialize()
        rows = {
            row["unifi_id"]: row
            for row in check.connection.execute(
                """SELECT unifi_id, origin, is_nominal
                   FROM vouchers ORDER BY unifi_id"""
            )
        }
        assert rows["created"]["origin"] == "APPLICATION"
        assert rows["created"]["is_nominal"] == 1
        assert rows["external"]["origin"] == "CONTROLLER"
        assert rows["external"]["is_nominal"] is None
    finally:
        check.close()


def test_creation_refresh_failure_persists_only_returned_created_rows_without_absence_inference(tmp_path):
    path = tmp_path / "creation-partial.sqlite"
    db = Database(path)
    db.initialize()
    controller = db.create_controller(
        name="A",
        api_root="https://a.example",
        created_at="t",
    )
    persist_successful_snapshot(
        db,
        controller_id=controller,
        vouchers=[voucher("existing")],
        observed_at="2026-09-29T07:00:00+00:00",
        sync_uuid="sync-before-create",
    )
    db.close()

    created = voucher("created")
    persist_creation_result_to_path(
        path,
        controller_id=controller,
        vouchers=[voucher("existing"), created],
        created=[created],
        observed_at="2026-09-29T08:00:00+00:00",
        is_nominal=False,
        snapshot_complete=False,
    )

    check = Database(path)
    try:
        check.initialize()
        rows = {
            row["unifi_id"]: row
            for row in check.connection.execute(
                """SELECT unifi_id, present_on_controller,
                          origin, is_nominal
                   FROM vouchers"""
            )
        }
        assert rows["existing"]["present_on_controller"] == 1
        assert rows["created"]["origin"] == "APPLICATION"
        assert rows["created"]["is_nominal"] == 0
        assert (
            check.connection.execute("SELECT COUNT(*) FROM sync_runs").fetchone()[0]
            == 1
        )
    finally:
        check.close()


def test_uncertain_creation_snapshot_never_guesses_created_or_nominal_flags(tmp_path):
    path = tmp_path / "creation-uncertain.sqlite"
    db = Database(path)
    db.initialize()
    controller = db.create_controller(
        name="A",
        api_root="https://a.example",
        created_at="t",
    )
    db.close()

    persist_creation_result_to_path(
        path,
        controller_id=controller,
        vouchers=[voucher("maybe-created")],
        created=[],
        observed_at="2026-09-29T08:00:00+00:00",
        is_nominal=True,
        snapshot_complete=True,
    )

    check = Database(path)
    try:
        check.initialize()
        row = check.connection.execute(
            """SELECT origin, is_nominal
               FROM vouchers WHERE unifi_id='maybe-created'"""
        ).fetchone()
        assert row["origin"] is None
        assert row["is_nominal"] is None
    finally:
        check.close()


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


def test_local_snapshot_round_trips_into_existing_ui_shape(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    db.initialize()
    controller = db.create_controller(name="A", api_root="https://a.example", created_at="t")
    try:
        persist_successful_snapshot(
            db,
            controller_id=controller,
            vouchers=[voucher("1", used=2, status="EXPIRED")],
            observed_at="2026-09-26T06:00:00+00:00",
            sync_uuid="sync-local",
        )
        local = load_local_vouchers(db, controller_id=controller)
        assert len(local) == 1
        restored = local[0]
        assert restored.id == "1"
        assert restored.code == "CODE-1"
        assert restored.used == 2
        assert restored.quota == 5
        assert restored.status == "EXPIRED"
        assert restored.create_time == 1_700_000_000
        assert restored.start_time == 1_700_000_100
        assert restored.end_time == 1_700_003_600
    finally:
        db.close()


def test_local_snapshot_keeps_voucher_after_controller_absence(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    db.initialize()
    controller = db.create_controller(name="A", api_root="https://a.example", created_at="t")
    try:
        persist_successful_snapshot(
            db, controller_id=controller, vouchers=[voucher("1")],
            observed_at="2026-09-26T06:00:00+00:00", sync_uuid="sync-present",
        )
        persist_successful_snapshot(
            db, controller_id=controller, vouchers=[],
            observed_at="2026-09-26T07:00:00+00:00", sync_uuid="sync-absent",
        )
        local = load_local_vouchers(db, controller_id=controller)
        assert [item.id for item in local] == ["1"]
    finally:
        db.close()



def test_archived_voucher_is_hidden_from_local_operator_snapshot(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    db.initialize()
    controller = db.create_controller(
        name="A",
        api_root="https://a.example",
        created_at="t",
    )
    try:
        persist_successful_snapshot(
            db,
            controller_id=controller,
            vouchers=[voucher("1")],
            observed_at="2026-01-01T06:00:00+00:00",
            sync_uuid="sync-present-archive",
        )
        persist_successful_snapshot(
            db,
            controller_id=controller,
            vouchers=[],
            observed_at="2026-01-02T06:00:00+00:00",
            sync_uuid="sync-absent-archive",
        )
        with db.transaction() as tx:
            tx.execute(
                """UPDATE vouchers
                   SET code='ARCHIVED-1', archived_at='2026-09-27T08:00:00+00:00'
                   WHERE controller_id=? AND unifi_id='1'""",
                (controller,),
            )

        assert load_local_vouchers(db, controller_id=controller) == []
    finally:
        db.close()


def test_controller_reappearance_reactivates_archived_voucher(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    db.initialize()
    controller = db.create_controller(
        name="A",
        api_root="https://a.example",
        created_at="t",
    )
    try:
        persist_successful_snapshot(
            db,
            controller_id=controller,
            vouchers=[voucher("1")],
            observed_at="2026-01-01T06:00:00+00:00",
            sync_uuid="sync-original",
        )
        persist_successful_snapshot(
            db,
            controller_id=controller,
            vouchers=[],
            observed_at="2026-01-02T06:00:00+00:00",
            sync_uuid="sync-gone",
        )
        with db.transaction() as tx:
            tx.execute(
                """UPDATE vouchers
                   SET code='ARCHIVED-1', name='', archived_at='2026-09-27T08:00:00+00:00'
                   WHERE controller_id=? AND unifi_id='1'""",
                (controller,),
            )

        persist_successful_snapshot(
            db,
            controller_id=controller,
            vouchers=[voucher("1")],
            observed_at="2026-09-27T09:00:00+00:00",
            sync_uuid="sync-returned",
        )

        row = db.connection.execute(
            """SELECT code, archived_at, present_on_controller
               FROM vouchers WHERE controller_id=? AND unifi_id='1'""",
            (controller,),
        ).fetchone()
        assert row["code"] == "CODE-1"
        assert row["archived_at"] is None
        assert row["present_on_controller"] == 1
        assert [item.id for item in load_local_vouchers(
            db,
            controller_id=controller,
        )] == ["1"]
    finally:
        db.close()


def test_worker_path_connection_persists_controller_and_snapshot(tmp_path):
    path = tmp_path / "worker.sqlite"
    bootstrap = Database(path)
    bootstrap.initialize()
    bootstrap.close()

    result = persist_connection_snapshot_to_path(
        path,
        api_root="https://controller.example",
        cert_sha256="AA",
        requested_name="Reception",
        site_name="Default Site",
        vouchers=[voucher("worker-1")],
        observed_at="2026-09-28T07:30:00+00:00",
    )

    db = Database(path)
    try:
        db.initialize()
        assert result.controller_name == "Reception"
        assert db.controller_name(result.controller_id) == "Reception"
        row = db.connection.execute(
            "SELECT COUNT(*) FROM vouchers WHERE controller_id=?",
            (result.controller_id,),
        ).fetchone()
        assert row[0] == 1
    finally:
        db.close()


def test_worker_path_refresh_updates_snapshot(tmp_path):
    path = tmp_path / "worker-refresh.sqlite"
    db = Database(path)
    db.initialize()
    controller = db.create_controller(
        name="Reception",
        api_root="https://controller.example",
        created_at="2026-09-28T07:00:00+00:00",
    )
    db.close()

    persist_refresh_snapshot_to_path(
        path,
        controller_id=controller,
        vouchers=[voucher("worker-refresh", used=2)],
        observed_at="2026-09-28T07:31:00+00:00",
    )

    check = Database(path)
    try:
        check.initialize()
        row = check.connection.execute(
            """SELECT authorized_guest_count
               FROM vouchers WHERE controller_id=?""",
            (controller,),
        ).fetchone()
        assert row["authorized_guest_count"] == 2
    finally:
        check.close()
