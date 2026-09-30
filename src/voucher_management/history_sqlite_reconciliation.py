"""Crash-safe reconciliation of modern HMAC print history into SQLite reports."""

from __future__ import annotations

import hashlib
import hmac
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from .database import Database
from .history import HistoryError, HistoryService
from .locking import LockTimeout, exclusive_file_lock


PENDING_KEY = "history_print_reconciliation_pending"
VERSION_KEY = "history_print_reconciliation_version"
RECONCILIATION_VERSION = "1"
IMPORTED_OPERATOR = "Storico importato"


class HistorySqliteReconciliationError(RuntimeError):
    """Raised when HMAC print evidence cannot be mapped to SQLite safely."""


@dataclass(frozen=True)
class HistorySqliteReconciliationResult:
    jobs_materialized: int
    print_rows_seen: int
    legacy_print_rows_skipped: int


@dataclass(frozen=True)
class _JobPlan:
    audit_id: str
    controller_id: int
    codes: tuple[str, ...]
    output_file: str
    document_copies: int
    printed_at: str


def _display_code(value: object) -> str:
    raw = str(value or "").strip()
    canonical = raw.replace("-", "")
    if len(canonical) == 10 and canonical.isdigit():
        return f"{canonical[:5]}-{canonical[5:]}"
    return raw


def _mark(database: Database, *, pending: bool) -> None:
    database.set_metadata_value(PENDING_KEY, "1" if pending else "0")


def mark_history_print_reconciliation_pending(database: Database) -> None:
    """Persist the fail-closed report gate before a history exchange merge."""

    _mark(database, pending=True)


def mark_history_print_reconciliation_pending_to_path(
    database_path: Path,
) -> None:
    database = Database(Path(database_path))
    try:
        database.initialize()
        mark_history_print_reconciliation_pending(database)
    finally:
        database.close()


def clear_history_print_reconciliation_pending_to_path(
    database_path: Path,
) -> None:
    database = Database(Path(database_path))
    try:
        database.initialize()
        _mark(database, pending=False)
    finally:
        database.close()


def _build_plans(
    database: Database,
    history: HistoryService,
) -> tuple[tuple[_JobPlan, ...], int, int]:
    try:
        _fingerprint, secret = history.verified_identity_material()
    except HistoryError as exc:
        raise HistorySqliteReconciliationError(
            "Identità cronologia non verificata per la riconciliazione SQLite"
        ) from exc

    try:
        with exclusive_file_lock(history.lock_path):
            rows = list(history._items() or ())
    except LockTimeout as exc:
        raise HistorySqliteReconciliationError(
            "Cronologia occupata durante la riconciliazione SQLite"
        ) from exc
    except HistoryError as exc:
        raise HistorySqliteReconciliationError(str(exc)) from exc

    print_rows = [
        row for row in rows
        if row.get("event", "generate") == "print"
    ]
    modern_rows = [
        row for row in print_rows
        if str(row.get("print_job_id", "") or "").strip()
    ]
    legacy_skipped = len(print_rows) - len(modern_rows)
    if not modern_rows:
        return (), len(print_rows), legacy_skipped

    key = secret.encode("utf-8")
    digest_candidates: dict[str, list[tuple[int, int, str]]] = defaultdict(list)
    voucher_rows = database.connection.execute(
        """SELECT id, controller_id, code
           FROM vouchers
           WHERE archived_at IS NULL"""
    ).fetchall()
    for row in voucher_rows:
        display_code = _display_code(row["code"])
        if not display_code:
            continue
        try:
            digest = hmac.new(
                key,
                display_code.encode("ascii"),
                hashlib.sha256,
            ).hexdigest()
        except UnicodeEncodeError:
            continue
        digest_candidates[digest].append(
            (
                int(row["id"]),
                int(row["controller_id"]),
                display_code,
            )
        )

    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in modern_rows:
        grouped[str(row.get("print_job_id") or "").strip()].append(row)

    plans: list[_JobPlan] = []
    for audit_id, job_rows in grouped.items():
        first = job_rows[0]
        printed_at = str(first.get("timestamp", "") or "").strip()
        output_file = str(first.get("output_file", "") or "").strip()
        document_copies = first.get("document_copies", 1)
        if (
            not audit_id
            or not printed_at
            or not output_file
            or type(document_copies) is not int
            or document_copies < 1
        ):
            raise HistorySqliteReconciliationError(
                "Cronologia moderna contiene un job di stampa incompleto"
            )

        controller_ids: set[int] = set()
        codes: list[str] = []
        for item in job_rows:
            if (
                str(item.get("timestamp", "") or "").strip() != printed_at
                or str(item.get("output_file", "") or "").strip() != output_file
                or item.get("document_copies", 1) != document_copies
            ):
                raise HistorySqliteReconciliationError(
                    "Cronologia moderna contiene dati discordanti nello stesso job"
                )

            digest = str(item.get("voucher_id", "") or "").strip()
            candidates = digest_candidates.get(digest, [])
            if len(candidates) != 1:
                state = "mancante" if not candidates else "ambiguo"
                raise HistorySqliteReconciliationError(
                    "Impossibile associare in modo univoco un voucher della "
                    f"cronologia importata: collegamento {state}"
                )
            _voucher_id, controller_id, display_code = candidates[0]
            controller_ids.add(controller_id)

            physical_copies = item.get("physical_copies", document_copies)
            if (
                type(physical_copies) is not int
                or physical_copies < document_copies
                or physical_copies % document_copies != 0
            ):
                raise HistorySqliteReconciliationError(
                    "Cronologia moderna contiene un conteggio copie incoerente"
                )
            labels = physical_copies // document_copies
            codes.extend([display_code] * labels)

        if len(controller_ids) != 1:
            raise HistorySqliteReconciliationError(
                "Un job di stampa importato coinvolge controller diversi e "
                "non può essere attribuito con certezza"
            )
        plans.append(
            _JobPlan(
                audit_id=audit_id,
                controller_id=next(iter(controller_ids)),
                codes=tuple(codes),
                output_file=Path(output_file).name,
                document_copies=document_copies,
                printed_at=printed_at,
            )
        )

    return tuple(plans), len(print_rows), legacy_skipped


