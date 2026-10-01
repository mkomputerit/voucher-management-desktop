from __future__ import annotations

import json

import pytest

from voucher_management.database import (
    Database,
    PRINT_STATE_NOT_PRINTED,
    PRINT_STATE_PRINTED,
    PRINT_STATE_UNKNOWN,
)
from voucher_management.preparation_deletion import (
    preparation_delete_facts,
    record_preparation_delete_requests,
    reconcile_preparation_delete_requests,
)
from voucher_management.sync_store import persist_successful_snapshot
from voucher_management.unifi_api import ApiVoucher


NOW = "2026-10-01T10:00:00+00:00"


def _db(tmp_path):
    db = Database(tmp_path / "preparation-delete.sqlite")
    db.initialize()
    controller = db.create_controller(
        name="Reception",
        api_root="https://controller.example",
        created_at="2026-01-01T00:00:00+00:00",
    )
    return db, controller


def _row(
    db,
    controller,
    remote_id,
    *,
    print_state=PRINT_STATE_NOT_PRINTED,
    aligned=True,
    ever_used=False,
):
    voucher_id = db.upsert_voucher(
        controller_id=controller,
        unifi_id=remote_id,
        code=f"CODE-{remote_id}",
        name=f"Guest {remote_id}",
        created_at="2026-09-01T08:00:00+00:00",
        imported_at="2026-09-01T08:00:00+00:00",
        duration_minutes=60,
        authorized_guest_limit=1,
        authorized_guest_count=0,
        expired=False,
        last_synced_at="2026-09-30T08:00:00+00:00",
    )
    with db.transaction() as tx:
        tx.execute(
            """UPDATE vouchers
               SET print_state=?,
                   alignment_completed_at=?,
                   ever_used=?,
                   usage_observed=1,
                   present_on_controller=1
               WHERE id=?""",
            (
                print_state,
                NOW if aligned else None,
                int(ever_used),
                voucher_id,
            ),
        )
    return voucher_id


def _api(remote_id):
    return ApiVoucher(
        id=remote_id,
        code=f"CODE-{remote_id}",
        recipient=f"Guest {remote_id}",
        duration_minutes=60,
        create_time=1_756_704_000,
        quota=1,
        used=0,
        status="VALID_ONE",
        start_time=0,
        end_time=0,
    )


def test_preparation_delete_facts_exposes_positive_local_evidence(tmp_path):
    db, controller = _db(tmp_path)
    try:
        voucher_id = _row(db, controller, "v1")
        facts = preparation_delete_facts(
            db,
            controller_id=controller,
            unifi_ids=["v1"],
        )
        assert facts["v1"].voucher_id == voucher_id
        assert facts["v1"].print_state == PRINT_STATE_NOT_PRINTED
        assert facts["v1"].alignment_completed is True
        assert facts["v1"].ever_used is False
    finally:
        db.close()


@pytest.mark.parametrize(
    ("print_state", "aligned", "ever_used"),
    [
        (PRINT_STATE_PRINTED, True, False),
        (PRINT_STATE_UNKNOWN, True, False),
        (PRINT_STATE_NOT_PRINTED, False, False),
        (PRINT_STATE_NOT_PRINTED, True, True),
    ],
)
def test_request_fails_closed_without_positive_preparation_state(
    tmp_path,
    print_state,
    aligned,
    ever_used,
):
    db, controller = _db(tmp_path)
    try:
        _row(
            db,
            controller,
            "v1",
            print_state=print_state,
            aligned=aligned,
            ever_used=ever_used,
        )
        with pytest.raises(RuntimeError):
            record_preparation_delete_requests(
                db,
                controller_id=controller,
                unifi_ids=["v1"],
                reason="Errore di preparazione",
                requested_at=NOW,
                windows_user=r"PC\operatore",
            )
        assert db.connection.execute(
            "SELECT COUNT(*) FROM voucher_events"
        ).fetchone()[0] == 0
    finally:
        db.close()


def test_verified_print_evidence_blocks_delete_even_if_state_is_corrupted(tmp_path):
    db, controller = _db(tmp_path)
    try:
        voucher_id = _row(
            db,
            controller,
            "v1",
            print_state=PRINT_STATE_NOT_PRINTED,
            aligned=True,
            ever_used=False,
        )
        db.record_print_audit(
            controller_id=controller,
            audit_id="verified-print-before-delete",
            codes=["CODE-v1"],
            output_file="voucher.pdf",
            document_copies=1,
            printed_at="2026-09-20T08:00:00+00:00",
            windows_user="operator",
        )
        with db.transaction() as tx:
            tx.execute(
                "UPDATE vouchers SET print_state=? WHERE id=?",
                (PRINT_STATE_NOT_PRINTED, voucher_id),
            )

        with pytest.raises(RuntimeError, match="stampa verificata"):
            record_preparation_delete_requests(
                db,
                controller_id=controller,
                unifi_ids=["v1"],
                reason="Errore preparazione",
                requested_at=NOW,
                windows_user="operator",
            )
    finally:
        db.close()


