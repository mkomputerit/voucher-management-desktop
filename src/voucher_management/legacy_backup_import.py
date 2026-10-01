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
from .identity import LEGACY_BACKUP_API_ROOT_PREFIX
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
    minimized_vouchers_preserved: int
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
    generated_at: str | None = None


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
        if current is not None and current.generated_at and current.generated_at <= stamp:
            continue
        duration = payload.get("duration_minutes")
        result[code] = _RecoveredMetadata(
            recipient=str(payload.get("recipient", "") or "").strip(),
            duration_minutes=(
                int(duration)
                if type(duration) is int and duration >= 0
                else None
            ),
            generated_at=stamp,
        )
    return result


def _retention_minimized_legacy_candidate(
    database: Database,
    material: _LegacyBackupMaterial,
    canonical: str,
) -> tuple[LegacyVoucherCandidate, int] | None:
    """Return the durable legacy identity for a retention-minimized code.

    A later legacy ZIP can contain an extended history whose whole-file SHA-256
    differs from the first import. The HMAC voucher digest remains stable, so
    an already RESOLVED identity can be linked back to its minimized voucher
    without restoring the clear credential in SQLite.
    """

    digests = tuple(sorted(_digest_spellings(canonical, material.secret)))
    placeholders = ",".join("?" for _ in digests)
    rows = database.connection.execute(
        f"""SELECT DISTINCT v.id, v.controller_id, v.unifi_id
            FROM legacy_audit_events AS lae
            JOIN vouchers AS v ON v.id=lae.voucher_id
            WHERE lae.voucher_digest IN ({placeholders})
              AND lae.resolution_status='RESOLVED'
              AND v.archived_at IS NOT NULL
              AND EXISTS (
                  SELECT 1 FROM voucher_events AS ve
                  WHERE ve.voucher_id=v.id
                    AND ve.event_type='RETENTION_ARCHIVED'
              )
            ORDER BY v.id""",
        digests,
    ).fetchall()
    if len(rows) > 1:
        raise LegacyMigrationError(
            "Identità legacy minimizzata ambigua: più voucher locali "
            "corrispondono allo stesso digest storico. Nessun voucher viene "
            "saltato automaticamente per evitare associazioni errate. "
            "L'app non dispone di una procedura per risolvere questo conflitto. "
            "Interrompere l'importazione, conservare lo ZIP e il backup di "
            "sicurezza .vmbk e richiedere assistenza tecnica. "
            "Non modificare manualmente il database."
        )
    if not rows:
        return None

    row = rows[0]
    voucher_id = int(row["id"])
    return (
        LegacyVoucherCandidate(
            controller_id=int(row["controller_id"]),
            unifi_id=str(row["unifi_id"]),
            # The clear code exists only transiently in memory for HMAC planning.
            # The persisted voucher remains ARCHIVED-<id>.
            code=canonical,
        ),
        voucher_id,
    )


