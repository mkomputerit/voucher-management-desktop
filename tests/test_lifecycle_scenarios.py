"""Cross-module lifecycle scenarios for the 5.1 release gate."""

from __future__ import annotations

from voucher_management.alignment import align_vouchers
from voucher_management.database import Database, PRINT_STATE_NOT_PRINTED
from voucher_management.operational_alerts import (
    set_unprinted_warning_days,
    unprinted_warning_candidates,
)
from voucher_management.reporting import ReportKind, build_report_dataset
from voucher_management.security_revocation import (
    security_revocation_candidates,
    set_security_revoke_days,
)
from voucher_management.sync_store import persist_successful_snapshot
from voucher_management.unifi_api import ApiVoucher


NOW = "2026-10-03T12:00:00+00:00"


def _remote(
    remote_id: str,
    *,
    used: int = 0,
    status: str = "VALID_MULTI",
    recipient: str = "Guest",
) -> ApiVoucher:
    return ApiVoucher(
        id=remote_id,
        code=f"CODE-{remote_id}",
        recipient=recipient,
        duration_minutes=60,
        create_time=1_758_000_000,
        quota=2,
        used=used,
        status=status,
        start_time=1_758_010_000 if used else 0,
        end_time=1_758_020_000 if status == "EXPIRED" else 0,
    )


def _db(tmp_path):
    db = Database(tmp_path / "lifecycle.sqlite")
    db.initialize()
    controller = db.create_controller(
        name="Reception",
        api_root="https://controller.example",
        created_at="2026-09-01T08:00:00+00:00",
    )
    return db, controller


def test_print_use_and_expiry_remain_consistent_across_sync_reports_and_security(
    tmp_path,
):
    db, controller = _db(tmp_path)
    try:
        persist_successful_snapshot(
            db,
            controller_id=controller,
            vouchers=[_remote("life-1")],
            observed_at="2026-09-10T08:00:00+00:00",
            sync_uuid="created",
        )
        voucher_id = int(
            db.connection.execute(
                "SELECT id FROM vouchers WHERE unifi_id='life-1'"
            ).fetchone()["id"]
        )
        align_vouchers(
            db,
            controller_id=controller,
            voucher_ids=[voucher_id],
            is_nominal=False,
            print_state=PRINT_STATE_NOT_PRINTED,
            aligned_at="2026-09-10T08:05:00+00:00",
            windows_user=r"PC\operator",
        )
        db.record_print_audit(
            controller_id=controller,
            audit_id="life-print",
            codes=["CODE-life-1"],
            output_file="voucher.pdf",
            document_copies=1,
            printed_at="2026-09-11T08:00:00+00:00",
            windows_user=r"PC\operator",
        )
        persist_successful_snapshot(
            db,
            controller_id=controller,
            vouchers=[_remote("life-1", used=1, status="EXPIRED")],
            observed_at="2026-09-12T08:00:00+00:00",
            sync_uuid="used-expired",
        )
        # A later incomplete/reset snapshot must not erase positive lifecycle facts.
        persist_successful_snapshot(
            db,
            controller_id=controller,
            vouchers=[_remote("life-1", used=0, status="VALID_MULTI")],
            observed_at="2026-09-13T08:00:00+00:00",
            sync_uuid="source-regression",
        )

        row = db.connection.execute(
            """SELECT authorized_guest_count, ever_used, expired, print_state
               FROM vouchers WHERE id=?""",
            (voucher_id,),
        ).fetchone()
        assert row["authorized_guest_count"] == 1
        assert row["ever_used"] == 1
        assert row["expired"] == 1
        assert row["print_state"] == "PRINTED"

        used = build_report_dataset(
            db,
            kind=ReportKind.USED,
            generated_at=NOW,
            controller_id=controller,
        )
        expired = build_report_dataset(
            db,
            kind=ReportKind.EXPIRED,
            generated_at=NOW,
            controller_id=controller,
        )
        assert [item.voucher_id for item in used.rows] == [voucher_id]
        assert [item.voucher_id for item in expired.rows] == [voucher_id]

        set_security_revoke_days(db, days=1, now=NOW)
        assert security_revocation_candidates(
            db,
            now=NOW,
            controller_id=controller,
        ) == ()
    finally:
        db.close()


def test_controller_discovered_voucher_becomes_normal_unprinted_work_after_alignment(
    tmp_path,
):
    db, controller = _db(tmp_path)
    try:
        persist_successful_snapshot(
            db,
            controller_id=controller,
            vouchers=[_remote("external-1", recipient="Pinco Pallino")],
            observed_at="2026-09-10T08:00:00+00:00",
            sync_uuid="discover-external",
        )
        voucher_id = int(
            db.connection.execute(
                "SELECT id FROM vouchers WHERE unifi_id='external-1'"
            ).fetchone()["id"]
        )

        align_vouchers(
            db,
            controller_id=controller,
            voucher_ids=[voucher_id],
            is_nominal=True,
            print_state=PRINT_STATE_NOT_PRINTED,
            aligned_at="2026-09-10T08:05:00+00:00",
            windows_user=r"PC\operator",
        )
        set_unprinted_warning_days(db, days=1, now=NOW)

        candidates = unprinted_warning_candidates(
            db,
            now=NOW,
            controller_id=controller,
        )
        never_printed = build_report_dataset(
            db,
            kind=ReportKind.NEVER_PRINTED,
            generated_at=NOW,
            controller_id=controller,
        )

        assert [item.voucher_id for item in candidates] == [voucher_id]
        assert [item.voucher_id for item in never_printed.rows] == [voucher_id]
        assert never_printed.rows[0].recipient == "Pinco Pallino"
        assert never_printed.rows[0].is_nominal is True
    finally:
        db.close()
