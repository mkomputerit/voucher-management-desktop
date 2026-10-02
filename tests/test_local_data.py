"""Regression tests for operator-owned voucher metadata."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from voucher_management.database import Database
from voucher_management.local_data import (
    LocalVoucherPatch,
    apply_local_voucher_patch,
)
import voucher_management.local_data_ui as local_data_ui
from voucher_management.local_data_ui import LocalDataMixin, selected_workspace_vouchers


NOW = "2026-10-01T09:00:00+00:00"


def _db(tmp_path):
    db = Database(tmp_path / "local-data.db")
    db.initialize()
    controller = db.create_controller(
        name="Reception",
        api_root="https://controller.example",
        created_at=NOW,
    )
    return db, controller


def _voucher(db, controller, remote_id, code):
    return db.upsert_voucher(
        controller_id=controller,
        unifi_id=remote_id,
        code=code,
        name=f"UniFi {remote_id}",
        created_at=NOW,
        imported_at=NOW,
        duration_minutes=60,
        authorized_guest_limit=1,
        authorized_guest_count=0,
        expires_at="2026-12-01T00:00:00+00:00",
        expired=False,
        last_synced_at=NOW,
    )


def test_batch_changes_nominality_only_and_audits_each_changed_row(tmp_path):
    db, controller = _db(tmp_path)
    try:
        first = _voucher(db, controller, "v1", "1111122222")
        second = _voucher(db, controller, "v2", "3333344444")

        result = apply_local_voucher_patch(
            db,
            controller_id=controller,
            voucher_ids=[first, second],
            patch=LocalVoucherPatch(
                apply_is_nominal=True,
                is_nominal=True,
            ),
            updated_at=NOW,
            windows_user=r"PC\operator",
        )

        assert result.updated_ids == (first, second)
        rows = db.connection.execute(
            """SELECT id, code, name, duration_minutes, expires_at,
                      notes, is_nominal, origin
               FROM vouchers ORDER BY id"""
        ).fetchall()
        assert [(row["notes"], row["is_nominal"]) for row in rows] == [
            ("", 1),
            ("", 1),
        ]
        assert [row["name"] for row in rows] == ["UniFi v1", "UniFi v2"]
        assert [row["code"] for row in rows] == ["1111122222", "3333344444"]
        assert [row["duration_minutes"] for row in rows] == [60, 60]
        assert [row["origin"] for row in rows] == ["CONTROLLER", "CONTROLLER"]

        events = db.connection.execute(
            """SELECT voucher_id, event_type, windows_user, details_json
               FROM voucher_events ORDER BY voucher_id"""
        ).fetchall()
        assert len(events) == 2
        assert all(row["event_type"] == "LOCAL_METADATA_UPDATED" for row in events)
        assert all(row["windows_user"] == r"PC\operator" for row in events)
        assert all("notes" not in row["details_json"] for row in events)
        assert all("is_nominal" in row["details_json"] for row in events)
        assert all("assigned_to" not in row["details_json"] for row in events)
    finally:
        db.close()


def test_batch_can_clear_notes_and_set_non_nominal_without_touching_unifi_name(tmp_path):
    db, controller = _db(tmp_path)
    try:
        voucher_id = _voucher(db, controller, "v1", "1111122222")
        with db.transaction() as tx:
            tx.execute(
                """UPDATE vouchers
                   SET notes='Old note', is_nominal=1
                   WHERE id=?""",
                (voucher_id,),
            )

        apply_local_voucher_patch(
            db,
            controller_id=controller,
            voucher_ids=[voucher_id],
            patch=LocalVoucherPatch(
                apply_notes=True,
                notes="",
                apply_is_nominal=True,
                is_nominal=False,
            ),
            updated_at=NOW,
            windows_user="operator",
        )

        row = db.connection.execute(
            """SELECT name, notes, is_nominal, nominality_redacted
               FROM vouchers WHERE id=?""",
            (voucher_id,),
        ).fetchone()
        assert row["name"] == "UniFi v1"
        assert row["notes"] == ""
        assert row["is_nominal"] == 0
        assert row["nominality_redacted"] == 0
    finally:
        db.close()


def test_nominal_classification_requires_unifi_recipient(tmp_path):
    db, controller = _db(tmp_path)
    try:
        voucher_id = db.upsert_voucher(
            controller_id=controller,
            unifi_id="blank-name",
            code="9999900000",
            name="",
            imported_at=NOW,
            last_synced_at=NOW,
        )
        with pytest.raises(ValueError, match="destinatario"):
            apply_local_voucher_patch(
                db,
                controller_id=controller,
                voucher_ids=[voucher_id],
                patch=LocalVoucherPatch(
                    apply_is_nominal=True,
                    is_nominal=True,
                ),
                updated_at=NOW,
                windows_user="operator",
            )
        row = db.connection.execute(
            "SELECT is_nominal FROM vouchers WHERE id=?",
            (voucher_id,),
        ).fetchone()
        assert row["is_nominal"] is None
    finally:
        db.close()


def test_nominality_patch_rejects_non_classified_choice(tmp_path):
    db, controller = _db(tmp_path)
    try:
        voucher_id = _voucher(db, controller, "v1", "1111122222")
        with pytest.raises(ValueError, match="Nominale o Non nominale"):
            apply_local_voucher_patch(
                db,
                controller_id=controller,
                voucher_ids=[voucher_id],
                patch=LocalVoucherPatch(
                    apply_is_nominal=True,
                    is_nominal=None,
                ),
                updated_at=NOW,
                windows_user="operator",
            )
    finally:
        db.close()


def test_notes_backend_rejects_multi_voucher_update(tmp_path):
    db, controller = _db(tmp_path)
    try:
        first = _voucher(db, controller, "v1", "1111122222")
        second = _voucher(db, controller, "v2", "3333344444")

        with pytest.raises(ValueError, match="un solo voucher"):
            apply_local_voucher_patch(
                db,
                controller_id=controller,
                voucher_ids=[first, second],
                patch=LocalVoucherPatch(
                    apply_notes=True,
                    notes="Nota condivisa non consentita",
                ),
                updated_at=NOW,
                windows_user="operator",
            )

        rows = db.connection.execute(
            "SELECT notes FROM vouchers ORDER BY id"
        ).fetchall()
        assert [row["notes"] for row in rows] == ["", ""]
    finally:
        db.close()


def test_batch_is_atomic_if_one_row_is_not_from_active_controller(tmp_path):
    db, controller = _db(tmp_path)
    other = db.create_controller(
        name="Other",
        api_root="https://other.example",
        created_at=NOW,
    )
    try:
        valid = _voucher(db, controller, "v1", "1111122222")
        foreign = _voucher(db, other, "v2", "3333344444")

        with pytest.raises(RuntimeError):
            apply_local_voucher_patch(
                db,
                controller_id=controller,
                voucher_ids=[valid, foreign],
                patch=LocalVoucherPatch(
                    apply_is_nominal=True,
                    is_nominal=True,
                ),
                updated_at=NOW,
                windows_user="operator",
            )

        assert db.connection.execute(
            "SELECT is_nominal FROM vouchers WHERE id=?",
            (valid,),
        ).fetchone()["is_nominal"] is None
        assert db.connection.execute(
            "SELECT COUNT(*) FROM voucher_events"
        ).fetchone()[0] == 0
    finally:
        db.close()


def test_noop_batch_creates_no_audit_noise(tmp_path):
    db, controller = _db(tmp_path)
    try:
        voucher_id = _voucher(db, controller, "v1", "1111122222")
        result = apply_local_voucher_patch(
            db,
            controller_id=controller,
            voucher_ids=[voucher_id],
            patch=LocalVoucherPatch(
                apply_notes=True,
                notes="",
            ),
            updated_at=NOW,
            windows_user="operator",
        )
        assert result.updated_ids == ()
        assert result.unchanged_ids == (voucher_id,)
        assert db.connection.execute(
            "SELECT COUNT(*) FROM voucher_events"
        ).fetchone()[0] == 0
    finally:
        db.close()


def test_patch_requires_at_least_one_explicit_field(tmp_path):
    db, controller = _db(tmp_path)
    try:
        voucher_id = _voucher(db, controller, "v1", "1111122222")
        with pytest.raises(ValueError):
            apply_local_voucher_patch(
                db,
                controller_id=controller,
                voucher_ids=[voucher_id],
                patch=LocalVoucherPatch(),
                updated_at=NOW,
                windows_user="operator",
            )
    finally:
        db.close()


def test_explicit_actions_use_existing_workspace_multiselection():
    one = SimpleNamespace(id="one")
    two = SimpleNamespace(id="two")
    three = SimpleNamespace(id="three")
    app = SimpleNamespace(
        checked_ids={"one", "three"},
        vouchers=(one, two, three),
    )

    assert selected_workspace_vouchers(app) == (one, three)



class _LocalDataHarness(LocalDataMixin):
    pass


def test_notes_action_rejects_multiple_workspace_selection(monkeypatch):
    one = SimpleNamespace(id="one")
    two = SimpleNamespace(id="two")
    app = _LocalDataHarness()
    app.checked_ids = {"one", "two"}
    app.vouchers = (one, two)

    messages = []
    opened = []
    monkeypatch.setattr(
        local_data_ui.messagebox,
        "showinfo",
        lambda *args, **kwargs: messages.append((args, kwargs)),
    )
    monkeypatch.setattr(
        local_data_ui,
        "NotesDialog",
        lambda *args, **kwargs: opened.append((args, kwargs)),
    )

    app.edit_selected_notes()

    assert opened == []
    assert len(messages) == 1
    assert "un solo voucher" in messages[0][0][1]


def test_notes_action_opens_for_exactly_one_selected_voucher(monkeypatch):
    one = SimpleNamespace(id="one")
    two = SimpleNamespace(id="two")
    app = _LocalDataHarness()
    app.checked_ids = {"two"}
    app.vouchers = (one, two)

    opened = []
    monkeypatch.setattr(
        local_data_ui,
        "NotesDialog",
        lambda *args, **kwargs: opened.append((args, kwargs)),
    )

    app.edit_selected_notes()

    assert len(opened) == 1
    assert opened[0][0][1] == (two,)
