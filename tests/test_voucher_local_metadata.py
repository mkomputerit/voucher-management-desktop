"""Regression tests for controller-read-only/local-only voucher enrichment."""

from datetime import datetime, timezone

import pytest

from voucher_management.database import Database
from voucher_management.voucher_local_metadata_ui import (
    classification_label,
    classification_value,
    local_metadata_summary,
)


NOW = "2026-09-29T15:30:00+00:00"


def _database(tmp_path):
    database = Database(tmp_path / "voucher-management.db")
    database.initialize()
    controller_id = database.create_controller(
        name="UniFi Test",
        api_root="https://unifi.example/proxy/network/integration/v1",
        created_at=NOW,
    )
    database.upsert_voucher(
        controller_id=controller_id,
        unifi_id="voucher-1",
        code="1234567890",
        name="EMI06",
        imported_at=NOW,
        last_synced_at=NOW,
        created_at="2026-09-29T12:00:00+00:00",
        duration_minutes=1440,
        authorized_guest_limit=1,
        authorized_guest_count=0,
        expires_at="2026-09-30T12:00:00+00:00",
    )
    return database, controller_id


def test_local_metadata_update_never_changes_controller_fields(tmp_path):
    database, controller_id = _database(tmp_path)
    before = dict(
        database.connection.execute(
            "SELECT * FROM vouchers WHERE controller_id=? AND unifi_id=?",
            (controller_id, "voucher-1"),
        ).fetchone()
    )

    metadata = database.update_voucher_local_metadata(
        controller_id=controller_id,
        unifi_id="voucher-1",
        assigned_to="Pinco Pallino",
        notes="Consegnato alla reception",
        is_nominal=True,
        updated_at=NOW,
        windows_user="TEST\\operator",
    )

    after = dict(
        database.connection.execute(
            "SELECT * FROM vouchers WHERE controller_id=? AND unifi_id=?",
            (controller_id, "voucher-1"),
        ).fetchone()
    )
    for field in (
        "unifi_id",
        "code",
        "name",
        "created_at",
        "duration_minutes",
        "authorized_guest_limit",
        "authorized_guest_count",
        "expires_at",
        "origin",
    ):
        assert after[field] == before[field]

    assert metadata.assigned_to == "Pinco Pallino"
    assert metadata.notes == "Consegnato alla reception"
    assert metadata.is_nominal is True
    assert metadata.controller_description == "EMI06"

    event = database.connection.execute(
        """SELECT event_type, source, windows_user, details_json
           FROM voucher_events ORDER BY id DESC LIMIT 1"""
    ).fetchone()
    assert event["event_type"] == "LOCAL_METADATA_UPDATED"
    assert event["source"] == "OPERATOR"
    assert event["windows_user"] == "TEST\\operator"
    assert "Pinco Pallino" not in event["details_json"]
    assert "reception" not in event["details_json"]
    database.close()


def test_controller_resync_preserves_local_metadata(tmp_path):
    database, controller_id = _database(tmp_path)
    database.update_voucher_local_metadata(
        controller_id=controller_id,
        unifi_id="voucher-1",
        assigned_to="Mario Rossi",
        notes="Nota locale",
        is_nominal=False,
        updated_at=NOW,
        windows_user="operator",
    )

    # Simulate a later controller snapshot changing controller-owned facts.
    database.upsert_voucher(
        controller_id=controller_id,
        unifi_id="voucher-1",
        code="1234567890",
        name="EMI06 aggiornato su UniFi",
        imported_at=NOW,
        last_synced_at="2026-09-29T16:00:00+00:00",
        created_at="2026-09-29T12:00:00+00:00",
        duration_minutes=1440,
        authorized_guest_limit=1,
        authorized_guest_count=1,
        expires_at="2026-09-30T12:00:00+00:00",
    )

    metadata = database.voucher_local_metadata(
        controller_id=controller_id,
        unifi_id="voucher-1",
    )
    assert metadata is not None
    assert metadata.controller_description == "EMI06 aggiornato su UniFi"
    assert metadata.assigned_to == "Mario Rossi"
    assert metadata.notes == "Nota locale"
    assert metadata.is_nominal is False
    database.close()


def test_local_metadata_can_be_reviewed_back_to_unclassified(tmp_path):
    database, controller_id = _database(tmp_path)
    with database.transaction() as connection:
        connection.execute(
            """UPDATE vouchers
               SET nominality_redacted=1, is_nominal=NULL
               WHERE controller_id=? AND unifi_id=?""",
            (controller_id, "voucher-1"),
        )

    metadata = database.update_voucher_local_metadata(
        controller_id=controller_id,
        unifi_id="voucher-1",
        assigned_to="",
        notes="",
        is_nominal=None,
        updated_at=NOW,
        windows_user="operator",
    )
    assert metadata.is_nominal is None
    assert metadata.nominality_redacted is False
    database.close()


def test_local_metadata_refuses_absent_controller_voucher(tmp_path):
    database, controller_id = _database(tmp_path)
    with database.transaction() as connection:
        connection.execute(
            """UPDATE vouchers SET present_on_controller=0
               WHERE controller_id=? AND unifi_id=?""",
            (controller_id, "voucher-1"),
        )

    with pytest.raises(RuntimeError, match="non è più attivo"):
        database.update_voucher_local_metadata(
            controller_id=controller_id,
            unifi_id="voucher-1",
            assigned_to="Mario Rossi",
            notes="",
            is_nominal=True,
            updated_at=NOW,
            windows_user="operator",
        )
    database.close()


def test_local_metadata_labels_are_explicit_and_searchable(tmp_path):
    database, controller_id = _database(tmp_path)
    metadata = database.update_voucher_local_metadata(
        controller_id=controller_id,
        unifi_id="voucher-1",
        assigned_to="Pinco Pallino",
        notes="",
        is_nominal=True,
        updated_at=datetime.now(timezone.utc).isoformat(),
        windows_user="operator",
    )
    assert classification_label(True) == "Nominale"
    assert classification_label(False) == "Non nominale"
    assert classification_label(None) == "Non classificato"
    assert classification_label(None, redacted=True) == "Rimossa per privacy"
    assert classification_value("Nominale") is True
    assert classification_value("Non nominale") is False
    assert classification_value("Non classificato") is None
    assert local_metadata_summary(metadata) == "Pinco Pallino · Nominale"

    metadata_map = database.voucher_local_metadata_map(
        controller_id=controller_id,
    )
    assert metadata_map["voucher-1"].assigned_to == "Pinco Pallino"
    database.close()
