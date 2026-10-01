"""Regression tests for durable historical reporting semantics."""

from __future__ import annotations

import sqlite3

import pytest

from voucher_management.database import Database
from voucher_management.operational_alerts import set_unprinted_warning_days
from voucher_management.report_policy import ReportPurpose
from voucher_management.reporting import ReportKind, build_report_dataset
from voucher_management.security_revocation import (
    record_security_revocations,
    set_security_revoke_days,
)
from voucher_management.sync_store import persist_successful_snapshot
from voucher_management.unifi_api import ApiVoucher


NOW = "2026-09-29T10:00:00+00:00"


def _db(tmp_path):
    db = Database(tmp_path / "reporting.db")
    db.initialize()
    controller = db.create_controller(
        name="Reception",
        api_root="https://controller.example",
        created_at="2026-09-01T08:00:00+00:00",
    )
    return db, controller


def _voucher(
    db,
    controller,
    unifi_id,
    code,
    *,
    name="",
    used=0,
    expired=False,
    created_at="2026-09-01T09:00:00+00:00",
    expires_at="2026-10-01T09:00:00+00:00",
    synced_at=NOW,
):
    return db.upsert_voucher(
        controller_id=controller,
        unifi_id=unifi_id,
        code=code,
        name=name,
        created_at=created_at,
        imported_at="2026-09-01T09:05:00+00:00",
        duration_minutes=60,
        authorized_guest_limit=1,
        authorized_guest_count=used,
        expires_at=expires_at,
        expired=expired,
        last_synced_at=synced_at,
    )


def _remote(remote_id: str, *, used: int = 0) -> ApiVoucher:
    return ApiVoucher(
        id=remote_id,
        code="1111122222",
        recipient="",
        duration_minutes=60,
        create_time=1_700_000_000,
        quota=1,
        used=used,
        status="VALID_MULTI",
    )


def test_summary_report_never_exposes_codes_even_when_requested(tmp_path):
    db, controller = _db(tmp_path)
    try:
        _voucher(db, controller, "v1", "1234567890", name="Guest One")
        dataset = build_report_dataset(
            db,
            kind=ReportKind.SUMMARY,
            generated_at=NOW,
            include_code_requested=True,
        )
        assert dataset.purpose is ReportPurpose.SUMMARY
        assert dataset.code_exposed is False
        assert dataset.rows[0].code == ""
    finally:
        db.close()


def test_full_history_is_audit_and_also_hides_codes(tmp_path):
    db, controller = _db(tmp_path)
    try:
        _voucher(db, controller, "v1", "1234567890")
        dataset = build_report_dataset(
            db,
            kind=ReportKind.FULL_HISTORY,
            generated_at=NOW,
            include_code_requested=True,
        )
        assert dataset.purpose is ReportPurpose.AUDIT
        assert dataset.code_exposed is False
        assert dataset.rows[0].code == ""
    finally:
        db.close()


def test_nominal_report_uses_explicit_flag_not_recipient_text(tmp_path):
    db, controller = _db(tmp_path)
    try:
        emi = _voucher(db, controller, "emi", "1111122222", name="EMI06")
        pinco = _voucher(db, controller, "pinco", "3333344444", name="Pinco Pallino")
        db.mark_application_created_vouchers(
            controller_id=controller,
            unifi_ids=["emi"],
            is_nominal=False,
            aligned_at="2026-10-01T08:00:00+00:00",
        )
        db.mark_application_created_vouchers(
            controller_id=controller,
            unifi_ids=["pinco"],
            is_nominal=True,
            aligned_at="2026-10-01T08:00:00+00:00",
        )
        dataset = build_report_dataset(db, kind=ReportKind.NOMINAL, generated_at=NOW)
        assert [row.voucher_id for row in dataset.rows] == [pinco]
        assert emi not in {row.voucher_id for row in dataset.rows}
    finally:
        db.close()


