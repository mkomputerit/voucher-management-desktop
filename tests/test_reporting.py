"""Tests for SQLite-backed historical reporting."""

from __future__ import annotations

import sqlite3

from voucher_management.database import Database
from voucher_management.report_policy import ReportPurpose
from voucher_management.reporting import ReportKind, build_report_dataset


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


def _classify(database, controller_id, unifi_id, nominal):
    database.mark_vouchers_created_by_app(
        controller_id=controller_id,
        unifi_ids=[unifi_id],
        is_nominal=nominal,
        classified_at=NOW,
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
        assert dataset.controller_label == "Tutto lo storico locale"
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


def test_nominal_report_uses_explicit_local_flag_not_recipient_text(tmp_path):
    database, controller_id = _database(tmp_path)
    try:
        _voucher(
            database,
            controller_id,
            unifi_id="code-like",
            code="1111122222",
            name="EMI06",
        )
        _voucher(
            database,
            controller_id,
            unifi_id="person",
            code="3333344444",
            name="Pinco Pallino",
        )
        _voucher(
            database,
            controller_id,
            unifi_id="old-unknown",
            code="5555566666",
            name="Mario Rossi",
        )
        _classify(database, controller_id, "code-like", False)
        _classify(database, controller_id, "person", True)

        dataset = build_report_dataset(
            database,
            kind=ReportKind.NOMINAL,
            generated_at=NOW,
        )

        assert [row.recipient for row in dataset.rows] == ["Pinco Pallino"]
        row = dataset.rows[0]
        assert row.is_nominal is True
        assert row.created_by_app is True

        summary = build_report_dataset(
            database,
            kind=ReportKind.SUMMARY,
            generated_at=NOW,
        )
        assert summary.totals.nominal_vouchers == 1
        assert summary.totals.non_nominal_vouchers == 1
        assert summary.totals.unclassified_nominality == 1
    finally:
        database.close()


def test_generated_unused_requires_definite_app_creation_and_no_ever_use(tmp_path):
    database, controller_id = _database(tmp_path)
    try:
        _voucher(
            database,
            controller_id,
            unifi_id="unused",
            code="1111122222",
            name="EMI06",
        )
        _classify(database, controller_id, "unused", False)

        _voucher(
            database,
            controller_id,
            unifi_id="used",
            code="3333344444",
            name="Pinco Pallino",
            uses=1,
        )
        _classify(database, controller_id, "used", True)
        # A later controller value must never erase historical evidence.
        _voucher(
            database,
            controller_id,
            unifi_id="used",
            code="3333344444",
            name="Pinco Pallino",
            uses=0,
        )

        _voucher(
            database,
            controller_id,
            unifi_id="controller-only",
            code="5555566666",
            name="External",
        )

        generated = build_report_dataset(
            database,
            kind=ReportKind.GENERATED,
            generated_at=NOW,
        )
        generated_unused = build_report_dataset(
            database,
            kind=ReportKind.GENERATED_UNUSED,
            generated_at=NOW,
        )
        used = build_report_dataset(
            database,
            kind=ReportKind.USED,
            generated_at=NOW,
        )

        assert {row.recipient for row in generated.rows} == {
            "EMI06",
            "Pinco Pallino",
        }
        assert [row.recipient for row in generated_unused.rows] == ["EMI06"]
        assert [row.recipient for row in used.rows] == ["Pinco Pallino"]
        assert used.rows[0].ever_used is True
        assert used.rows[0].authorized_guest_count == 0
    finally:
        database.close()


def test_printed_unused_and_never_printed_are_local_print_facts(tmp_path):
    database, controller_id = _database(tmp_path)
    try:
        printed = _voucher(
            database,
            controller_id,
            unifi_id="printed-unused",
            code="1111122222",
            name="Printed",
        )
        _voucher(
            database,
            controller_id,
            unifi_id="never-printed",
            code="3333344444",
            name="Never printed",
        )
        database.record_print_audit(
            controller_id=controller_id,
            audit_id="job-1",
            codes=["11111-22222"],
            output_file="first.pdf",
            document_copies=2,
            printed_at="2026-09-02T10:00:00+00:00",
            windows_user="PC\\alice",
        )

        printed_report = build_report_dataset(
            database,
            kind=ReportKind.PRINTED,
            generated_at=NOW,
        )
        printed_unused = build_report_dataset(
            database,
            kind=ReportKind.PRINTED_UNUSED,
            generated_at=NOW,
        )
        never_printed = build_report_dataset(
            database,
            kind=ReportKind.NEVER_PRINTED,
            generated_at=NOW,
        )

        assert [row.voucher_id for row in printed_report.rows] == [printed]
        assert [row.voucher_id for row in printed_unused.rows] == [printed]
        assert [row.recipient for row in never_printed.rows] == ["Never printed"]
        assert printed_report.rows[0].physical_copies == 2
    finally:
        database.close()


def test_summary_totals_describe_complete_local_archive(tmp_path):
    database, controller_id = _database(tmp_path)
    try:
        _voucher(
            database,
            controller_id,
            unifi_id="nominal-used",
            code="1111122222",
            name="Pinco Pallino",
            uses=2,
        )
        _classify(database, controller_id, "nominal-used", True)

        _voucher(
            database,
            controller_id,
            unifi_id="non-nominal-unused",
            code="3333344444",
            name="EMI06",
        )
        _classify(database, controller_id, "non-nominal-unused", False)

        _voucher(
            database,
            controller_id,
            unifi_id="legacy-unknown",
            code="5555566666",
            name="Legacy",
            expired=True,
            expires_at="2026-09-10T09:00:00+00:00",
        )
        database.record_print_audit(
            controller_id=controller_id,
            audit_id="job-summary",
            codes=["33333-44444"],
            output_file="print.pdf",
            document_copies=1,
            printed_at="2026-09-02T10:00:00+00:00",
            windows_user="PC\\alice",
        )

        totals = build_report_dataset(
            database,
            kind=ReportKind.SUMMARY,
            generated_at=NOW,
        ).totals

        assert totals.vouchers == 3
        assert totals.generated_by_app == 2
        assert totals.generated_never_used == 1
        assert totals.used_vouchers == 1
        assert totals.total_controller_uses == 2
        assert totals.expired_vouchers == 1
        assert totals.printed_vouchers == 1
        assert totals.printed_never_used == 1
        assert totals.never_printed == 2
        assert totals.nominal_vouchers == 1
        assert totals.non_nominal_vouchers == 1
        assert totals.unclassified_nominality == 1
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
        assert all_controllers.controller_label == "Tutto lo storico locale"
        assert len(all_controllers.rows) == 2
    finally:
        database.close()


def test_expired_report_uses_persisted_expiration_time_offline(tmp_path):
    database, controller_id = _database(tmp_path)
    try:
        voucher_id = _voucher(
            database,
            controller_id,
            unifi_id="time-expired",
            code="9999900000",
            expired=False,
            expires_at="2026-09-20T09:00:00+00:00",
        )
        dataset = build_report_dataset(
            database,
            kind=ReportKind.EXPIRED,
            generated_at=NOW,
        )
        assert [row.voucher_id for row in dataset.rows] == [voucher_id]
        assert dataset.rows[0].expired is True
        assert dataset.rows[0].status == "Scaduto"
    finally:
        database.close()


def test_controller_foreign_key_preserves_report_history(tmp_path):
    database, controller_id = _database(tmp_path)
    try:
        _voucher(
            database,
            controller_id,
            unifi_id="kept",
            code="1111122222",
        )
        with database.transaction() as db:
            try:
                db.execute(
                    "DELETE FROM controllers WHERE id=?",
                    (controller_id,),
                )
            except sqlite3.IntegrityError:
                pass
            else:
                raise AssertionError(
                    "referenced controller deletion must be blocked"
                )

        dataset = build_report_dataset(
            database,
            kind=ReportKind.FULL_HISTORY,
            generated_at=NOW,
        )
        assert len(dataset.rows) == 1
        assert dataset.rows[0].controller_name == "Sala Assemblee"
    finally:
        database.close()


def test_retention_archived_row_remains_in_history_with_archived_status(tmp_path):
    database, controller_id = _database(tmp_path)
    try:
        voucher_id = _voucher(
            database,
            controller_id,
            unifi_id="archived",
            code="1234567890",
            name="Old guest",
            created_at="2025-01-01T09:00:00+00:00",
        )
        with database.transaction() as db:
            db.execute(
                """UPDATE vouchers
                   SET code=?, name='', assigned_to='', notes='', archived_at=?
                   WHERE id=?""",
                (
                    f"ARCHIVED-{voucher_id}",
                    "2026-09-27T08:00:00+00:00",
                    voucher_id,
                ),
            )

        dataset = build_report_dataset(
            database,
            kind=ReportKind.FULL_HISTORY,
            generated_at=NOW,
        )
        row = next(item for item in dataset.rows if item.voucher_id == voucher_id)
        assert row.status == "Archiviato"
        assert row.archived_at == "2026-09-27T08:00:00+00:00"
        assert row.code == ""
        assert row.recipient == ""
    finally:
        database.close()
