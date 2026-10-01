"""Tests for complete-snapshot persistence and UniFi observation history."""

from __future__ import annotations

from voucher_management.database import Database
from voucher_management.security_revocation import (
    pending_security_revocation_ids,
    record_security_revocation_request,
)
from voucher_management.sync_store import (
    load_local_vouchers,
    persist_connection_snapshot_to_path,
    persist_create_result_to_path,
    persist_refresh_snapshot_to_path,
    persist_successful_snapshot,
)
from voucher_management.unifi_api import ApiVoucher


def voucher(remote_id, *, used=0, status="VALID_MULTI", recipient=""):
    return ApiVoucher(
        id=remote_id,
        code=f"CODE-{remote_id}",
        recipient=recipient,
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




def test_new_controller_voucher_keeps_unifi_facts_and_does_not_invent_local_classification(tmp_path):
    db = Database(tmp_path / "discovered.sqlite")
    db.initialize()
    controller = db.create_controller(
        name="Reception",
        api_root="https://controller.example",
        created_at="t",
    )
    try:
        persist_successful_snapshot(
            db,
            controller_id=controller,
            vouchers=[voucher("external-1", recipient="Ospite Controller")],
            observed_at="2026-10-01T08:00:00+00:00",
            sync_uuid="discover-external",
        )

        row = db.connection.execute(
            """SELECT name, created_at, origin, is_nominal, print_state,
                      notes, present_on_controller
               FROM vouchers
               WHERE controller_id=? AND unifi_id='external-1'""",
            (controller,),
        ).fetchone()

        assert row["name"] == "Ospite Controller"
        assert row["created_at"] == "2023-11-14T22:13:20+00:00"
        assert row["origin"] == "CONTROLLER"
        assert row["is_nominal"] is None
        assert row["print_state"] == "UNKNOWN"
        assert row["notes"] == ""
        assert row["present_on_controller"] == 1
    finally:
        db.close()


def test_reobserved_unknown_voucher_is_classified_as_controller_import(tmp_path):
    db = Database(tmp_path / "reobserved-origin.sqlite")
    db.initialize()
    controller = db.create_controller(
        name="Reception",
        api_root="https://controller.example",
        created_at="t",
    )
    try:
        voucher_id = db.upsert_voucher(
            controller_id=controller,
            unifi_id="legacy-live",
            code="CODE-legacy-live",
            name="Ospite storico",
            created_at="2023-11-14T22:13:20+00:00",
            imported_at="2026-09-30T08:00:00+00:00",
            last_synced_at="2026-09-30T08:00:00+00:00",
        )
        with db.transaction() as tx:
            tx.execute(
                "UPDATE vouchers SET origin='UNKNOWN' WHERE id=?",
                (voucher_id,),
            )

        persist_successful_snapshot(
            db,
            controller_id=controller,
            vouchers=[voucher("legacy-live", recipient="Ospite storico")],
            observed_at="2026-10-01T08:00:00+00:00",
            sync_uuid="reobserve-unknown",
        )

        row = db.connection.execute(
            "SELECT origin FROM vouchers WHERE id=?",
            (voucher_id,),
        ).fetchone()
        assert row["origin"] == "CONTROLLER"
    finally:
        db.close()


def test_controller_absence_preserves_local_alignment_and_history(tmp_path):
    db = Database(tmp_path / "absence-history.sqlite")
    db.initialize()
    controller = db.create_controller(
        name="Reception",
        api_root="https://controller.example",
        created_at="t",
    )
    try:
        persist_successful_snapshot(
            db,
            controller_id=controller,
            vouchers=[voucher("external-2", recipient="Tecnico Rossi")],
            observed_at="2026-10-01T08:00:00+00:00",
            sync_uuid="external-present",
        )
        with db.transaction() as tx:
            tx.execute(
                """UPDATE vouchers
                   SET is_nominal=1, print_state='PRINTED',
                       notes='Allineato operatore'
                   WHERE controller_id=? AND unifi_id='external-2'""",
                (controller,),
            )

        persist_successful_snapshot(
            db,
            controller_id=controller,
            vouchers=[],
            observed_at="2026-10-01T09:00:00+00:00",
            sync_uuid="external-absent",
        )

        row = db.connection.execute(
            """SELECT name, origin, is_nominal, print_state, notes,
                      present_on_controller
               FROM vouchers
               WHERE controller_id=? AND unifi_id='external-2'""",
            (controller,),
        ).fetchone()
        assert row is not None
        assert row["name"] == "Tecnico Rossi"
        assert row["origin"] == "CONTROLLER"
        assert row["is_nominal"] == 1
        assert row["print_state"] == "PRINTED"
        assert row["notes"] == "Allineato operatore"
        assert row["present_on_controller"] == 0
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


def test_create_result_persists_application_origin_and_nominal_flag(tmp_path):
    path = tmp_path / "create-result.sqlite"
    db = Database(path)
    db.initialize()
    controller = db.create_controller(
        name="Reception",
        api_root="https://controller.example",
        created_at="2026-09-29T08:00:00+00:00",
    )
    db.close()

    created = voucher("nominal-created", recipient="Pinco Pallino")
    persist_create_result_to_path(
        path,
        controller_id=controller,
        snapshot=[created],
        created=[created],
        snapshot_complete=True,
        snapshot_observed=True,
        is_nominal=True,
        observed_at="2026-09-29T08:01:00+00:00",
    )

    check = Database(path)
    try:
        check.initialize()
        row = check.connection.execute(
            """SELECT origin, is_nominal, print_state, name
               FROM vouchers WHERE unifi_id=?""",
            ("nominal-created",),
        ).fetchone()
        assert row["origin"] == "APPLICATION"
        assert row["is_nominal"] == 1
        assert row["print_state"] == "NOT_PRINTED"
        assert row["name"] == "Pinco Pallino"
    finally:
        check.close()


def test_partial_create_result_does_not_mark_unseen_local_rows_absent(tmp_path):
    path = tmp_path / "partial-create.sqlite"
    db = Database(path)
    db.initialize()
    controller = db.create_controller(name="A", api_root="https://a.example", created_at="t")
    persist_successful_snapshot(
        db,
        controller_id=controller,
        vouchers=[voucher("existing")],
        observed_at="2026-09-29T08:00:00+00:00",
        sync_uuid="before-create",
    )
    db.close()

    created = voucher("created")
    persist_create_result_to_path(
        path,
        controller_id=controller,
        snapshot=[voucher("existing"), created],
        created=[created],
        snapshot_complete=False,
        snapshot_observed=True,
        is_nominal=False,
        observed_at="2026-09-29T08:05:00+00:00",
    )

    check = Database(path)
    try:
        check.initialize()
        rows = {
            row["unifi_id"]: row
            for row in check.connection.execute(
                """SELECT unifi_id, present_on_controller, origin, is_nominal,
                          print_state, name
                   FROM vouchers"""
            )
        }
        assert rows["existing"]["present_on_controller"] == 1
        assert rows["created"]["origin"] == "APPLICATION"
        assert rows["created"]["is_nominal"] == 0
        assert rows["created"]["print_state"] == "NOT_PRINTED"
    finally:
        check.close()


def test_create_snapshot_and_classification_roll_back_together_on_failure(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "create-atomic.sqlite"
    db = Database(path)
    db.initialize()
    controller = db.create_controller(
        name="A",
        api_root="https://a.example",
        created_at="t",
    )
    db.close()

    original = Database.mark_application_created_vouchers

    def fail_after_upsert(self, **kwargs):
        if kwargs.get("connection") is not None:
            raise RuntimeError("synthetic classification failure")
        return original(self, **kwargs)

    monkeypatch.setattr(
        Database,
        "mark_application_created_vouchers",
        fail_after_upsert,
    )

    try:
        persist_create_result_to_path(
            path,
            controller_id=controller,
            snapshot=[voucher("atomic")],
            created=[voucher("atomic")],
            snapshot_complete=True,
            snapshot_observed=True,
            is_nominal=True,
            observed_at="2026-09-29T10:00:00+00:00",
        )
    except RuntimeError as exc:
        assert "synthetic classification failure" in str(exc)
    else:
        raise AssertionError("classification failure must abort the transaction")

    check = Database(path)
    try:
        check.initialize()
        assert check.connection.execute(
            "SELECT COUNT(*) FROM vouchers WHERE unifi_id='atomic'"
        ).fetchone()[0] == 0
        assert check.connection.execute(
            "SELECT COUNT(*) FROM sync_runs"
        ).fetchone()[0] == 0
    finally:
        check.close()


def test_stale_successful_create_snapshot_upserts_positive_rows_without_absence(
    tmp_path,
):
    path = tmp_path / "create-stale.sqlite"
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
        vouchers=[voucher("existing"), voucher("other")],
        observed_at="2026-09-29T08:00:00+00:00",
        sync_uuid="before-stale-create",
    )
    db.close()

    created = voucher("created")
    persist_create_result_to_path(
        path,
        controller_id=controller,
        snapshot=[voucher("existing"), created],
        created=[created],
        snapshot_complete=False,
        snapshot_observed=True,
        is_nominal=False,
        observed_at="2026-09-29T08:05:00+00:00",
    )

    check = Database(path)
    try:
        check.initialize()
        rows = {
            row["unifi_id"]: row
            for row in check.connection.execute(
                """SELECT unifi_id, present_on_controller, origin, is_nominal
                   FROM vouchers"""
            )
        }
        assert rows["other"]["present_on_controller"] == 1
        assert rows["created"]["origin"] == "APPLICATION"
        assert rows["created"]["is_nominal"] == 0
    finally:
        check.close()



def test_complete_snapshot_reconciles_pending_security_revocation(tmp_path):
    db = Database(tmp_path / "security-reconcile.sqlite")
    db.initialize()
    controller = db.create_controller(
        name="A",
        api_root="https://a.example",
        created_at="2026-01-01T00:00:00+00:00",
    )
    try:
        persist_successful_snapshot(
            db,
            controller_id=controller,
            vouchers=[voucher("pending-security")],
            observed_at="2026-09-25T10:00:00+00:00",
            sync_uuid="security-before",
        )
        row = db.connection.execute(
            "SELECT id FROM vouchers WHERE controller_id=? AND unifi_id=?",
            (controller, "pending-security"),
        ).fetchone()
        voucher_id = int(row["id"])
        record_security_revocation_request(
            db,
            voucher_id=voucher_id,
            requested_at="2026-09-25T11:00:00+00:00",
            windows_user=r"PC\operator",
        )
        assert pending_security_revocation_ids(db) == (voucher_id,)

        persist_successful_snapshot(
            db,
            controller_id=controller,
            vouchers=[],
            observed_at="2026-09-25T12:00:00+00:00",
            sync_uuid="security-after",
        )

        assert pending_security_revocation_ids(db) == ()
        event = db.connection.execute(
            """SELECT event_type, source, windows_user, details_json
               FROM voucher_events WHERE voucher_id=?""",
            (voucher_id,),
        ).fetchone()
        assert event["event_type"] == "SECURITY_REVOKED"
        assert event["source"] == "SYSTEM"
        assert event["windows_user"] == "SYSTEM"
        assert '"confirmation_source":"fresh_snapshot_absent"' in event["details_json"]
    finally:
        db.close()


def test_complete_snapshot_closes_pending_security_revocation_when_still_present(
    tmp_path,
):
    db = Database(tmp_path / "security-present.sqlite")
    db.initialize()
    controller = db.create_controller(
        name="A",
        api_root="https://a.example",
        created_at="2026-01-01T00:00:00+00:00",
    )
    try:
        persist_successful_snapshot(
            db,
            controller_id=controller,
            vouchers=[voucher("pending-present")],
            observed_at="2026-09-25T10:00:00+00:00",
            sync_uuid="security-present-before",
        )
        row = db.connection.execute(
            "SELECT id FROM vouchers WHERE controller_id=? AND unifi_id=?",
            (controller, "pending-present"),
        ).fetchone()
        voucher_id = int(row["id"])
        record_security_revocation_request(
            db,
            voucher_id=voucher_id,
            requested_at="2026-09-25T11:00:00+00:00",
            windows_user=r"PC\operator",
        )

        persist_successful_snapshot(
            db,
            controller_id=controller,
            vouchers=[voucher("pending-present")],
            observed_at="2026-09-25T12:00:00+00:00",
            sync_uuid="security-present-after",
        )

        assert pending_security_revocation_ids(db) == ()
        event = db.connection.execute(
            """SELECT event_type, source, windows_user
               FROM voucher_events WHERE voucher_id=?""",
            (voucher_id,),
        ).fetchone()
        assert event["event_type"] == "SECURITY_REVOKE_NOT_APPLIED"
        assert event["source"] == "SYSTEM"
        assert event["windows_user"] == "SYSTEM"
    finally:
        db.close()



def test_security_reconciliation_failure_rolls_back_entire_snapshot(tmp_path, monkeypatch):
    from voucher_management import sync_store

    db = Database(tmp_path / "security-atomic.sqlite")
    db.initialize()
    controller = db.create_controller(
        name="A",
        api_root="https://a.example",
        created_at="2026-01-01T00:00:00+00:00",
    )
    try:
        persist_successful_snapshot(
            db,
            controller_id=controller,
            vouchers=[voucher("atomic")],
            observed_at="2026-09-25T10:00:00+00:00",
            sync_uuid="atomic-before",
        )

        def fail_reconciliation(*args, **kwargs):
            raise RuntimeError("synthetic security reconciliation failure")

        monkeypatch.setattr(
            sync_store,
            "reconcile_pending_security_revocations",
            fail_reconciliation,
        )

        try:
            persist_successful_snapshot(
                db,
                controller_id=controller,
                vouchers=[voucher("atomic", used=2)],
                observed_at="2026-09-25T11:00:00+00:00",
                sync_uuid="atomic-failing",
            )
        except RuntimeError as exc:
            assert "synthetic security reconciliation failure" in str(exc)
        else:
            raise AssertionError("reconciliation failure must abort the snapshot")

        row = db.connection.execute(
            """SELECT authorized_guest_count, last_synced_at
               FROM vouchers WHERE controller_id=? AND unifi_id='atomic'""",
            (controller,),
        ).fetchone()
        assert row["authorized_guest_count"] == 0
        assert row["last_synced_at"] == "2026-09-25T10:00:00+00:00"
        assert db.connection.execute(
            "SELECT COUNT(*) FROM sync_runs WHERE sync_uuid='atomic-failing'"
        ).fetchone()[0] == 0
    finally:
        db.close()