def test_only_confirmed_application_origin_counts_as_generated(tmp_path):
    db, controller = _db(tmp_path)
    try:
        app_id = _voucher(db, controller, "app", "1111122222")
        legacy = _voucher(db, controller, "legacy", "3333344444")
        external = _voucher(db, controller, "external", "5555566666")
        db.mark_application_created_vouchers(
            controller_id=controller,
            unifi_ids=["app"],
            is_nominal=False,
            aligned_at="2026-10-01T08:00:00+00:00",
        )
        with db.transaction() as tx:
            tx.execute(
                "UPDATE vouchers SET origin='LEGACY_APPLICATION' WHERE id=?",
                (legacy,),
            )

        dataset = build_report_dataset(
            db,
            kind=ReportKind.GENERATED,
            generated_at=NOW,
        )
        assert [row.voucher_id for row in dataset.rows] == [app_id]

        unknown = build_report_dataset(
            db,
            kind=ReportKind.ORIGIN_UNKNOWN,
            generated_at=NOW,
        )
        assert {row.voucher_id for row in unknown.rows} == {legacy, external}
    finally:
        db.close()


def test_generated_unused_requires_confirmed_origin_and_observed_zero(tmp_path):
    db, controller = _db(tmp_path)
    try:
        created_unused = _voucher(db, controller, "created-unused", "1111122222")
        _voucher(db, controller, "created-used", "3333344444", used=2)
        unknown_usage = _voucher(db, controller, "unknown-usage", "5555566666")
        db.mark_application_created_vouchers(
            controller_id=controller,
            unifi_ids=["created-unused", "created-used", "unknown-usage"],
            is_nominal=False,
            aligned_at="2026-10-01T08:00:00+00:00",
        )
        with db.transaction() as tx:
            tx.execute(
                "UPDATE vouchers SET usage_observed=0 WHERE id=?",
                (unknown_usage,),
            )

        dataset = build_report_dataset(
            db,
            kind=ReportKind.GENERATED_UNUSED,
            generated_at=NOW,
        )
        assert [row.voucher_id for row in dataset.rows] == [created_unused]
    finally:
        db.close()


def test_missing_controller_usage_evidence_is_explicitly_unknown(tmp_path):
    db, controller = _db(tmp_path)
    try:
        voucher_id = _voucher(db, controller, "legacy-unknown", "1212121212")
        with db.transaction() as tx:
            tx.execute(
                "UPDATE vouchers SET usage_observed=0, ever_used=0 WHERE id=?",
                (voucher_id,),
            )
        dataset = build_report_dataset(
            db,
            kind=ReportKind.USAGE_UNKNOWN,
            generated_at=NOW,
        )
        assert [row.voucher_id for row in dataset.rows] == [voucher_id]
        assert dataset.rows[0].usage_observed is False
    finally:
        db.close()


def test_used_is_monotonic_even_if_latest_controller_count_returns_zero(tmp_path):
    db, controller = _db(tmp_path)
    try:
        voucher_id = _voucher(db, controller, "used-history", "1111122222")
        persist_successful_snapshot(
            db,
            controller_id=controller,
            vouchers=[_remote("used-history", used=2)],
            observed_at="2026-09-29T08:00:00+00:00",
            sync_uuid="used-once",
        )
        persist_successful_snapshot(
            db,
            controller_id=controller,
            vouchers=[_remote("used-history", used=0)],
            observed_at="2026-09-29T09:00:00+00:00",
            sync_uuid="counter-reset",
        )
        dataset = build_report_dataset(db, kind=ReportKind.USED, generated_at=NOW)
        assert [row.voucher_id for row in dataset.rows] == [voucher_id]
        assert dataset.rows[0].ever_used is True
        assert dataset.rows[0].authorized_guest_count == 0
    finally:
        db.close()


