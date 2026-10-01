"""Regression tests for conservative Voucher Management 5.0 retention."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from voucher_management.database import Database
from voucher_management.history import HistoryError
from voucher_management.retention import (
    archive_retention_candidates,
    configure_retention_policy,
    ensure_retention_policy,
    mark_retention_intro_seen,
    retention_candidates,
    retention_intro_seen,
    reviewable_retention_candidates,
    security_revocation_candidates,
    prepare_security_revocation_operation,
    update_retention_days,
)


NOW = "2026-09-27T08:00:00+00:00"
OLD = "2026-01-01T08:00:00+00:00"
RECENT = "2026-09-20T08:00:00+00:00"




def _history(*, generated_codes=()):
    blocked = {
        str(code).strip().replace("-", "")
        for code in generated_codes
    }

    def stats_for_codes(codes, settings):
        return {
            code: SimpleNamespace(
                generated_documents=int(
                    str(code).strip().replace("-", "") in blocked
                ),
                generated_copies=int(
                    str(code).strip().replace("-", "") in blocked
                ),
                print_jobs=0,
                printed_copies=0,
            )
            for code in codes
        }

    return SimpleNamespace(stats_for_codes=stats_for_codes)

def _database(tmp_path, *, configure=True):
    database = Database(tmp_path / "retention.db")
    database.initialize()
    controller_id = database.create_controller(
        name="Sala",
        api_root="https://controller.example",
        created_at=OLD,
    )
    if configure:
        configure_retention_policy(
            database,
            unused_unprinted_days=180,
            printed_unused_revoke_days=60,
            now=NOW,
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
    expires_at=OLD,
    expired=True,
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
        expired=expired,
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


def test_policy_is_never_invented_and_requires_explicit_configuration(tmp_path):
    database, _controller = _database(tmp_path, configure=False)
    try:
        with pytest.raises(RuntimeError, match="non è ancora configurata"):
            ensure_retention_policy(database, now=NOW)

        policy = configure_retention_policy(
            database,
            unused_unprinted_days=180,
            printed_unused_revoke_days=60,
            now=NOW,
        )
        assert policy.unused_unprinted_days == 180
        assert policy.printed_unused_revoke_days == 60
        assert policy.configured is True
        assert policy.protect_used is True
        assert policy.protect_printed is True
    finally:
        database.close()


def test_policy_edit_preserves_security_threshold(tmp_path):
    database, _controller = _database(tmp_path)
    try:
        policy = update_retention_days(
            database,
            days=365,
            now="2026-09-27T09:00:00+00:00",
        )
        assert policy.unused_unprinted_days == 365
        assert policy.printed_unused_revoke_days == 60
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
            expires_at=RECENT,
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


def test_historically_used_voucher_never_becomes_retention_candidate_after_counter_reset(tmp_path):
    database, controller = _database(tmp_path)
    try:
        voucher_id = _voucher(
            database,
            controller,
            remote_id="used-then-reset",
            code="1919191919",
            uses=1,
        )
        database.upsert_voucher(
            controller_id=controller,
            unifi_id="used-then-reset",
            code="1919191919",
            name="Guest used-then-reset",
            created_at=OLD,
            imported_at=OLD,
            authorized_guest_count=0,
            last_synced_at=NOW,
        )
        with database.transaction() as db:
            db.execute(
                "UPDATE vouchers SET present_on_controller=0 WHERE id=?",
                (voucher_id,),
            )

        row = database.connection.execute(
            "SELECT authorized_guest_count, ever_used FROM vouchers WHERE id=?",
            (voucher_id,),
        ).fetchone()
        assert row["authorized_guest_count"] == 0
        assert row["ever_used"] == 1
        assert all(
            candidate.voucher_id != voucher_id
            for candidate in retention_candidates(database, now=NOW)
        )
    finally:
        database.close()


def test_usage_indeterminate_row_is_never_offered_for_retention(tmp_path):
    database, controller = _database(tmp_path)
    try:
        voucher_id = _voucher(
            database,
            controller,
            remote_id="usage-unknown",
            code="9090909090",
        )
        with database.transaction() as db:
            db.execute(
                "UPDATE vouchers SET usage_observed=0 WHERE id=?",
                (voucher_id,),
            )

        assert retention_candidates(database, now=NOW) == ()

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


def test_recent_controller_absence_is_not_old_enough_for_local_retention(tmp_path):
    database, controller = _database(tmp_path)
    try:
        voucher_id = _voucher(
            database,
            controller,
            remote_id="recent-absence",
            code="8181818181",
            imported_at=OLD,
            created_at=OLD,
            expires_at=None,
            expired=False,
        )
        with database.transaction() as db:
            db.execute(
                """UPDATE vouchers
                   SET present_on_controller=0,
                       last_synced_at='2026-09-20T08:00:00+00:00'
                   WHERE id=?""",
                (voucher_id,),
            )
        assert retention_candidates(database, now=NOW) == ()
    finally:
        database.close()


def test_local_retention_does_not_require_voucher_expiry(tmp_path):
    database, controller = _database(tmp_path)
    try:
        voucher_id = _voucher(
            database,
            controller,
            remote_id="no-expiry",
            code="1717171717",
            created_at=OLD,
            imported_at=OLD,
            expires_at=None,
            expired=False,
        )
        assert [item.voucher_id for item in retention_candidates(
            database,
            now=NOW,
        )] == [voucher_id]
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
                   SET assigned_to='Mario Rossi', notes='private note',
                       is_nominal=1
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
        assert row["is_nominal"] is None
        assert row["nominality_redacted"] == 1
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


def test_security_revocation_candidates_require_old_print_and_post_print_observation(tmp_path):
    database, controller = _database(tmp_path)
    try:
        voucher_id = _voucher(
            database,
            controller,
            remote_id="printed-live",
            code="1212121212",
            created_at=OLD,
            imported_at=OLD,
            expires_at=None,
            expired=False,
            present=True,
        )
        database.record_print_audit(
            controller_id=controller,
            audit_id="security-print",
            codes=["12121-21212"],
            output_file="voucher.pdf",
            document_copies=1,
            printed_at="2026-06-01T08:00:00+00:00",
            windows_user="operator",
        )
        with database.transaction() as db:
            db.execute(
                "UPDATE vouchers SET last_seen_at=? WHERE id=?",
                (NOW, voucher_id),
            )

        candidates = security_revocation_candidates(
            database,
            now=NOW,
            controller_id=controller,
        )
        assert [item.voucher_id for item in candidates] == [voucher_id]
        assert candidates[0].last_printed_at == "2026-06-01T08:00:00+00:00"
    finally:
        database.close()


def test_security_revocation_excludes_used_unknown_recent_and_unobserved_after_print(tmp_path):
    database, controller = _database(tmp_path)
    try:
        cases = {}
        for remote_id, code in (
            ("used", "1313131313"),
            ("unknown", "1414141414"),
            ("recent-print", "1515151515"),
            ("stale-observation", "1616161616"),
        ):
            cases[remote_id] = _voucher(
                database,
                controller,
                remote_id=remote_id,
                code=code,
                created_at=OLD,
                imported_at=OLD,
                expires_at=None,
                expired=False,
                present=True,
            )
        with database.transaction() as db:
            db.execute(
                "UPDATE vouchers SET ever_used=1, authorized_guest_count=1 WHERE id=?",
                (cases["used"],),
            )
            db.execute(
                "UPDATE vouchers SET usage_observed=0 WHERE id=?",
                (cases["unknown"],),
            )
        for remote_id, code, stamp in (
            ("used", "13131-31313", "2026-06-01T08:00:00+00:00"),
            ("unknown", "14141-41414", "2026-06-01T08:00:00+00:00"),
            ("recent-print", "15151-51515", "2026-09-20T08:00:00+00:00"),
            ("stale-observation", "16161-61616", "2026-06-01T08:00:00+00:00"),
        ):
            database.record_print_audit(
                controller_id=controller,
                audit_id=f"job-{remote_id}",
                codes=[code],
                output_file=f"{remote_id}.pdf",
                document_copies=1,
                printed_at=stamp,
                windows_user="operator",
            )
        with database.transaction() as db:
            for key in ("used", "unknown", "recent-print"):
                db.execute(
                    "UPDATE vouchers SET last_seen_at=? WHERE id=?",
                    (NOW, cases[key]),
                )
            db.execute(
                "UPDATE vouchers SET last_seen_at=? WHERE id=?",
                ("2026-05-01T08:00:00+00:00", cases["stale-observation"]),
            )

        assert security_revocation_candidates(
            database,
            now=NOW,
            controller_id=controller,
        ) == ()
    finally:
        database.close()


def test_identity_review_required_blocks_security_revocation(tmp_path):
    database, controller = _database(tmp_path)
    try:
        voucher_id = _voucher(
            database,
            controller,
            remote_id="review-required",
            code="1818181818",
            expires_at=None,
            expired=False,
            present=True,
        )
        database.record_print_audit(
            controller_id=controller,
            audit_id="review-print",
            codes=["18181-81818"],
            output_file="review.pdf",
            document_copies=1,
            printed_at="2026-06-01T08:00:00+00:00",
            windows_user="operator",
        )
        with database.transaction() as db:
            db.execute(
                "UPDATE vouchers SET last_seen_at=? WHERE id=?",
                (NOW, voucher_id),
            )
            db.execute(
                """INSERT INTO voucher_events(
                       event_uuid, voucher_id, event_type, occurred_at,
                       source, details_json
                   ) VALUES ('review-required-event', ?,
                       'LEGACY_IDENTITY_REVIEW_REQUIRED', ?, 'SYSTEM', '{}')""",
                (voucher_id, NOW),
            )
        assert security_revocation_candidates(
            database,
            now=NOW,
            controller_id=controller,
        ) == ()
    finally:
        database.close()


def test_prepare_security_revocation_operation_revalidates_and_persists_intent(tmp_path):
    database, controller = _database(tmp_path)
    try:
        voucher_id = _voucher(
            database,
            controller,
            remote_id="security-ready",
            code="1919191919",
            expires_at=None,
            expired=False,
            present=True,
        )
        database.record_print_audit(
            controller_id=controller,
            audit_id="security-ready-print",
            codes=["19191-91919"],
            output_file="ready.pdf",
            document_copies=1,
            printed_at="2026-06-01T08:00:00+00:00",
            windows_user="operator",
        )
        with database.transaction() as db:
            db.execute(
                "UPDATE vouchers SET last_seen_at=? WHERE id=?",
                (NOW, voucher_id),
            )

        remote_ids = prepare_security_revocation_operation(
            database,
            controller_id=controller,
            voucher_ids=[voucher_id],
            operation_uuid="security-operation",
            requested_at=NOW,
            windows_user=r"PC\operator",
        )
        assert remote_ids == ("security-ready",)
        row = database.connection.execute(
            "SELECT status, requested_by FROM security_revocations"
        ).fetchone()
        assert row["status"] == "PREPARED"
        assert row["requested_by"] == r"PC\operator"
    finally:
        database.close()


def test_security_revoked_printed_voucher_can_later_be_minimized_locally(tmp_path):
    database, controller = _database(tmp_path)
    try:
        voucher_id = _voucher(
            database,
            controller,
            remote_id="revoked-then-retained",
            code="2121212121",
            created_at=OLD,
            imported_at=OLD,
            expires_at=None,
            expired=False,
            present=False,
        )
        database.record_print_audit(
            controller_id=controller,
            audit_id="revoked-retention-print",
            codes=["21212-12121"],
            output_file="revoked.pdf",
            document_copies=1,
            printed_at="2026-01-02T08:00:00+00:00",
            windows_user="operator",
        )
        revoked_at = "2026-02-01T08:00:00+00:00"
        with database.transaction() as db:
            db.execute(
                """UPDATE vouchers
                   SET revoked_for_security_at=?, last_synced_at=?,
                       present_on_controller=0
                   WHERE id=?""",
                (revoked_at, revoked_at, voucher_id),
            )

        candidates = reviewable_retention_candidates(
            database,
            history=_history(generated_codes={"2121212121"}),
            settings={},
            now=NOW,
        )
        assert [item.voucher_id for item in candidates] == [voucher_id]

        result = archive_retention_candidates(
            database,
            voucher_ids=[voucher_id],
            archived_at=NOW,
            windows_user="operator",
            history=_history(generated_codes={"2121212121"}),
            settings={},
        )
        assert result.archived_ids == (voucher_id,)
        row = database.connection.execute(
            "SELECT code, revoked_for_security_at, archived_at FROM vouchers WHERE id=?",
            (voucher_id,),
        ).fetchone()
        assert row["code"] == f"ARCHIVED-{voucher_id}"
        assert row["revoked_for_security_at"] == revoked_at
        assert row["archived_at"] == NOW
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


def test_retention_candidate_exposes_last_actual_presence_not_absence_sync(tmp_path):
    database, controller = _database(tmp_path)
    try:
        voucher_id = _voucher(
            database,
            controller,
            remote_id="retention-freshness",
            code="7373737373",
        )
        absence_observed = "2026-03-01T08:00:00+00:00"
        with database.transaction() as db:
            db.execute(
                """UPDATE vouchers
                   SET last_seen_at=?, last_synced_at=?
                   WHERE id=?""",
                (OLD, absence_observed, voucher_id),
            )

        candidates = retention_candidates(database, now=NOW)
        candidate = next(
            item for item in candidates if item.voucher_id == voucher_id
        )
        assert candidate.last_seen_at == OLD
        assert candidate.last_synced_at == absence_observed
        assert candidate.age_basis == absence_observed
    finally:
        database.close()


def test_retention_candidate_keeps_unifi_description_and_local_recipient_separate(
    tmp_path,
):
    database, controller = _database(tmp_path)
    try:
        voucher_id = _voucher(
            database,
            controller,
            remote_id="identity-separation",
            code="8585858585",
        )
        with database.transaction() as db:
            db.execute(
                """UPDATE vouchers
                   SET name='Descrizione UniFi',
                       assigned_to='Mario Rossi'
                   WHERE id=?""",
                (voucher_id,),
            )

        candidate = next(
            item
            for item in retention_candidates(database, now=NOW)
            if item.voucher_id == voucher_id
        )
        assert candidate.controller_description == "Descrizione UniFi"
        assert candidate.assigned_to == "Mario Rossi"
    finally:
        database.close()


def test_generated_pdf_hmac_lookup_uses_printed_code_format(tmp_path):
    database, controller = _database(tmp_path)
    try:
        voucher_id = _voucher(
            database,
            controller,
            remote_id="hmac-format",
            code="1234567890",
        )
        seen = {}

        def stats_for_codes(codes, settings):
            seen["codes"] = list(codes)
            return {
                code: SimpleNamespace(
                    generated_documents=int(code == "12345-67890"),
                    generated_copies=int(code == "12345-67890"),
                    print_jobs=0,
                    printed_copies=0,
                )
                for code in codes
            }

        history = SimpleNamespace(stats_for_codes=stats_for_codes)
        assert reviewable_retention_candidates(
            database,
            history=history,
            settings={},
            now=NOW,
        ) == ()
        assert seen["codes"] == ["12345-67890"]

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
    finally:
        database.close()


def test_pending_security_revocation_is_not_offered_again(tmp_path):
    database, controller = _database(tmp_path)
    try:
        voucher_id = _voucher(
            database,
            controller,
            remote_id="pending-security",
            code="2323232323",
            expires_at=None,
            expired=False,
            present=True,
        )
        database.record_print_audit(
            controller_id=controller,
            audit_id="pending-security-print",
            codes=["23232-32323"],
            output_file="pending.pdf",
            document_copies=1,
            printed_at="2026-06-01T08:00:00+00:00",
            windows_user="operator",
        )
        with database.transaction() as db:
            db.execute(
                "UPDATE vouchers SET last_seen_at=? WHERE id=?",
                (NOW, voucher_id),
            )

        before = security_revocation_candidates(
            database,
            now=NOW,
            controller_id=controller,
        )
        assert [item.voucher_id for item in before] == [voucher_id]

        prepare_security_revocation_operation(
            database,
            controller_id=controller,
            voucher_ids=[voucher_id],
            operation_uuid="pending-security-operation",
            requested_at=NOW,
            windows_user="operator",
        )

        assert security_revocation_candidates(
            database,
            now=NOW,
            controller_id=controller,
        ) == ()
    finally:
        database.close()


def test_database_rejects_second_pending_security_revocation_intent(tmp_path):
    database, controller = _database(tmp_path)
    try:
        voucher_id = _voucher(
            database,
            controller,
            remote_id="double-pending-security",
            code="2424242424",
            expires_at=None,
            expired=False,
            present=True,
        )
        database.record_print_audit(
            controller_id=controller,
            audit_id="double-pending-print",
            codes=["24242-42424"],
            output_file="double-pending.pdf",
            document_copies=1,
            printed_at="2026-06-01T08:00:00+00:00",
            windows_user="operator",
        )
        with database.transaction() as db:
            db.execute(
                "UPDATE vouchers SET last_seen_at=? WHERE id=?",
                (NOW, voucher_id),
            )

        database.prepare_security_revocations(
            controller_id=controller,
            unifi_ids=["double-pending-security"],
            operation_uuid="first-pending-operation",
            requested_at=NOW,
            requested_by="operator",
        )
        with pytest.raises(RuntimeError, match="già una revoca"):
            database.prepare_security_revocations(
                controller_id=controller,
                unifi_ids=["double-pending-security"],
                operation_uuid="second-pending-operation",
                requested_at=NOW,
                requested_by="operator",
            )

        rows = database.connection.execute(
            """SELECT operation_uuid, status
               FROM security_revocations
               WHERE voucher_id=?""",
            (voucher_id,),
        ).fetchall()
        assert [(row["operation_uuid"], row["status"]) for row in rows] == [
            ("first-pending-operation", "PREPARED")
        ]
    finally:
        database.close()
