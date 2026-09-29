"""Regression tests for the Voucher Management 5.0 SQLite foundation."""

from __future__ import annotations

import sqlite3

import pytest

from voucher_management import database as database_module
from voucher_management.database import Database, SCHEMA_SQL, SCHEMA_VERSION


def _db(tmp_path):
    database = Database(tmp_path / "voucher-management.db")
    database.initialize()
    return database


def test_schema_initializes_with_foreign_keys_wal_and_version(tmp_path):
    db = _db(tmp_path)
    try:
        assert db.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert db.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert db.connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        db.integrity_check()
    finally:
        db.close()


def test_schema_is_idempotent(tmp_path):
    db = _db(tmp_path)
    try:
        db.initialize()
        assert db.connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    finally:
        db.close()


def test_newer_schema_fails_closed(tmp_path):
    path = tmp_path / "future.db"
    raw = sqlite3.connect(path)
    raw.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    raw.close()

    db = Database(path)
    try:
        with pytest.raises(RuntimeError, match="newer"):
            db.initialize()
    finally:
        db.close()


def test_controller_schema_has_no_credential_columns(tmp_path):
    db = _db(tmp_path)
    try:
        columns = {
            row["name"]
            for row in db.connection.execute("PRAGMA table_info(controllers)")
        }
        forbidden = {"api_key", "password", "token", "secret", "credential"}
        assert columns.isdisjoint(forbidden)
    finally:
        db.close()


def test_voucher_upsert_preserves_local_fields(tmp_path):
    db = _db(tmp_path)
    try:
        controller = db.create_controller(
            name="Sala",
            api_root="https://controller.example/proxy/network/integration/v1",
            created_at="2026-09-25T12:00:00+00:00",
        )
        voucher_id = db.upsert_voucher(
            controller_id=controller,
            unifi_id="remote-1",
            code="12345-67890",
            imported_at="2026-09-25T12:01:00+00:00",
            last_synced_at="2026-09-25T12:01:00+00:00",
            authorized_guest_count=0,
        )
        db.connection.execute(
            """UPDATE vouchers
               SET assigned_to=?, notes=?, origin='APPLICATION', is_nominal=1
               WHERE id=?""",
            ("Mario Rossi", "Consegna reception", voucher_id),
        )
        db.connection.commit()

        same_id = db.upsert_voucher(
            controller_id=controller,
            unifi_id="remote-1",
            code="12345-67890",
            imported_at="2026-09-25T12:01:00+00:00",
            last_synced_at="2026-09-25T13:00:00+00:00",
            authorized_guest_count=2,
            expired=True,
        )
        row = db.connection.execute(
            "SELECT * FROM vouchers WHERE id=?", (voucher_id,)
        ).fetchone()

        assert same_id == voucher_id
        assert row["authorized_guest_count"] == 2
        assert row["expired"] == 1
        assert row["assigned_to"] == "Mario Rossi"
        assert row["notes"] == "Consegna reception"
        assert row["origin"] == "APPLICATION"
        assert row["is_nominal"] == 1
    finally:
        db.close()


def test_ever_used_is_monotonic_across_controller_counter_changes(tmp_path):
    db = _db(tmp_path)
    try:
        controller = db.create_controller(
            name="A",
            api_root="https://a.example",
            created_at="t",
        )
        voucher_id = db.upsert_voucher(
            controller_id=controller,
            unifi_id="ever-used",
            code="1234567890",
            imported_at="t1",
            last_synced_at="t1",
            authorized_guest_count=2,
        )
        db.upsert_voucher(
            controller_id=controller,
            unifi_id="ever-used",
            code="1234567890",
            imported_at="t1",
            last_synced_at="t2",
            authorized_guest_count=0,
        )
        row = db.connection.execute(
            "SELECT authorized_guest_count, ever_used FROM vouchers WHERE id=?",
            (voucher_id,),
        ).fetchone()
        assert row["authorized_guest_count"] == 0
        assert row["ever_used"] == 1
    finally:
        db.close()