def test_summary_exposes_data_quality_and_freshness_without_clear_codes(tmp_path):
    db, controller = _db(tmp_path)
    try:
        external = _voucher(
            db,
            controller,
            "external",
            "1111122222",
            synced_at="2026-09-20T08:00:00+00:00",
        )
        _voucher(
            db,
            controller,
            "nominal",
            "3333344444",
            synced_at="2026-09-29T09:00:00+00:00",
        )
        db.mark_application_created_vouchers(
            controller_id=controller,
            unifi_ids=["nominal"],
            is_nominal=True,
            aligned_at="2026-10-01T08:00:00+00:00",
        )
        dataset = build_report_dataset(
            db,
            kind=ReportKind.SUMMARY,
            generated_at=NOW,
            include_code_requested=True,
        )
        assert dataset.controller_label == "Tutto lo storico locale"
        assert dataset.code_exposed is False
        assert dataset.totals.generated_vouchers == 1
        assert dataset.totals.unknown_origin_vouchers == 1
        assert dataset.totals.nominal_vouchers == 1
        assert dataset.totals.unclassified_vouchers == 1
        assert dataset.data_from == "2026-09-20T08:00:00+00:00"
        assert dataset.data_as_of == "2026-09-29T09:00:00+00:00"
        assert external in {row.voucher_id for row in dataset.rows}
    finally:
        db.close()


def test_nominality_redaction_is_not_reported_as_never_classified(tmp_path):
    db, controller = _db(tmp_path)
    try:
        voucher_id = _voucher(db, controller, "redacted", "1111122222")
        with db.transaction() as tx:
            tx.execute(
                """UPDATE vouchers
                   SET is_nominal=NULL, nominality_redacted=1
                   WHERE id=?""",
                (voucher_id,),
            )
        summary = build_report_dataset(db, kind=ReportKind.SUMMARY, generated_at=NOW)
        assert summary.totals.unclassified_vouchers == 0
        assert summary.totals.redacted_nominality_vouchers == 1
        redacted = build_report_dataset(
            db,
            kind=ReportKind.NOMINALITY_REDACTED,
            generated_at=NOW,
        )
        assert [row.voucher_id for row in redacted.rows] == [voucher_id]
    finally:
        db.close()


def test_printed_never_observed_used_uses_historical_usage_fact(tmp_path):
    db, controller = _db(tmp_path)
    try:
        voucher_id = _voucher(db, controller, "printed", "1111122222")
        db.record_print_audit(
            controller_id=controller,
            audit_id="job-1",
            codes=["11111-22222"],
            output_file="voucher.pdf",
            document_copies=1,
            printed_at="2026-09-02T10:00:00+00:00",
            windows_user="PC\\\\alice",
        )
        dataset = build_report_dataset(
            db,
            kind=ReportKind.PRINTED_UNUSED,
            generated_at=NOW,
        )
        assert [row.voucher_id for row in dataset.rows] == [voucher_id]
    finally:
        db.close()


def test_print_operator_is_preserved_in_report_dataset(tmp_path):
    db, controller = _db(tmp_path)
    try:
        voucher_id = _voucher(db, controller, "printed-operator", "5656565656")
        db.record_print_audit(
            controller_id=controller,
            audit_id="operator-job",
            codes=["56565-65656"],
            output_file="operator.pdf",
            document_copies=1,
            printed_at="2026-09-02T10:00:00+00:00",
            windows_user=r"RECEPTION\alice",
        )

        dataset = build_report_dataset(
            db,
            kind=ReportKind.PRINTED,
            generated_at=NOW,
        )

        assert [row.voucher_id for row in dataset.rows] == [voucher_id]
        assert dataset.rows[0].print_operators == (r"RECEPTION\alice",)
    finally:
        db.close()


