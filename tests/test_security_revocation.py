"""Regression tests for printed-unused security revocation."""

from __future__ import annotations

from voucher_management.database import Database
from voucher_management.security_revocation import (
    record_security_revocations,
    security_revocation_candidates,
    set_security_revoke_days,
)


NOW = "2026-10-01T08:00:00+00:00"


def _database(tmp_path):
    db = Database(tmp_path / "revocation.db")
    db.initialize()
    controller = db.create_controller(
        name="Reception",
        api_root="https://controller.example",
        created_at="2026-01-01T00:00:00+00:00",
    )
    return db, controller


def _voucher(db, controller, *, remote_id="v1", code="1234567890"):
    voucher_id = db.upsert_voucher(
        controller_id=controller,
        unifi_id=remote_id,
        code=code,
        name="Guest",
        created_at="2026-09-01T08:00:00+00:00",
        imported_at="2026-09-01T08:00:00+00:00",
        duration_minutes=1440,
        authorized_guest_limit=1,
        authorized_guest_count=0,
        expires_at="2026-12-01T08:00:00+00:00",
        expired=False,
        last_synced_at="2026-09-30T08:00:00+00:00",
    )
    with db.transaction() as tx:
        tx.execute(
            """UPDATE vouchers
               SET usage_observed=1, ever_used=0, present_on_controller=1,
                   last_seen_at='2026-09-30T08:00:00+00:00'
               WHERE id=?""",
            (voucher_id,),
        )
    return voucher_id


def _print(db, controller, *, code="1234567890", printed_at="2026-09-10T08:00:00+00:00"):
    db.record_print_audit(
        controller_id=controller,
        audit_id=f"job-{printed_at}",
        codes=[code],
        output_file="voucher.pdf",
        document_copies=1,
        printed_at=printed_at,
        windows_user="PC\\operator",
    )


def test_threshold_must_be_configured_explicitly(tmp_path):
    db, controller = _database(tmp_path)
    try:
        _voucher(db, controller)
        _print(db, controller)
        assert security_revocation_candidates(db, now=NOW) == ()
    finally:
        db.close()


def test_printed_unused_candidate_requires_observation_after_print(tmp_path):
    db, controller = _database(tmp_path)
    try:
        voucher_id = _voucher(db, controller)
        _print(db, controller)
        set_security_revoke_days(db, days=10, now=NOW)

        assert [item.voucher_id for item in security_revocation_candidates(db, now=NOW)] == [voucher_id]

        with db.transaction() as tx:
            tx.execute(
                "UPDATE vouchers SET last_seen_at='2026-09-05T08:00:00+00:00' WHERE id=?",
                (voucher_id,),
            )
        assert security_revocation_candidates(db, now=NOW) == ()
    finally:
        db.close()


def test_positive_historical_use_blocks_revocation(tmp_path):
    db, controller = _database(tmp_path)
    try:
        voucher_id = _voucher(db, controller)
        _print(db, controller)
        set_security_revoke_days(db, days=10, now=NOW)
        with db.transaction() as tx:
            tx.execute(
                "UPDATE vouchers SET ever_used=1, authorized_guest_count=0 WHERE id=?",
                (voucher_id,),
            )
        assert security_revocation_candidates(db, now=NOW) == ()
    finally:
        db.close()


def test_record_revocation_preserves_code_and_local_metadata(tmp_path):
    db, controller = _database(tmp_path)
    try:
        voucher_id = _voucher(db, controller)
        _print(db, controller)
        with db.transaction() as tx:
            tx.execute(
                """UPDATE vouchers
                   SET assigned_to='Desk', notes='Audit note', is_nominal=1
                   WHERE id=?""",
                (voucher_id,),
            )

        recorded = record_security_revocations(
            db,
            voucher_ids=[voucher_id],
            revoked_at=NOW,
            windows_user="PC\\operator",
        )
        assert recorded == (voucher_id,)

        row = db.connection.execute(
            """SELECT code, name, assigned_to, notes, is_nominal,
                      present_on_controller, archived_at
               FROM vouchers WHERE id=?""",
            (voucher_id,),
        ).fetchone()
        assert row["code"] == "1234567890"
        assert row["name"] == "Guest"
        assert row["assigned_to"] == "Desk"
        assert row["notes"] == "Audit note"
        assert row["is_nominal"] == 1
        assert row["present_on_controller"] == 0
        assert row["archived_at"] is None

        event = db.connection.execute(
            """SELECT event_type, details_json
               FROM voucher_events WHERE voucher_id=?""",
            (voucher_id,),
        ).fetchone()
        assert event["event_type"] == "SECURITY_REVOKED"
        assert "credential_preserved_locally" in event["details_json"]
    finally:
        db.close()


def test_record_revocation_is_idempotent(tmp_path):
    db, controller = _database(tmp_path)
    try:
        voucher_id = _voucher(db, controller)
        first = record_security_revocations(
            db,
            voucher_ids=[voucher_id],
            revoked_at=NOW,
            windows_user="PC\\operator",
        )
        second = record_security_revocations(
            db,
            voucher_ids=[voucher_id],
            revoked_at=NOW,
            windows_user="PC\\operator",
        )
        assert first == (voucher_id,)
        assert second == (voucher_id,)
        count = db.connection.execute(
            """SELECT COUNT(*) FROM voucher_events
               WHERE voucher_id=? AND event_type='SECURITY_REVOKED'""",
            (voucher_id,),
        ).fetchone()[0]
        assert count == 1
    finally:
        db.close()
