"""Tests for durable reconciliation of confirmed create reporting facts."""

from __future__ import annotations

import json

import pytest

from voucher_management.create_reporting_recovery import (
    CreateReportingRecoveryError,
    load_pending_create_reporting,
    reconcile_pending_create_reporting,
    write_pending_create_reporting,
)
from voucher_management.database import Database


def _database(tmp_path):
    db = Database(tmp_path / "voucher_management.db")
    db.initialize()
    controller = db.create_controller(
        name="Reception",
        api_root="https://controller.example",
        created_at="2026-09-29T08:00:00+00:00",
    )
    return db, controller


def test_marker_contains_only_privacy_safe_reconciliation_facts(tmp_path):
    marker = tmp_path / "pending_create_reporting.json"

    write_pending_create_reporting(
        marker,
        controller_id=7,
        voucher_ids=["uuid-1", "uuid-2"],
        is_nominal=True,
        confirmed_at="2026-09-29T08:05:00+00:00",
    )

    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert payload == {
        "format": 1,
        "controller_id": 7,
        "voucher_ids": ["uuid-1", "uuid-2"],
        "is_nominal": True,
        "confirmed_at": "2026-09-29T08:05:00+00:00",
    }
    serialized = marker.read_text(encoding="utf-8")
    assert "api" not in serialized.lower()
    assert "recipient" not in serialized.lower()
    assert "code" not in serialized.lower()


def test_marker_is_kept_until_all_confirmed_rows_exist(tmp_path):
    db, controller = _database(tmp_path)
    marker = tmp_path / "pending_create_reporting.json"
    try:
        write_pending_create_reporting(
            marker,
            controller_id=controller,
            voucher_ids=["created-1"],
            is_nominal=True,
            confirmed_at="2026-09-29T08:05:00+00:00",
        )

        assert reconcile_pending_create_reporting(
            db,
            marker,
            controller_id=controller,
        ) is False
        assert marker.exists()

        db.upsert_voucher(
            controller_id=controller,
            unifi_id="created-1",
            code="1234567890",
            imported_at="2026-09-29T08:06:00+00:00",
            last_synced_at="2026-09-29T08:06:00+00:00",
        )

        assert reconcile_pending_create_reporting(
            db,
            marker,
            controller_id=controller,
        ) is True
        assert not marker.exists()
        row = db.connection.execute(
            "SELECT origin, is_nominal FROM vouchers WHERE unifi_id='created-1'"
        ).fetchone()
        assert row["origin"] == "APPLICATION"
        assert row["is_nominal"] == 1
    finally:
        db.close()


def test_marker_for_other_controller_is_not_consumed(tmp_path):
    db, controller = _database(tmp_path)
    marker = tmp_path / "pending_create_reporting.json"
    try:
        write_pending_create_reporting(
            marker,
            controller_id=controller,
            voucher_ids=["created-1"],
            is_nominal=False,
            confirmed_at="2026-09-29T08:05:00+00:00",
        )
        assert reconcile_pending_create_reporting(
            db,
            marker,
            controller_id=controller + 1,
        ) is False
        assert marker.exists()
    finally:
        db.close()


def test_invalid_marker_fails_closed(tmp_path):
    marker = tmp_path / "pending_create_reporting.json"
    marker.write_text('{"format":1,"controller_id":7}', encoding="utf-8")

    with pytest.raises(CreateReportingRecoveryError):
        load_pending_create_reporting(marker)