def test_printed_report_includes_unknown_usage_without_calling_it_unused(tmp_path):
    db, controller = _db(tmp_path)
    try:
        voucher_id = _voucher(db, controller, "legacy-like", "2222233333")
        db.record_print_audit(
            controller_id=controller,
            audit_id="legacy-print",
            codes=["22222-33333"],
            output_file="legacy.pdf",
            document_copies=1,
            printed_at="2026-09-02T10:00:00+00:00",
            windows_user="MIGRATION",
        )
        with db.transaction() as tx:
            tx.execute(
                "UPDATE vouchers SET usage_observed=0, ever_used=0 WHERE id=?",
                (voucher_id,),
            )

        dataset = build_report_dataset(
            db,
            kind=ReportKind.PRINTED_UNUSED,
            generated_at=NOW,
        )

        assert [row.voucher_id for row in dataset.rows] == [voucher_id]
        assert dataset.rows[0].usage_observed is False
        assert dataset.rows[0].ever_used is False
        assert dataset.totals.printed_never_used == 0
        assert dataset.totals.printed_usage_unknown == 1
    finally:
        db.close()


def test_expired_report_uses_persisted_expiration_time_offline(tmp_path):
    db, controller = _db(tmp_path)
    try:
        voucher_id = _voucher(
            db,
            controller,
            "expired-by-time",
            "9999900000",
            expired=False,
            expires_at="2026-09-20T09:00:00+00:00",
        )
        dataset = build_report_dataset(db, kind=ReportKind.EXPIRED, generated_at=NOW)
        assert [row.voucher_id for row in dataset.rows] == [voucher_id]
        assert dataset.rows[0].status == "Scaduto"
    finally:
        db.close()


def test_controller_scope_filters_historical_rows(tmp_path):
    db, first = _db(tmp_path)
    second = db.create_controller(
        name="Seconda sede",
        api_root="https://b.example",
        created_at=NOW,
    )
    try:
        _voucher(db, first, "first", "1111122222")
        _voucher(db, second, "second", "3333344444")
        dataset = build_report_dataset(
            db,
            kind=ReportKind.SUMMARY,
            generated_at=NOW,
            controller_id=first,
        )
        assert len(dataset.rows) == 1
        assert dataset.controller_label == "Reception"
    finally:
        db.close()


def test_empty_scoped_report_keeps_controller_label(tmp_path):
    db, controller = _db(tmp_path)
    try:
        dataset = build_report_dataset(
            db,
            kind=ReportKind.SUMMARY,
            generated_at=NOW,
            controller_id=controller,
        )
        assert dataset.rows == ()
        assert dataset.controller_label == "Reception"
    finally:
        db.close()


def test_controller_foreign_key_preserves_report_history(tmp_path):
    db, controller = _db(tmp_path)
    try:
        _voucher(db, controller, "kept", "1111122222")
        with db.transaction() as tx:
            with pytest.raises(sqlite3.IntegrityError):
                tx.execute("DELETE FROM controllers WHERE id=?", (controller,))
        dataset = build_report_dataset(
            db,
            kind=ReportKind.FULL_HISTORY,
            generated_at=NOW,
        )
        assert len(dataset.rows) == 1
        assert dataset.rows[0].controller_name == "Reception"
    finally:
        db.close()


def test_retention_archived_row_remains_in_full_history(tmp_path):
    db, controller = _db(tmp_path)
    try:
        voucher_id = _voucher(
            db,
            controller,
            "archived",
            "1234567890",
            name="Old guest",
            created_at="2025-01-01T09:00:00+00:00",
        )
        with db.transaction() as tx:
            tx.execute(
                """UPDATE vouchers
                   SET code=?, name='', assigned_to='', notes='',
                       is_nominal=NULL, nominality_redacted=1, archived_at=?
                   WHERE id=?""",
                (
                    f"ARCHIVED-{voucher_id}",
                    "2026-09-27T08:00:00+00:00",
                    voucher_id,
                ),
            )
        dataset = build_report_dataset(
            db,
            kind=ReportKind.FULL_HISTORY,
            generated_at=NOW,
        )
        row = next(item for item in dataset.rows if item.voucher_id == voucher_id)
        assert row.status == "Archiviato"
        assert row.archived_at == "2026-09-27T08:00:00+00:00"
        assert row.code == ""
        assert row.recipient == ""
        assert row.nominality_redacted is True
    finally:
        db.close()


