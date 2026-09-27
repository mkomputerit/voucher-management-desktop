"""Regression tests for conservative Voucher Management 5.0 retention."""

from __future__ import annotations

from voucher_management.database import Database
from voucher_management.retention import (
    DEFAULT_UNUSED_UNPRINTED_DAYS,
    archive_retention_candidates,
    ensure_retention_policy,
    mark_retention_intro_seen,
    retention_candidates,
    retention_intro_seen,
    update_retention_days,
)


NOW = "2026-09-27T08:00:00+00:00"
OLD = "2026-01-01T08:00:00+00:00"
RECENT = "2026-09-20T08:00:00+00:00"


def _database(tmp_path):
    database = Database(tmp_path / "retention.db")
    database.initialize()
    controller_id = database.create_controller(
        name="Sala",
        api_root="https://controller.example",
        created_at=OLD,
    )
    return database, controller_id


def _voucher(
    database,
    controller_id,
    *,
    remote_id,
    code,
    imported_at=OLD,
    created_at=OLD,
    expires_at=None,
    uses=0,
    present=False,
):
    voucher_id = database.upsert_voucher(
        controller_id=controller_id,
        unifi_id=remote_id,
        code=code,
        name=f"Guest {remote_id}",
        created_at=created_at,
        imported_at=imported_at,
        expires_at=expires_at,
        authorized_guest_count=uses,
        last_synced_at=imported_at,
    )
    if not present:
        with database.transaction() as db:
            db.execute(
                "UPDATE vouchers SET present_on_controller=0 WHERE id=?",
                (voucher_id,),
            )
    return voucher_id


def test_default_policy_is_created_once_with_fixed_protections(tmp_path):
    database, _controller = _database(tmp_path)
    try:
        first = ensure_retention_policy(database, now=NOW)
        second = ensure_retention_policy(
            database,
            now="2026-09-27T09:00:00+00:00",
        )

        assert first.unused_unprinted_days == DEFAULT_UNUSED_UNPRINTED_DAYS
        assert first.protect_used is True
        assert first.protect_printed is True
        assert second.updated_at == first.updated_at
    finally:
        database.close()


def test_policy_edit_can_change_only_age_threshold(tmp_path):
    database, _controller = _database(tmp_path)
    try:
        ensure_retention_policy(database, now=NOW)
        policy = update_retention_days(
            database,
            days=365,
            now="2026-09-27T09:00:00+00:00",
        )

        assert policy.unused_unprinted_days == 365
        assert policy.protect_used is True
        assert policy.protect_printed is True
    finally:
        database.close()


def test_candidates_require_old_absent_unused_unprinted_rows(tmp_path):
    database, controller = _database(tmp_path)
    try:
        eligible = _voucher(
            database,
            controller,
            remote_id="eligible",
            code="1111122222",
        )
        _voucher(
            database,
            controller,
            remote_id="present",
            code="2222233333",
            present=True,
        )
        _voucher(
            database,
            controller,
            remote_id="used",
            code="3333344444",
            uses=1,
        )
        printed = _voucher(
            database,
            controller,
            remote_id="printed",
            code="4444455555",
        )
        _voucher(
            database,
            controller,
            remote_id="recent",
            code="5555566666",
            imported_at=RECENT,
            created_at=RECENT,
        )
        database.record_print_audit(
            controller_id=controller,
            audit_id="retention-print",
            codes=["44444-55555"],
            output_file="Voucher.pdf",
            document_copies=1,
            printed_at=OLD,
            windows_user="operator",
        )

        candidates = retention_candidates(
            database,
            now=NOW,
        )

        assert [item.voucher_id for item in candidates] == [eligible]
        assert all(item.voucher_id != printed for item in candidates)
    finally:
        database.close()


def test_expiry_is_conservative_age_basis_when_present(tmp_path):
    database, controller = _database(tmp_path)
    try:
        _voucher(
            database,
            controller,
            remote_id="future-expiry",
            code="1111122222",
            created_at=OLD,
            imported_at=OLD,
            expires_at="2026-12-01T08:00:00+00:00",
        )

        assert retention_candidates(database, now=NOW) == ()
    finally:
        database.close()


def test_reviewed_archive_scrubs_credential_and_personal_text(tmp_path):
    database, controller = _database(tmp_path)
    try:
        voucher_id = _voucher(
            database,
            controller,
            remote_id="archive-me",
            code="1234567890",
        )
        with database.transaction() as db:
            db.execute(
                """UPDATE vouchers
                   SET assigned_to='Mario Rossi', notes='private note'
                   WHERE id=?""",
                (voucher_id,),
            )

        result = archive_retention_candidates(
            database,
            voucher_ids=[voucher_id],
            archived_at=NOW,
            windows_user=r"PC\operator",
        )

        assert result.archived_ids == (voucher_id,)
        assert result.skipped_ids == ()
        row = database.connection.execute(
            "SELECT * FROM vouchers WHERE id=?",
            (voucher_id,),
        ).fetchone()
        assert row["code"] == f"ARCHIVED-{voucher_id}"
        assert row["name"] == ""
        assert row["assigned_to"] == ""
        assert row["notes"] == ""
        assert row["archived_at"] == NOW

        event = database.connection.execute(
            """SELECT * FROM voucher_events
               WHERE voucher_id=? AND event_type='RETENTION_ARCHIVED'""",
            (voucher_id,),
        ).fetchone()
        assert event is not None
        assert event["windows_user"] == r"PC\operator"
        assert '"credential_removed":true' in event["details_json"]
    finally:
        database.close()


def test_archive_revalidates_and_skips_row_that_became_used(tmp_path):
    database, controller = _database(tmp_path)
    try:
        voucher_id = _voucher(
            database,
            controller,
            remote_id="stale",
            code="1234567890",
        )
        assert [item.voucher_id for item in retention_candidates(
            database,
            now=NOW,
        )] == [voucher_id]

        with database.transaction() as db:
            db.execute(
                "UPDATE vouchers SET authorized_guest_count=1 WHERE id=?",
                (voucher_id,),
            )

        result = archive_retention_candidates(
            database,
            voucher_ids=[voucher_id],
            archived_at=NOW,
            windows_user="operator",
        )

        assert result.archived_ids == ()
        assert result.skipped_ids == (voucher_id,)
        row = database.connection.execute(
            "SELECT code, archived_at FROM vouchers WHERE id=?",
            (voucher_id,),
        ).fetchone()
        assert row["code"] == "1234567890"
        assert row["archived_at"] is None
    finally:
        database.close()


def test_archived_row_is_not_offered_again(tmp_path):
    database, controller = _database(tmp_path)
    try:
        voucher_id = _voucher(
            database,
            controller,
            remote_id="once",
            code="1234567890",
        )
        archive_retention_candidates(
            database,
            voucher_ids=[voucher_id],
            archived_at=NOW,
            windows_user="operator",
        )

        assert retention_candidates(database, now=NOW) == ()
    finally:
        database.close()


def test_retention_intro_marker_is_installation_scoped(tmp_path):
    database, _controller = _database(tmp_path)
    try:
        assert retention_intro_seen(database) is False

        mark_retention_intro_seen(database, now=NOW)

        assert retention_intro_seen(database) is True
    finally:
        database.close()
