"""Regression tests for controller-read-only/local-only voucher enrichment."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from voucher_management.database import Database
from voucher_management import voucher_local_metadata_ui as metadata_ui
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


def test_application_nominal_creation_preserves_name_as_verified_local_recipient(tmp_path):
    database, controller_id = _database(tmp_path)
    database.upsert_voucher(
        controller_id=controller_id,
        unifi_id="nominal-created",
        code="9999900000",
        name="Pinco Pallino",
        imported_at=NOW,
        last_synced_at=NOW,
        created_at=NOW,
        duration_minutes=60,
        authorized_guest_limit=1,
    )
    database.mark_application_created_vouchers(
        controller_id=controller_id,
        unifi_ids=["nominal-created"],
        is_nominal=True,
    )
    metadata = database.voucher_local_metadata(
        controller_id=controller_id,
        unifi_id="nominal-created",
    )
    assert metadata is not None
    assert metadata.origin == "APPLICATION"
    assert metadata.is_nominal is True
    assert metadata.controller_description == "Pinco Pallino"
    assert metadata.assigned_to == "Pinco Pallino"
    database.close()


def test_application_non_nominal_creation_does_not_invent_local_recipient(tmp_path):
    database, controller_id = _database(tmp_path)
    database.mark_application_created_vouchers(
        controller_id=controller_id,
        unifi_ids=["voucher-1"],
        is_nominal=False,
    )
    metadata = database.voucher_local_metadata(
        controller_id=controller_id,
        unifi_id="voucher-1",
    )
    assert metadata is not None
    assert metadata.controller_description == "EMI06"
    assert metadata.assigned_to == ""
    assert metadata.is_nominal is False
    database.close()


def test_nominal_local_metadata_requires_recipient(tmp_path):
    database, controller_id = _database(tmp_path)
    try:
        with pytest.raises(ValueError, match="richiede un destinatario locale"):
            database.update_voucher_local_metadata(
                controller_id=controller_id,
                unifi_id="voucher-1",
                assigned_to="",
                notes="",
                is_nominal=True,
                updated_at=NOW,
                windows_user="operator",
            )
        metadata = database.voucher_local_metadata(
            controller_id=controller_id,
            unifi_id="voucher-1",
        )
        assert metadata is not None
        assert metadata.is_nominal is None
        assert metadata.assigned_to == ""
    finally:
        database.close()


def test_metadata_map_omits_notes_unless_search_explicitly_requests_them(tmp_path):
    database, controller_id = _database(tmp_path)
    database.update_voucher_local_metadata(
        controller_id=controller_id,
        unifi_id="voucher-1",
        assigned_to="Pinco Pallino",
        notes="Nota riservata ricercabile",
        is_nominal=True,
        updated_at=NOW,
        windows_user="operator",
    )
    try:
        ordinary = database.voucher_local_metadata_map(
            controller_id=controller_id,
        )
        searched = database.voucher_local_metadata_map(
            controller_id=controller_id,
            include_notes=True,
        )
        assert ordinary["voucher-1"].notes == ""
        assert ordinary["voucher-1"].controller_description == ""
        assert searched["voucher-1"].notes == "Nota riservata ricercabile"
        assert searched["voucher-1"].controller_description == ""
    finally:
        database.close()


def test_successful_local_metadata_update_deselects_only_handled_voucher(
    monkeypatch,
    tmp_path,
):
    database, controller_id = _database(tmp_path)
    try:
        voucher = SimpleNamespace(id="voucher-1")
        other = SimpleNamespace(id="voucher-2")
        database.upsert_voucher(
            controller_id=controller_id,
            unifi_id="voucher-2",
            code="2222233333",
            name="OTHER",
            imported_at=NOW,
            last_synced_at=NOW,
        )
        refreshed = []
        populated = []
        fake = SimpleNamespace(
            _background_results=None,
            active_controller_id=controller_id,
            controller_snapshot_live=True,
            tree=SimpleNamespace(focus=lambda: "row-1"),
            by_iid={"row-1": voucher},
            vouchers=[voucher, other],
            checked_ids={"voucher-1"},
            database=database,
            wait_window=lambda dialog: None,
            _windows_operator_identity=lambda: "TEST\\operator",
            _voucher_for_local_metadata=lambda: voucher,
            populate=lambda: populated.append(True),
            _refresh_report_summary=lambda: refreshed.append(True),
        )

        class Dialog:
            def __init__(self, *args, **kwargs):
                self.result = ("Mario Rossi", "Nota locale", True)

        monkeypatch.setattr(metadata_ui, "VoucherLocalMetadataDialog", Dialog)
        monkeypatch.setattr(
            metadata_ui.messagebox,
            "showinfo",
            lambda *args, **kwargs: None,
        )
        monkeypatch.setattr(
            metadata_ui.messagebox,
            "showerror",
            lambda *args, **kwargs: None,
        )
        monkeypatch.setattr(
            metadata_ui.messagebox,
            "showwarning",
            lambda *args, **kwargs: None,
        )

        metadata_ui.VoucherLocalMetadataMixin.edit_local_voucher_metadata(fake)

        assert fake.checked_ids == set()
        assert populated == [True]
        assert refreshed == [True]
        saved = database.voucher_local_metadata(
            controller_id=controller_id,
            unifi_id="voucher-1",
        )
        assert saved is not None
        assert saved.is_nominal is True
        assert saved.assigned_to == "Mario Rossi"
    finally:
        database.close()


def test_batch_classification_preserves_recipient_and_notes_atomically(tmp_path):
    database, controller_id = _database(tmp_path)
    try:
        database.upsert_voucher(
            controller_id=controller_id,
            unifi_id="voucher-2",
            code="2222233333",
            name="OTHER",
            imported_at=NOW,
            last_synced_at=NOW,
        )
        database.update_voucher_local_metadata(
            controller_id=controller_id,
            unifi_id="voucher-1",
            assigned_to="Mario Rossi",
            notes="Nota uno",
            is_nominal=False,
            updated_at=NOW,
            windows_user="operator",
        )
        database.update_voucher_local_metadata(
            controller_id=controller_id,
            unifi_id="voucher-2",
            assigned_to="Luigi Bianchi",
            notes="Nota due",
            is_nominal=False,
            updated_at=NOW,
            windows_user="operator",
        )

        updated = database.update_voucher_local_classification_batch(
            controller_id=controller_id,
            unifi_ids=["voucher-1", "voucher-2"],
            is_nominal=True,
            updated_at="2026-09-29T16:00:00+00:00",
            windows_user=r"PC\operator",
        )

        assert [item.unifi_id for item in updated] == ["voucher-1", "voucher-2"]
        metadata = {
            remote_id: database.voucher_local_metadata(
                controller_id=controller_id,
                unifi_id=remote_id,
            )
            for remote_id in ("voucher-1", "voucher-2")
        }
        assert metadata["voucher-1"].assigned_to == "Mario Rossi"
        assert metadata["voucher-1"].notes == "Nota uno"
        assert metadata["voucher-1"].is_nominal is True
        assert metadata["voucher-2"].assigned_to == "Luigi Bianchi"
        assert metadata["voucher-2"].notes == "Nota due"
        assert metadata["voucher-2"].is_nominal is True

        events = database.connection.execute(
            """SELECT voucher_id, details_json
               FROM voucher_events
               WHERE event_type='LOCAL_METADATA_UPDATED'
               ORDER BY id DESC LIMIT 2"""
        ).fetchall()
        assert len(events) == 2
        assert all('"is_nominal"' in row["details_json"] for row in events)
        assert all("Mario Rossi" not in row["details_json"] for row in events)
        assert all("Luigi Bianchi" not in row["details_json"] for row in events)
    finally:
        database.close()


def test_batch_nominal_classification_fails_without_partial_updates(tmp_path):
    database, controller_id = _database(tmp_path)
    try:
        database.upsert_voucher(
            controller_id=controller_id,
            unifi_id="voucher-2",
            code="2222233333",
            name="OTHER",
            imported_at=NOW,
            last_synced_at=NOW,
        )
        database.update_voucher_local_metadata(
            controller_id=controller_id,
            unifi_id="voucher-1",
            assigned_to="Mario Rossi",
            notes="",
            is_nominal=False,
            updated_at=NOW,
            windows_user="operator",
        )
        database.update_voucher_local_metadata(
            controller_id=controller_id,
            unifi_id="voucher-2",
            assigned_to="",
            notes="",
            is_nominal=False,
            updated_at=NOW,
            windows_user="operator",
        )

        with pytest.raises(ValueError, match="destinatario locale"):
            database.update_voucher_local_classification_batch(
                controller_id=controller_id,
                unifi_ids=["voucher-1", "voucher-2"],
                is_nominal=True,
                updated_at="2026-09-29T16:00:00+00:00",
                windows_user="operator",
            )

        assert database.voucher_local_metadata(
            controller_id=controller_id,
            unifi_id="voucher-1",
        ).is_nominal is False
        assert database.voucher_local_metadata(
            controller_id=controller_id,
            unifi_id="voucher-2",
        ).is_nominal is False
    finally:
        database.close()


def test_multi_selection_uses_batch_classification_and_deselects_all(
    monkeypatch,
    tmp_path,
):
    database, controller_id = _database(tmp_path)
    try:
        database.upsert_voucher(
            controller_id=controller_id,
            unifi_id="voucher-2",
            code="2222233333",
            name="OTHER",
            imported_at=NOW,
            last_synced_at=NOW,
        )
        for remote_id, recipient in (
            ("voucher-1", "Mario Rossi"),
            ("voucher-2", "Luigi Bianchi"),
        ):
            database.update_voucher_local_metadata(
                controller_id=controller_id,
                unifi_id=remote_id,
                assigned_to=recipient,
                notes=f"Nota {remote_id}",
                is_nominal=False,
                updated_at=NOW,
                windows_user="operator",
            )

        voucher_1 = SimpleNamespace(id="voucher-1")
        voucher_2 = SimpleNamespace(id="voucher-2")
        populated = []
        refreshed = []
        fake = SimpleNamespace(
            _background_results=None,
            active_controller_id=controller_id,
            controller_snapshot_live=True,
            vouchers=[voucher_1, voucher_2],
            checked_ids={"voucher-1", "voucher-2"},
            database=database,
            wait_window=lambda dialog: None,
            _windows_operator_identity=lambda: r"PC\operator",
            _selected_vouchers_for_local_metadata=lambda: [voucher_1, voucher_2],
            _edit_local_voucher_classification_batch=lambda **kwargs:
                metadata_ui.VoucherLocalMetadataMixin._edit_local_voucher_classification_batch(
                    fake, **kwargs
                ),
            populate=lambda: populated.append(True),
            _refresh_report_summary=lambda: refreshed.append(True),
        )

        class BatchDialog:
            def __init__(self, *args, **kwargs):
                assert kwargs["count"] == 2
                self.result = True

        monkeypatch.setattr(
            metadata_ui,
            "VoucherBatchClassificationDialog",
            BatchDialog,
        )
        monkeypatch.setattr(
            metadata_ui.messagebox,
            "showinfo",
            lambda *args, **kwargs: None,
        )
        monkeypatch.setattr(
            metadata_ui.messagebox,
            "showerror",
            lambda *args, **kwargs: None,
        )
        monkeypatch.setattr(
            metadata_ui.messagebox,
            "showwarning",
            lambda *args, **kwargs: None,
        )

        metadata_ui.VoucherLocalMetadataMixin.edit_local_voucher_metadata(fake)

        assert fake.checked_ids == set()
        assert populated == [True]
        assert refreshed == [True]
        assert database.voucher_local_metadata(
            controller_id=controller_id,
            unifi_id="voucher-1",
        ).is_nominal is True
        assert database.voucher_local_metadata(
            controller_id=controller_id,
            unifi_id="voucher-2",
        ).is_nominal is True
    finally:
        database.close()