def test_reason_is_mandatory_before_request_is_recorded(tmp_path):
    db, controller = _db(tmp_path)
    try:
        _row(db, controller, "v1")
        with pytest.raises(ValueError, match="motivazione"):
            record_preparation_delete_requests(
                db,
                controller_id=controller,
                unifi_ids=["v1"],
                reason="   ",
                requested_at=NOW,
                windows_user=r"PC\operatore",
            )
    finally:
        db.close()


def test_reason_length_is_bounded_before_request_is_recorded(tmp_path):
    db, controller = _db(tmp_path)
    try:
        _row(db, controller, "v1")
        with pytest.raises(ValueError, match="1000"):
            record_preparation_delete_requests(
                db,
                controller_id=controller,
                unifi_ids=["v1"],
                reason="x" * 1001,
                requested_at=NOW,
                windows_user=r"PC\operatore",
            )
        assert db.connection.execute(
            "SELECT COUNT(*) FROM voucher_events"
        ).fetchone()[0] == 0
    finally:
        db.close()


def test_absent_fresh_snapshot_confirms_delete_and_preserves_reason(tmp_path):
    db, controller = _db(tmp_path)
    try:
        voucher_id = _row(db, controller, "v1")
        record_preparation_delete_requests(
            db,
            controller_id=controller,
            unifi_ids=["v1"],
            reason="Destinatario errato",
            requested_at=NOW,
            windows_user=r"PC\operatore",
        )

        deleted, not_applied = reconcile_preparation_delete_requests(
            db,
            controller_id=controller,
            present_unifi_ids=set(),
            observed_at="2026-10-01T10:05:00+00:00",
        )

        assert deleted == (voucher_id,)
        assert not_applied == ()
        event = db.connection.execute(
            """SELECT event_type, details_json
               FROM voucher_events WHERE voucher_id=?""",
            (voucher_id,),
        ).fetchone()
        assert event["event_type"] == "PREPARATION_DELETED"
        details = json.loads(event["details_json"])
        assert details["reason"] == "Destinatario errato"
        assert details["requested_at"] == NOW
        assert details["confirmed_at"] == "2026-10-01T10:05:00+00:00"
        assert details["workflow"] == "preparation_error"
        assert details["confirmation_source"] == "fresh_snapshot_absent"
    finally:
        db.close()


def test_present_fresh_snapshot_marks_request_not_applied_and_allows_retry(tmp_path):
    db, controller = _db(tmp_path)
    try:
        voucher_id = _row(db, controller, "v1")
        record_preparation_delete_requests(
            db,
            controller_id=controller,
            unifi_ids=["v1"],
            reason="Errore preparazione",
            requested_at=NOW,
            windows_user="operator",
        )

        deleted, not_applied = reconcile_preparation_delete_requests(
            db,
            controller_id=controller,
            present_unifi_ids={"v1"},
            observed_at="2026-10-01T10:05:00+00:00",
        )

        assert deleted == ()
        assert not_applied == (voucher_id,)
        assert db.connection.execute(
            """SELECT event_type FROM voucher_events
               WHERE voucher_id=? ORDER BY id DESC LIMIT 1""",
            (voucher_id,),
        ).fetchone()["event_type"] == "PREPARATION_DELETE_NOT_APPLIED"

        second = record_preparation_delete_requests(
            db,
            controller_id=controller,
            unifi_ids=["v1"],
            reason="Secondo tentativo",
            requested_at="2026-10-01T10:06:00+00:00",
            windows_user="operator",
        )
        assert second == (voucher_id,)
    finally:
        db.close()


def test_complete_sync_automatically_reconciles_pending_preparation_delete(tmp_path):
    db, controller = _db(tmp_path)
    try:
        voucher_id = _row(db, controller, "v1")
        record_preparation_delete_requests(
            db,
            controller_id=controller,
            unifi_ids=["v1"],
            reason="Creazione errata",
            requested_at=NOW,
            windows_user="operator",
        )

        persist_successful_snapshot(
            db,
            controller_id=controller,
            vouchers=[],
            observed_at="2026-10-01T10:10:00+00:00",
            sync_uuid="delete-reconcile",
        )

        row = db.connection.execute(
            "SELECT present_on_controller FROM vouchers WHERE id=?",
            (voucher_id,),
        ).fetchone()
        assert row["present_on_controller"] == 0
        event = db.connection.execute(
            """SELECT event_type, details_json FROM voucher_events
               WHERE voucher_id=?""",
            (voucher_id,),
        ).fetchone()
        assert event["event_type"] == "PREPARATION_DELETED"
        assert json.loads(event["details_json"])["reason"] == "Creazione errata"
    finally:
        db.close()
