"""Regression tests for the Voucher Management 5.0 SQLite foundation."""

from __future__ import annotations

import sqlite3

import pytest

from voucher_management.database import Database, SCHEMA_VERSION


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
            "UPDATE vouchers SET assigned_to=?, notes=? WHERE id=?",
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