def test_same_remote_id_is_scoped_per_controller(tmp_path):
    db = _db(tmp_path)
    try:
        first = db.create_controller(name="A", api_root="https://a.example", created_at="t")
        second = db.create_controller(name="B", api_root="https://b.example", created_at="t")
        ids = {
            db.upsert_voucher(
                controller_id=controller,
                unifi_id="same-id",
                code=f"CODE-{controller}",
                imported_at="t",
                last_synced_at="t",
            )
            for controller in (first, second)
        }
        assert len(ids) == 2
    finally:
        db.close()


def test_used_and_printed_protection_cannot_be_disabled(tmp_path):
    db = _db(tmp_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            db.connection.execute(
                """INSERT INTO retention_policy
                   (id, unused_unprinted_days, protect_used, protect_printed, updated_at)
                   VALUES (1, 180, 0, 1, 't')"""
            )
    finally:
        db.close()


def test_print_summary_counts_jobs_and_physical_copies(tmp_path):
    db = _db(tmp_path)
    try:
        controller = db.create_controller(name="A", api_root="https://a.example", created_at="t")
        voucher = db.upsert_voucher(
            controller_id=controller,
            unifi_id="v1",
            code="CODE",
            imported_at="t",
            last_synced_at="t",
        )
        for sequence, copies, stamp in ((1, 1, "2026-09-25T10:00:00Z"), (2, 2, "2026-09-25T11:00:00Z")):
            cursor = db.connection.execute(
                """INSERT INTO print_jobs
                   (print_job_uuid, created_at, submitted_at, windows_user,
                    document_copies, status)
                   VALUES (?, ?, ?, ?, ?, 'AUDITED')""",
                (f"job-{sequence}", stamp, stamp, r"SALA\operatore", copies),
            )
            db.connection.execute(
                """INSERT INTO voucher_prints
                   (print_job_id, voucher_id, printed_at, windows_user,
                    physical_copies, print_sequence, is_reprint)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (cursor.lastrowid, voucher, stamp, r"SALA\operatore", copies, sequence, int(sequence > 1)),
            )
        db.connection.commit()

        summary = db.print_summary(voucher)
        assert summary.print_jobs == 2
        assert summary.physical_copies == 3
        assert summary.first_printed_at.endswith("10:00:00Z")
        assert summary.last_printed_at.endswith("11:00:00Z")
    finally:
        db.close()


def test_get_or_create_controller_reuses_api_root_without_credentials(tmp_path):
    db = _db(tmp_path)
    try:
        first = db.get_or_create_controller(
            name="Sala",
            api_root="https://controller.example",
            observed_at="2026-09-26T05:00:00+00:00",
            cert_sha256="AA",
        )
        second = db.get_or_create_controller(
            name="Sala aggiornata",
            api_root="https://controller.example",
            observed_at="2026-09-26T06:00:00+00:00",
            cert_sha256="BB",
        )
        assert second == first
        row = db.connection.execute(
            "SELECT * FROM controllers WHERE id=?", (first,)
        ).fetchone()
        assert row["name"] == "Sala aggiornata"
        assert row["last_used_at"] == "2026-09-26T06:00:00+00:00"
        assert row["cert_sha256"] == "BB"
        assert db.connection.execute("SELECT COUNT(*) FROM controllers").fetchone()[0] == 1
    finally:
        db.close()


def test_controller_can_be_renamed_without_touching_connection_identity(tmp_path):
    db = _db(tmp_path)
    try:
        controller = db.create_controller(
            name="Controller UniFi",
            api_root="https://controller.example",
            created_at="2026-09-28T06:00:00+00:00",
            cert_sha256="AA",
        )

        db.rename_controller(controller, "Reception")

        row = db.connection.execute(
            "SELECT name, api_root, cert_sha256 FROM controllers WHERE id=?",
            (controller,),
        ).fetchone()
        assert row["name"] == "Reception"
        assert row["api_root"] == "https://controller.example"
        assert row["cert_sha256"] == "AA"
    finally:
        db.close()


def test_record_print_audit_is_idempotent_and_sequences_reprints(tmp_path):
    db = _db(tmp_path)
    try:
        controller = db.create_controller(
            name="Sala",
            api_root="https://controller.example",
            created_at="t",
        )
        voucher = db.upsert_voucher(
            controller_id=controller,
            unifi_id="voucher-1",
            code="12345-67890",
            imported_at="t",
            last_synced_at="t",
        )

        common = dict(
            controller_id=controller,
            codes=["12345-67890"],
            output_file="Voucher_Test.pdf",
            document_copies=2,
            printed_at="2026-09-26T08:00:00+00:00",
            windows_user=r"SALA\\operatore",
        )
        db.record_print_audit(audit_id="audit-1", **common)
        db.record_print_audit(audit_id="audit-1", **common)

        summary = db.print_summary(voucher)
        assert summary.print_jobs == 1
        assert summary.physical_copies == 2

        db.record_print_audit(
            audit_id="audit-2",
            **{
                **common,
                "document_copies": 1,
                "printed_at": "2026-09-26T08:05:00+00:00",
            },
        )
        rows = db.connection.execute(
            """SELECT print_sequence, is_reprint, physical_copies
               FROM voucher_prints WHERE voucher_id=? ORDER BY print_sequence""",
            (voucher,),
        ).fetchall()
        assert [tuple(row) for row in rows] == [(1, 0, 2), (2, 1, 1)]
    finally:
        db.close()


def test_record_print_audit_counts_repeated_labels_on_same_document(tmp_path):
    db = _db(tmp_path)
    try:
        controller = db.create_controller(name="A", api_root="https://a.example", created_at="t")
        voucher = db.upsert_voucher(
            controller_id=controller,
            unifi_id="v1",
            code="11111-22222",
            imported_at="t",
            last_synced_at="t",
        )

        db.record_print_audit(
            controller_id=controller,
            audit_id="audit-labels",
            codes=["11111-22222", "11111-22222", "11111-22222"],
            output_file="Voucher_Multi.pdf",
            document_copies=2,
            printed_at="2026-09-26T08:10:00+00:00",
            windows_user="operator",
        )

        summary = db.print_summary(voucher)
        assert summary.print_jobs == 1
        assert summary.physical_copies == 6
    finally:
        db.close()


def test_record_print_audit_fails_closed_on_missing_or_ambiguous_code(tmp_path):
    db = _db(tmp_path)
    try:
        controller = db.create_controller(name="A", api_root="https://a.example", created_at="t")
        db.upsert_voucher(
            controller_id=controller,
            unifi_id="v1",
            code="DUPLICATE",
            imported_at="t",
            last_synced_at="t",
        )
        db.upsert_voucher(
            controller_id=controller,
            unifi_id="v2",
            code="DUPLICATE",
            imported_at="t",
            last_synced_at="t",
        )

        with pytest.raises(RuntimeError, match="missing or ambiguous"):
            db.record_print_audit(
                controller_id=controller,
                audit_id="audit-ambiguous",
                codes=["DUPLICATE"],
                output_file="Voucher.pdf",
                document_copies=1,
                printed_at="2026-09-26T08:20:00+00:00",
                windows_user="operator",
            )

        assert db.connection.execute("SELECT COUNT(*) FROM print_jobs").fetchone()[0] == 0
    finally:
        db.close()


def test_print_summaries_for_codes_normalizes_display_format(tmp_path):
    db = _db(tmp_path)
    try:
        controller = db.create_controller(name="A", api_root="https://a.example", created_at="t")
        voucher = db.upsert_voucher(
            controller_id=controller,
            unifi_id="v1",
            code="1234567890",
            imported_at="t",
            last_synced_at="t",
        )
        job = db.connection.execute(
            """INSERT INTO print_jobs
               (print_job_uuid, created_at, submitted_at, windows_user,
                document_copies, status)
               VALUES ('job-summary', 't', 't', 'operator', 1, 'AUDITED')"""
        )
        db.connection.execute(
            """INSERT INTO voucher_prints
               (print_job_id, voucher_id, printed_at, windows_user,
                physical_copies, print_sequence, is_reprint)
               VALUES (?, ?, 't', 'operator', 1, 1, 0)""",
            (job.lastrowid, voucher),
        )
        db.connection.commit()

        summaries = db.print_summaries_for_codes(
            controller_id=controller,
            codes=["12345-67890"],
        )
        assert summaries["1234567890"].print_jobs == 1
        assert summaries["1234567890"].physical_copies == 1
    finally:
        db.close()


def test_schema_one_upgrades_to_legacy_evidence_schema(tmp_path):
    path = tmp_path / "schema-one.db"
    raw = sqlite3.connect(path)
    raw.executescript(SCHEMA_SQL)
    raw.execute("DROP INDEX IF EXISTS idx_vouchers_origin")
    raw.execute("DROP INDEX IF EXISTS idx_vouchers_nominal")
    raw.execute("DROP INDEX IF EXISTS idx_vouchers_ever_used")
    raw.execute("DROP INDEX IF EXISTS idx_vouchers_usage_observed")
    raw.execute("ALTER TABLE vouchers DROP COLUMN is_nominal")
    raw.execute("ALTER TABLE vouchers DROP COLUMN origin")
    raw.execute("ALTER TABLE vouchers DROP COLUMN ever_used")
    raw.execute("ALTER TABLE vouchers DROP COLUMN usage_observed")
    raw.execute("DROP TABLE legacy_audit_events")
    raw.execute("DROP TABLE migration_runs")
    raw.execute("PRAGMA user_version = 1")
    raw.execute(
        "INSERT OR REPLACE INTO app_metadata(key, value) VALUES ('schema_version', '1')"
    )
    raw.commit()
    raw.close()

    db = Database(path)
    try:
        db.initialize()
        assert db.connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        tables = {
            row[0]
            for row in db.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"migration_runs", "legacy_audit_events"} <= tables
        assert (
            db.connection.execute(
                "SELECT value FROM app_metadata WHERE key='schema_version'"
            ).fetchone()[0]
            == str(SCHEMA_VERSION)
        )
        columns = {
            row["name"]
            for row in db.connection.execute("PRAGMA table_info(vouchers)")
        }
        assert {"origin", "is_nominal", "ever_used", "usage_observed"} <= columns
        db.integrity_check()
    finally:
        db.close()


def test_schema_two_upgrade_preserves_unknown_classification_for_existing_rows(tmp_path):
    path = tmp_path / "schema-two.db"
    db = Database(path)
    db.initialize()
    controller = db.create_controller(
        name="A",
        api_root="https://a.example",
        created_at="t",
    )
    voucher_id = db.upsert_voucher(
        controller_id=controller,
        unifi_id="existing",
        code="1234567890",
        imported_at="t",
        last_synced_at="t",
    )
    db.close()

    raw = sqlite3.connect(path)
    raw.execute("DROP INDEX IF EXISTS idx_vouchers_origin")
    raw.execute("DROP INDEX IF EXISTS idx_vouchers_nominal")
    raw.execute("DROP INDEX IF EXISTS idx_vouchers_ever_used")
    raw.execute("DROP INDEX IF EXISTS idx_vouchers_usage_observed")
    raw.execute("ALTER TABLE vouchers DROP COLUMN is_nominal")
    raw.execute("ALTER TABLE vouchers DROP COLUMN origin")
    raw.execute("ALTER TABLE vouchers DROP COLUMN ever_used")
    raw.execute("ALTER TABLE vouchers DROP COLUMN usage_observed")
    raw.execute("PRAGMA user_version = 2")
    raw.execute(
        "INSERT OR REPLACE INTO app_metadata(key, value) VALUES ('schema_version', '2')"
    )
    raw.commit()
    raw.close()

    migrated = Database(path)
    try:
        migrated.initialize()
        row = migrated.connection.execute(
            "SELECT origin, is_nominal, ever_used, usage_observed FROM vouchers WHERE id=?",
            (voucher_id,),
        ).fetchone()
        assert row["origin"] == "UNKNOWN"
        assert row["is_nominal"] is None
        assert row["ever_used"] == 0
        assert row["usage_observed"] == 1
        assert migrated.connection.execute("PRAGMA user_version").fetchone()[0] == 3
    finally:
        migrated.close()


def test_failed_schema_two_upgrade_rolls_back_partial_ddl(tmp_path, monkeypatch):
    path = tmp_path / "schema-two-failure.db"
    db = Database(path)
    db.initialize()
    db.close()

    raw = sqlite3.connect(path)
    raw.execute("DROP INDEX IF EXISTS idx_vouchers_origin")
    raw.execute("DROP INDEX IF EXISTS idx_vouchers_nominal")
    raw.execute("DROP INDEX IF EXISTS idx_vouchers_ever_used")
    raw.execute("DROP INDEX IF EXISTS idx_vouchers_usage_observed")
    raw.execute("ALTER TABLE vouchers DROP COLUMN is_nominal")
    raw.execute("ALTER TABLE vouchers DROP COLUMN origin")
    raw.execute("ALTER TABLE vouchers DROP COLUMN ever_used")
    raw.execute("ALTER TABLE vouchers DROP COLUMN usage_observed")
    raw.execute("PRAGMA user_version = 2")
    raw.execute(
        "INSERT OR REPLACE INTO app_metadata(key, value) VALUES ('schema_version', '2')"
    )
    raw.commit()
    raw.close()

    monkeypatch.setattr(
        database_module,
        "MIGRATION_2_TO_3_SQL",
        """
ALTER TABLE vouchers ADD COLUMN reporting_partial_probe INTEGER;
THIS IS NOT VALID SQL;
""",
    )

    migrated = Database(path)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            migrated.initialize()
        assert migrated.connection.execute("PRAGMA user_version").fetchone()[0] == 2
        columns = {
            row["name"]
            for row in migrated.connection.execute("PRAGMA table_info(vouchers)")
        }
        assert "reporting_partial_probe" not in columns
        assert (
            migrated.connection.execute(
                "SELECT value FROM app_metadata WHERE key='schema_version'"
            ).fetchone()[0]
            == "2"
        )
    finally:
        migrated.close()


def test_failed_schema_one_upgrade_rolls_back_partial_ddl(tmp_path, monkeypatch):
    path = tmp_path / "schema-one-failure.db"
    raw = sqlite3.connect(path)
    raw.executescript(SCHEMA_SQL)
    raw.execute("DROP INDEX IF EXISTS idx_vouchers_origin")
    raw.execute("DROP INDEX IF EXISTS idx_vouchers_nominal")
    raw.execute("DROP INDEX IF EXISTS idx_vouchers_ever_used")
    raw.execute("DROP INDEX IF EXISTS idx_vouchers_usage_observed")
    raw.execute("ALTER TABLE vouchers DROP COLUMN is_nominal")
    raw.execute("ALTER TABLE vouchers DROP COLUMN origin")
    raw.execute("ALTER TABLE vouchers DROP COLUMN ever_used")
    raw.execute("ALTER TABLE vouchers DROP COLUMN usage_observed")
    raw.execute("DROP TABLE legacy_audit_events")
    raw.execute("DROP TABLE migration_runs")
    raw.execute("PRAGMA user_version = 1")
    raw.execute(
        "INSERT OR REPLACE INTO app_metadata(key, value) VALUES ('schema_version', '1')"
    )
    raw.commit()
    raw.close()

    monkeypatch.setattr(
        database_module,
        "MIGRATION_1_TO_2_SQL",
        """
CREATE TABLE migration_partial_probe(id INTEGER PRIMARY KEY);
THIS IS NOT VALID SQL;
""",
    )

    db = Database(path)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            db.initialize()
        assert db.connection.execute("PRAGMA user_version").fetchone()[0] == 1
        tables = {
            row[0]
            for row in db.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "migration_partial_probe" not in tables
        assert (
            db.connection.execute(
                "SELECT value FROM app_metadata WHERE key='schema_version'"
            ).fetchone()[0]
            == "1"
        )
    finally:
        db.close()



def test_application_session_records_clean_close_and_backup_outcome(tmp_path):
    db = _db(tmp_path)
    try:
        controller = db.create_controller(
            name="A",
            api_root="https://a.example",
            created_at="2026-09-27T05:00:00+00:00",
        )
        db.start_application_session(
            session_uuid="session-1",
            windows_user=r"DOMAIN\\operator",
            started_at="2026-09-27T05:01:00+00:00",
            app_version="4.3.3",
        )

        open_row = db.connection.execute(
            "SELECT * FROM application_sessions WHERE session_uuid='session-1'"
        ).fetchone()
        assert open_row["closed_at"] is None
        assert open_row["backup_status"] is None

        db.close_application_session(
            session_uuid="session-1",
            closed_at="2026-09-27T06:00:00+00:00",
            controller_id=controller,
            close_status="CLOSED",
            backup_status="SUCCESS",
        )

        row = db.connection.execute(
            "SELECT * FROM application_sessions WHERE session_uuid='session-1'"
        ).fetchone()
        assert row["windows_user"] == r"DOMAIN\\operator"
        assert row["controller_id"] == controller
        assert row["closed_at"] == "2026-09-27T06:00:00+00:00"
        assert row["close_status"] == "CLOSED"
        assert row["backup_status"] == "SUCCESS"
    finally:
        db.close()


def test_application_session_cannot_be_closed_twice(tmp_path):
    db = _db(tmp_path)
    try:
        db.start_application_session(
            session_uuid="session-2",
            windows_user="operator",
            started_at="start",
            app_version="4.3.3",
        )
        args = dict(
            session_uuid="session-2",
            closed_at="close",
            controller_id=None,
            close_status="CLOSED",
            backup_status="DISABLED",
        )
        db.close_application_session(**args)

        with pytest.raises(RuntimeError, match="already closed"):
            db.close_application_session(**args)
    finally:
        db.close()



def test_backup_history_records_verified_artifact_metadata(tmp_path):
    db = _db(tmp_path)
    try:
        row_id = db.record_backup_history(
            started_at="2026-09-27T07:00:00+00:00",
            completed_at="2026-09-27T07:00:05+00:00",
            destination="MANUAL",
            filename="VoucherManagement-backup.vmbk",
            status="SUCCESS",
            sha256="a" * 64,
            backup_format=2,
            schema_version=2,
        )

        row = db.connection.execute(
            "SELECT * FROM backup_history WHERE id=?",
            (row_id,),
        ).fetchone()
        assert row["destination"] == "MANUAL"
        assert row["filename"] == "VoucherManagement-backup.vmbk"
        assert row["status"] == "SUCCESS"
        assert row["sha256"] == "a" * 64
        assert row["backup_format"] == 2
        assert row["schema_version"] == 2
        assert row["error_summary"] is None
    finally:
        db.close()


def test_failed_backup_history_never_keeps_unverified_hash_metadata(tmp_path):
    db = _db(tmp_path)
    try:
        row_id = db.record_backup_history(
            started_at="2026-09-27T07:00:00+00:00",
            completed_at="2026-09-27T07:00:02+00:00",
            destination="SHUTDOWN_AUTO",
            filename="VoucherManagement-auto.vmbk",
            status="FAILED",
            sha256="f" * 64,
            backup_format=2,
            schema_version=2,
            error_summary="BackupError",
        )

        row = db.connection.execute(
            "SELECT * FROM backup_history WHERE id=?",
            (row_id,),
        ).fetchone()
        assert row["status"] == "FAILED"
        assert row["sha256"] is None
        assert row["backup_format"] is None
        assert row["schema_version"] is None
        assert row["error_summary"] == "BackupError"
    finally:
        db.close()


@pytest.mark.parametrize(
    "filename",
    [
        "../backup.vmbk",
        r"C:\\Temp\\backup.vmbk",
        "folder/backup.vmbk",
    ],
)
def test_backup_history_rejects_paths_in_filename(tmp_path, filename):
    db = _db(tmp_path)
    try:
        with pytest.raises(ValueError, match="basename"):
            db.record_backup_history(
                started_at="start",
                completed_at="done",
                destination="MANUAL",
                filename=filename,
                status="FAILED",
                error_summary="BackupError",
            )
    finally:
        db.close()


def test_successful_backup_history_requires_verified_sha256(tmp_path):
    db = _db(tmp_path)
    try:
        with pytest.raises(ValueError, match="SHA-256"):
            db.record_backup_history(
                started_at="start",
                completed_at="done",
                destination="MANUAL",
                filename="backup.vmbk",
                status="SUCCESS",
                sha256="not-a-hash",
                backup_format=2,
                schema_version=2,
            )
    finally:
        db.close()



def test_backup_history_rejects_full_path_as_destination(tmp_path):
    db = _db(tmp_path)
    try:
        with pytest.raises(ValueError, match="destination"):
            db.record_backup_history(
                started_at="start",
                completed_at="done",
                destination=r"C:\\Temp\\Backups",
                filename="backup.vmbk",
                status="FAILED",
                error_summary="BackupError",
            )
    finally:
        db.close()
