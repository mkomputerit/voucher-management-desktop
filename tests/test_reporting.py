"""Regression tests for durable historical reporting semantics."""

from __future__ import annotations

import sqlite3

import pytest

from voucher_management.database import Database
from voucher_management.report_policy import ReportPurpose
from voucher_management.reporting import (
    ReportKind,
    build_report_dataset,
    validate_report_dataset_consistency,
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
        assert dataset.rows == ()
        assert dataset.totals.vouchers == 1
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
        )
        db.mark_application_created_vouchers(
            controller_id=controller,
            unifi_ids=["pinco"],
            is_nominal=True,
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
        assert dataset.rows == ()
        assert dataset.totals.vouchers == 2
        assert external > 0
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
        assert dataset.rows == ()
        assert dataset.totals.vouchers == 1
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


def test_legacy_import_is_not_expiry_or_controller_observation(tmp_path):
    db, controller = _db(tmp_path)
    legacy = db.create_controller(name="Previous backup", api_root="legacy-backup://synthetic", created_at=NOW)
    _voucher(db, legacy, "old", "9876543210", expired=True, expires_at=None)
    _voucher(db, controller, "live", "1111122222", used=1)
    expired = build_report_dataset(db, kind=ReportKind.EXPIRED, generated_at=NOW)
    assert expired.totals.vouchers == 0
    summary = build_report_dataset(db, kind=ReportKind.SUMMARY, generated_at=NOW)
    assert summary.rows == ()
    history = build_report_dataset(db, kind=ReportKind.FULL_HISTORY, generated_at=NOW)
    old = next(row for row in history.rows if row.controller_name == "Previous backup")
    assert old.last_synced_at == ""
    assert not old.expired
    assert "backup" in old.status.lower()
    assert "Registrazioni da backup precedente: 1" in summary.coverage_note
    db.close()


def test_empty_filtered_report_keeps_scope_observation_and_missing_data_reason(tmp_path):
    db, controller = _db(tmp_path)
    _voucher(db, controller, "v", "1111122222")
    report = build_report_dataset(db, kind=ReportKind.NOMINAL, generated_at=NOW)
    assert report.totals.vouchers == 0
    assert report.data_as_of == NOW
    assert "Nessun risultato" in report.coverage_note
    assert "nominalità non classificata 1" in report.coverage_note
    db.close()


def test_non_nominal_report_is_distinct_from_unclassified_and_redacted(tmp_path):
    db, controller = _db(tmp_path)
    try:
        non_nominal = _voucher(db, controller, "non-nominal", "1111122222")
        unclassified = _voucher(db, controller, "unclassified", "3333344444")
        redacted = _voucher(db, controller, "redacted", "5555566666")
        db.mark_application_created_vouchers(
            controller_id=controller,
            unifi_ids=["non-nominal"],
            is_nominal=False,
        )
        with db.transaction() as tx:
            tx.execute(
                """UPDATE vouchers SET is_nominal=NULL, nominality_redacted=1
                   WHERE id=?""",
                (redacted,),
            )

        dataset = build_report_dataset(
            db,
            kind=ReportKind.NON_NOMINAL,
            generated_at=NOW,
        )
        assert [row.voucher_id for row in dataset.rows] == [non_nominal]
        assert unclassified not in {row.voucher_id for row in dataset.rows}
        assert redacted not in {row.voucher_id for row in dataset.rows}
    finally:
        db.close()


def test_report_keeps_unifi_description_separate_from_local_recipient(tmp_path):
    db, controller = _db(tmp_path)
    try:
        _voucher(
            db,
            controller,
            "external",
            "1111122222",
            name="EMI06",
        )
        db.update_voucher_local_metadata(
            controller_id=controller,
            unifi_id="external",
            assigned_to="Mario Rossi",
            notes="",
            is_nominal=True,
            updated_at=NOW,
            windows_user="operator",
        )
        dataset = build_report_dataset(
            db,
            kind=ReportKind.NOMINAL,
            generated_at=NOW,
        )
        assert dataset.rows[0].controller_description == "EMI06"
        assert dataset.rows[0].recipient == "Mario Rossi"
    finally:
        db.close()


def test_usage_total_excludes_rows_without_controller_usage_evidence(tmp_path):
    db, controller = _db(tmp_path)
    try:
        known = _voucher(db, controller, "known", "1111122222", used=3)
        unknown = _voucher(db, controller, "unknown", "3333344444", used=7)
        with db.transaction() as tx:
            tx.execute(
                "UPDATE vouchers SET usage_observed=0 WHERE id=?",
                (unknown,),
            )
        summary = build_report_dataset(
            db,
            kind=ReportKind.SUMMARY,
            generated_at=NOW,
        )
        assert summary.totals.total_controller_uses == 3
        assert summary.totals.usage_unknown_vouchers == 1
        assert known > 0
    finally:
        db.close()


def test_coverage_separates_unclassified_from_privacy_redaction(tmp_path):
    db, controller = _db(tmp_path)
    try:
        _voucher(db, controller, "unclassified", "1111122222")
        redacted = _voucher(db, controller, "redacted", "3333344444")
        with db.transaction() as tx:
            tx.execute(
                """UPDATE vouchers SET nominality_redacted=1
                   WHERE id=?""",
                (redacted,),
            )
        summary = build_report_dataset(
            db,
            kind=ReportKind.SUMMARY,
            generated_at=NOW,
        )
        assert "nominalità non classificata 1" in summary.coverage_note
        assert "nominalità rimossa per privacy 1" in summary.coverage_note
    finally:
        db.close()


def test_freshness_bounds_compare_timezone_offsets_chronologically(tmp_path):
    db, controller = _db(tmp_path)
    try:
        _voucher(
            db,
            controller,
            "later-text-earlier-time",
            "1111122222",
            synced_at="2026-09-29T10:30:00+02:00",
        )
        _voucher(
            db,
            controller,
            "earlier-text-later-time",
            "3333344444",
            synced_at="2026-09-29T09:00:00+00:00",
        )
        summary = build_report_dataset(
            db,
            kind=ReportKind.SUMMARY,
            generated_at=NOW,
        )
        assert summary.data_from == "2026-09-29T10:30:00+02:00"
        assert summary.data_as_of == "2026-09-29T09:00:00+00:00"
    finally:
        db.close()


def test_report_query_defaults_to_credential_minimization(tmp_path):
    db, controller = _db(tmp_path)
    try:
        voucher_id = _voucher(
            db,
            controller,
            "safe-query",
            "1234567890",
            name="Descrizione UniFi",
        )
        db.update_voucher_local_metadata(
            controller_id=controller,
            unifi_id="safe-query",
            assigned_to="Mario Rossi",
            notes="Nota non esportata",
            is_nominal=True,
            updated_at=NOW,
            windows_user="PC\\operator",
        )
        db.record_print_audit(
            controller_id=controller,
            audit_id="safe-query-print",
            codes=["12345-67890"],
            output_file="voucher.pdf",
            document_copies=1,
            printed_at=NOW,
            windows_user="PC\\operator",
        )

        row = db.report_voucher_rows(controller_id=controller)[0]
        assert int(row["voucher_id"]) == voucher_id
        assert row["code"] == ""
        assert row["name"] == ""
        assert row["assigned_to"] == ""
        assert row["print_operators"] == ""
        assert "controller_api_root" not in row.keys()
        assert row["legacy_source"] == 0

        details = db.report_voucher_personal_details(
            voucher_ids=[voucher_id],
        )
        assert set(details) == {voucher_id}
        assert details[voucher_id]["name"] == "Descrizione UniFi"
        assert details[voucher_id]["assigned_to"] == "Mario Rossi"
        assert details[voucher_id]["print_operators"] == "PC\\operator"
    finally:
        db.close()


def test_report_query_exposes_code_only_when_explicitly_authorized(tmp_path):
    db, controller = _db(tmp_path)
    try:
        _voucher(db, controller, "handoff", "1234567890")
        hidden = db.report_voucher_rows(controller_id=controller)[0]
        exposed = db.report_voucher_rows(
            controller_id=controller,
            include_voucher_code=True,
        )[0]
        assert hidden["code"] == ""
        assert exposed["code"] == "1234567890"
    finally:
        db.close()


def test_filtered_report_enriches_only_rows_that_match(tmp_path):
    db, controller = _db(tmp_path)
    try:
        nominal = _voucher(
            db,
            controller,
            "nominal-only",
            "1111122222",
            name="Nominale UniFi",
        )
        excluded = _voucher(
            db,
            controller,
            "excluded",
            "3333344444",
            name="Non nominale UniFi",
        )
        db.update_voucher_local_metadata(
            controller_id=controller,
            unifi_id="nominal-only",
            assigned_to="Mario Rossi",
            notes="",
            is_nominal=True,
            updated_at=NOW,
            windows_user="PC\\alice",
        )
        db.update_voucher_local_metadata(
            controller_id=controller,
            unifi_id="excluded",
            assigned_to="Dato che non deve entrare",
            notes="",
            is_nominal=False,
            updated_at=NOW,
            windows_user="PC\\bob",
        )

        requested = []
        original = db.report_voucher_personal_details

        def tracked(*, voucher_ids):
            requested.extend(voucher_ids)
            return original(voucher_ids=voucher_ids)

        db.report_voucher_personal_details = tracked
        dataset = build_report_dataset(
            db,
            kind=ReportKind.NOMINAL,
            generated_at=NOW,
        )
        assert requested == [nominal]
        assert [row.voucher_id for row in dataset.rows] == [nominal]
        assert dataset.rows[0].recipient == "Mario Rossi"
        assert dataset.rows[0].controller_description == "Nominale UniFi"
        assert excluded not in requested
    finally:
        db.close()


def test_personal_detail_lookup_chunks_large_id_sets(tmp_path):
    db, controller = _db(tmp_path)
    try:
        real_id = _voucher(
            db,
            controller,
            "chunked-detail",
            "1234567890",
            name="Descrizione",
        )
        requested = list(range(10_000, 11_005)) + [real_id]
        details = db.report_voucher_personal_details(voucher_ids=requested)
        assert set(details) == {real_id}
        assert details[real_id]["name"] == "Descrizione"
    finally:
        db.close()


def test_uncertain_usage_provenance_dominates_conflicting_sticky_flag(tmp_path):
    db, controller = _db(tmp_path)
    try:
        voucher_id = _voucher(
            db,
            controller,
            "migrated-used",
            "1212121212",
            used=0,
        )
        # Simulate contradictory migrated metadata. The coverage/provenance
        # flag is the reporting gate: without trusted usage coverage we must
        # not promote the sticky flag to a confirmed "used" statement.
        with db.transaction() as tx:
            tx.execute(
                """UPDATE vouchers
                   SET ever_used=1, usage_observed=0
                   WHERE id=?""",
                (voucher_id,),
            )

        summary = build_report_dataset(
            db,
            kind=ReportKind.SUMMARY,
            generated_at=NOW,
        )
        assert summary.totals.vouchers == 1
        assert summary.totals.used_vouchers == 0
        assert summary.totals.never_used_vouchers == 0
        assert summary.totals.usage_unknown_vouchers == 1
        assert (
            summary.totals.used_vouchers
            + summary.totals.never_used_vouchers
            + summary.totals.usage_unknown_vouchers
            == summary.totals.vouchers
        )

        unknown = build_report_dataset(
            db,
            kind=ReportKind.USAGE_UNKNOWN,
            generated_at=NOW,
        )
        assert [row.voucher_id for row in unknown.rows] == [voucher_id]
        assert unknown.rows[0].status == "Utilizzo non determinabile"

        used = build_report_dataset(
            db,
            kind=ReportKind.USED,
            generated_at=NOW,
        )
        assert used.rows == ()
    finally:
        db.close()


def test_unprinted_row_status_describes_evidence_not_absolute_history(tmp_path):
    db, controller = _db(tmp_path)
    try:
        voucher_id = _voucher(
            db,
            controller,
            "no-print-evidence",
            "3434343434",
        )
        dataset = build_report_dataset(
            db,
            kind=ReportKind.FULL_HISTORY,
            generated_at=NOW,
        )
        row = next(row for row in dataset.rows if row.voucher_id == voucher_id)
        assert row.status == "Senza stampe registrate"
    finally:
        db.close()


def test_report_totals_count_distinct_print_jobs_not_voucher_relations(tmp_path):
    db, controller = _db(tmp_path)
    try:
        first = _voucher(db, controller, "print-a", "1111122222")
        second = _voucher(db, controller, "print-b", "3333344444")

        db.record_print_audit(
            controller_id=controller,
            audit_id="shared-job-1",
            codes=["11111-22222", "33333-44444"],
            output_file="batch.pdf",
            document_copies=1,
            printed_at="2026-09-10T10:00:00+00:00",
            windows_user="PC\\alice",
        )
        db.record_print_audit(
            controller_id=controller,
            audit_id="shared-job-2",
            codes=["11111-22222", "33333-44444"],
            output_file="batch-reprint.pdf",
            document_copies=1,
            printed_at="2026-09-11T10:00:00+00:00",
            windows_user="PC\\alice",
        )

        summary = build_report_dataset(
            db,
            kind=ReportKind.SUMMARY,
            generated_at=NOW,
        )
        assert summary.totals.vouchers == 2
        assert summary.totals.printed_vouchers == 2
        assert summary.totals.print_jobs == 2
        assert summary.totals.reprint_jobs == 1
        assert summary.totals.physical_copies == 4
        assert summary.totals.reprint_copies == 2

        full = build_report_dataset(
            db,
            kind=ReportKind.FULL_HISTORY,
            generated_at=NOW,
        )
        by_id = {row.voucher_id: row for row in full.rows}
        assert by_id[first].print_jobs == 2
        assert by_id[second].print_jobs == 2
        assert by_id[first].reprint_jobs == 1
        assert by_id[second].reprint_jobs == 1
        # Per-voucher detail remains per-voucher; only aggregate "job" totals
        # are deduplicated at the document submission level.
        assert full.totals.print_jobs == 2
        assert full.totals.reprint_jobs == 1
    finally:
        db.close()


def test_printed_unused_requires_controller_observation_after_first_print(tmp_path):
    db, controller = _db(tmp_path)
    try:
        voucher_id = _voucher(
            db,
            controller,
            "printed-after-last-sync",
            "5656565656",
            synced_at="2026-09-01T09:00:00+00:00",
        )
        db.record_print_audit(
            controller_id=controller,
            audit_id="after-sync-print",
            codes=["56565-65656"],
            output_file="voucher.pdf",
            document_copies=1,
            printed_at="2026-09-02T10:00:00+00:00",
            windows_user="PC\\alice",
        )

        stale = build_report_dataset(
            db,
            kind=ReportKind.PRINTED_UNUSED,
            generated_at=NOW,
        )
        assert stale.rows == ()
        stale_summary = build_report_dataset(
            db,
            kind=ReportKind.SUMMARY,
            generated_at=NOW,
        )
        assert stale_summary.totals.printed_never_used == 0

        with db.transaction() as tx:
            tx.execute(
                """UPDATE vouchers
                   SET last_synced_at=?, last_seen_at=?, usage_observed=1,
                       authorized_guest_count=0, ever_used=0
                   WHERE id=?""",
                (
                    "2026-09-03T10:00:00+00:00",
                    "2026-09-03T10:00:00+00:00",
                    voucher_id,
                ),
            )

        observed_after_print = build_report_dataset(
            db,
            kind=ReportKind.PRINTED_UNUSED,
            generated_at=NOW,
        )
        assert [row.voucher_id for row in observed_after_print.rows] == [voucher_id]
        fresh_summary = build_report_dataset(
            db,
            kind=ReportKind.SUMMARY,
            generated_at=NOW,
        )
        assert fresh_summary.totals.printed_never_used == 1
    finally:
        db.close()


def test_internal_migration_operator_is_labeled_as_historical_import(tmp_path):
    db, controller = _db(tmp_path)
    try:
        voucher_id = _voucher(db, controller, "legacy-print-label", "7878787878")
        db.record_print_audit(
            controller_id=controller,
            audit_id="migration-print-label",
            codes=["78787-87878"],
            output_file="legacy.pdf",
            document_copies=1,
            printed_at="2026-09-02T10:00:00+00:00",
            windows_user="MIGRATION",
        )
        dataset = build_report_dataset(
            db,
            kind=ReportKind.PRINTED,
            generated_at=NOW,
        )
        row = next(row for row in dataset.rows if row.voucher_id == voucher_id)
        assert row.print_operators == ("Importazione storica",)
    finally:
        db.close()


def test_privacy_redaction_dominates_stale_nominal_bit(tmp_path):
    db, controller = _db(tmp_path)
    try:
        voucher_id = _voucher(db, controller, "redacted-stale-bit", "9090909090")
        with db.transaction() as tx:
            tx.execute(
                """UPDATE vouchers
                   SET is_nominal=1, nominality_redacted=1
                   WHERE id=?""",
                (voucher_id,),
            )

        summary = build_report_dataset(
            db,
            kind=ReportKind.SUMMARY,
            generated_at=NOW,
        )
        assert summary.totals.vouchers == 1
        assert summary.totals.nominal_vouchers == 0
        assert summary.totals.non_nominal_vouchers == 0
        assert summary.totals.unclassified_vouchers == 0
        assert summary.totals.redacted_nominality_vouchers == 1

        nominal = build_report_dataset(
            db,
            kind=ReportKind.NOMINAL,
            generated_at=NOW,
        )
        assert nominal.rows == ()

        redacted = build_report_dataset(
            db,
            kind=ReportKind.NOMINALITY_REDACTED,
            generated_at=NOW,
        )
        assert [row.voucher_id for row in redacted.rows] == [voucher_id]
    finally:
        db.close()


def test_absence_sync_does_not_make_usage_evidence_look_fresher(tmp_path):
    db, controller = _db(tmp_path)
    try:
        voucher_id = _voucher(
            db,
            controller,
            "disappeared",
            "6767676767",
            synced_at="2026-09-01T09:00:00+00:00",
        )
        db.record_print_audit(
            controller_id=controller,
            audit_id="disappeared-print",
            codes=["67676-76767"],
            output_file="voucher.pdf",
            document_copies=1,
            printed_at="2026-09-02T10:00:00+00:00",
            windows_user="PC\\alice",
        )
        with db.transaction() as tx:
            tx.execute(
                """UPDATE vouchers
                   SET present_on_controller=0,
                       last_synced_at=?
                   WHERE id=?""",
                ("2026-09-05T10:00:00+00:00", voucher_id),
            )

        printed_unused = build_report_dataset(
            db,
            kind=ReportKind.PRINTED_UNUSED,
            generated_at=NOW,
        )
        assert printed_unused.rows == ()

        history = build_report_dataset(
            db,
            kind=ReportKind.FULL_HISTORY,
            generated_at=NOW,
        )
        row = next(row for row in history.rows if row.voucher_id == voucher_id)
        assert row.last_synced_at == "2026-09-05T10:00:00+00:00"
        assert row.last_seen_at == "2026-09-01T09:00:00+00:00"
        assert history.data_as_of == "2026-09-01T09:00:00+00:00"
    finally:
        db.close()


def test_verified_controller_absence_is_visible_in_report_status(tmp_path):
    db, controller = _db(tmp_path)
    try:
        voucher_id = _voucher(
            db,
            controller,
            "absent-status",
            "4545454545",
        )
        with db.transaction() as tx:
            tx.execute(
                """UPDATE vouchers
                   SET present_on_controller=0, last_synced_at=?
                   WHERE id=?""",
                ("2026-09-30T08:00:00+00:00", voucher_id),
            )

        dataset = build_report_dataset(
            db,
            kind=ReportKind.FULL_HISTORY,
            generated_at=NOW,
        )
        row = next(row for row in dataset.rows if row.voucher_id == voucher_id)
        assert row.status == "Senza stampe registrate · non presente su UniFi"
    finally:
        db.close()


def test_summary_classification_partitions_cover_scope_exactly_once(tmp_path):
    db, controller = _db(tmp_path)
    try:
        used = _voucher(db, controller, "partition-used", "1010101010", used=1)
        unused = _voucher(db, controller, "partition-unused", "2020202020", used=0)
        unknown = _voucher(db, controller, "partition-unknown", "3030303030", used=0)
        redacted = _voucher(db, controller, "partition-redacted", "4040404040", used=0)

        db.mark_application_created_vouchers(
            controller_id=controller,
            unifi_ids=["partition-used"],
            is_nominal=True,
        )
        db.mark_application_created_vouchers(
            controller_id=controller,
            unifi_ids=["partition-unused"],
            is_nominal=False,
        )
        with db.transaction() as tx:
            tx.execute(
                "UPDATE vouchers SET usage_observed=0 WHERE id=?",
                (unknown,),
            )
            tx.execute(
                """UPDATE vouchers
                   SET is_nominal=NULL, nominality_redacted=1
                   WHERE id=?""",
                (redacted,),
            )

        db.record_print_audit(
            controller_id=controller,
            audit_id="partition-print",
            codes=["10101-01010"],
            output_file="partition.pdf",
            document_copies=1,
            printed_at="2026-09-05T10:00:00+00:00",
            windows_user="PC\\alice",
        )

        summary = build_report_dataset(
            db,
            kind=ReportKind.SUMMARY,
            generated_at=NOW,
        )
        totals = summary.totals
        assert totals.vouchers == 4
        assert (
            totals.used_vouchers
            + totals.never_used_vouchers
            + totals.usage_unknown_vouchers
            == totals.vouchers
        )
        assert (
            totals.nominal_vouchers
            + totals.non_nominal_vouchers
            + totals.unclassified_vouchers
            + totals.redacted_nominality_vouchers
            == totals.vouchers
        )
        assert totals.printed_vouchers + totals.never_printed == totals.vouchers
        assert totals.generated_vouchers + totals.unknown_origin_vouchers == totals.vouchers

        assert used > 0
        assert unused > 0
    finally:
        db.close()


def test_report_consistency_guard_rejects_overlapping_usage_partition(tmp_path):
    db, controller = _db(tmp_path)
    try:
        _voucher(db, controller, "guard-row", "5151515151")
        dataset = build_report_dataset(
            db,
            kind=ReportKind.SUMMARY,
            generated_at=NOW,
        )
        from dataclasses import replace

        broken = replace(
            dataset,
            totals=replace(
                dataset.totals,
                used_vouchers=1,
                never_used_vouchers=1,
                usage_unknown_vouchers=0,
            ),
        )
        with pytest.raises(RuntimeError, match="usage"):
            validate_report_dataset_consistency(broken)
    finally:
        db.close()


def test_report_consistency_guard_rejects_detail_count_mismatch(tmp_path):
    db, controller = _db(tmp_path)
    try:
        _voucher(db, controller, "guard-detail", "6161616161")
        dataset = build_report_dataset(
            db,
            kind=ReportKind.FULL_HISTORY,
            generated_at=NOW,
        )
        from dataclasses import replace

        broken = replace(dataset, rows=())
        with pytest.raises(RuntimeError, match="row count"):
            validate_report_dataset_consistency(broken)
    finally:
        db.close()


def test_naive_persisted_times_follow_utc_contract(tmp_path):
    db, controller = _db(tmp_path)
    try:
        voucher_id = _voucher(
            db,
            controller,
            "naive-time",
            "7272727272",
            expires_at="2026-09-29T09:30:00",
            synced_at="2026-09-29T09:00:00",
        )
        dataset = build_report_dataset(
            db,
            kind=ReportKind.EXPIRED,
            generated_at="2026-09-29T11:00:00+02:00",
        )
        # 11:00 +02 == 09:00 UTC, so a naive persisted 09:30 UTC expiry
        # has not yet occurred.
        assert dataset.rows == ()

        expired = build_report_dataset(
            db,
            kind=ReportKind.EXPIRED,
            generated_at="2026-09-29T12:00:00+02:00",
        )
        assert [row.voucher_id for row in expired.rows] == [voucher_id]
    finally:
        db.close()


def test_privacy_redacted_report_never_exports_stale_local_recipient(tmp_path):
    db, controller = _db(tmp_path)
    try:
        voucher_id = _voucher(
            db,
            controller,
            "redacted-recipient",
            "8383838383",
            name="Descrizione UniFi non locale",
        )
        with db.transaction() as tx:
            tx.execute(
                """UPDATE vouchers
                   SET assigned_to=?, is_nominal=1, nominality_redacted=1
                   WHERE id=?""",
                ("Dato locale che deve restare nascosto", voucher_id),
            )

        dataset = build_report_dataset(
            db,
            kind=ReportKind.NOMINALITY_REDACTED,
            generated_at=NOW,
        )
        assert [row.voucher_id for row in dataset.rows] == [voucher_id]
        assert dataset.rows[0].recipient == ""
        assert dataset.rows[0].controller_description == "Descrizione UniFi non locale"
    finally:
        db.close()


def test_archived_report_defensively_hides_stale_personal_text(tmp_path):
    db, controller = _db(tmp_path)
    try:
        voucher_id = _voucher(
            db,
            controller,
            "archived-stale-text",
            "8484848484",
            name="Nome rimasto per errore",
        )
        with db.transaction() as tx:
            tx.execute(
                """UPDATE vouchers
                   SET assigned_to=?, archived_at=?, nominality_redacted=1
                   WHERE id=?""",
                (
                    "Destinatario rimasto per errore",
                    "2026-09-20T10:00:00+00:00",
                    voucher_id,
                ),
            )

        dataset = build_report_dataset(
            db,
            kind=ReportKind.FULL_HISTORY,
            generated_at=NOW,
        )
        row = next(row for row in dataset.rows if row.voucher_id == voucher_id)
        assert row.status == "Archiviato"
        assert row.recipient == ""
        assert row.controller_description == ""
    finally:
        db.close()


def test_filtered_report_freshness_uses_only_exported_rows_when_nonempty(tmp_path):
    db, controller = _db(tmp_path)
    try:
        old_unclassified = _voucher(
            db,
            controller,
            "old-unclassified",
            "1212121212",
            synced_at="2026-09-10T08:00:00+00:00",
        )
        recent_nominal = _voucher(
            db,
            controller,
            "recent-nominal",
            "3434343434",
            synced_at="2026-09-29T09:30:00+00:00",
        )
        db.mark_application_created_vouchers(
            controller_id=controller,
            unifi_ids=["recent-nominal"],
            is_nominal=True,
        )

        report = build_report_dataset(
            db,
            kind=ReportKind.NOMINAL,
            generated_at=NOW,
        )

        assert [row.voucher_id for row in report.rows] == [recent_nominal]
        assert old_unclassified not in {
            row.voucher_id for row in report.rows
        }
        assert report.data_from == "2026-09-29T09:30:00+00:00"
        assert report.data_as_of == "2026-09-29T09:30:00+00:00"
    finally:
        db.close()
