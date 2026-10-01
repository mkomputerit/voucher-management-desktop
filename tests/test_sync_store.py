"""Tests for complete-snapshot persistence and UniFi observation history."""

from __future__ import annotations

from voucher_management.database import Database
from voucher_management.reporting import ReportKind, build_report_dataset
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
            "SELECT origin, is_nominal FROM vouchers WHERE unifi_id=?",
            ("nominal-created",),
        ).fetchone()
        assert row["origin"] == "APPLICATION"
        assert row["is_nominal"] == 1
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
                "SELECT unifi_id, present_on_controller, origin, is_nominal FROM vouchers"
            )
        }
        assert rows["existing"]["present_on_controller"] == 1
        assert rows["created"]["origin"] == "APPLICATION"
        assert rows["created"]["is_nominal"] == 0
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


def _live_voucher(remote_id: str, code: str, *, used: int = 0) -> ApiVoucher:
    return ApiVoucher(
        id=remote_id,
        code=code,
        recipient="Descrizione live",
        duration_minutes=0,
        create_time=1_700_000_000,
        quota=1,
        used=used,
        status="VALID_MULTI",
        start_time=0,
        end_time=0,
    )


def test_sync_consolidates_unique_legacy_placeholder_into_live_identity(tmp_path):
    db = Database(tmp_path / "legacy-consolidation.sqlite")
    db.initialize()
    legacy_controller = db.create_controller(
        name="Archivio backup precedente",
        api_root="legacy-backup://fixture",
        created_at="2026-09-25T10:00:00+00:00",
    )
    live_controller = db.create_controller(
        name="Reception",
        api_root="https://controller.example",
        created_at="2026-09-29T08:00:00+00:00",
    )
    try:
        legacy_id = db.upsert_voucher(
            controller_id=legacy_controller,
            unifi_id="legacy-placeholder",
            code="12345-67890",
            name="Mario Legacy",
            imported_at="2026-09-28T08:00:00+00:00",
            last_synced_at="2026-09-28T08:00:00+00:00",
            expired=True,
        )
        with db.transaction() as tx:
            tx.execute(
                "UPDATE vouchers SET usage_observed=0, present_on_controller=0 WHERE id=?",
                (legacy_id,),
            )
        db.record_print_audit(
            controller_id=legacy_controller,
            audit_id="legacy-print-1",
            codes=["12345-67890"],
            output_file="legacy.pdf",
            document_copies=1,
            printed_at="2026-09-25T10:15:00+00:00",
            windows_user="MIGRATION",
        )

        persist_successful_snapshot(
            db,
            controller_id=live_controller,
            vouchers=[_live_voucher("live-voucher", "1234567890")],
            observed_at="2026-09-29T09:00:00+00:00",
            sync_uuid="sync-consolidate-legacy",
        )

        canonical_rows = db.connection.execute(
            """SELECT id, controller_id, unifi_id, assigned_to, usage_observed
               FROM vouchers
               WHERE REPLACE(code, '-', '')='1234567890'"""
        ).fetchall()
        assert len(canonical_rows) == 1
        live_row = canonical_rows[0]
        assert int(live_row["controller_id"]) == live_controller
        assert live_row["unifi_id"] == "live-voucher"
        assert live_row["assigned_to"] == "Mario Legacy"
        assert live_row["usage_observed"] == 1
        assert db.print_summary(int(live_row["id"])).print_jobs == 1

        event = db.connection.execute(
            """SELECT event_type FROM voucher_events
               WHERE voucher_id=? AND event_type='LEGACY_IDENTITY_CONSOLIDATED'""",
            (int(live_row["id"]),),
        ).fetchone()
        assert event is not None

        report = build_report_dataset(
            db,
            kind=ReportKind.PRINTED_UNUSED,
            generated_at="2026-09-29T10:00:00+00:00",
            controller_id=live_controller,
        )
        assert [row.voucher_id for row in report.rows] == [int(live_row["id"])]
        assert report.rows[0].print_jobs == 1
        assert report.rows[0].usage_observed is True
        assert report.rows[0].ever_used is False
    finally:
        db.close()


