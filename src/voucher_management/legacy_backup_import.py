"""Verified import of pre-SQLite Voucher Management backup archives.

Legacy 4.x/early-5.x ZIP backups can contain real print-history evidence
without a SQLite database.  This module imports that evidence into the current
5.x database without overwriting current settings or requiring the controller
still to expose expired vouchers.

The backup's own PDFs are used as an independent source of clear voucher codes.
Those codes are correlated against the HMAC history using the portable history
key stored in the same backup.  No HMAC is reversed or guessed.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pypdfium2 as pdfium

from .backup import BackupError, BackupService
from .database import Database
from .legacy_migration import (
    LegacyMigrationApplyResult,
    LegacyMigrationError,
    LegacyMaterializationResult,
    LegacyMigrationPlan,
    LegacyVoucherCandidate,
    apply_legacy_migration_plan,
    build_legacy_migration_plan,
    legacy_candidates_from_database,
    materialize_resolved_legacy_events,
)
from .security.history_key import HistoryKeyStore
from .settings import SettingsStore


_CODE_PATTERN = re.compile(r"(?<!\d)(\d{5})\s*-?\s*(\d{5})(?!\d)")
MAX_LEGACY_PDF_BYTES = 64 * 1024 * 1024
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class LegacyBackupInspection:
    source_sha256: str
    created_utc: str
    backup_format: int
    history_rows: int
    generated_rows: int
    print_rows: int
    unique_history_vouchers: int
    pdf_files: int
    recovered_codes: int
    matched_history_vouchers: int
    unmatched_history_vouchers: int
    unmatched_pdf_codes: int


@dataclass(frozen=True)
class LegacyBackupImportResult:
    inspection: LegacyBackupInspection
    safety_backup_path: Path
    evidence: LegacyMigrationApplyResult
    materialization: LegacyMaterializationResult
    reused_vouchers: int
    historical_vouchers_created: int
    pdfs_copied: int
    pdfs_already_present: int


@dataclass(frozen=True)
class _LegacyBackupMaterial:
    source: Path
    source_sha256: str
    created_utc: str
    backup_format: int
    fingerprint: str
    secret: str
    history_bytes: bytes
    pdf_members: tuple[str, ...]
    pdf_codes: tuple[str, ...]


@dataclass(frozen=True)
class _RecoveredMetadata:
    recipient: str = ""
    duration_minutes: int | None = None
    created_at: str | None = None


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _decode_history_secret(payload: bytes) -> str:
    prefixes = (
        HistoryKeyStore.PORTABLE_PREFIX,
        HistoryKeyStore.LEGACY_PORTABLE_PREFIX,
    )
    encoded = None
    for prefix in prefixes:
        if payload.startswith(prefix):
            encoded = payload[len(prefix):]
            break
    if encoded is None:
        raise LegacyMigrationError(
            "Il backup non contiene una chiave cronologia portabile supportata"
        )
    try:
        secret = base64.b64decode(encoded, validate=True).decode("utf-8").strip()
    except (ValueError, UnicodeDecodeError) as exc:
        raise LegacyMigrationError(
            "Chiave cronologia del backup non valida"
        ) from exc
    if len(secret) < 16:
        raise LegacyMigrationError(
            "Chiave cronologia del backup troppo corta"
        )
    return secret


def _extract_pdf_codes(payload: bytes) -> set[str]:
    codes: set[str] = set()
    try:
        document = pdfium.PdfDocument(payload)
    except Exception as exc:
        raise LegacyMigrationError(
            "Uno dei PDF del backup non è leggibile"
        ) from exc
    try:
        for index in range(len(document)):
            page = document[index]
            textpage = None
            try:
                textpage = page.get_textpage()
                text = textpage.get_text_range()
            except Exception as exc:
                raise LegacyMigrationError(
                    "Impossibile leggere il testo di un PDF del backup"
                ) from exc
            finally:
                if textpage is not None:
                    textpage.close()
                page.close()
            for left, right in _CODE_PATTERN.findall(text):
                codes.add(left + right)
    finally:
        document.close()
    return codes


def _load_material(
    source: Path,
    validator: BackupService,
) -> _LegacyBackupMaterial:
    source = Path(source)
    if validator.is_encrypted_backup(source):
        raise LegacyMigrationError(
            "Usare un backup ZIP precedente non cifrato per questa importazione"
        )

    manifest = validator.validate(source)
    if manifest.get("sqlite_snapshot") is not None:
        raise LegacyMigrationError(
            "Questo backup contiene già SQLite: usare Ripristina backup"
        )

    try:
        with zipfile.ZipFile(source, "r") as archive:
            names = set(archive.namelist())
            required = {
                "config/settings.json",
                "data/history.jsonl",
                "data/history_secret.key",
            }
            if not required.issubset(names):
                raise LegacyMigrationError(
                    "Il backup precedente non contiene cronologia e identità complete"
                )
            settings = json.loads(
                archive.read("config/settings.json").decode("utf-8")
            )
            if not isinstance(settings, dict):
                raise LegacyMigrationError(
                    "Configurazione del backup non valida"
                )
            fingerprint = str(
                settings.get("history_key_fingerprint", "") or ""
            ).strip().lower()
            secret = _decode_history_secret(
                archive.read("data/history_secret.key")
            )
            actual = hashlib.sha256(secret.encode("utf-8")).hexdigest()[:16]
            if not fingerprint or actual != fingerprint:
                raise LegacyMigrationError(
                    "Chiave e fingerprint della cronologia non corrispondono"
                )

            history_bytes = archive.read("data/history.jsonl")
            pdf_members = tuple(
                sorted(
                    name
                    for name in names
                    if name.startswith("Print/")
                    and name.lower().endswith(".pdf")
                )
            )
            codes: set[str] = set()
            for member in pdf_members:
                info = archive.getinfo(member)
                if int(info.file_size) > MAX_LEGACY_PDF_BYTES:
                    raise LegacyMigrationError(
                        "Uno dei PDF del backup supera il limite di sicurezza "
                        f"di {MAX_LEGACY_PDF_BYTES // (1024 * 1024)} MiB"
                    )
                codes.update(_extract_pdf_codes(archive.read(member)))
    except LegacyMigrationError:
        raise
    except (
        OSError,
        zipfile.BadZipFile,
        json.JSONDecodeError,
        UnicodeDecodeError,
        KeyError,
    ) as exc:
        raise LegacyMigrationError(
            "Backup precedente danneggiato o non leggibile"
        ) from exc

    backup_format = manifest.get("format")
    if type(backup_format) is not int:
        raise LegacyMigrationError("Formato backup precedente non valido")

    return _LegacyBackupMaterial(
        source=source,
        source_sha256=_file_sha256(source),
        created_utc=str(manifest.get("created_utc", "") or ""),
        backup_format=backup_format,
        fingerprint=fingerprint,
        secret=secret,
        history_bytes=history_bytes,
        pdf_members=pdf_members,
        pdf_codes=tuple(sorted(codes)),
    )


def _history_path(history_bytes: bytes, root: Path) -> Path:
    path = Path(root) / "history.jsonl"
    path.write_bytes(history_bytes)
    return path


def _pdf_candidates(codes: tuple[str, ...]) -> tuple[LegacyVoucherCandidate, ...]:
    return tuple(
        LegacyVoucherCandidate(
            controller_id=1,
            unifi_id=f"pdf-{hashlib.sha256(code.encode('ascii')).hexdigest()[:20]}",
            code=code,
        )
        for code in codes
    )


def _all_plan_rows(plan: LegacyMigrationPlan):
    for item in plan.resolved:
        yield item.row
    for item in plan.ambiguous:
        yield item.row
    for item in plan.unresolved:
        yield item.row


def _inspect_material(material: _LegacyBackupMaterial) -> LegacyBackupInspection:
    with tempfile.TemporaryDirectory(prefix="voucher-legacy-inspect-") as temp:
        history_path = _history_path(material.history_bytes, Path(temp))
        plan = build_legacy_migration_plan(
            history_path=history_path,
            expected_fingerprint=material.fingerprint,
            secret=material.secret,
            candidates=_pdf_candidates(material.pdf_codes),
        )

    rows = tuple(_all_plan_rows(plan))
    unique_history = {row.voucher_digest for row in rows}
    matched = {item.row.voucher_digest for item in plan.resolved}
    matched_codes = {
        item.candidate.code.replace("-", "")
        for item in plan.resolved
    }
    unmatched = unique_history - matched
    unmatched_pdf_codes = (
        len({code.replace("-", "") for code in material.pdf_codes})
        - len(matched_codes)
    )
    return LegacyBackupInspection(
        source_sha256=material.source_sha256,
        created_utc=material.created_utc,
        backup_format=material.backup_format,
        history_rows=plan.total_rows,
        generated_rows=sum(row.event == "generate" for row in rows),
        print_rows=sum(row.event == "print" for row in rows),
        unique_history_vouchers=len(unique_history),
        pdf_files=len(material.pdf_members),
        recovered_codes=len(material.pdf_codes),
        matched_history_vouchers=len(matched),
        unmatched_history_vouchers=len(unmatched),
        unmatched_pdf_codes=max(0, unmatched_pdf_codes),
    )


def inspect_legacy_backup(
    source: Path,
    *,
    validator: BackupService,
) -> LegacyBackupInspection:
    """Validate a pre-SQLite backup and report recoverable historical facts."""

    return _inspect_material(_load_material(Path(source), validator))


def _digest_spellings(code: str, secret: str) -> set[str]:
    compact = str(code).strip().replace("-", "")
    spellings = {compact}
    if len(compact) == 10:
        spellings.add(f"{compact[:5]}-{compact[5:]}")
    key = secret.encode("utf-8")
    return {
        hmac.new(key, spelling.encode("ascii"), hashlib.sha256).hexdigest()
        for spelling in spellings
    }


def _resolved_pdf_codes(
    material: _LegacyBackupMaterial,
) -> tuple[str, ...]:
    """Return only PDF candidates cryptographically linked to legacy history.

    PDF text can contain unrelated ten-digit strings such as telephone numbers.
    A clear code is eligible for voucher creation only when at least one HMAC
    history row resolves to it with the backup's verified history key.
    """

    with tempfile.TemporaryDirectory(prefix="voucher-legacy-resolved-") as temp:
        path = _history_path(material.history_bytes, Path(temp))
        plan = build_legacy_migration_plan(
            history_path=path,
            expected_fingerprint=material.fingerprint,
            secret=material.secret,
            candidates=_pdf_candidates(material.pdf_codes),
        )
    return tuple(
        sorted(
            {
                item.candidate.code.replace("-", "")
                for item in plan.resolved
            }
        )
    )


def _metadata_by_code(
    material: _LegacyBackupMaterial,
) -> dict[str, _RecoveredMetadata]:
    with tempfile.TemporaryDirectory(prefix="voucher-legacy-meta-") as temp:
        path = _history_path(material.history_bytes, Path(temp))
        plan = build_legacy_migration_plan(
            history_path=path,
            expected_fingerprint=material.fingerprint,
            secret=material.secret,
            candidates=_pdf_candidates(material.pdf_codes),
        )

    result: dict[str, _RecoveredMetadata] = {}
    for item in plan.resolved:
        if item.row.event != "generate":
            continue
        code = item.candidate.code.replace("-", "")
        payload = item.row.payload
        current = result.get(code)
        stamp = item.row.timestamp
        if current is not None and current.created_at and current.created_at <= stamp:
            continue
        duration = payload.get("duration_minutes")
        result[code] = _RecoveredMetadata(
            recipient=str(payload.get("recipient", "") or "").strip(),
            duration_minutes=(
                int(duration)
                if type(duration) is int and duration >= 0
                else None
            ),
            created_at=stamp,
        )
    return result


def _ensure_import_candidates(
    database: Database,
    material: _LegacyBackupMaterial,
    *,
    imported_at: str,
    preferred_controller_id: int | None,
) -> tuple[tuple[LegacyVoucherCandidate, ...], int, int]:
    """Resolve PDF codes to existing vouchers or deterministic archive rows."""

    metadata = _metadata_by_code(material)
    resolved_pdf_codes = _resolved_pdf_codes(material)
    chosen: list[LegacyVoucherCandidate] = []
    missing: list[str] = []
    reused = 0

    for canonical in resolved_pdf_codes:
        rows = database.connection.execute(
            """SELECT id, controller_id, unifi_id, code
               FROM vouchers
               WHERE REPLACE(code, '-', '')=?
               ORDER BY id""",
            (canonical,),
        ).fetchall()
        selected = None
        if preferred_controller_id is not None:
            preferred = [
                row for row in rows
                if int(row["controller_id"]) == int(preferred_controller_id)
            ]
            if len(preferred) == 1:
                selected = preferred[0]
        if selected is None and len(rows) == 1:
            selected = rows[0]

        if selected is not None:
            chosen.append(
                LegacyVoucherCandidate(
                    controller_id=int(selected["controller_id"]),
                    unifi_id=str(selected["unifi_id"]),
                    code=str(selected["code"]),
                )
            )
            reused += 1
        else:
            missing.append(canonical)

    created = 0
    if missing:
        archive_api_root = f"legacy-backup://{material.source_sha256}"
        with database.transaction() as db:
            controller = db.execute(
                """SELECT id FROM controllers
                   WHERE api_root=? ORDER BY id LIMIT 1""",
                (archive_api_root,),
            ).fetchone()
            if controller is None:
                cursor = db.execute(
                    """INSERT INTO controllers
                       (name, api_root, description, cert_sha256, created_at,
                        is_active)
                       VALUES (?, ?, ?, '', ?, 0)""",
                    (
                        "Archivio backup precedente",
                        archive_api_root,
                        (
                            "Cronologia importata da backup precedente "
                            f"{material.created_utc or material.source.name}"
                        ),
                        imported_at,
                    ),
                )
                archive_controller_id = int(cursor.lastrowid)
            else:
                archive_controller_id = int(controller["id"])

            for canonical in missing:
                unifi_id = (
                    "legacy-backup-"
                    + material.source_sha256[:12]
                    + "-"
                    + hashlib.sha256(canonical.encode("ascii")).hexdigest()[:20]
                )
                existing = db.execute(
                    """SELECT id FROM vouchers
                       WHERE controller_id=? AND unifi_id=?""",
                    (archive_controller_id, unifi_id),
                ).fetchone()
                meta = metadata.get(canonical, _RecoveredMetadata())
                display_code = f"{canonical[:5]}-{canonical[5:]}"
                if existing is None:
                    cursor = db.execute(
                        """INSERT INTO vouchers
                           (controller_id, unifi_id, code, name, created_at,
                            imported_at, duration_minutes,
                            authorized_guest_count, expired,
                            present_on_controller, last_seen_at,
                            last_synced_at, archived_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, 0, 1, 0, NULL, ?, NULL)""",
                        (
                            archive_controller_id,
                            unifi_id,
                            display_code,
                            meta.recipient,
                            meta.created_at,
                            imported_at,
                            meta.duration_minutes,
                            imported_at,
                        ),
                    )
                    created += 1
                else:
                    db.execute(
                        """UPDATE vouchers
                           SET code=?, name=CASE
                                   WHEN TRIM(name)='' THEN ? ELSE name END,
                               duration_minutes=COALESCE(duration_minutes, ?),
                               expired=1, present_on_controller=0,
                               archived_at=CASE
                                   WHEN code LIKE 'ARCHIVED-%'
                                   THEN archived_at
                                   ELSE NULL
                               END
                           WHERE id=?""",
                        (
                            display_code,
                            meta.recipient,
                            meta.duration_minutes,
                            int(existing["id"]),
                        ),
                    )
                chosen.append(
                    LegacyVoucherCandidate(
                        controller_id=archive_controller_id,
                        unifi_id=unifi_id,
                        code=display_code,
                    )
                )

    recovered = {item.code.replace("-", "") for item in chosen}
    for item in legacy_candidates_from_database(database):
        canonical = item.code.replace("-", "")
        if canonical not in recovered:
            chosen.append(item)

    dedup: dict[tuple[int, str], LegacyVoucherCandidate] = {}
    for item in chosen:
        dedup[item.key] = item
    return tuple(dedup.values()), reused, created


def _copy_pdf_archive(
    material: _LegacyBackupMaterial,
    *,
    prints_root: Path,
) -> tuple[int, int]:
    copied = 0
    present = 0
    root = (
        Path(prints_root) / "Imported" / material.source_sha256[:12]
    ).resolve()

    with zipfile.ZipFile(material.source, "r") as archive:
        for member in material.pdf_members:
            relative = Path(*Path(member).parts[1:])
            target = (root / relative).resolve()
            try:
                target.relative_to(root)
            except ValueError as exc:
                raise LegacyMigrationError(
                    "Percorso PDF del backup non sicuro"
                ) from exc
            info = archive.getinfo(member)
            if int(info.file_size) > MAX_LEGACY_PDF_BYTES:
                raise LegacyMigrationError(
                    "Uno dei PDF del backup supera il limite di sicurezza "
                    f"di {MAX_LEGACY_PDF_BYTES // (1024 * 1024)} MiB"
                )
            payload = archive.read(member)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_file() and target.read_bytes() == payload:
                present += 1
                continue
            temp = target.with_suffix(target.suffix + ".tmp")
            try:
                temp.write_bytes(payload)
                os.replace(temp, target)
            finally:
                temp.unlink(missing_ok=True)
            copied += 1
    return copied, present


def execute_legacy_backup_import(
    *,
    database: Database,
    live_backup_service: BackupService,
    source: Path,
    safety_backup_destination: Path,
    safety_backup_password: str,
    imported_at: str,
    migration_uuid: str,
    preferred_controller_id: int | None = None,
) -> LegacyBackupImportResult:
    """Import a verified pre-SQLite backup into the current 5.x database."""

    if not str(safety_backup_password or ""):
        raise LegacyMigrationError(
            "L'importazione richiede un backup cifrato di sicurezza"
        )
    material = _load_material(Path(source), live_backup_service)
    inspection = _inspect_material(material)

    backup_started_at = datetime.now(timezone.utc).isoformat()
    destination = Path(safety_backup_destination)
    try:
        artifact = live_backup_service.create_verified(
            destination,
            password=safety_backup_password,
        )
        database.record_backup_history(
            started_at=backup_started_at,
            completed_at=datetime.now(timezone.utc).isoformat(),
            destination="PRE_MIGRATION",
            filename=artifact.path.name,
            status="SUCCESS",
            sha256=artifact.sha256,
            backup_format=artifact.backup_format,
            schema_version=artifact.schema_version,
        )
    except Exception as exc:
        try:
            database.record_backup_history(
                started_at=backup_started_at,
                completed_at=datetime.now(timezone.utc).isoformat(),
                destination="PRE_MIGRATION",
                filename=destination.name,
                status="FAILED",
                error_summary=type(exc).__name__,
            )
        except Exception as audit_exc:
            _LOGGER.warning(
                "legacy_backup_preimport_audit_failed type=%s",
                type(audit_exc).__name__,
            )
        raise LegacyMigrationError(
            "Backup di sicurezza pre-importazione non riuscito"
        ) from exc

    try:
        candidates, reused, created = _ensure_import_candidates(
            database,
            material,
            imported_at=imported_at,
            preferred_controller_id=preferred_controller_id,
        )

        with tempfile.TemporaryDirectory(
            prefix="voucher-legacy-import-"
        ) as temp:
            history_path = _history_path(material.history_bytes, Path(temp))
            plan = build_legacy_migration_plan(
                history_path=history_path,
                expected_fingerprint=material.fingerprint,
                secret=material.secret,
                candidates=candidates,
            )
            evidence = apply_legacy_migration_plan(
                database=database,
                plan=plan,
                migration_uuid=migration_uuid,
                applied_at=imported_at,
            )
            materialization = materialize_resolved_legacy_events(
                database=database,
                materialized_at=imported_at,
                migration_uuid=migration_uuid,
            )

        database.integrity_check()
        copied, present = _copy_pdf_archive(
            material,
            prints_root=live_backup_service.paths.prints,
        )
    except Exception as exc:
        _LOGGER.warning(
            "legacy_backup_import_incomplete type=%s source_sha256_prefix=%s",
            type(exc).__name__,
            material.source_sha256[:12],
        )
        raise LegacyMigrationError(
            "Importazione interrotta dopo la creazione del backup di "
            "sicurezza. Alcune fasi possono essere già state registrate, ma "
            "l'operazione è idempotente: correggere la causa e rieseguire lo "
            "stesso backup senza cancellare dati."
        ) from exc
    return LegacyBackupImportResult(
        inspection=inspection,
        safety_backup_path=Path(artifact.path),
        evidence=evidence,
        materialization=materialization,
        reused_vouchers=reused,
        historical_vouchers_created=created,
        pdfs_copied=copied,
        pdfs_already_present=present,
    )
