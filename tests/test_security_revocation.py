"""Regression tests for printed-unused security revocation."""

from __future__ import annotations

from voucher_management.database import Database
from types import SimpleNamespace

from voucher_management.security_revocation import (
    live_security_revocation_allowed,
    pending_security_revocation_ids,
    reconcile_pending_security_revocations,
    record_security_revocation_request,
    record_security_revocations,
    revoke_security_candidates_live,
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



def test_live_check_requires_same_uuid_nonexpired_and_zero_usage(tmp_path):
    db, controller = _database(tmp_path)
    try:
        voucher_id = _voucher(db, controller)
        _print(db, controller)
        set_security_revoke_days(db, days=10, now=NOW)
        candidate = security_revocation_candidates(db, now=NOW)[0]

        assert live_security_revocation_allowed(
            candidate,
            SimpleNamespace(id="v1", status="VALID_MULTI", used=0),
        )
        assert not live_security_revocation_allowed(
            candidate,
            SimpleNamespace(id="other", status="VALID_MULTI", used=0),
        )
        assert not live_security_revocation_allowed(
            candidate,
            SimpleNamespace(id="v1", status="EXPIRED", used=0),
        )
        assert not live_security_revocation_allowed(
            candidate,
            SimpleNamespace(id="v1", status="VALID_MULTI", used=1),
        )
        assert voucher_id == candidate.voucher_id
    finally:
        db.close()


def test_request_marker_is_durable_and_promoted_on_confirmation(tmp_path):
    db, controller = _database(tmp_path)
    try:
        voucher_id = _voucher(db, controller)
        assert record_security_revocation_request(
            db,
            voucher_id=voucher_id,
            requested_at=NOW,
            windows_user=r"PC\operator",
        )
        assert pending_security_revocation_ids(db) == (voucher_id,)

        recorded = record_security_revocations(
            db,
            voucher_ids=[voucher_id],
            revoked_at=NOW,
            windows_user=r"PC\operator",
        )
        assert recorded == (voucher_id,)
        assert pending_security_revocation_ids(db) == ()
        events = db.connection.execute(
            """SELECT event_type, details_json
               FROM voucher_events WHERE voucher_id=?""",
            (voucher_id,),
        ).fetchall()
        assert [row["event_type"] for row in events] == ["SECURITY_REVOKED"]
        assert '"remote_delete_confirmed":true' in events[0]["details_json"]
        assert '"credential_preserved_locally":true' in events[0]["details_json"]
    finally:
        db.close()


def test_live_batch_fresh_reads_before_delete_and_keeps_local_history(tmp_path):
    db, controller = _database(tmp_path)
    try:
        voucher_id = _voucher(db, controller)
        _print(db, controller)
        set_security_revoke_days(db, days=10, now=NOW)
        candidate = security_revocation_candidates(db, now=NOW)[0]

        calls = []
        client = SimpleNamespace(
            get_voucher=lambda remote_id: (
                calls.append(("get", remote_id))
                or SimpleNamespace(id=remote_id, status="VALID_MULTI", used=0)
            ),
            delete_vouchers=lambda ids: calls.append(("delete", tuple(ids))),
        )
        result = revoke_security_candidates_live(
            db,
            client=client,
            candidates=[candidate],
            revoked_at=NOW,
            windows_user=r"PC\operator",
        )

        assert result.revoked_ids == (voucher_id,)
        assert result.skipped_ids == ()
        assert result.failed_ids == ()
        assert result.local_persistence_failed_ids == ()
        assert calls == [("get", "v1"), ("delete", ("v1",))]
        row = db.connection.execute(
            """SELECT code, name, present_on_controller
               FROM vouchers WHERE id=?""",
            (voucher_id,),
        ).fetchone()
        assert row["code"] == "1234567890"
        assert row["name"] == "Guest"
        assert row["present_on_controller"] == 0
    finally:
        db.close()


def test_live_batch_skips_candidate_that_became_used(tmp_path):
    db, controller = _database(tmp_path)
    try:
        voucher_id = _voucher(db, controller)
        _print(db, controller)
        set_security_revoke_days(db, days=10, now=NOW)
        candidate = security_revocation_candidates(db, now=NOW)[0]
        deleted = []
        client = SimpleNamespace(
            get_voucher=lambda remote_id: SimpleNamespace(
                id=remote_id,
                status="USED_MULTIPLE",
                used=1,
            ),
            delete_vouchers=lambda ids: deleted.append(tuple(ids)),
        )

        result = revoke_security_candidates_live(
            db,
            client=client,
            candidates=[candidate],
            revoked_at=NOW,
            windows_user="operator",
        )

        assert result.revoked_ids == ()
        assert result.skipped_ids == (voucher_id,)
        assert deleted == []
        assert pending_security_revocation_ids(db) == ()
    finally:
        db.close()


def test_delete_failure_leaves_durable_pending_marker_for_reconciliation(tmp_path):
    db, controller = _database(tmp_path)
    try:
        voucher_id = _voucher(db, controller)
        _print(db, controller)
        set_security_revoke_days(db, days=10, now=NOW)
        candidate = security_revocation_candidates(db, now=NOW)[0]

        def fail_delete(_ids):
            raise RuntimeError("transport failed after mutation boundary")

        client = SimpleNamespace(
            get_voucher=lambda remote_id: SimpleNamespace(
                id=remote_id,
                status="VALID_MULTI",
                used=0,
            ),
            delete_vouchers=fail_delete,
        )
        result = revoke_security_candidates_live(
            db,
            client=client,
            candidates=[candidate],
            revoked_at=NOW,
            windows_user="operator",
        )

        assert result.revoked_ids == ()
        assert result.failed_ids == (voucher_id,)
        assert pending_security_revocation_ids(db) == (voucher_id,)
        row = db.connection.execute(
            "SELECT present_on_controller FROM vouchers WHERE id=?",
            (voucher_id,),
        ).fetchone()
        assert row["present_on_controller"] == 1
    finally:
        db.close()



def test_pending_request_is_not_offered_again_until_reconciled(tmp_path):
    db, controller = _database(tmp_path)
    try:
        voucher_id = _voucher(db, controller)
        _print(db, controller)
        set_security_revoke_days(db, days=10, now=NOW)
        assert [item.voucher_id for item in security_revocation_candidates(
            db,
            now=NOW,
        )] == [voucher_id]

        record_security_revocation_request(
            db,
            voucher_id=voucher_id,
            requested_at=NOW,
            windows_user="operator",
        )

        assert security_revocation_candidates(db, now=NOW) == ()
        assert pending_security_revocation_ids(db) == (voucher_id,)
    finally:
        db.close()


def test_fresh_full_snapshot_reconciles_absent_pending_revocation(tmp_path):
    db, controller = _database(tmp_path)
    try:
        voucher_id = _voucher(db, controller)
        record_security_revocation_request(
            db,
            voucher_id=voucher_id,
            requested_at=NOW,
            windows_user="operator",
        )

        confirmed = reconcile_pending_security_revocations(
            db,
            controller_id=controller,
            live_voucher_ids=frozenset(),
            observed_at=NOW,
            windows_user="operator",
        )

        assert confirmed == (voucher_id,)
        assert pending_security_revocation_ids(db) == ()
        row = db.connection.execute(
            "SELECT code, present_on_controller FROM vouchers WHERE id=?",
            (voucher_id,),
        ).fetchone()
        assert row["code"] == "1234567890"
        assert row["present_on_controller"] == 0
    finally:
        db.close()


def test_fresh_full_snapshot_closes_pending_when_voucher_still_exists(tmp_path):
    db, controller = _database(tmp_path)
    try:
        voucher_id = _voucher(db, controller)
        record_security_revocation_request(
            db,
            voucher_id=voucher_id,
            requested_at=NOW,
            windows_user="operator",
        )

        confirmed = reconcile_pending_security_revocations(
            db,
            controller_id=controller,
            live_voucher_ids=frozenset({"v1"}),
            observed_at=NOW,
            windows_user="operator",
        )

        assert confirmed == ()
        assert pending_security_revocation_ids(db) == ()
        event = db.connection.execute(
            """SELECT event_type, details_json
               FROM voucher_events WHERE voucher_id=?""",
            (voucher_id,),
        ).fetchone()
        assert event["event_type"] == "SECURITY_REVOKE_NOT_APPLIED"
        assert '"fresh_snapshot_confirmed_present":true' in event["details_json"]
    finally:
        db.close()
