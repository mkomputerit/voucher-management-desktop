"""Tests for SQLite-backed historical reporting."""

from __future__ import annotations

from voucher_management.database import Database
from voucher_management.reporting import ReportKind, build_report_dataset


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


def _voucher(db, controller, unifi_id, code, *, name="", used=0, expired=False):
    return db.upsert_voucher(
        controller_id=controller,
        unifi_id=unifi_id,
        code=code,
        name=name,
        created_at="2026-09-01T09:00:00+00:00",
        imported_at="2026-09-01T09:05:00+00:00",
        duration_minutes=60,
        authorized_guest_limit=1,
        authorized_guest_count=used,
        expires_at="2026-10-01T09:00:00+00:00",
        expired=expired,
        last_synced_at=NOW,
    )


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
        assert dataset.rows[0].recipient == "Pinco Pallino"
        assert dataset.rows[0].is_nominal is True
        assert emi not in {row.voucher_id for row in dataset.rows}
    finally:
        db.close()


def test_generated_unused_requires_application_provenance_and_no_observed_use(tmp_path):
    db, controller = _db(tmp_path)
    try:
        created_unused = _voucher(db, controller, "created-unused", "1111122222")
        created_used = _voucher(db, controller, "created-used", "3333344444", used=2)
        external_unused = _voucher(db, controller, "external", "5555566666")
        db.mark_application_created_vouchers(
            controller_id=controller,
            unifi_ids=["created-unused", "created-used"],
            is_nominal=False,
        )

        dataset = build_report_dataset(
            db,
            kind=ReportKind.GENERATED_UNUSED,
            generated_at=NOW,
        )

        assert [row.voucher_id for row in dataset.rows] == [created_unused]
        assert created_used not in {row.voucher_id for row in dataset.rows}
        assert external_unused not in {row.voucher_id for row in dataset.rows}
    finally:
        db.close()


def test_missing_controller_usage_evidence_is_not_reported_as_never_used(tmp_path):
    db, controller = _db(tmp_path)
    try:
        voucher_id = _voucher(db, controller, "legacy-unknown", "1212121212")
        with db.transaction() as tx:
            tx.execute(
                """UPDATE vouchers
                   SET origin='LEGACY_APPLICATION', usage_observed=0, ever_used=0
                   WHERE id=?""",
                (voucher_id,),
            )

        never_used = build_report_dataset(
            db,
            kind=ReportKind.GENERATED_UNUSED,
            generated_at=NOW,
        )
        unknown = build_report_dataset(
            db,
            kind=ReportKind.USAGE_UNKNOWN,
            generated_at=NOW,
        )

        assert never_used.rows == ()
        assert [row.voucher_id for row in unknown.rows] == [voucher_id]
        assert unknown.rows[0].usage_observed is False
    finally:
        db.close()


def test_used_means_ever_observed_used_even_if_latest_counter_returns_zero(tmp_path):
    db, controller = _db(tmp_path)
    try:
        voucher_id = _voucher(db, controller, "used-history", "1111122222", used=0)
        from voucher_management.sync_store import persist_successful_snapshot
        from voucher_management.unifi_api import ApiVoucher

        def remote(used):
            return ApiVoucher(
                id="used-history",
                code="1111122222",
                recipient="",
                duration_minutes=60,
                create_time=1_700_000_000,
                quota=1,
                used=used,
                status="VALID_MULTI",
            )

        persist_successful_snapshot(
            db,
            controller_id=controller,
            vouchers=[remote(2)],
            observed_at="2026-09-29T08:00:00+00:00",
            sync_uuid="used-once",
        )
        persist_successful_snapshot(
            db,
            controller_id=controller,
            vouchers=[remote(0)],
            observed_at="2026-09-29T09:00:00+00:00",
            sync_uuid="counter-reset",
        )

        dataset = build_report_dataset(db, kind=ReportKind.USED, generated_at=NOW)

        assert [row.voucher_id for row in dataset.rows] == [voucher_id]
        assert dataset.rows[0].ever_used is True
        assert dataset.rows[0].authorized_guest_count == 0
    finally:
        db.close()


def test_summary_exposes_local_history_totals_without_clear_codes(tmp_path):
    db, controller = _db(tmp_path)
    try:
        _voucher(db, controller, "external", "1111122222", name="EMI06")
        nominal = _voucher(db, controller, "nominal", "3333344444", name="Pinco Pallino")
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
        assert all(row.code == "" for row in dataset.rows)
        assert dataset.totals.vouchers == 2
        assert dataset.totals.generated_vouchers == 1
        assert dataset.totals.nominal_vouchers == 1
        assert dataset.totals.unclassified_vouchers == 1
        assert dataset.totals.usage_unknown_vouchers == 0
    finally:
        db.close()


def test_printed_unused_uses_historical_usage_not_only_current_counter(tmp_path):
    db, controller = _db(tmp_path)
    try:
        voucher_id = _voucher(db, controller, "printed", "1111122222", used=0)
        db.record_print_audit(
            controller_id=controller,
            audit_id="job-1",
            codes=["11111-22222"],
            output_file="voucher.pdf",
            document_copies=1,
            printed_at="2026-09-02T10:00:00+00:00",
            windows_user="PC\\alice",
        )
        dataset = build_report_dataset(
            db,
            kind=ReportKind.PRINTED_UNUSED,
            generated_at=NOW,
        )
        assert [row.voucher_id for row in dataset.rows] == [voucher_id]
    finally:
        db.close()


def test_expired_report_uses_expiry_time_offline(tmp_path):
    db, controller = _db(tmp_path)
    try:
        voucher_id = db.upsert_voucher(
            controller_id=controller,
            unifi_id="expired-by-time",
            code="9999900000",
            imported_at="2026-09-01T09:05:00+00:00",
            created_at="2026-09-01T09:00:00+00:00",
            duration_minutes=60,
            authorized_guest_limit=1,
            authorized_guest_count=0,
            expires_at="2026-09-20T09:00:00+00:00",
            expired=False,
            last_synced_at=NOW,
        )
        dataset = build_report_dataset(db, kind=ReportKind.EXPIRED, generated_at=NOW)
        assert [row.voucher_id for row in dataset.rows] == [voucher_id]
    finally:
        db.close()


def test_controller_scope_filters_historical_rows(tmp_path):
    db, first = _db(tmp_path)
    second = db.create_controller(name="Seconda sede", api_root="https://b.example", created_at=NOW)
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