def test_sync_keeps_ambiguous_legacy_identity_unmerged_and_marks_review(tmp_path):
    db = Database(tmp_path / "legacy-ambiguous.sqlite")
    db.initialize()
    first_legacy = db.create_controller(
        name="Archivio 1",
        api_root="legacy-backup://first",
        created_at="2026-09-25T10:00:00+00:00",
    )
    second_legacy = db.create_controller(
        name="Archivio 2",
        api_root="legacy-backup://second",
        created_at="2026-09-26T10:00:00+00:00",
    )
    live_controller = db.create_controller(
        name="Reception",
        api_root="https://controller.example",
        created_at="2026-09-29T08:00:00+00:00",
    )
    try:
        for controller, remote_id in (
            (first_legacy, "legacy-a"),
            (second_legacy, "legacy-b"),
        ):
            voucher_id = db.upsert_voucher(
                controller_id=controller,
                unifi_id=remote_id,
                code="12345-67890",
                imported_at="2026-09-28T08:00:00+00:00",
                last_synced_at="2026-09-28T08:00:00+00:00",
            )
            with db.transaction() as tx:
                tx.execute(
                    "UPDATE vouchers SET usage_observed=0, present_on_controller=0 WHERE id=?",
                    (voucher_id,),
                )

        persist_successful_snapshot(
            db,
            controller_id=live_controller,
            vouchers=[_live_voucher("live-voucher", "1234567890")],
            observed_at="2026-09-29T09:00:00+00:00",
            sync_uuid="sync-ambiguous-legacy",
        )

        rows = db.connection.execute(
            """SELECT id, controller_id FROM vouchers
               WHERE REPLACE(code, '-', '')='1234567890'
               ORDER BY id"""
        ).fetchall()
        assert len(rows) == 3
        live_id = next(
            int(row["id"])
            for row in rows
            if int(row["controller_id"]) == live_controller
        )
        review = db.connection.execute(
            """SELECT details_json FROM voucher_events
               WHERE voucher_id=?
                 AND event_type='LEGACY_IDENTITY_REVIEW_REQUIRED'""",
            (live_id,),
        ).fetchone()
        assert review is not None
        assert '"candidate_count":2' in review["details_json"]
    finally:
        db.close()


def test_legacy_and_live_prints_are_renumbered_chronologically_after_merge(tmp_path):
    db = Database(tmp_path / "legacy-print-order.sqlite")
    db.initialize()
    legacy_controller = db.create_controller(
        name="Archivio",
        api_root="legacy-backup://order",
        created_at="2026-09-20T08:00:00+00:00",
    )
    live_controller = db.create_controller(
        name="Reception",
        api_root="https://controller.example",
        created_at="2026-09-20T08:00:00+00:00",
    )
    try:
        legacy_id = db.upsert_voucher(
            controller_id=legacy_controller,
            unifi_id="legacy",
            code="12345-67890",
            imported_at="2026-09-20T08:00:00+00:00",
            last_synced_at="2026-09-20T08:00:00+00:00",
        )
        with db.transaction() as tx:
            tx.execute(
                "UPDATE vouchers SET usage_observed=0, present_on_controller=0 WHERE id=?",
                (legacy_id,),
            )
        db.record_print_audit(
            controller_id=legacy_controller,
            audit_id="legacy-earlier",
            codes=["12345-67890"],
            output_file="legacy.pdf",
            document_copies=1,
            printed_at="2026-09-21T10:30:00+02:00",
            windows_user="MIGRATION",
        )

        live_id = db.upsert_voucher(
            controller_id=live_controller,
            unifi_id="live",
            code="1234567890",
            imported_at="2026-09-20T08:00:00+00:00",
            last_synced_at="2026-09-20T08:00:00+00:00",
        )
        db.record_print_audit(
            controller_id=live_controller,
            audit_id="live-later",
            codes=["12345-67890"],
            output_file="live.pdf",
            document_copies=1,
            printed_at="2026-09-21T09:00:00+00:00",
            windows_user="operator",
        )

        persist_successful_snapshot(
            db,
            controller_id=live_controller,
            vouchers=[_live_voucher("live", "1234567890")],
            observed_at="2026-09-29T09:00:00+00:00",
            sync_uuid="sync-print-order",
        )

        prints = db.connection.execute(
            """SELECT vp.print_sequence, vp.is_reprint, pj.print_job_uuid
               FROM voucher_prints AS vp
               JOIN print_jobs AS pj ON pj.id=vp.print_job_id
               WHERE vp.voucher_id=?
               ORDER BY vp.print_sequence""",
            (live_id,),
        ).fetchall()
        assert [
            (row["print_sequence"], row["is_reprint"], row["print_job_uuid"])
            for row in prints
        ] == [
            (1, 0, "legacy-earlier"),
            (2, 1, "live-later"),
        ]
    finally:
        db.close()