def test_security_revoked_report_preserves_history_and_labels_status(tmp_path):
    db, controller = _db(tmp_path)
    try:
        voucher_id = _voucher(
            db,
            controller,
            "revoked",
            "7777788888",
            name="Guest Revoked",
        )
        db.record_print_audit(
            controller_id=controller,
            audit_id="revoked-job",
            codes=["77777-88888"],
            output_file="revoked.pdf",
            document_copies=1,
            printed_at="2026-09-10T08:00:00+00:00",
            windows_user="PC\\\\operator",
        )
        record_security_revocations(
            db,
            voucher_ids=[voucher_id],
            revoked_at="2026-09-30T08:00:00+00:00",
            windows_user="PC\\\\operator",
        )

        dataset = build_report_dataset(
            db,
            kind=ReportKind.SECURITY_REVOKED,
            generated_at=NOW,
        )
        assert [row.voucher_id for row in dataset.rows] == [voucher_id]
        row = dataset.rows[0]
        assert row.status == "Revocato per sicurezza"
        assert row.recipient == "Guest Revoked"
        assert row.unifi_name == "Guest Revoked"
        assert row.security_revoked_at == "2026-09-30T08:00:00+00:00"
        assert dataset.totals.security_revoked_vouchers == 1

        stored = db.connection.execute(
            "SELECT code, archived_at FROM vouchers WHERE id=?",
            (voucher_id,),
        ).fetchone()
        assert stored["code"] == "7777788888"
        assert stored["archived_at"] is None
    finally:
        db.close()



def test_report_rows_remain_unique_with_multiple_prints_and_revocation_events(tmp_path):
    db, controller = _db(tmp_path)
    try:
        voucher_id = _voucher(db, controller, "unique-row", "1010101010")
        db.record_print_audit(
            controller_id=controller,
            audit_id="job-unique-1",
            codes=["10101-01010"],
            output_file="first.pdf",
            document_copies=2,
            printed_at="2026-09-02T10:00:00+00:00",
            windows_user=r"PC\alice",
        )
        db.record_print_audit(
            controller_id=controller,
            audit_id="job-unique-2",
            codes=["10101-01010"],
            output_file="second.pdf",
            document_copies=3,
            printed_at="2026-09-03T10:00:00+00:00",
            windows_user=r"PC\bob",
        )
        record_security_revocations(
            db,
            voucher_ids=[voucher_id],
            revoked_at="2026-09-30T08:00:00+00:00",
            windows_user=r"PC\admin",
        )
        # Additional historical event of the same type must not multiply report rows.
        with db.transaction() as tx:
            tx.execute(
                """INSERT INTO voucher_events(
                       event_uuid, voucher_id, event_type, occurred_at,
                       source, windows_user, details_json
                   ) VALUES (
                       'extra-security-event', ?, 'SECURITY_REVOKED',
                       '2026-09-30T09:00:00+00:00',
                       'SYSTEM', 'PC\\admin', '{}'
                   )""",
                (voucher_id,),
            )

        dataset = build_report_dataset(
            db,
            kind=ReportKind.FULL_HISTORY,
            generated_at=NOW,
        )

        matching = [row for row in dataset.rows if row.voucher_id == voucher_id]
        assert len(matching) == 1
        row = matching[0]
        assert row.print_jobs == 2
        assert row.physical_copies == 5
        assert row.reprint_jobs == 1
        assert row.reprint_copies == 3
        assert row.print_operators == (r"PC\alice", r"PC\bob")
        assert row.security_revoked_at == "2026-09-30T09:00:00+00:00"
        assert dataset.totals.vouchers == 1
        assert dataset.totals.print_jobs == 2
        assert dataset.totals.physical_copies == 5
        assert dataset.totals.security_revoked_vouchers == 1
    finally:
        db.close()


