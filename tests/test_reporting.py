"""Tests for SQLite-backed privacy-safe reporting."""

from __future__ import annotations

from voucher_management.database import Database
from voucher_management.report_policy import ReportPurpose
from voucher_management.reporting import (
    ReportKind,
    build_report_dataset,
)


NOW = "2026-09-26T12:00:00+00:00"


def _database(tmp_path):
    database = Database(tmp_path / "voucher_management.db")
    database.initialize()
    controller_id = database.create_controller(
        name="Sala Assemblee",
        api_root="https://controller.example/proxy/network/integration/v1",
        created_at="2026-09-01T08:00:00+00:00",
    )
    return database, controller_id


def _voucher(
    database,
    controller_id,
    *,
    unifi_id,
    code,
    name="",
    uses=0,
    expired=False,
    created_at="2026-09-01T09:00:00+00:00",
    expires_at="2026-10-01T09:00:00+00:00",
):
    return database.upsert_voucher(
        controller_id=controller_id,
        unifi_id=unifi_id,
        code=code,
        name=name,
        created_at=created_at,
        imported_at="2026-09-01T09:05:00+00:00",
        duration_minutes=60,
        authorized_guest_limit=1,
        authorized_guest_count=uses,
        expires_at=expires_at,
        expired=expired,
        last_synced_at=NOW,
    )


def test_summary_report_never_exposes_codes_even_when_requested(tmp_path):
    database, controller_id = _database(tmp_path)
    try:
        _voucher(
            database,
            controller_id,
            unifi_id="v1",
            code="1234567890",
            name="Guest One",
        )

        dataset = build_report_dataset(
            database,
            kind=ReportKind.SUMMARY,
            generated_at=NOW,
            include_code_requested=True,
        )

        assert dataset.purpose is ReportPurpose.SUMMARY
        assert dataset.code_exposed is False
        assert len(dataset.rows) == 1
        assert dataset.rows[0].code == ""
        assert dataset.rows[0].recipient == "Guest One"
    finally:
        database.close()


def test_full_history_is_audit_and_also_hides_codes(tmp_path):
    database, controller_id = _database(tmp_path)
    try:
        _voucher(
            database,
            controller_id,
            unifi_id="v1",
            code="1234567890",
        )

        dataset = build_report_dataset(
            database,
            kind=ReportKind.FULL_HISTORY,
            generated_at=NOW,
            include_code_requested=True,
        )

        assert dataset.purpose is ReportPurpose.AUDIT
        assert dataset.code_exposed is False
        assert dataset.rows[0].code == ""
    finally:
        database.close()


def test_report_totals_are_derived_from_atomic_usage_and_print_facts(tmp_path):
    database, controller_id = _database(tmp_path)
    try:
        used = _voucher(
            database,
            controller_id,
            unifi_id="used",
            code="1111122222",
            name="Used guest",
            uses=3,
        )
        printed_unused = _voucher(
            database,
            controller_id,
            unifi_id="printed",
            code="3333344444",
            name="Printed guest",
        )
        expired = _voucher(
            database,
            controller_id,
            unifi_id="expired",
            code="5555566666",
            name="Expired guest",
            expired=True,
            expires_at="2026-09-10T09:00:00+00:00",
        )
        never_printed = _voucher(
            database,
            controller_id,
            unifi_id="never",
            code="7777788888",
            name="Never printed",
        )
        assert len({used, printed_unused, expired, never_printed}) == 4

        database.record_print_audit(
            controller_id=controller_id,
            audit_id="job-1",
            codes=["33333-44444"],
            output_file="first.pdf",
            document_copies=1,
            printed_at="2026-09-02T10:00:00+00:00",
            windows_user="PC\\alice",
        )
        database.record_print_audit(
            controller_id=controller_id,
            audit_id="job-2",
            codes=["33333-44444"],
            output_file="second.pdf",
            document_copies=2,
            printed_at="2026-09-03T10:00:00+00:00",
            windows_user="PC\\bob",
        )

        with database.transaction() as db:
            db.execute(
                "UPDATE vouchers SET assigned_to='Mario Rossi' WHERE id=?",
                (printed_unused,),
            )

        dataset = build_report_dataset(
            database,
            kind=ReportKind.SUMMARY,
            generated_at=NOW,
        )

        totals = dataset.totals
        assert totals.vouchers == 4
        assert totals.used_vouchers == 1
        assert totals.total_controller_uses == 3
        assert totals.expired_vouchers == 1
        assert totals.printed_vouchers == 1
        assert totals.print_jobs == 2
        assert totals.physical_copies == 3
        assert totals.reprint_jobs == 1
        assert totals.reprint_copies == 2
        assert totals.printed_never_used == 1
        assert totals.never_printed == 3
        assert totals.nominal_vouchers == 1

        row = next(
            item for item in dataset.rows if item.voucher_id == printed_unused
        )
        assert row.first_printed_at == "2026-09-02T10:00:00+00:00"
        assert row.last_printed_at == "2026-09-03T10:00:00+00:00"
        assert row.print_operators == ("PC\\alice", "PC\\bob")
        assert row.assigned_to == "Mario Rossi"
        assert row.recipient == "Mario Rossi"
    finally:
        database.close()