def test_legacy_identity_is_not_merged_when_live_creation_is_later_than_legacy_evidence(
    tmp_path,
):
    db = Database(tmp_path / "legacy-temporal-guard.sqlite")
    db.initialize()
    legacy_controller = db.create_controller(
        name="Archivio",
        api_root="legacy-backup://temporal-guard",
        created_at="2026-06-01T08:00:00+00:00",
    )
    live_controller = db.create_controller(
        name="Reception",
        api_root="https://controller.example",
        created_at="2027-01-01T08:00:00+00:00",
    )
    try:
        legacy_id = db.upsert_voucher(
            controller_id=legacy_controller,
            unifi_id="legacy-old",
            code="12345-67890",
            created_at="2026-06-01T08:00:00+00:00",
            imported_at="2026-06-02T08:00:00+00:00",
            last_synced_at="2026-06-02T08:00:00+00:00",
        )
        with db.transaction() as tx:
            tx.execute(
                "UPDATE vouchers SET usage_observed=0, present_on_controller=0 WHERE id=?",
                (legacy_id,),
            )
        db.record_print_audit(
            controller_id=legacy_controller,
            audit_id="legacy-temporal-print",
            codes=["12345-67890"],
            output_file="legacy.pdf",
            document_copies=1,
            printed_at="2026-06-01T09:00:00+00:00",
            windows_user="MIGRATION",
        )

        later_live = ApiVoucher(
            id="live-reused-code",
            code="1234567890",
            recipient="Nuovo voucher",
            duration_minutes=60,
            create_time=1_798_761_600,  # 2027-01-01 UTC
            quota=1,
            used=0,
            status="VALID_MULTI",
            start_time=0,
            end_time=0,
        )
        persist_successful_snapshot(
            db,
            controller_id=live_controller,
            vouchers=[later_live],
            observed_at="2027-01-02T08:00:00+00:00",
            sync_uuid="sync-temporal-guard",
        )

        rows = db.connection.execute(
            """SELECT id, controller_id
               FROM vouchers
               WHERE REPLACE(code, '-', '')='1234567890'
               ORDER BY id"""
        ).fetchall()
        assert len(rows) == 2
        live_id = next(
            int(row["id"])
            for row in rows
            if int(row["controller_id"]) == live_controller
        )
        review = db.connection.execute(
            """SELECT details_json
               FROM voucher_events
               WHERE voucher_id=?
                 AND event_type='LEGACY_IDENTITY_REVIEW_REQUIRED'""",
            (live_id,),
        ).fetchone()
        assert review is not None
        assert "live_created_after_legacy_evidence" in review["details_json"]
        assert db.print_summary(live_id).print_jobs == 0
        assert db.print_summary(legacy_id).print_jobs == 1
    finally:
        db.close()