def test_same_code_different_unifi_ids_are_distinct_and_print_audit_fails_closed(tmp_path):
    db, controller = _db(tmp_path)
    try:
        first = _voucher(db, controller, "uuid-first", "2020202020", name="First")
        second = _voucher(db, controller, "uuid-second", "2020202020", name="Second")

        dataset = build_report_dataset(
            db,
            kind=ReportKind.FULL_HISTORY,
            generated_at=NOW,
        )
        ids = [row.voucher_id for row in dataset.rows]
        assert ids.count(first) == 1
        assert ids.count(second) == 1
        assert dataset.totals.vouchers == 2
        assert {row.unifi_id for row in dataset.rows} == {
            "uuid-first",
            "uuid-second",
        }

        with pytest.raises(RuntimeError, match="missing or ambiguous"):
            db.record_print_audit(
                controller_id=controller,
                audit_id="ambiguous-code-job",
                codes=["20202-02020"],
                output_file="ambiguous.pdf",
                document_copies=1,
                printed_at="2026-09-02T10:00:00+00:00",
                windows_user=r"PC\operator",
            )

        assert db.connection.execute(
            "SELECT COUNT(*) FROM print_jobs"
        ).fetchone()[0] == 0
        assert db.connection.execute(
            "SELECT COUNT(*) FROM voucher_prints"
        ).fetchone()[0] == 0
    finally:
        db.close()



def test_nominality_exports_partition_rows_without_overlap(tmp_path):
    db, controller = _db(tmp_path)
    try:
        nominal = _voucher(db, controller, "nominal-partition", "3030303030")
        non_nominal = _voucher(db, controller, "non-nominal-partition", "4040404040")
        unclassified = _voucher(db, controller, "unclassified-partition", "5050505050")
        redacted = _voucher(db, controller, "redacted-partition", "6060606060")

        db.mark_application_created_vouchers(
            controller_id=controller,
            unifi_ids=["nominal-partition"],
            is_nominal=True,
            aligned_at="2026-10-01T08:00:00+00:00",
        )
        db.mark_application_created_vouchers(
            controller_id=controller,
            unifi_ids=["non-nominal-partition"],
            is_nominal=False,
            aligned_at="2026-10-01T08:00:00+00:00",
        )
        with db.transaction() as tx:
            tx.execute(
                """UPDATE vouchers
                   SET is_nominal=NULL, nominality_redacted=1
                   WHERE id=?""",
                (redacted,),
            )

        nominal_ds = build_report_dataset(
            db, kind=ReportKind.NOMINAL, generated_at=NOW
        )
        non_nominal_ds = build_report_dataset(
            db, kind=ReportKind.NON_NOMINAL, generated_at=NOW
        )
        unclassified_ds = build_report_dataset(
            db, kind=ReportKind.UNCLASSIFIED, generated_at=NOW
        )
        redacted_ds = build_report_dataset(
            db, kind=ReportKind.NOMINALITY_REDACTED, generated_at=NOW
        )
        summary = build_report_dataset(
            db, kind=ReportKind.SUMMARY, generated_at=NOW
        )

        assert {row.voucher_id for row in nominal_ds.rows} == {nominal}
        assert {row.voucher_id for row in non_nominal_ds.rows} == {non_nominal}
        assert {row.voucher_id for row in unclassified_ds.rows} == {unclassified}
        assert {row.voucher_id for row in redacted_ds.rows} == {redacted}
        assert (
            summary.totals.nominal_vouchers
            + summary.totals.non_nominal_vouchers
            + summary.totals.unclassified_vouchers
            + summary.totals.redacted_nominality_vouchers
            == summary.totals.vouchers
        )
    finally:
        db.close()


