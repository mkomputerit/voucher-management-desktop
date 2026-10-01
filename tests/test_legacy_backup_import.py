"""Integration tests for verified import of pre-SQLite backup archives."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import zipfile
from pathlib import Path
from types import SimpleNamespace

from reportlab.pdfgen import canvas

from voucher_management.backup import BackupService
from voucher_management.database import Database
from voucher_management import legacy_backup_import
from voucher_management.legacy_backup_import import (
    execute_legacy_backup_import,
    inspect_legacy_backup,
)
from voucher_management.legacy_migration import LegacyMigrationError
from voucher_management.reporting import ReportKind, build_report_dataset
from voucher_management.retention import (
    configure_retention_policy,
    prepare_security_revocation_operation,
    security_revocation_candidates,
)
from voucher_management.security.history_key import HistoryKeyStore
from voucher_management.sync_store import persist_successful_snapshot
from voucher_management.unifi_api import ApiVoucher


FIXTURE_KEY = secrets.token_hex(32)
FINGERPRINT = hashlib.sha256(FIXTURE_KEY.encode("utf-8")).hexdigest()[:16]


def _digest(code: str) -> str:
    return hmac.new(
        FIXTURE_KEY.encode("utf-8"),
        code.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def _pdf(
    path: Path,
    codes: list[str],
    *,
    extra_lines: tuple[str, ...] = (),
) -> None:
    document = canvas.Canvas(str(path))
    y = 800
    for code in codes:
        compact = code.replace("-", "")
        document.drawString(
            50,
            y,
            f"Codice / Code: {compact[:5]}-{compact[5:]}",
        )
        y -= 40
    for line in extra_lines:
        document.drawString(50, y, line)
        y -= 40
    document.save()


def _legacy_backup(
    tmp_path: Path,
    *,
    extra_pdf_lines: tuple[str, ...] = (),
    codes: tuple[str, ...] = ("12345-67890", "98765-43210"),
    backup_name: str = "legacy-backup.zip",
    event_prefix: str = "",
) -> Path:
    source = tmp_path / f"{Path(backup_name).stem}-source"
    (source / "config").mkdir(parents=True)
    (source / "data").mkdir()
    (source / "Print" / "2026" / "09").mkdir(parents=True)

    (source / "config" / "settings.json").write_text(
        json.dumps(
            {
                "structure_name": "Legacy",
                "history_key_fingerprint": FINGERPRINT,
            }
        ),
        encoding="utf-8",
    )
    HistoryKeyStore(source).set(FIXTURE_KEY)

    rows = []
    for index, code in enumerate(codes, start=1):
        rows.extend(
            [
                {
                    "event": "generate",
                    "event_id": f"{event_prefix}generate-{index}",
                    "voucher_id": _digest(code),
                    "recipient": f"Ospite {index}",
                    "duration_minutes": 1440,
                    "timestamp": f"2026-09-25T10:0{index}:00+00:00",
                    "output_file": "Voucher_Legacy.pdf",
                },
                {
                    "event": "print",
                    "voucher_id": _digest(code),
                    "timestamp": f"2026-09-25T10:1{index}:00+00:00",
                    "output_file": "Voucher_Legacy.pdf",
                    "document_copies": 1,
                    "physical_copies": 1,
                    "print_job_id": f"{event_prefix}legacy-job-{index}",
                },
            ]
        )
    (source / "data" / "history.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    pdf = source / "Print" / "2026" / "09" / "Voucher_Legacy.pdf"
    _pdf(
        pdf,
        [code.replace("-", "") for code in codes],
        extra_lines=extra_pdf_lines,
    )

    backup = tmp_path / backup_name
    manifest = {
        "format": 2,
        "created_utc": "2026-09-25T10:30:00+00:00",
        "application": "Voucher Management",
        "includes": ["config", "data", "Print", "Loghi"],
        "portable_history_key": True,
    }
    with zipfile.ZipFile(backup, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("backup_manifest.json", json.dumps(manifest))
        for path in source.rglob("*"):
            if path.is_file():
                archive.write(path, path.relative_to(source).as_posix())
    return backup


def _live(tmp_path: Path):
    root = tmp_path / "live"
    for name in ("config", "data", "Print", "Loghi"):
        (root / name).mkdir(parents=True, exist_ok=True)
    live_key = secrets.token_hex(32)
    live_fp = hashlib.sha256(live_key.encode("utf-8")).hexdigest()[:16]
    (root / "config" / "settings.json").write_text(
        json.dumps(
            {
                "structure_name": "Current",
                "history_key_fingerprint": live_fp,
            }
        ),
        encoding="utf-8",
    )
    HistoryKeyStore(root).set(live_key)
    (root / "data" / "history.jsonl").write_text("", encoding="utf-8")

    paths = SimpleNamespace(
        user_root=root,
        data=root / "data",
        prints=root / "Print",
        database=root / "data" / "voucher_management.db",
    )
    database = Database(paths.database)
    database.initialize()
    return paths, database


def test_inspection_recovers_codes_from_pdf_and_matches_hmac_history(tmp_path):
    source = _legacy_backup(tmp_path)
    paths, database = _live(tmp_path)
    try:
        inspection = inspect_legacy_backup(
            source,
            validator=BackupService(paths),
        )
        assert inspection.history_rows == 4
        assert inspection.generated_rows == 2
        assert inspection.print_rows == 2
        assert inspection.unique_history_vouchers == 2
        assert inspection.pdf_files == 1
        assert inspection.recovered_codes == 2
        assert inspection.matched_history_vouchers == 2
        assert inspection.unmatched_history_vouchers == 0
        assert inspection.unmatched_pdf_codes == 0
    finally:
        database.close()


def test_import_materializes_prints_without_controller_presence(tmp_path):
    source = _legacy_backup(tmp_path)
    paths, database = _live(tmp_path)
    try:
        result = execute_legacy_backup_import(
            database=database,
            live_backup_service=BackupService(paths),
            source=source,
            safety_backup_destination=tmp_path / "pre-import.vmbk",
            safety_backup_password="a" * 24,
            imported_at="2026-09-28T08:00:00+00:00",
            migration_uuid="legacy-import-1",
        )

        assert result.evidence.resolved_rows == 4
        assert result.evidence.unresolved_rows == 0
        assert result.materialization.print_rows == 2
        assert result.historical_vouchers_created == 2
        assert database.connection.execute(
            "SELECT COUNT(*) FROM voucher_prints"
        ).fetchone()[0] == 2
        rows = database.connection.execute(
            """SELECT code, present_on_controller, expired, archived_at,
                      origin, is_nominal, usage_observed
               FROM vouchers ORDER BY code"""
        ).fetchall()
        assert [row["code"] for row in rows] == [
            "12345-67890",
            "98765-43210",
        ]
        assert all(row["present_on_controller"] == 0 for row in rows)
        assert all(row["expired"] == 1 for row in rows)
        assert all(row["archived_at"] is None for row in rows)
        assert all(row["origin"] == "CONTROLLER" for row in rows)
        assert all(row["is_nominal"] is None for row in rows)
        assert all(row["usage_observed"] == 0 for row in rows)
        # Legacy "generate" rows prove PDF generation, not who created the
        # voucher on UniFi; creation provenance must therefore not be invented.
        imported_pdf = (
            paths.prints
            / "Imported"
            / result.inspection.source_sha256[:12]
            / "2026"
            / "09"
            / "Voucher_Legacy.pdf"
        )
        assert imported_pdf.is_file()
        assert result.pdfs_copied == 1
    finally:
        database.close()


def test_reimport_is_idempotent_for_vouchers_and_physical_prints(tmp_path):
    source = _legacy_backup(tmp_path)
    paths, database = _live(tmp_path)
    try:
        first = execute_legacy_backup_import(
            database=database,
            live_backup_service=BackupService(paths),
            source=source,
            safety_backup_destination=tmp_path / "pre-import-1.vmbk",
            safety_backup_password="a" * 24,
            imported_at="2026-09-28T08:00:00+00:00",
            migration_uuid="legacy-import-1",
        )
        second = execute_legacy_backup_import(
            database=database,
            live_backup_service=BackupService(paths),
            source=source,
            safety_backup_destination=tmp_path / "pre-import-2.vmbk",
            safety_backup_password="b" * 24,
            imported_at="2026-09-28T08:10:00+00:00",
            migration_uuid="legacy-import-2",
        )

        assert database.connection.execute(
            "SELECT COUNT(*) FROM vouchers"
        ).fetchone()[0] == 2
        assert database.connection.execute(
            "SELECT COUNT(*) FROM voucher_prints"
        ).fetchone()[0] == 2
        assert second.historical_vouchers_created == 0
        assert second.pdfs_already_present == 1
        assert first.materialization.print_rows == 2
    finally:
        database.close()


def test_import_reuses_unique_current_voucher_when_available(tmp_path):
    source = _legacy_backup(tmp_path)
    paths, database = _live(tmp_path)
    try:
        controller = database.create_controller(
            name="Reception",
            api_root="https://controller.example",
            created_at="2026-09-28T07:00:00+00:00",
        )
        database.upsert_voucher(
            controller_id=controller,
            unifi_id="current-voucher",
            code="1234567890",
            name="Current",
            imported_at="2026-09-28T07:01:00+00:00",
            last_synced_at="2026-09-28T07:01:00+00:00",
        )

        result = execute_legacy_backup_import(
            database=database,
            live_backup_service=BackupService(paths),
            source=source,
            safety_backup_destination=tmp_path / "pre-import.vmbk",
            safety_backup_password="a" * 24,
            imported_at="2026-09-28T08:00:00+00:00",
            migration_uuid="legacy-import-current",
            preferred_controller_id=controller,
        )

        assert result.reused_vouchers == 1
        current_id = database.connection.execute(
            """SELECT id FROM vouchers
               WHERE controller_id=? AND unifi_id='current-voucher'""",
            (controller,),
        ).fetchone()["id"]
        assert database.print_summary(current_id).print_jobs == 1
    finally:
        database.close()


def test_unrelated_ten_digit_pdf_text_is_not_created_as_voucher(tmp_path):
    source = _legacy_backup(
        tmp_path,
        extra_pdf_lines=("Tel. 3331234567",),
    )
    paths, database = _live(tmp_path)
    try:
        inspection = inspect_legacy_backup(
            source,
            validator=BackupService(paths),
        )
        assert inspection.recovered_codes == 3
        assert inspection.matched_history_vouchers == 2
        assert inspection.unmatched_pdf_codes == 1

        result = execute_legacy_backup_import(
            database=database,
            live_backup_service=BackupService(paths),
            source=source,
            safety_backup_destination=tmp_path / "pre-import.vmbk",
            safety_backup_password="a" * 24,
            imported_at="2026-09-28T08:00:00+00:00",
            migration_uuid="legacy-import-phone",
        )

        codes = {
            row["code"]
            for row in database.connection.execute(
                "SELECT code FROM vouchers"
            ).fetchall()
        }
        assert codes == {"12345-67890", "98765-43210"}
        assert "33312-34567" not in codes
        assert result.historical_vouchers_created == 2
    finally:
        database.close()


def test_oversized_legacy_pdf_is_rejected_before_pdfium(monkeypatch, tmp_path):
    source = _legacy_backup(tmp_path)
    paths, database = _live(tmp_path)
    try:
        monkeypatch.setattr(
            legacy_backup_import,
            "MAX_LEGACY_PDF_BYTES",
            1,
        )
        try:
            inspect_legacy_backup(
                source,
                validator=BackupService(paths),
            )
        except LegacyMigrationError as exc:
            assert "limite di sicurezza" in str(exc)
        else:
            raise AssertionError("oversized legacy PDF must be rejected")
    finally:
        database.close()


def test_reimport_does_not_restore_retention_minimized_legacy_voucher(tmp_path):
    source = _legacy_backup(tmp_path)
    paths, database = _live(tmp_path)
    try:
        first = execute_legacy_backup_import(
            database=database,
            live_backup_service=BackupService(paths),
            source=source,
            safety_backup_destination=tmp_path / "pre-import-1.vmbk",
            safety_backup_password="a" * 24,
            imported_at="2026-09-28T08:00:00+00:00",
            migration_uuid="legacy-import-minimize-1",
        )
        voucher = database.connection.execute(
            """SELECT id FROM vouchers
               WHERE code='12345-67890'"""
        ).fetchone()
        voucher_id = int(voucher["id"])
        archived_at = "2027-06-01T08:00:00+00:00"

        # Reproduce a row minimized by an earlier build, including its durable
        # retention audit fact.  The new importer must never resurrect either
        # the credential or recipient on a later import of the same history.
        with database.transaction() as db:
            db.execute(
                """UPDATE vouchers
                   SET code=?, name='', assigned_to='', notes='', archived_at=?
                   WHERE id=?""",
                (f"ARCHIVED-{voucher_id}", archived_at, voucher_id),
            )
            db.execute(
                """INSERT INTO voucher_events
                   (event_uuid, voucher_id, event_type, occurred_at, source,
                    windows_user, details_json)
                   VALUES (?, ?, 'RETENTION_ARCHIVED', ?, 'OPERATOR',
                           'TEST\\operator', '{"credential_removed":true}')""",
                (f"retention-{voucher_id}", voucher_id, archived_at),
            )

        second = execute_legacy_backup_import(
            database=database,
            live_backup_service=BackupService(paths),
            source=source,
            safety_backup_destination=tmp_path / "pre-import-2.vmbk",
            safety_backup_password="b" * 24,
            imported_at="2027-06-02T08:00:00+00:00",
            migration_uuid="legacy-import-minimize-2",
        )

        row = database.connection.execute(
            "SELECT code, name, archived_at FROM vouchers WHERE id=?",
            (voucher_id,),
        ).fetchone()
        assert row["code"] == f"ARCHIVED-{voucher_id}"
        assert row["name"] == ""
        assert row["archived_at"] == archived_at
        assert second.minimized_vouchers_preserved >= 1
        assert second.evidence.already_applied is True
        assert first.evidence.resolved_rows == second.evidence.resolved_rows
    finally:
        database.close()


def test_extended_history_reimport_preserves_minimized_legacy_identity(
    tmp_path,
):
    source_a = _legacy_backup(
        tmp_path,
        backup_name="legacy-a.zip",
    )
    paths, database = _live(tmp_path)
    try:
        first = execute_legacy_backup_import(
            database=database,
            live_backup_service=BackupService(paths),
            source=source_a,
            safety_backup_destination=tmp_path / "pre-import-a.vmbk",
            safety_backup_password="a" * 24,
            imported_at="2026-09-28T08:00:00+00:00",
            migration_uuid="legacy-import-extended-a",
        )
        vouchers = database.connection.execute(
            """SELECT id, code FROM vouchers
               WHERE code IN ('12345-67890', '98765-43210')
               ORDER BY id"""
        ).fetchall()
        minimized_ids = {int(row["id"]) for row in vouchers}
        archived_at = "2027-06-01T08:00:00+00:00"
        with database.transaction() as db:
            for row in vouchers:
                voucher_id = int(row["id"])
                db.execute(
                    """UPDATE vouchers
                       SET code=?, name='', assigned_to='', notes='', archived_at=?
                       WHERE id=?""",
                    (f"ARCHIVED-{voucher_id}", archived_at, voucher_id),
                )
                db.execute(
                    """INSERT INTO voucher_events
                       (event_uuid, voucher_id, event_type, occurred_at, source,
                        windows_user, details_json)
                       VALUES (?, ?, 'RETENTION_ARCHIVED', ?, 'OPERATOR',
                               'TEST\\operator', '{"credential_removed":true}')""",
                    (
                        f"retention-extended-{voucher_id}",
                        voucher_id,
                        archived_at,
                    ),
                )

        unrelated = _legacy_backup(
            tmp_path,
            codes=("44444-33333",),
            backup_name="legacy-unrelated.zip",
            event_prefix="unrelated-",
        )
        unrelated_result = execute_legacy_backup_import(
            database=database,
            live_backup_service=BackupService(paths),
            source=unrelated,
            safety_backup_destination=tmp_path / "pre-import-unrelated.vmbk",
            safety_backup_password="c" * 24,
            imported_at="2027-06-01T12:00:00+00:00",
            migration_uuid="legacy-import-unrelated",
        )
        assert unrelated_result.historical_vouchers_created == 1
        unrelated_row = database.connection.execute(
            "SELECT id FROM vouchers WHERE code='44444-33333'"
        ).fetchone()
        unrelated_id = int(unrelated_row["id"])
        with database.transaction() as db:
            db.execute(
                """UPDATE vouchers
                   SET code=?, name='', assigned_to='', notes='', archived_at=?
                   WHERE id=?""",
                (f"ARCHIVED-{unrelated_id}", archived_at, unrelated_id),
            )
            db.execute(
                """INSERT INTO voucher_events
                   (event_uuid, voucher_id, event_type, occurred_at, source,
                    windows_user, details_json)
                   VALUES (?, ?, 'RETENTION_ARCHIVED', ?, 'OPERATOR',
                           'TEST\\operator', '{"credential_removed":true}')""",
                (
                    f"retention-unrelated-{unrelated_id}",
                    unrelated_id,
                    archived_at,
                ),
            )

        source_b = _legacy_backup(
            tmp_path,
            codes=(
                "12345-67890",
                "98765-43210",
                "55555-66666",
            ),
            backup_name="legacy-b.zip",
        )
        second = execute_legacy_backup_import(
            database=database,
            live_backup_service=BackupService(paths),
            source=source_b,
            safety_backup_destination=tmp_path / "pre-import-b.vmbk",
            safety_backup_password="b" * 24,
            imported_at="2027-06-02T08:00:00+00:00",
            migration_uuid="legacy-import-extended-b",
        )

        preserved = database.connection.execute(
            """SELECT id, code, name, archived_at FROM vouchers
               WHERE id IN (?, ?) ORDER BY id""",
            tuple(sorted(minimized_ids)),
        ).fetchall()
        assert len(preserved) == 2
        for row in preserved:
            assert row["code"] == f"ARCHIVED-{int(row['id'])}"
            assert row["name"] == ""
            assert row["archived_at"] == archived_at

        unrelated_preserved = database.connection.execute(
            "SELECT code, name, archived_at FROM vouchers WHERE id=?",
            (unrelated_id,),
        ).fetchone()
        assert unrelated_preserved["code"] == f"ARCHIVED-{unrelated_id}"
        assert unrelated_preserved["name"] == ""

        codes = {
            row["code"]
            for row in database.connection.execute(
                "SELECT code FROM vouchers"
            ).fetchall()
        }
        assert "12345-67890" not in codes
        assert "55555-66666" in codes
        assert database.connection.execute(
            "SELECT COUNT(*) FROM vouchers"
        ).fetchone()[0] == 4
        assert second.minimized_vouchers_preserved == 2
        assert second.evidence.already_applied is False
        assert second.evidence.resolved_rows == 6
        assert first.inspection.source_sha256 != second.inspection.source_sha256
    finally:
        database.close()


def test_reimport_repairs_credential_resurrected_by_early_5_1_bug(tmp_path):
    source = _legacy_backup(tmp_path)
    paths, database = _live(tmp_path)
    try:
        execute_legacy_backup_import(
            database=database,
            live_backup_service=BackupService(paths),
            source=source,
            safety_backup_destination=tmp_path / "pre-import-1.vmbk",
            safety_backup_password="a" * 24,
            imported_at="2026-09-28T08:00:00+00:00",
            migration_uuid="legacy-import-repair-1",
        )
        row = database.connection.execute(
            "SELECT id FROM vouchers WHERE code='12345-67890'"
        ).fetchone()
        voucher_id = int(row["id"])
        archived_at = "2027-06-01T08:00:00+00:00"
        with database.transaction() as db:
            db.execute(
                """UPDATE vouchers
                   SET code='12345-67890', name='Ospite 1', archived_at=?
                   WHERE id=?""",
                (archived_at, voucher_id),
            )
            db.execute(
                """INSERT INTO voucher_events
                   (event_uuid, voucher_id, event_type, occurred_at, source,
                    windows_user, details_json)
                   VALUES (?, ?, 'RETENTION_ARCHIVED', ?, 'OPERATOR',
                           'TEST\\operator', '{"credential_removed":true}')""",
                (f"retention-repair-{voucher_id}", voucher_id, archived_at),
            )

        result = execute_legacy_backup_import(
            database=database,
            live_backup_service=BackupService(paths),
            source=source,
            safety_backup_destination=tmp_path / "pre-import-2.vmbk",
            safety_backup_password="b" * 24,
            imported_at="2027-06-02T08:00:00+00:00",
            migration_uuid="legacy-import-repair-2",
        )

        repaired = database.connection.execute(
            "SELECT code, name, archived_at FROM vouchers WHERE id=?",
            (voucher_id,),
        ).fetchone()
        assert repaired["code"] == f"ARCHIVED-{voucher_id}"
        assert repaired["name"] == ""
        assert repaired["archived_at"] == archived_at
        assert result.minimized_vouchers_preserved >= 1
    finally:
        database.close()


def test_specific_legacy_import_error_is_preserved_after_safety_backup(
    monkeypatch,
    tmp_path,
):
    source = _legacy_backup(tmp_path)
    paths, database = _live(tmp_path)
    try:
        monkeypatch.setattr(
            legacy_backup_import,
            "_copy_pdf_archive",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                LegacyMigrationError("Percorso PDF del backup non sicuro")
            ),
        )
        try:
            execute_legacy_backup_import(
                database=database,
                live_backup_service=BackupService(paths),
                source=source,
                safety_backup_destination=tmp_path / "pre-import.vmbk",
                safety_backup_password="a" * 24,
                imported_at="2026-09-28T08:00:00+00:00",
                migration_uuid="legacy-import-specific-error",
            )
        except LegacyMigrationError as exc:
            assert str(exc) == "Percorso PDF del backup non sicuro"
        else:
            raise AssertionError("specific LegacyMigrationError must propagate")
    finally:
        database.close()


def test_legacy_print_becomes_live_printed_unused_then_security_revoked(tmp_path):
    """Golden lifecycle: legacy print -> live UniFi -> report -> revocation audit."""

    source = _legacy_backup(
        tmp_path,
        codes=("12345-67890",),
        backup_name="legacy-security-lifecycle.zip",
        event_prefix="security-",
    )
    paths, database = _live(tmp_path)
    try:
        imported = execute_legacy_backup_import(
            database=database,
            live_backup_service=BackupService(paths),
            source=source,
            safety_backup_destination=tmp_path / "pre-import-security.vmbk",
            safety_backup_password="a" * 24,
            imported_at="2026-09-28T08:00:00+00:00",
            migration_uuid="legacy-security-import",
        )
        assert imported.materialization.print_rows == 1

        live_controller = database.create_controller(
            name="Reception",
            api_root="https://controller.example",
            created_at="2026-09-28T09:00:00+00:00",
        )
        live = ApiVoucher(
            id="live-security-voucher",
            code="1234567890",
            recipient="Ospite 1",
            duration_minutes=0,
            create_time=1_758_793_000,
            quota=1,
            used=0,
            status="VALID_MULTI",
            start_time=0,
            end_time=0,
        )
        observed_at = "2026-09-30T10:00:00+00:00"
        persist_successful_snapshot(
            database,
            controller_id=live_controller,
            vouchers=[live],
            observed_at=observed_at,
            sync_uuid="legacy-security-live-sync",
        )
        configure_retention_policy(
            database,
            unused_unprinted_days=180,
            printed_unused_revoke_days=1,
            now=observed_at,
        )

        live_row = database.connection.execute(
            """SELECT id, usage_observed, ever_used, present_on_controller
               FROM vouchers
               WHERE controller_id=? AND unifi_id=?""",
            (live_controller, live.id),
        ).fetchone()
        assert live_row is not None
        live_id = int(live_row["id"])
        assert live_row["usage_observed"] == 1
        assert live_row["ever_used"] == 0
        assert live_row["present_on_controller"] == 1
        assert database.print_summary(live_id).print_jobs == 1

        # The synthetic archive identity was consolidated, not double-counted.
        assert database.connection.execute(
            """SELECT COUNT(*) FROM vouchers AS v
               JOIN controllers AS c ON c.id=v.controller_id
               WHERE REPLACE(v.code, '-', '')='1234567890'"""
        ).fetchone()[0] == 1

        printed_unused = build_report_dataset(
            database,
            kind=ReportKind.PRINTED_UNUSED,
            generated_at=observed_at,
            controller_id=live_controller,
        )
        assert [row.voucher_id for row in printed_unused.rows] == [live_id]
        assert printed_unused.rows[0].print_jobs == 1
        assert printed_unused.rows[0].usage_observed is True
        assert printed_unused.rows[0].ever_used is False

        candidates = security_revocation_candidates(
            database,
            now=observed_at,
            controller_id=live_controller,
        )
        assert [item.voucher_id for item in candidates] == [live_id]

        candidate_report = build_report_dataset(
            database,
            kind=ReportKind.SECURITY_REVOCATION_CANDIDATES,
            generated_at=observed_at,
            controller_id=live_controller,
        )
        assert [row.voucher_id for row in candidate_report.rows] == [live_id]

        remote_ids = prepare_security_revocation_operation(
            database,
            controller_id=live_controller,
            voucher_ids=[live_id],
            operation_uuid="security-revoke-golden",
            requested_at=observed_at,
            windows_user=r"PC\operator",
        )
        assert remote_ids == (live.id,)

        # A later complete snapshot proves the controller credential is gone.
        confirmed_at = "2026-09-30T10:05:00+00:00"
        persist_successful_snapshot(
            database,
            controller_id=live_controller,
            vouchers=[],
            observed_at=confirmed_at,
            sync_uuid="legacy-security-revoked-sync",
        )

        row = database.connection.execute(
            """SELECT present_on_controller, revoked_for_security_at
               FROM vouchers WHERE id=?""",
            (live_id,),
        ).fetchone()
        assert row["present_on_controller"] == 0
        assert row["revoked_for_security_at"] == confirmed_at
        assert database.print_summary(live_id).print_jobs == 1

        revoked_report = build_report_dataset(
            database,
            kind=ReportKind.SECURITY_REVOKED,
            generated_at=confirmed_at,
            controller_id=live_controller,
        )
        assert [row.voucher_id for row in revoked_report.rows] == [live_id]
        assert revoked_report.rows[0].status == "Revocato per sicurezza"
        assert revoked_report.rows[0].print_jobs == 1
    finally:
        database.close()
