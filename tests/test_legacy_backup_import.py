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
from voucher_management.legacy_backup_import import (
    execute_legacy_backup_import,
    inspect_legacy_backup,
)
from voucher_management.security.history_key import HistoryKeyStore


FIXTURE_KEY = secrets.token_hex(32)
FINGERPRINT = hashlib.sha256(FIXTURE_KEY.encode("utf-8")).hexdigest()[:16]


def _digest(code: str) -> str:
    return hmac.new(
        FIXTURE_KEY.encode("utf-8"),
        code.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def _pdf(path: Path, codes: list[str]) -> None:
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
    document.save()


def _legacy_backup(tmp_path: Path) -> Path:
    source = tmp_path / "legacy-source"
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
    for index, code in enumerate(("12345-67890", "98765-43210"), start=1):
        rows.extend(
            [
                {
                    "event": "generate",
                    "event_id": f"generate-{index}",
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
                    "print_job_id": f"legacy-job-{index}",
                },
            ]
        )
    (source / "data" / "history.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    pdf = source / "Print" / "2026" / "09" / "Voucher_Legacy.pdf"
    _pdf(pdf, ["1234567890", "9876543210"])

    backup = tmp_path / "legacy-backup.zip"
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
            """SELECT code, present_on_controller, expired
               FROM vouchers ORDER BY code"""
        ).fetchall()
        assert [row["code"] for row in rows] == [
            "12345-67890",
            "98765-43210",
        ]
        assert all(row["present_on_controller"] == 0 for row in rows)
        assert all(row["expired"] == 1 for row in rows)
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