def _ensure_import_candidates(
    database: Database,
    material: _LegacyBackupMaterial,
    *,
    imported_at: str,
    preferred_controller_id: int | None,
) -> tuple[
    tuple[LegacyVoucherCandidate, ...],
    int,
    int,
    frozenset[int],
]:
    """Resolve PDF codes to existing vouchers or deterministic archive rows."""

    metadata = _metadata_by_code(material)
    resolved_pdf_codes = _resolved_pdf_codes(material)
    chosen: list[LegacyVoucherCandidate] = []
    missing: list[str] = []
    reused = 0
    minimized_ids: set[int] = set()

    for canonical in resolved_pdf_codes:
        rows = database.connection.execute(
            """SELECT id, controller_id, unifi_id, code, name, archived_at
               FROM vouchers
               WHERE REPLACE(code, '-', '')=?
               ORDER BY id""",
            (canonical,),
        ).fetchall()
        minimized_match = _retention_minimized_legacy_candidate(
            database,
            material,
            canonical,
        )
        if minimized_match is not None:
            minimized_candidate, minimized_id = minimized_match
            if rows:
                raise LegacyMigrationError(
                    "Un codice presente nello ZIP appartiene già a un voucher "
                    "minimizzato ma compare anche su un'altra identità locale. "
                    "L'importazione viene fermata per non associare evidenze "
                    "alla credenziale sbagliata. L'app non dispone di una "
                    "procedura per risolvere questo conflitto. Conservare ZIP "
                    "e backup .vmbk e richiedere assistenza tecnica. "
                    "Non modificare manualmente il database."
                )
            chosen.append(minimized_candidate)
            minimized_ids.add(minimized_id)
            continue

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
            selected_id = int(selected["id"])
            retention_event = database.connection.execute(
                """SELECT 1 FROM voucher_events
                   WHERE voucher_id=? AND event_type='RETENTION_ARCHIVED'
                   LIMIT 1""",
                (selected_id,),
            ).fetchone()
            if selected["archived_at"] is not None and retention_event is not None:
                minimized_ids.add(selected_id)
                continue
            repair_archived = (
                selected["archived_at"] is not None
                and retention_event is None
            )
            if repair_archived:
                with database.transaction() as db:
                    # Repair the early 5.1 import bug where archived_at was
                    # used as an import marker rather than retention.
                    db.execute(
                        "UPDATE vouchers SET archived_at=NULL WHERE id=?",
                        (selected_id,),
                    )
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
        archive_api_root = f"{LEGACY_BACKUP_API_ROOT_PREFIX}{material.source_sha256}"
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
                    """SELECT id, code, archived_at FROM vouchers
                       WHERE controller_id=? AND unifi_id=?""",
                    (archive_controller_id, unifi_id),
                ).fetchone()
                meta = metadata.get(canonical, _RecoveredMetadata())
                display_code = f"{canonical[:5]}-{canonical[5:]}"
                if existing is None:
                    cursor = db.execute(
                        """INSERT INTO vouchers
                           (controller_id, unifi_id, code, name,
                            created_at, imported_at, duration_minutes,
                            authorized_guest_count, ever_used, usage_observed,
                            expired, present_on_controller, last_seen_at,
                            last_synced_at, archived_at, origin)
                           VALUES (?, ?, ?, ?, NULL, ?, ?, 0, 0, 0, 1, 0, NULL, ?, NULL, 'UNKNOWN')""",
                        (
                            archive_controller_id,
                            unifi_id,
                            display_code,
                            meta.recipient,
                            imported_at,
                            meta.duration_minutes,
                            imported_at,
                        ),
                    )
                    created += 1
                else:
                    existing_id = int(existing["id"])
                    retention_event = db.execute(
                        """SELECT 1 FROM voucher_events
                           WHERE voucher_id=?
                             AND event_type='RETENTION_ARCHIVED'
                           LIMIT 1""",
                        (existing_id,),
                    ).fetchone()
                    if (
                        existing["archived_at"] is not None
                        and retention_event is not None
                    ):
                        minimized_ids.add(existing_id)
                        continue
                    db.execute(
                        """UPDATE vouchers
                           SET code=?,
                               name=CASE
                                   WHEN TRIM(name)='' THEN ?
                                   ELSE name
                               END,
                               origin='UNKNOWN',
                               duration_minutes=COALESCE(duration_minutes, ?),
                               expired=1, present_on_controller=0,
                               archived_at=NULL
                           WHERE id=?""",
                        (
                            display_code,
                            meta.recipient,
                            meta.duration_minutes,
                            existing_id,
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
        # Preserve the first candidate for an identity. A retention-minimized
        # voucher may intentionally carry its old clear code only in memory for
        # HMAC planning; the later SQLite snapshot of the same identity contains
        # ARCHIVED-<id> and must not replace that transient proof candidate.
        dedup.setdefault(item.key, item)
    return (
        tuple(dedup.values()),
        reused,
        created,
        frozenset(minimized_ids),
    )


def _source_history_sha256(material: _LegacyBackupMaterial) -> str:
    return hashlib.sha256(material.history_bytes).hexdigest()


def _fully_resolved_prior_run(
    database: Database,
    material: _LegacyBackupMaterial,
):
    """Return a prior complete evidence run for this exact history, if any."""

    return database.connection.execute(
        """SELECT migration_uuid, status, total_rows, resolved_rows,
                  ambiguous_rows, unresolved_rows
           FROM migration_runs
           WHERE source_history_sha256=?
             AND status IN ('EVIDENCE_READY', 'COMPLETED')
             AND total_rows=resolved_rows
             AND ambiguous_rows=0
             AND unresolved_rows=0
           ORDER BY id DESC
           LIMIT 1""",
        (_source_history_sha256(material),),
    ).fetchone()


def _repair_minimized_legacy_vouchers(
    database: Database,
) -> frozenset[int]:
    """Keep true retention minimization irreversible across legacy reimports.

    Repair all RESOLVED legacy-linked vouchers carrying RETENTION_ARCHIVED,
    rather than limiting recovery to one exact source-history hash. This keeps
    field-preview states safe when a later ZIP contains an extended history.
    """

    rows = database.connection.execute(
        """SELECT DISTINCT v.id
           FROM vouchers AS v
           JOIN legacy_audit_events AS lae ON lae.voucher_id=v.id
           WHERE lae.resolution_status='RESOLVED'
             AND v.archived_at IS NOT NULL
             AND EXISTS (
                 SELECT 1 FROM voucher_events AS ve
                 WHERE ve.voucher_id=v.id
                   AND ve.event_type='RETENTION_ARCHIVED'
             )"""
    ).fetchall()
    if not rows:
        return frozenset()

    protected_ids = frozenset(int(row["id"]) for row in rows)
    # Deliberately global: field-preview builds could have rehydrated a legacy
    # voucher during any earlier import. Every durable RETENTION_ARCHIVED fact
    # is authoritative, regardless of which ZIP happens to be imported now.
    with database.transaction() as db:
        for row in rows:
            voucher_id = int(row["id"])
            db.execute(
                """UPDATE vouchers
                   SET code=?, name='', assigned_to='', notes=''
                   WHERE id=?""",
                (f"ARCHIVED-{voucher_id}", voucher_id),
            )
    return protected_ids


def _minimized_voucher_ids_for_migration(
    database: Database,
    migration_uuid: str,
) -> frozenset[int]:
    """Return minimized voucher identities actually represented by one import."""

    rows = database.connection.execute(
        """SELECT DISTINCT lae.voucher_id
           FROM legacy_audit_events AS lae
           JOIN vouchers AS v ON v.id=lae.voucher_id
           WHERE lae.resolution_status='RESOLVED'
             AND lae.voucher_id IS NOT NULL
             AND (
                 lae.first_migration_uuid=?
                 OR lae.last_migration_uuid=?
             )
             AND v.archived_at IS NOT NULL
             AND v.code=('ARCHIVED-' || v.id)
             AND EXISTS (
                 SELECT 1 FROM voucher_events AS ve
                 WHERE ve.voucher_id=v.id
                   AND ve.event_type='RETENTION_ARCHIVED'
             )""",
        (migration_uuid, migration_uuid),
    ).fetchall()
    return frozenset(int(row["voucher_id"]) for row in rows)


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
        repaired_minimized_ids = _repair_minimized_legacy_vouchers(database)
        resolution_minimized_ids: frozenset[int] = frozenset()
        prior_run = _fully_resolved_prior_run(database, material)
        if prior_run is not None:
            reused = 0
            created = 0
            evidence = LegacyMigrationApplyResult(
                migration_uuid=str(prior_run["migration_uuid"]),
                total_rows=int(prior_run["total_rows"]),
                resolved_rows=int(prior_run["resolved_rows"]),
                ambiguous_rows=int(prior_run["ambiguous_rows"]),
                unresolved_rows=int(prior_run["unresolved_rows"]),
                already_applied=True,
            )
            materialization = materialize_resolved_legacy_events(
                database=database,
                materialized_at=imported_at,
                migration_uuid=str(prior_run["migration_uuid"]),
            )
        else:
            candidates, reused, created, resolution_minimized_ids = (
                _ensure_import_candidates(
                    database,
                    material,
                    imported_at=imported_at,
                    preferred_controller_id=preferred_controller_id,
                )
            )
            with tempfile.TemporaryDirectory(
                prefix="voucher-legacy-import-"
            ) as temp:
                history_path = _history_path(
                    material.history_bytes,
                    Path(temp),
                )
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

        current_minimized_ids = _minimized_voucher_ids_for_migration(
            database,
            evidence.migration_uuid,
        )
        # Count distinct minimized vouchers represented by this import only.
        # The repair pass is intentionally global, while this operator-facing
        # summary must not include unrelated legacy rows from other ZIPs.
        minimized_preserved = len(
            current_minimized_ids
            & (repaired_minimized_ids | resolution_minimized_ids)
        )

        database.integrity_check()
        copied, present = _copy_pdf_archive(
            material,
            prints_root=live_backup_service.paths.prints,
        )
    except LegacyMigrationError:
        raise
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
        minimized_vouchers_preserved=minimized_preserved,
        pdfs_copied=copied,
        pdfs_already_present=present,
    )