def test_report_kinds_filter_without_changing_underlying_totals_semantics(tmp_path):
    database, controller_id = _database(tmp_path)
    try:
        _voucher(
            database,
            controller_id,
            unifi_id="used",
            code="1111122222",
            uses=1,
        )
        printed = _voucher(
            database,
            controller_id,
            unifi_id="printed",
            code="3333344444",
        )
        _voucher(
            database,
            controller_id,
            unifi_id="expired",
            code="5555566666",
            expired=True,
        )
        nominal = _voucher(
            database,
            controller_id,
            unifi_id="nominal",
            code="7777788888",
        )
        with database.transaction() as db:
            db.execute(
                "UPDATE vouchers SET assigned_to='Assigned person' WHERE id=?",
                (nominal,),
            )
        database.record_print_audit(
            controller_id=controller_id,
            audit_id="job-printed",
            codes=["33333-44444"],
            output_file="print.pdf",
            document_copies=1,
            printed_at="2026-09-02T10:00:00+00:00",
            windows_user="PC\\alice",
        )

        expected = {
            ReportKind.USED: {"used"},
            ReportKind.EXPIRED: {"expired"},
            ReportKind.PRINTED_UNUSED: {"printed"},
            ReportKind.NEVER_PRINTED: {"used", "expired", "nominal"},
            ReportKind.NOMINAL: {"nominal"},
        }

        for kind, expected_ids in expected.items():
            dataset = build_report_dataset(
                database,
                kind=kind,
                generated_at=NOW,
            )
            unifi_ids = {
                database.connection.execute(
                    "SELECT unifi_id FROM vouchers WHERE id=?",
                    (row.voucher_id,),
                ).fetchone()[0]
                for row in dataset.rows
            }
            assert unifi_ids == expected_ids
            assert dataset.totals.vouchers == len(expected_ids)
    finally:
        database.close()


def test_controller_filter_never_mixes_controller_rows(tmp_path):
    database, first_controller = _database(tmp_path)
    second_controller = database.create_controller(
        name="Seconda sede",
        api_root="https://second.example/proxy/network/integration/v1",
        created_at=NOW,
    )
    try:
        _voucher(
            database,
            first_controller,
            unifi_id="first",
            code="1111122222",
        )
        _voucher(
            database,
            second_controller,
            unifi_id="second",
            code="3333344444",
        )

        first = build_report_dataset(
            database,
            kind=ReportKind.SUMMARY,
            generated_at=NOW,
            controller_id=first_controller,
        )
        all_controllers = build_report_dataset(
            database,
            kind=ReportKind.SUMMARY,
            generated_at=NOW,
        )

        assert first.controller_label == "Sala Assemblee"
        assert len(first.rows) == 1
        assert first.rows[0].controller_name == "Sala Assemblee"
        assert all_controllers.controller_label == "Tutti i controller"
        assert len(all_controllers.rows) == 2
    finally:
        database.close()
