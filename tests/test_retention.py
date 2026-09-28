"""Regression tests for conservative Voucher Management 5.0 retention."""

from __future__ import annotations

from types import SimpleNamespace

from voucher_management.database import Database
from voucher_management.history import HistoryError
from voucher_management.retention import (
    DEFAULT_UNUSED_UNPRINTED_DAYS,
    archive_retention_candidates,
    ensure_retention_policy,
    mark_retention_intro_seen,
    retention_candidates,
    retention_intro_seen,
    reviewable_retention_candidates,
    update_retention_days,
)


NOW = "2026-09-27T08:00:00+00:00"
OLD = "2026-01-01T08:00:00+00:00"
RECENT = "2026-09-20T08:00:00+00:00"




def _history(*, generated_codes=()):
    blocked = set(generated_codes)

    def stats_for_codes(codes, settings):
        return {
            code: SimpleNamespace(
                generated_documents=int(code in blocked),
                generated_copies=int(code in blocked),
                print_jobs=0,
                printed_copies=0,
            )
            for code in codes
        }

    return SimpleNamespace(stats_for_codes=stats_for_codes)

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
            history=_history(),
            settings={},
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
            history=_history(),
            settings={},
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
            history=_history(),
            settings={},
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



def test_generated_pdf_evidence_blocks_review_and_archive(tmp_path):
    database, controller = _database(tmp_path)
    try:
        voucher_id = _voucher(
            database,
            controller,
            remote_id="generated",
            code="1234567890",
        )
        history = _history(generated_codes={"1234567890"})

        assert [item.voucher_id for item in retention_candidates(
            database,
            now=NOW,
        )] == [voucher_id]
        assert reviewable_retention_candidates(
            database,
            history=history,
            settings={},
            now=NOW,
        ) == ()

        result = archive_retention_candidates(
            database,
            voucher_ids=[voucher_id],
            archived_at=NOW,
            windows_user="operator",
            history=history,
            settings={},
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



def test_unverifiable_history_blocks_archive_without_partial_change(tmp_path):
    database, controller = _database(tmp_path)
    try:
        voucher_id = _voucher(
            database,
            controller,
            remote_id="history-failure",
            code="1234567890",
        )
        history = SimpleNamespace(
            stats_for_codes=lambda codes, settings: (
                (_ for _ in ()).throw(HistoryError("corrupt"))
            )
        )

        try:
            archive_retention_candidates(
                database,
                voucher_ids=[voucher_id],
                archived_at=NOW,
                windows_user="operator",
                history=history,
                settings={},
            )
        except HistoryError:
            pass
        else:
            raise AssertionError("unverifiable history must block retention")

        row = database.connection.execute(
            "SELECT code, archived_at FROM vouchers WHERE id=?",
            (voucher_id,),
        ).fetchone()
        assert row["code"] == "1234567890"
        assert row["archived_at"] is None
        assert database.connection.execute(
            """SELECT COUNT(*) FROM voucher_events
               WHERE voucher_id=? AND event_type='RETENTION_ARCHIVED'""",
            (voucher_id,),
        ).fetchone()[0] == 0
    finally:
        database.close()


def test_legacy_materialized_generation_blocks_retention_with_empty_live_history(
    tmp_path,
):
    database, controller = _database(tmp_path)
    try:
        voucher_id = _voucher(
            database,
            controller,
            remote_id="legacy-generated",
            code="1234567890",
        )
        with database.transaction() as db:
            db.execute(
                """INSERT INTO voucher_events
                   (event_uuid, voucher_id, event_type, occurred_at, source,
                    windows_user, details_json)
                   VALUES ('legacy-generated-event', ?,
                           'LEGACY_PDF_GENERATED', ?, 'MIGRATION', NULL, '{}')""",
                (voucher_id, OLD),
            )

        assert reviewable_retention_candidates(
            database,
            history=_history(),
            settings={},
            now=NOW,
        ) == ()

        result = archive_retention_candidates(
            database,
            voucher_ids=[voucher_id],
            archived_at=NOW,
            windows_user="operator",
            history=_history(),
            settings={},
        )
        assert result.archived_ids == ()
        assert result.skipped_ids == (voucher_id,)
    finally:
        database.close()


def test_legacy_evidence_ready_generation_blocks_before_materialization(tmp_path):
    database, controller = _database(tmp_path)
    try:
        voucher_id = _voucher(
            database,
            controller,
            remote_id="legacy-evidence",
            code="1234567890",
        )
        with database.transaction() as db:
            db.execute(
                """INSERT INTO migration_runs
                   (migration_uuid, source_kind, source_history_sha256,
                    started_at, status, total_rows, resolved_rows,
                    ambiguous_rows, unresolved_rows)
                   VALUES ('retention-legacy-run', 'LEGACY_4X_HISTORY',
                           'history-sha', ?, 'EVIDENCE_READY', 1, 1, 0, 0)""",
                (OLD,),
            )
            db.execute(
                """INSERT INTO legacy_audit_events
                   (legacy_event_key, source_line, voucher_digest, event_type,
                    occurred_at, payload_json, resolution_status, voucher_id,
                    first_migration_uuid, last_migration_uuid)
                   VALUES ('legacy-evidence-key', 1, ?, 'generate', ?, '{}',
                           'RESOLVED', ?, 'retention-legacy-run',
                           'retention-legacy-run')""",
                ("a" * 64, OLD, voucher_id),
            )

        result = archive_retention_candidates(
            database,
            voucher_ids=[voucher_id],
            archived_at=NOW,
            windows_user="operator",
            history=_history(),
            settings={},
        )

        assert result.archived_ids == ()
        assert result.skipped_ids == (voucher_id,)
    finally:
        database.close()