def test_reporting_fails_closed_on_inconsistent_usage_state(tmp_path):
    db, controller = _db(tmp_path)
    try:
        voucher_id = _voucher(db, controller, "bad-usage", "7070707070")
        with db.transaction() as tx:
            tx.execute(
                """UPDATE vouchers
                   SET ever_used=1, usage_observed=0
                   WHERE id=?""",
                (voucher_id,),
            )

        with pytest.raises(RuntimeError, match="without usage evidence"):
            build_report_dataset(
                db,
                kind=ReportKind.SUMMARY,
                generated_at=NOW,
            )
    finally:
        db.close()


def test_reporting_fails_closed_on_inconsistent_nominality_state(tmp_path):
    db, controller = _db(tmp_path)
    try:
        voucher_id = _voucher(db, controller, "bad-nominality", "8080808080")
        with db.transaction() as tx:
            tx.execute(
                """UPDATE vouchers
                   SET is_nominal=1, nominality_redacted=1
                   WHERE id=?""",
                (voucher_id,),
            )

        with pytest.raises(RuntimeError, match="classification and redaction"):
            build_report_dataset(
                db,
                kind=ReportKind.SUMMARY,
                generated_at=NOW,
            )
    finally:
        db.close()



def test_reporting_rejects_missing_unifi_identity(tmp_path):
    db, controller = _db(tmp_path)
    try:
        voucher_id = _voucher(db, controller, "valid-id", "9090909090")
        with db.transaction() as tx:
            tx.execute(
                "UPDATE vouchers SET unifi_id='' WHERE id=?",
                (voucher_id,),
            )

        with pytest.raises(RuntimeError, match="without UniFi identity"):
            build_report_dataset(
                db,
                kind=ReportKind.FULL_HISTORY,
                generated_at=NOW,
            )
    finally:
        db.close()



def test_report_uses_unifi_name_as_the_single_recipient(tmp_path):
    db, controller = _db(tmp_path)
    try:
        voucher_id = _voucher(
            db,
            controller,
            "source-separation",
            "3131313131",
            name="Descrizione controller",
        )
        with db.transaction() as tx:
            # assigned_to is a deprecated compatibility column from an earlier
            # 5.1 preview and must never override the controller-owned name.
            tx.execute(
                "UPDATE vouchers SET assigned_to=?, notes=? WHERE id=?",
                ("Valore locale obsoleto", "Nota amministrativa", voucher_id),
            )

        dataset = build_report_dataset(
            db,
            kind=ReportKind.FULL_HISTORY,
            generated_at=NOW,
        )
        row = next(item for item in dataset.rows if item.voucher_id == voucher_id)
        assert row.recipient == "Descrizione controller"
        assert row.unifi_name == "Descrizione controller"
        assert row.local_notes == "Nota amministrativa"
    finally:
        db.close()


def test_report_preserves_controller_recipient_without_local_duplicate(tmp_path):
    db, controller = _db(tmp_path)
    try:
        voucher_id = _voucher(
            db,
            controller,
            "canonical-recipient",
            "4141414141",
            name="Solo descrizione UniFi",
        )

        dataset = build_report_dataset(
            db,
            kind=ReportKind.FULL_HISTORY,
            generated_at=NOW,
        )
        row = next(item for item in dataset.rows if item.voucher_id == voucher_id)
        assert row.recipient == "Solo descrizione UniFi"
        assert row.unifi_name == "Solo descrizione UniFi"
    finally:
        db.close()



