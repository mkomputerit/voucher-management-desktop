from __future__ import annotations

from voucher_management.database import (
    Database,
    PRINT_STATE_NOT_PRINTED,
    PRINT_STATE_PRINTED,
    PRINT_STATE_UNKNOWN,
)
from voucher_management.operational_alerts import (
    set_unprinted_warning_days,
    unprinted_warning_candidates,
    unprinted_warning_days,
)


NOW = "2026-10-01T12:00:00+00:00"


def _db(tmp_path):
    db = Database(tmp_path / "operational-alerts.sqlite")
    db.initialize()
    controller = db.create_controller(
        name="Reception",
        api_root="https://controller.example",
        created_at="2026-01-01T00:00:00+00:00",
    )
    return db, controller


def _voucher(
    db,
    controller,
    remote_id,
    *,
    created_at="2026-09-20T08:00:00+00:00",
    print_state=PRINT_STATE_NOT_PRINTED,
    aligned=True,
    used=0,
    origin="CONTROLLER",
):
    voucher_id = db.upsert_voucher(
        controller_id=controller,
        unifi_id=remote_id,
        code=f"CODE-{remote_id}",
        name=f"Guest {remote_id}",
        created_at=created_at,
        imported_at=NOW,
        duration_minutes=60,
        authorized_guest_limit=1,
        authorized_guest_count=used,
        expired=False,
        last_synced_at=NOW,
    )
    with db.transaction() as tx:
        tx.execute(
            """UPDATE vouchers
               SET print_state=?,
                   alignment_completed_at=?,
                   origin=?,
                   usage_observed=1,
                   ever_used=?,
                   present_on_controller=1,
                   last_seen_at=?
               WHERE id=?""",
            (
                print_state,
                NOW if aligned else None,
                origin,
                int(used > 0),
                NOW,
                voucher_id,
            ),
        )
    return voucher_id


def test_unprinted_warning_threshold_must_be_explicit(tmp_path):
    db, controller = _db(tmp_path)
    try:
        _voucher(db, controller, "old")
        assert unprinted_warning_days(db) is None
        assert unprinted_warning_candidates(
            db,
            now=NOW,
            controller_id=controller,
        ) == ()
    finally:
        db.close()


def test_old_aligned_unprinted_live_voucher_is_candidate_from_creation_date(tmp_path):
    db, controller = _db(tmp_path)
    try:
        old_id = _voucher(
            db,
            controller,
            "old",
            created_at="2026-09-20T08:00:00+00:00",
            origin="APPLICATION",
        )
        _voucher(
            db,
            controller,
            "recent",
            created_at="2026-09-30T08:00:00+00:00",
            origin="APPLICATION",
        )
        set_unprinted_warning_days(db, days=7, now=NOW)

        candidates = unprinted_warning_candidates(
            db,
            now=NOW,
            controller_id=controller,
        )

        assert [item.voucher_id for item in candidates] == [old_id]
        assert candidates[0].created_at == "2026-09-20T08:00:00+00:00"
    finally:
        db.close()


def test_imported_controller_voucher_can_join_same_alert_after_operator_alignment(tmp_path):
    db, controller = _db(tmp_path)
    try:
        imported_id = _voucher(
            db,
            controller,
            "external",
            created_at="2026-09-01T08:00:00+00:00",
            print_state=PRINT_STATE_NOT_PRINTED,
            aligned=True,
            origin="CONTROLLER",
        )
        set_unprinted_warning_days(db, days=7, now=NOW)

        candidates = unprinted_warning_candidates(
            db,
            now=NOW,
            controller_id=controller,
        )

        assert [item.voucher_id for item in candidates] == [imported_id]
        assert candidates[0].origin == "CONTROLLER"
    finally:
        db.close()


def test_unknown_or_unaligned_print_history_is_not_guessed_as_unprinted(tmp_path):
    db, controller = _db(tmp_path)
    try:
        _voucher(
            db,
            controller,
            "unknown",
            print_state=PRINT_STATE_UNKNOWN,
            aligned=True,
        )
        _voucher(
            db,
            controller,
            "not-aligned",
            print_state=PRINT_STATE_NOT_PRINTED,
            aligned=False,
        )
        set_unprinted_warning_days(db, days=1, now=NOW)

        assert unprinted_warning_candidates(
            db,
            now=NOW,
            controller_id=controller,
        ) == ()
    finally:
        db.close()


def test_used_or_printed_voucher_is_not_an_operational_unprinted_candidate(tmp_path):
    db, controller = _db(tmp_path)
    try:
        _voucher(
            db,
            controller,
            "used",
            print_state=PRINT_STATE_NOT_PRINTED,
            aligned=True,
            used=1,
        )
        printed_id = _voucher(
            db,
            controller,
            "printed",
            print_state=PRINT_STATE_PRINTED,
            aligned=True,
        )
        db.record_print_audit(
            controller_id=controller,
            audit_id="printed-alert-test",
            codes=["CODE-printed"],
            output_file="voucher.pdf",
            document_copies=1,
            printed_at="2026-09-21T08:00:00+00:00",
            windows_user="operator",
        )
        assert printed_id
        set_unprinted_warning_days(db, days=1, now=NOW)

        assert unprinted_warning_candidates(
            db,
            now=NOW,
            controller_id=controller,
        ) == ()
    finally:
        db.close()


def test_verified_print_evidence_wins_even_if_print_state_is_corrupted(tmp_path):
    db, controller = _db(tmp_path)
    try:
        voucher_id = _voucher(
            db,
            controller,
            "printed",
            print_state=PRINT_STATE_NOT_PRINTED,
            aligned=True,
        )
        db.record_print_audit(
            controller_id=controller,
            audit_id="verified-print-alert",
            codes=["CODE-printed"],
            output_file="voucher.pdf",
            document_copies=1,
            printed_at="2026-09-21T08:00:00+00:00",
            windows_user="operator",
        )
        with db.transaction() as tx:
            tx.execute(
                "UPDATE vouchers SET print_state=? WHERE id=?",
                (PRINT_STATE_NOT_PRINTED, voucher_id),
            )
        set_unprinted_warning_days(db, days=1, now=NOW)

        assert unprinted_warning_candidates(
            db,
            now=NOW,
            controller_id=controller,
        ) == ()
    finally:
        db.close()
