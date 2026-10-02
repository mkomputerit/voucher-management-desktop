"""Regression tests for operator-confirmed uncertain create recovery."""

from __future__ import annotations

import json

from voucher_management.database import Database, PRINT_STATE_NOT_PRINTED
from voucher_management.sync_store import persist_successful_snapshot
from voucher_management.uncertain_create_recovery import (
    PendingCreateIntent,
    confirm_pending_create_intent_to_path,
    load_pending_create_intent,
    match_pending_create_intent,
    reject_pending_create_intent_to_path,
    write_pending_create_intent,
)
from voucher_management.unifi_api import ApiVoucher


NOW = "2026-10-02T10:00:00+00:00"


def _voucher(
    remote_id: str,
    *,
    name: str = "Mario Rossi",
    duration: int = 1440,
    quota: int = 1,
    data_mb=None,
    down_kbps=None,
    up_kbps=None,
):
    return ApiVoucher(
        id=remote_id,
        code=f"CODE-{remote_id}",
        recipient=name,
        duration_minutes=duration,
        create_time=1_759_400_000,
        quota=quota,
        used=0,
        status="VALID_MULTI",
        start_time=0,
        end_time=0,
        data_mb=data_mb,
        down_kbps=down_kbps,
        up_kbps=up_kbps,
    )


def _pending(*, quantity=2):
    return PendingCreateIntent(
        controller_id=7,
        site_id="site-a",
        requested_at=NOW,
        baseline_ids=("old-1",),
        quantity=quantity,
        recipient_digest="a" * 64,
        duration_minutes=1440,
        quota=1,
        data_mb=None,
        down_kbps=None,
        up_kbps=None,
        is_nominal=True,
    )


def _digest(name: str) -> str:
    return "a" * 64 if name == "Mario Rossi" else "b" * 64


def test_marker_keeps_recipient_and_codes_out_of_plaintext(tmp_path):
    marker = tmp_path / "pending_create_intent.json"
    write_pending_create_intent(
        marker,
        controller_id=7,
        site_id="site-a",
        requested_at=NOW,
        baseline_ids=["old-1", "old-2"],
        quantity=2,
        recipient_digest="a" * 64,
        duration_minutes=1440,
        quota=1,
        data_mb=None,
        down_kbps=50_000,
        up_kbps=None,
        is_nominal=True,
    )

    pending = load_pending_create_intent(marker)
    assert pending is not None
    assert pending.quantity == 2
    assert pending.site_id == "site-a"

    raw = marker.read_text(encoding="utf-8")
    assert "Mario Rossi" not in raw
    assert "CODE-" not in raw
    assert "api_key" not in raw.lower()
    payload = json.loads(raw)
    assert payload["recipient_digest"] == "a" * 64


def test_exact_match_is_candidate_only_and_never_auto_association():
    match = match_pending_create_intent(
        _pending(quantity=2),
        [
            _voucher("old-1"),
            _voucher("new-1"),
            _voucher("new-2"),
            _voucher("new-other", name="Other"),
        ],
        recipient_digest=_digest,
    )

    assert match.exact is True
    assert match.compatible_ids == ("new-1", "new-2")
    assert set(match.new_ids) == {"new-1", "new-2", "new-other"}


def test_more_compatible_rows_is_ambiguous_not_exact():
    match = match_pending_create_intent(
        _pending(quantity=2),
        [
            _voucher("new-1"),
            _voucher("new-2"),
            _voucher("new-3"),
        ],
        recipient_digest=_digest,
    )

    assert match.exact is False
    assert match.status == "ambiguous"
    assert len(match.compatible_ids) == 3


def _db(tmp_path):
    path = tmp_path / "voucher_management.db"
    db = Database(path)
    db.initialize()
    controller = db.create_controller(
        name="Reception",
        api_root="https://controller.example",
        site_id="site-a",
        created_at=NOW,
    )
    persist_successful_snapshot(
        db,
        controller_id=controller,
        vouchers=[_voucher("new-1"), _voucher("new-2")],
        observed_at=NOW,
        sync_uuid="uncertain-create-snapshot",
    )
    return db, controller, path


def test_operator_confirmation_marks_candidates_application_created(tmp_path):
    db, controller, database_path = _db(tmp_path)
    marker = tmp_path / "pending_create_intent.json"
    try:
        write_pending_create_intent(
            marker,
            controller_id=controller,
            site_id="site-a",
            requested_at=NOW,
            baseline_ids=["old-1"],
            quantity=2,
            recipient_digest="a" * 64,
            duration_minutes=1440,
            quota=1,
            data_mb=None,
            down_kbps=None,
            up_kbps=None,
            is_nominal=True,
        )

        local_ids = confirm_pending_create_intent_to_path(
            database_path,
            marker,
            controller_id=controller,
            candidate_ids=["new-1", "new-2"],
            confirmed_at="2026-10-02T10:05:00+00:00",
            windows_user=r"PC\operator",
        )

        assert len(local_ids) == 2
        assert not marker.exists()
        rows = db.connection.execute(
            """SELECT origin, is_nominal, print_state, alignment_completed_at
               FROM vouchers ORDER BY unifi_id"""
        ).fetchall()
        assert all(row["origin"] == "APPLICATION" for row in rows)
        assert all(row["is_nominal"] == 1 for row in rows)
        assert all(row["print_state"] == PRINT_STATE_NOT_PRINTED for row in rows)
        events = db.connection.execute(
            """SELECT event_type, source, windows_user
               FROM voucher_events ORDER BY id"""
        ).fetchall()
        assert [row["event_type"] for row in events] == [
            "UNCERTAIN_CREATE_ASSOCIATED",
            "UNCERTAIN_CREATE_ASSOCIATED",
        ]
        assert all(row["source"] == "OPERATOR" for row in events)
    finally:
        db.close()


def test_operator_rejection_leaves_candidates_controller_owned(tmp_path):
    db, controller, database_path = _db(tmp_path)
    marker = tmp_path / "pending_create_intent.json"
    try:
        write_pending_create_intent(
            marker,
            controller_id=controller,
            site_id="site-a",
            requested_at=NOW,
            baseline_ids=["old-1"],
            quantity=2,
            recipient_digest="a" * 64,
            duration_minutes=1440,
            quota=1,
            data_mb=None,
            down_kbps=None,
            up_kbps=None,
            is_nominal=True,
        )

        reject_pending_create_intent_to_path(
            database_path,
            marker,
            controller_id=controller,
            candidate_ids=["new-1", "new-2"],
            rejected_at="2026-10-02T10:06:00+00:00",
            windows_user=r"PC\operator",
        )

        assert not marker.exists()
        rows = db.connection.execute(
            "SELECT origin, is_nominal FROM vouchers ORDER BY unifi_id"
        ).fetchall()
        assert all(row["origin"] == "CONTROLLER" for row in rows)
        assert all(row["is_nominal"] is None for row in rows)
        events = db.connection.execute(
            """SELECT event_type FROM voucher_events ORDER BY id"""
        ).fetchall()
        assert [row["event_type"] for row in events] == [
            "UNCERTAIN_CREATE_ASSOCIATION_REJECTED",
            "UNCERTAIN_CREATE_ASSOCIATION_REJECTED",
        ]
    finally:
        db.close()