def test_unprinted_warning_report_uses_configured_creation_threshold(tmp_path):
    db, controller = _db(tmp_path)
    try:
        eligible = _voucher(
            db,
            controller,
            "old-unprinted",
            "1010101010",
            created_at="2026-09-01T09:00:00+00:00",
        )
        _voucher(
            db,
            controller,
            "recent-unprinted",
            "2020202020",
            created_at="2026-09-25T09:00:00+00:00",
        )
        with db.transaction() as tx:
            tx.execute(
                """UPDATE vouchers
                   SET alignment_completed_at=?,
                       print_state='NOT_PRINTED',
                       usage_observed=1,
                       ever_used=0,
                       authorized_guest_count=0,
                       present_on_controller=1,
                       expired=0
                   WHERE controller_id=?""",
                (NOW, controller),
            )
        set_unprinted_warning_days(db, days=10, now=NOW)

        dataset = build_report_dataset(
            db,
            kind=ReportKind.UNPRINTED_WARNING,
            generated_at=NOW,
            controller_id=controller,
        )

        assert [row.voucher_id for row in dataset.rows] == [eligible]
        assert dataset.title.endswith("(10 giorni dalla creazione)")
    finally:
        db.close()


def test_security_review_report_uses_last_print_threshold(tmp_path):
    db, controller = _db(tmp_path)
    try:
        voucher_id = _voucher(
            db,
            controller,
            "printed-unused",
            "3030303030",
            created_at="2026-08-01T09:00:00+00:00",
        )
        db.record_print_audit(
            controller_id=controller,
            audit_id="security-report-print",
            codes=["30303-03030"],
            output_file="voucher.pdf",
            document_copies=1,
            printed_at="2026-09-01T09:00:00+00:00",
            windows_user="operator",
        )
        with db.transaction() as tx:
            tx.execute(
                """UPDATE vouchers
                   SET usage_observed=1,
                       ever_used=0,
                       authorized_guest_count=0,
                       present_on_controller=1,
                       expired=0,
                       last_seen_at=?
                   WHERE id=?""",
                (NOW, voucher_id),
            )
        set_security_revoke_days(db, days=10, now=NOW)

        dataset = build_report_dataset(
            db,
            kind=ReportKind.SECURITY_REVIEW,
            generated_at=NOW,
            controller_id=controller,
        )

        assert [row.voucher_id for row in dataset.rows] == [voucher_id]
        assert dataset.title.endswith("(10 giorni dall'ultima stampa)")
    finally:
        db.close()



def test_unknown_print_state_is_not_reported_as_never_printed(tmp_path):
    db, controller = _db(tmp_path)
    try:
        voucher_id = _voucher(
            db,
            controller,
            "legacy-unknown-print",
            "4040404040",
        )

        never_printed = build_report_dataset(
            db,
            kind=ReportKind.NEVER_PRINTED,
            generated_at=NOW,
        )
        unknown = build_report_dataset(
            db,
            kind=ReportKind.PRINT_UNKNOWN,
            generated_at=NOW,
        )

        assert voucher_id not in {row.voucher_id for row in never_printed.rows}
        assert [row.voucher_id for row in unknown.rows] == [voucher_id]
        assert unknown.totals.print_unknown_vouchers == 1
        assert unknown.totals.never_printed == 0
    finally:
        db.close()


def test_explicit_legacy_printed_state_counts_as_printed_without_known_print_date(
    tmp_path,
):
    db, controller = _db(tmp_path)
    try:
        voucher_id = _voucher(
            db,
            controller,
            "legacy-known-printed",
            "5050505050",
        )
        with db.transaction() as tx:
            tx.execute(
                """UPDATE vouchers
                   SET print_state='PRINTED',
                       alignment_completed_at=?
                   WHERE id=?""",
                (NOW, voucher_id),
            )

        printed = build_report_dataset(
            db,
            kind=ReportKind.PRINTED,
            generated_at=NOW,
        )

        assert [row.voucher_id for row in printed.rows] == [voucher_id]
        assert printed.rows[0].last_printed_at == ""
        assert printed.rows[0].status == "Stampato"
        assert printed.totals.printed_vouchers == 1
        assert printed.totals.print_unknown_vouchers == 0
    finally:
        db.close()
