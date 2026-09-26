"""Fail-closed planning for migration of legacy 4.x HMAC audit history.

This module does not mutate SQLite or legacy files.  It proves which historical
rows can be associated with independently known voucher codes before the
transactional apply phase is allowed to exist.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Mapping, Sequence

if TYPE_CHECKING:
    from .backup import BackupService
    from .database import Database


class LegacyMigrationError(RuntimeError):
    """Raised when legacy evidence cannot be interpreted safely."""


@dataclass(frozen=True)
class LegacyVoucherCandidate:
    """One independently known voucher that may explain a legacy HMAC."""

    controller_id: int
    unifi_id: str
    code: str

    @property
    def key(self) -> tuple[int, str]:
        return (self.controller_id, self.unifi_id)


@dataclass(frozen=True)
class LegacyAuditRow:
    """Validated legacy row plus its stable migration identity."""

    line_number: int
    event: str
    voucher_digest: str
    timestamp: str
    payload: Mapping[str, object]

    @property
    def event_key(self) -> str:
        event_id = str(self.payload.get("event_id", "") or "").strip()
        if self.event == "generate" and event_id:
            return f"generate:{event_id}"

        print_job_id = str(
            self.payload.get("print_job_id", "") or ""
        ).strip()
        if self.event == "print" and print_job_id:
            return f"print:{print_job_id}:{self.voucher_digest}"

        canonical = json.dumps(
            dict(self.payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return f"legacy:{self.line_number}:{digest}"


@dataclass(frozen=True)
class ResolvedLegacyRow:
    """A row whose HMAC maps to exactly one independently known voucher."""

    row: LegacyAuditRow
    candidate: LegacyVoucherCandidate


@dataclass(frozen=True)
class AmbiguousLegacyRow:
    """A row whose HMAC maps to more than one possible voucher record."""

    row: LegacyAuditRow
    candidates: tuple[LegacyVoucherCandidate, ...]


@dataclass(frozen=True)
class UnresolvedLegacyRow:
    """A valid historical row for which no independently known code matches."""

    row: LegacyAuditRow
    reason: str = "no_known_code"


@dataclass(frozen=True)
class LegacyMigrationPlan:
    """Immutable dry-run result used as the boundary before migration writes."""

    history_fingerprint: str
    source_history_sha256: str
    resolved: tuple[ResolvedLegacyRow, ...]
    ambiguous: tuple[AmbiguousLegacyRow, ...]
    unresolved: tuple[UnresolvedLegacyRow, ...]

    @property
    def total_rows(self) -> int:
        return len(self.resolved) + len(self.ambiguous) + len(self.unresolved)

    @property
    def fully_resolved(self) -> bool:
        return not self.ambiguous and not self.unresolved


_HEX = frozenset("0123456789abcdef")


def _fingerprint(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:16]


def _digest(code: str, secret: bytes) -> str:
    try:
        encoded = code.encode("ascii")
    except UnicodeEncodeError as exc:
        raise LegacyMigrationError(
            "Codice voucher candidato non ASCII"
        ) from exc
    return hmac.new(secret, encoded, hashlib.sha256).hexdigest()


def _candidate_spellings(code: str) -> tuple[str, ...]:
    """Return deterministic spellings used by the 4.x print-history path."""

    value = str(code).strip()
    if not value:
        raise LegacyMigrationError("Codice voucher candidato vuoto")
    compact = value.replace("-", "")
    spellings = [value]
    if len(compact) == 10:
        formatted = f"{compact[:5]}-{compact[5:]}"
        if formatted not in spellings:
            spellings.append(formatted)
    return tuple(spellings)


def _validated_history_rows(
    history_path: Path,
) -> tuple[tuple[LegacyAuditRow, ...], str]:
    """Parse one immutable file snapshot or fail on the first unsafe row."""

    history_path = Path(history_path)
    if not history_path.exists():
        return (), hashlib.sha256(b"").hexdigest()
    if not history_path.is_file():
        raise LegacyMigrationError("Cronologia legacy non leggibile")

    rows: list[LegacyAuditRow] = []
    try:
        raw = history_path.read_bytes()
        text = raw.decode("utf-8")
        for line_number, line in enumerate(text.splitlines(), start=1):
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise LegacyMigrationError(
                        f"Cronologia legacy danneggiata alla riga {line_number}"
                    ) from exc
                if not isinstance(payload, dict):
                    raise LegacyMigrationError(
                        f"Record legacy non valido alla riga {line_number}"
                    )

                event = str(payload.get("event", "generate") or "generate")
                if event not in {"generate", "print"}:
                    raise LegacyMigrationError(
                        f"Evento legacy non supportato alla riga {line_number}"
                    )

                digest = str(payload.get("voucher_id", "") or "").lower()
                if len(digest) != 64 or any(ch not in _HEX for ch in digest):
                    raise LegacyMigrationError(
                        f"HMAC voucher non valido alla riga {line_number}"
                    )

                timestamp = str(payload.get("timestamp", "") or "").strip()
                if not timestamp:
                    raise LegacyMigrationError(
                        f"Timestamp legacy mancante alla riga {line_number}"
                    )

                if event == "print":
                    for field in ("document_copies", "physical_copies"):
                        value = payload.get(field, 1)
                        if type(value) is not int or value < 1:
                            raise LegacyMigrationError(
                                f"Conteggio stampa legacy non valido alla riga "
                                f"{line_number}"
                            )

                rows.append(
                    LegacyAuditRow(
                        line_number=line_number,
                        event=event,
                        voucher_digest=digest,
                        timestamp=timestamp,
                        payload=dict(payload),
                    )
                )
    except LegacyMigrationError:
        raise
    except (OSError, UnicodeError) as exc:
        raise LegacyMigrationError(
            "Cronologia legacy non leggibile"
        ) from exc

    return tuple(rows), hashlib.sha256(raw).hexdigest()


def build_legacy_migration_plan(
    *,
    history_path: Path,
    expected_fingerprint: str,
    secret: str,
    candidates: Sequence[LegacyVoucherCandidate],
) -> LegacyMigrationPlan:
    """Build a migration plan without modifying either legacy or SQLite state.

    The history key must match the fingerprint stored with the 4.x settings.
    Each HMAC is then recomputed only from independently known candidate codes.
    Zero matches preserve the row as unresolved evidence; multiple distinct
    voucher identities preserve it as ambiguous evidence.
    """

    expected = str(expected_fingerprint or "").strip().lower()
    secret = str(secret or "")
    if (
        len(expected) != 16
        or any(ch not in _HEX for ch in expected)
        or not secret
        or _fingerprint(secret) != expected
    ):
        raise LegacyMigrationError(
            "Identità HMAC della cronologia legacy non verificabile"
        )

    by_digest: dict[str, dict[tuple[int, str], LegacyVoucherCandidate]] = {}
    secret_bytes = secret.encode("utf-8")

    for candidate in candidates:
        if candidate.controller_id < 1:
            raise LegacyMigrationError("Controller candidato non valido")
        if not str(candidate.unifi_id).strip():
            raise LegacyMigrationError("Identificativo UniFi candidato vuoto")
        for spelling in _candidate_spellings(candidate.code):
            digest = _digest(spelling, secret_bytes)
            by_digest.setdefault(digest, {})[candidate.key] = candidate

    resolved: list[ResolvedLegacyRow] = []
    ambiguous: list[AmbiguousLegacyRow] = []
    unresolved: list[UnresolvedLegacyRow] = []

    rows, source_history_sha256 = _validated_history_rows(history_path)
    event_keys = [row.event_key for row in rows]
    if len(event_keys) != len(set(event_keys)):
        raise LegacyMigrationError(
            "Identità evento legacy duplicata nella cronologia"
        )

    for row in rows:
        matches = tuple(
            sorted(
                by_digest.get(row.voucher_digest, {}).values(),
                key=lambda item: (item.controller_id, item.unifi_id),
            )
        )
        if len(matches) == 1:
            resolved.append(
                ResolvedLegacyRow(row=row, candidate=matches[0])
            )
        elif len(matches) > 1:
            ambiguous.append(
                AmbiguousLegacyRow(row=row, candidates=matches)
            )
        else:
            unresolved.append(UnresolvedLegacyRow(row=row))

    return LegacyMigrationPlan(
        history_fingerprint=expected,
        source_history_sha256=source_history_sha256,
        resolved=tuple(resolved),
        ambiguous=tuple(ambiguous),
        unresolved=tuple(unresolved),
    )


@dataclass(frozen=True)
class LegacyMigrationApplyResult:
    """Result of one atomic evidence-persistence transaction."""

    migration_uuid: str
    total_rows: int
    resolved_rows: int
    ambiguous_rows: int
    unresolved_rows: int
    already_applied: bool = False


def _canonical_code(value: str) -> str:
    return str(value or "").strip().replace("-", "")


def apply_legacy_migration_plan(
    *,
    database: "Database",
    plan: LegacyMigrationPlan,
    migration_uuid: str,
    applied_at: str,
) -> LegacyMigrationApplyResult:
    """Persist a verified plan atomically without materializing print facts yet.

    This phase preserves original legacy evidence in SQLite. Resolved rows gain
    a foreign-key link only after the planned controller/unifi identity and code
    are revalidated against the current database inside the same transaction.
    """

    migration_uuid = str(migration_uuid or "").strip()
    applied_at = str(applied_at or "").strip()
    if not migration_uuid or not applied_at:
        raise LegacyMigrationError("Identificativo/timestamp migrazione mancante")

    counts = (
        plan.total_rows,
        len(plan.resolved),
        len(plan.ambiguous),
        len(plan.unresolved),
    )

    with database.transaction() as db:
        existing_run = db.execute(
            """SELECT * FROM migration_runs WHERE migration_uuid=?""",
            (migration_uuid,),
        ).fetchone()
        if existing_run is not None:
            expected = (
                plan.source_history_sha256,
                *counts,
            )
            actual = (
                existing_run["source_history_sha256"],
                existing_run["total_rows"],
                existing_run["resolved_rows"],
                existing_run["ambiguous_rows"],
                existing_run["unresolved_rows"],
            )
            if actual != expected or existing_run["status"] not in {
                "EVIDENCE_READY",
                "COMPLETED",
            }:
                raise LegacyMigrationError(
                    "Identificativo migrazione già usato con dati diversi"
                )
            return LegacyMigrationApplyResult(
                migration_uuid=migration_uuid,
                total_rows=counts[0],
                resolved_rows=counts[1],
                ambiguous_rows=counts[2],
                unresolved_rows=counts[3],
                already_applied=True,
            )

        db.execute(
            """INSERT INTO migration_runs
               (migration_uuid, source_kind, source_history_sha256,
                started_at, status, total_rows, resolved_rows,
                ambiguous_rows, unresolved_rows)
               VALUES (?, 'LEGACY_4X_HISTORY', ?, ?, 'STARTED', ?, ?, ?, ?)""",
            (
                migration_uuid,
                plan.source_history_sha256,
                applied_at,
                *counts,
            ),
        )

        resolutions: list[
            tuple[LegacyAuditRow, str, int | None]
        ] = []

        for item in plan.resolved:
            rows = db.execute(
                """SELECT id, code FROM vouchers
                   WHERE controller_id=? AND unifi_id=?""",
                (
                    item.candidate.controller_id,
                    item.candidate.unifi_id,
                ),
            ).fetchall()
            if len(rows) != 1:
                raise LegacyMigrationError(
                    "Voucher risolto non più presente univocamente in SQLite"
                )
            voucher_row = rows[0]
            if _canonical_code(voucher_row["code"]) != _canonical_code(
                item.candidate.code
            ):
                raise LegacyMigrationError(
                    "Codice voucher cambiato dopo la pianificazione"
                )
            resolutions.append(
                (item.row, "RESOLVED", int(voucher_row["id"]))
            )

        resolutions.extend(
            (item.row, "AMBIGUOUS", None)
            for item in plan.ambiguous
        )
        resolutions.extend(
            (item.row, "UNRESOLVED", None)
            for item in plan.unresolved
        )

        for row, status, voucher_id in resolutions:
            payload_json = json.dumps(
                dict(row.payload),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            existing = db.execute(
                """SELECT * FROM legacy_audit_events
                   WHERE legacy_event_key=?""",
                (row.event_key,),
            ).fetchone()
            if existing is None:
                db.execute(
                    """INSERT INTO legacy_audit_events
                       (legacy_event_key, source_line, voucher_digest,
                        event_type, occurred_at, payload_json,
                        resolution_status, voucher_id,
                        first_migration_uuid, last_migration_uuid)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        row.event_key,
                        row.line_number,
                        row.voucher_digest,
                        row.event,
                        row.timestamp,
                        payload_json,
                        status,
                        voucher_id,
                        migration_uuid,
                        migration_uuid,
                    ),
                )
                continue

            immutable = (
                existing["voucher_digest"],
                existing["event_type"],
                existing["occurred_at"],
                existing["payload_json"],
            )
            wanted = (
                row.voucher_digest,
                row.event,
                row.timestamp,
                payload_json,
            )
            if immutable != wanted:
                raise LegacyMigrationError(
                    "Evento legacy già noto con contenuto differente"
                )

            previous_status = existing["resolution_status"]
            previous_voucher = existing["voucher_id"]
            if previous_status == "RESOLVED":
                if status != "RESOLVED" or previous_voucher != voucher_id:
                    raise LegacyMigrationError(
                        "Una risoluzione legacy esistente non può regredire"
                    )
            elif status == "RESOLVED":
                db.execute(
                    """UPDATE legacy_audit_events
                       SET resolution_status='RESOLVED', voucher_id=?,
                           last_migration_uuid=?
                       WHERE legacy_event_key=?""",
                    (voucher_id, migration_uuid, row.event_key),
                )
            else:
                db.execute(
                    """UPDATE legacy_audit_events
                       SET resolution_status=?, voucher_id=NULL,
                           last_migration_uuid=?
                       WHERE legacy_event_key=?""",
                    (status, migration_uuid, row.event_key),
                )

        db.execute(
            """UPDATE migration_runs
               SET status='EVIDENCE_READY'
               WHERE migration_uuid=?""",
            (migration_uuid,),
        )

    return LegacyMigrationApplyResult(
        migration_uuid=migration_uuid,
        total_rows=counts[0],
        resolved_rows=counts[1],
        ambiguous_rows=counts[2],
        unresolved_rows=counts[3],
    )


