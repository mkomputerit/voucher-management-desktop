"""Regression coverage for HMAC-to-SQLite print reconciliation."""

from __future__ import annotations

from pathlib import Path

import pytest

from voucher_management.database import Database
from voucher_management.history import HistoryService
from voucher_management.history_sqlite_reconciliation import (
    PENDING_KEY,
    HistorySqliteReconciliationError,
    reconcile_history_print_audits,
)
from voucher_management.security.history_key import HistoryKeyStore
from voucher_management.settings import SettingsStore


NOW = "2026-09-30T08:00:00+00:00"


def _history(tmp_path):
    settings_store = SettingsStore(tmp_path / "config" / "settings.json")
    history = HistoryService(
        tmp_path / "history" / "history.jsonl",
        tmp_path / "history" / "history.lock",
        settings_store,
        secret_store=HistoryKeyStore(tmp_path / "identity"),
    )
    return settings_store, history


def _database(tmp_path):
    database = Database(tmp_path / "voucher_management.db")
    database.initialize()
    return database


def _voucher(database, controller, remote_id, code):
    return database.upsert_voucher(
        controller_id=controller,
        unifi_id=remote_id,
        code=code,
        imported_at=NOW,
        last_synced_at=NOW,
    )


def test_modern_imported_print_materializes_once_and_clears_report_gate(tmp_path):
    database = _database(tmp_path)
    settings_store, history = _history(tmp_path)
    try:
        controller = database.create_controller(
            name="Reception",
            api_root="https://controller.example",
            created_at=NOW,
        )
        voucher_id = _voucher(
            database,
            controller,
            "voucher-1",
            "1234567890",
        )
        history.record_print(
            ["12345-67890"],
            Path("Voucher_Group.pdf"),
            2,
            settings_store.load(),
            audit_id="abcd1234",
            submitted_at=NOW,
        )

        result = reconcile_history_print_audits(
            database,
            history,
            force=True,
        )
        assert result.jobs_materialized == 1
        assert result.print_rows_seen == 1
        assert database.metadata_value(PENDING_KEY) == "0"
        summary = database.print_summary(voucher_id)
        assert summary.print_jobs == 1
        assert summary.physical_copies == 2

        again = reconcile_history_print_audits(
            database,
            history,
            force=True,
        )
        assert again.jobs_materialized == 0
        assert database.print_summary(voucher_id).print_jobs == 1
    finally:
        database.close()


def test_ambiguous_hmac_mapping_keeps_reports_fail_closed(tmp_path):
    database = _database(tmp_path)
    settings_store, history = _history(tmp_path)
    try:
        first = database.create_controller(
            name="A",
            api_root="https://a.example",
            created_at=NOW,
        )
        second = database.create_controller(
            name="B",
            api_root="https://b.example",
            created_at=NOW,
        )
        _voucher(database, first, "a-1", "1234567890")
        _voucher(database, second, "b-1", "1234567890")
        history.record_print(
            ["12345-67890"],
            Path("Voucher_Ambiguous.pdf"),
            1,
            settings_store.load(),
            audit_id="ambiguous1234",
            submitted_at=NOW,
        )

        with pytest.raises(
            HistorySqliteReconciliationError,
            match="ambiguo",
        ):
            reconcile_history_print_audits(
                database,
                history,
                force=True,
            )

        assert database.metadata_value(PENDING_KEY) == "1"
        assert (
            database.connection.execute(
                "SELECT COUNT(*) FROM print_jobs"
            ).fetchone()[0]
            == 0
        )
    finally:
        database.close()


def test_existing_sqlite_job_does_not_become_ambiguous_after_code_reuse(tmp_path):
    database = _database(tmp_path)
    settings_store, history = _history(tmp_path)
    try:
        first = database.create_controller(
            name="A",
            api_root="https://a.example",
            created_at=NOW,
        )
        voucher_id = _voucher(database, first, "a-1", "1234567890")
        history.record_print(
            ["12345-67890"],
            Path("Voucher_Existing.pdf"),
            1,
            settings_store.load(),
            audit_id="existing1234",
            submitted_at=NOW,
        )
        database.record_print_audit(
            controller_id=first,
            audit_id="existing1234",
            codes=["12345-67890"],
            output_file="Voucher_Existing.pdf",
            document_copies=1,
            printed_at=NOW,
            windows_user="PC\\operator",
        )

        second = database.create_controller(
            name="B",
            api_root="https://b.example",
            created_at=NOW,
        )
        _voucher(database, second, "b-1", "1234567890")

        result = reconcile_history_print_audits(
            database,
            history,
            force=True,
        )
        assert result.jobs_materialized == 0
        assert database.metadata_value(PENDING_KEY) == "0"
        assert database.print_summary(voucher_id).print_jobs == 1
    finally:
        database.close()