def reconcile_history_print_audits(
    database: Database,
    history: HistoryService,
    *,
    force: bool = False,
) -> HistorySqliteReconciliationResult:
    """Materialize modern HMAC print jobs into SQLite or keep reports blocked.

    The pending marker is committed before any planning/writes. Therefore a
    crash, an ambiguous HMAC mapping or an I/O failure can never make reports
    silently treat incomplete imported history as complete.
    """

    pending = database.metadata_value(PENDING_KEY) == "1"
    version = database.metadata_value(VERSION_KEY)
    if not force and not pending and version == RECONCILIATION_VERSION:
        return HistorySqliteReconciliationResult(0, 0, 0)

    mark_history_print_reconciliation_pending(database)
    plans, print_rows_seen, legacy_skipped = _build_plans(database, history)

    materialized = 0
    for plan in plans:
        existing = database.connection.execute(
            "SELECT windows_user FROM print_jobs WHERE print_job_uuid=?",
            (plan.audit_id,),
        ).fetchone()
        operator = (
            str(existing["windows_user"])
            if existing is not None
            else IMPORTED_OPERATOR
        )
        database.record_print_audit(
            controller_id=plan.controller_id,
            audit_id=plan.audit_id,
            codes=list(plan.codes),
            output_file=plan.output_file,
            document_copies=plan.document_copies,
            printed_at=plan.printed_at,
            windows_user=operator,
        )
        if existing is None:
            materialized += 1

    database.set_metadata_value(
        VERSION_KEY,
        RECONCILIATION_VERSION,
    )
    _mark(database, pending=False)
    return HistorySqliteReconciliationResult(
        jobs_materialized=materialized,
        print_rows_seen=print_rows_seen,
        legacy_print_rows_skipped=legacy_skipped,
    )


def reconcile_history_print_audits_to_path(
    database_path: Path,
    history: HistoryService,
    *,
    force: bool = False,
) -> HistorySqliteReconciliationResult:
    database = Database(Path(database_path))
    try:
        database.initialize()
        return reconcile_history_print_audits(
            database,
            history,
            force=force,
        )
    finally:
        database.close()