@dataclass(frozen=True)
class LegacyMaterializationResult:
    """Operational facts verified/materialized from resolved legacy evidence."""

    generated_events: int
    print_rows: int
    affected_vouchers: int


def _portable_basename(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return PurePosixPath(text.replace("\\", "/")).name


def _legacy_print_job_uuid(row: LegacyAuditRow) -> str:
    recorded = str(row.payload.get("print_job_id", "") or "").strip()
    if recorded:
        return recorded
    digest = hashlib.sha256(row.event_key.encode("utf-8")).hexdigest()[:32]
    return f"legacy-{digest}"


def _legacy_generate_event_uuid(row: LegacyAuditRow) -> str:
    digest = hashlib.sha256(row.event_key.encode("utf-8")).hexdigest()
    return f"legacy-generate-{digest}"


def materialize_resolved_legacy_events(
    *,
    database: "Database",
    materialized_at: str,
    migration_uuid: str | None = None,
) -> LegacyMaterializationResult:
    """Translate only RESOLVED evidence into operational SQLite facts.

    The function is idempotent and verifies existing Milestone A print jobs
    rather than duplicating them.  Unresolved/ambiguous evidence is never
    materialized. Print sequences are normalized after inserts so older legacy
    prints can correctly precede already-recorded 5.0 prints.
    """

    materialized_at = str(materialized_at or "").strip()
    if not materialized_at:
        raise LegacyMigrationError("Timestamp materializzazione mancante")

    generated_events = 0
    print_rows = 0
    affected_vouchers: set[int] = set()

    with database.transaction() as db:
        evidence = db.execute(
            """SELECT * FROM legacy_audit_events
               WHERE resolution_status='RESOLVED'
               ORDER BY occurred_at, source_line, legacy_event_key"""
        ).fetchall()

        for item in evidence:
            voucher_id = int(item["voucher_id"])
            try:
                payload = json.loads(item["payload_json"])
            except json.JSONDecodeError as exc:
                raise LegacyMigrationError(
                    "Evidenza legacy SQLite non più leggibile"
                ) from exc
            if not isinstance(payload, dict):
                raise LegacyMigrationError(
                    "Evidenza legacy SQLite non valida"
                )

            row = LegacyAuditRow(
                line_number=int(item["source_line"]),
                event=str(item["event_type"]),
                voucher_digest=str(item["voucher_digest"]),
                timestamp=str(item["occurred_at"]),
                payload=payload,
            )
            if row.event_key != item["legacy_event_key"]:
                raise LegacyMigrationError(
                    "Identità evidenza legacy SQLite incoerente"
                )

            if row.event == "generate":
                event_uuid = _legacy_generate_event_uuid(row)
                details = {
                    "legacy_event_key": row.event_key,
                    "output_file": _portable_basename(
                        payload.get("output_file", "")
                    ),
                    "recipient": str(
                        payload.get("recipient", "") or ""
                    ),
                    "duration_minutes": payload.get("duration_minutes"),
                    "reprint": bool(payload.get("reprint", False)),
                }
                details_json = json.dumps(
                    details,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                existing = db.execute(
                    "SELECT * FROM voucher_events WHERE event_uuid=?",
                    (event_uuid,),
                ).fetchone()
                if existing is None:
                    db.execute(
                        """INSERT INTO voucher_events
                           (event_uuid, voucher_id, event_type, occurred_at,
                            source, windows_user, details_json)
                           VALUES (?, ?, 'LEGACY_PDF_GENERATED', ?, 'MIGRATION',
                                   NULL, ?)""",
                        (
                            event_uuid,
                            voucher_id,
                            row.timestamp,
                            details_json,
                        ),
                    )
                else:
                    comparable = (
                        int(existing["voucher_id"]),
                        existing["event_type"],
                        existing["occurred_at"],
                        existing["source"],
                        existing["details_json"],
                    )
                    wanted = (
                        voucher_id,
                        "LEGACY_PDF_GENERATED",
                        row.timestamp,
                        "MIGRATION",
                        details_json,
                    )
                    if comparable != wanted:
                        raise LegacyMigrationError(
                            "Evento PDF legacy già materializzato con dati diversi"
                        )
                generated_events += 1

            elif row.event == "print":
                job_uuid = _legacy_print_job_uuid(row)
                output_file = _portable_basename(
                    payload.get("output_file", "")
                )
                document_copies = payload.get("document_copies", 1)
                physical_copies = payload.get("physical_copies", 1)
                if (
                    type(document_copies) is not int
                    or document_copies < 1
                    or type(physical_copies) is not int
                    or physical_copies < 1
                ):
                    raise LegacyMigrationError(
                        "Conteggio stampa legacy materializzato non valido"
                    )

                job = db.execute(
                    "SELECT * FROM print_jobs WHERE print_job_uuid=?",
                    (job_uuid,),
                ).fetchone()
                if job is None:
                    cursor = db.execute(
                        """INSERT INTO print_jobs
                           (print_job_uuid, created_at, submitted_at,
                            windows_user, output_file, document_copies, status)
                           VALUES (?, ?, ?, 'MIGRATION', ?, ?, 'AUDITED')""",
                        (
                            job_uuid,
                            row.timestamp,
                            row.timestamp,
                            output_file,
                            document_copies,
                        ),
                    )
                    print_job_id = int(cursor.lastrowid)
                    print_user = "MIGRATION"
                else:
                    comparable = (
                        job["submitted_at"],
                        str(job["output_file"] or ""),
                        int(job["document_copies"]),
                        job["status"],
                    )
                    wanted = (
                        row.timestamp,
                        output_file,
                        document_copies,
                        "AUDITED",
                    )
                    if comparable != wanted:
                        raise LegacyMigrationError(
                            "Job stampa legacy già presente con dati diversi"
                        )
                    print_job_id = int(job["id"])
                    print_user = str(job["windows_user"])

                existing_prints = db.execute(
                    """SELECT * FROM voucher_prints
                       WHERE print_job_id=? AND voucher_id=?""",
                    (print_job_id, voucher_id),
                ).fetchall()
                if len(existing_prints) > 1:
                    raise LegacyMigrationError(
                        "Più stampe SQLite per lo stesso job/voucher legacy"
                    )
                if existing_prints:
                    existing_print = existing_prints[0]
                    if (
                        existing_print["printed_at"] != row.timestamp
                        or int(existing_print["physical_copies"])
                        != physical_copies
                    ):
                        raise LegacyMigrationError(
                            "Stampa legacy già materializzata con dati diversi"
                        )
                else:
                    previous_max = db.execute(
                        """SELECT COALESCE(MAX(print_sequence), 0)
                           FROM voucher_prints WHERE voucher_id=?""",
                        (voucher_id,),
                    ).fetchone()[0]
                    db.execute(
                        """INSERT INTO voucher_prints
                           (print_job_id, voucher_id, printed_at, windows_user,
                            physical_copies, print_sequence, is_reprint)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (
                            print_job_id,
                            voucher_id,
                            row.timestamp,
                            print_user,
                            physical_copies,
                            int(previous_max) + 1,
                            int(int(previous_max) > 0),
                        ),
                    )

                affected_vouchers.add(voucher_id)
                print_rows += 1

            else:
                raise LegacyMigrationError(
                    "Tipo evento legacy materializzabile non supportato"
                )

            db.execute(
                """UPDATE legacy_audit_events
                   SET materialized_at=COALESCE(materialized_at, ?)
                   WHERE legacy_event_key=?""",
                (materialized_at, row.event_key),
            )

        # Historical rows can predate print facts already recorded by 5.0.
        # Renumber all affected vouchers after insertion so sequence 1 remains
        # the chronologically earliest known physical print.
        for voucher_id in sorted(affected_vouchers):
            prints = db.execute(
                """SELECT vp.id, vp.print_sequence, vp.printed_at,
                          pj.print_job_uuid
                   FROM voucher_prints AS vp
                   JOIN print_jobs AS pj ON pj.id=vp.print_job_id
                   WHERE vp.voucher_id=?
                   ORDER BY vp.printed_at, pj.print_job_uuid, vp.id""",
                (voucher_id,),
            ).fetchall()
            if not prints:
                continue
            max_sequence = max(int(row["print_sequence"]) for row in prints)
            offset = max_sequence + len(prints) + 1
            db.execute(
                """UPDATE voucher_prints
                   SET print_sequence=print_sequence+?
                   WHERE voucher_id=?""",
                (offset, voucher_id),
            )
            for sequence, print_row in enumerate(prints, start=1):
                db.execute(
                    """UPDATE voucher_prints
                       SET print_sequence=?, is_reprint=?
                       WHERE id=?""",
                    (
                        sequence,
                        int(sequence > 1),
                        int(print_row["id"]),
                    ),
                )

        if migration_uuid is not None:
            run_id = str(migration_uuid).strip()
            run = db.execute(
                """SELECT status FROM migration_runs
                   WHERE migration_uuid=?""",
                (run_id,),
            ).fetchone()
            if run is None or run["status"] not in {
                "EVIDENCE_READY",
                "COMPLETED",
            }:
                raise LegacyMigrationError(
                    "Run migrazione non pronto per la materializzazione"
                )
            db.execute(
                """UPDATE migration_runs
                   SET status='COMPLETED', completed_at=?
                   WHERE migration_uuid=?""",
                (materialized_at, run_id),
            )

    return LegacyMaterializationResult(
        generated_events=generated_events,
        print_rows=print_rows,
        affected_vouchers=len(affected_vouchers),
    )


@dataclass(frozen=True)
class LegacyMigrationExecutionResult:
    """Result of one safety-backed migration execution."""

    backup_path: Path
    evidence: LegacyMigrationApplyResult
    materialization: LegacyMaterializationResult


def execute_legacy_migration(
    *,
    database: "Database",
    backup_service: "BackupService",
    backup_destination: Path,
    backup_password: str,
    plan: LegacyMigrationPlan,
    migration_uuid: str,
    applied_at: str,
    materialized_at: str,
) -> LegacyMigrationExecutionResult:
    """Execute the explicit migration only after a verified encrypted backup.

    Evidence persistence and operational materialization are individually
    transactional. If materialization fails, no partial print/event facts are
    committed; the already-persisted evidence is deliberately harmless and
    idempotent, so the operator can repair/retry without rewriting legacy files.
    """

    password = str(backup_password or "")
    if not password:
        raise LegacyMigrationError(
            "La migrazione richiede un backup cifrato pre-migrazione"
        )

    destination = Path(backup_destination)
    try:
        backup_path = Path(
            backup_service.create(
                destination,
                password=password,
            )
        )
    except Exception as exc:
        raise LegacyMigrationError(
            "Backup di sicurezza pre-migrazione non riuscito"
        ) from exc

    if (
        not backup_path.is_file()
        or not backup_service.is_encrypted_backup(backup_path)
    ):
        raise LegacyMigrationError(
            "Backup di sicurezza pre-migrazione non verificabile"
        )

    evidence = apply_legacy_migration_plan(
        database=database,
        plan=plan,
        migration_uuid=migration_uuid,
        applied_at=applied_at,
    )
    materialization = materialize_resolved_legacy_events(
        database=database,
        materialized_at=materialized_at,
        migration_uuid=migration_uuid,
    )
    database.integrity_check()

    return LegacyMigrationExecutionResult(
        backup_path=backup_path,
        evidence=evidence,
        materialization=materialization,
    )
